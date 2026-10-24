"""Example: a local retrieval-augmented QA pipeline on an NVIDIA GPU.

Twelve fixed questions about a small fictional handbook (data/). Each
question is one trace:

    agent:answer          one question, start to finish
      retrieval:search    BM25 over the 26 handbook passages (CPU)
      llm:generate        Qwen2.5-1.5B-Instruct answers from the passages (GPU)
      verify:answer       deterministic checks of the answer (CPU)

Before the questions, once, as one separate trace:

    setup:prepare         everything before the first question
      model:load          read the downloaded weights and move them to the GPU
      index:build         build the BM25 index
      llm:warmup          two generations on a question that is not scored

The handbook is invented, so its facts are not in the model's training data:
to pass the reference checks, a reply has to use the retrieved passages. The labels are chosen here,
by the code that knows what each step is; the profiler never infers them.

    pip install -r examples/local_rag/requirements.txt
    python examples/local_rag/rag.py --download          # once, about 3 GB
    mkdir -p runs
    agent-energy-profiler validate --counter-disturbance --json runs/hw.json
    agent-energy-profiler run --hardware-report runs/hw.json --out-dir runs/rag -- \\
        python examples/local_rag/rag.py
    agent-energy-profiler report runs/rag --html runs/rag/report.html

See README.md beside this file for what is measured and what is estimated.
"""

import argparse
import hashlib
import json
import math
import os
import platform
import random
import re
import sys
import time
import unicodedata
from collections import Counter

from agent_energy_profiler import Session

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")

MODEL = "Qwen/Qwen2.5-1.5B-Instruct"
#: the commit this example was run with; pass --revision to use another
REVISION = "989aa7980e4cf806f80c7fef2b1adb7bc71aa306"
MODEL_LICENSE = "apache-2.0"
SEED = 0

#: generation is greedy, so the seed changes nothing unless sampling is added
GENERATION = {"max_new_tokens": 64, "do_sample": False, "repetition_penalty": 1.0}

SYSTEM = ("You answer questions about the Larkspur Research Station handbook, using "
          "only the passages given. Reply with one line: \"Answer: \", one short "
          "sentence that answers the question, and the id of the passage you used "
          "in square brackets. If the passages do not contain the answer, reply: "
          "Answer: I do not know.")

#: a worked example shown before every question (passage id, question, reply);
#: its passage is never the source of a scored question
EXAMPLE = ("P03", "On which day is the backup generator test-run?",
           "Answer: The backup generator is test-run every Tuesday. [P03]")

#: warms the model up; never scored
WARMUP = "What download capacity does the satellite link provide?"

STOP = {"a", "an", "and", "are", "at", "be", "by", "do", "does", "for", "from",
        "how", "in", "is", "it", "its", "may", "must", "of", "on", "or", "the",
        "to", "what", "when", "where", "which", "who", "with"}

CITATION = re.compile(r"\[\s*(P\d{2})\s*\]")


# --- data ------------------------------------------------------------------

def load_jsonl(path):
	with open(path, encoding="utf-8") as f:
		return [json.loads(line) for line in f if line.strip()]


def sha256(path):
	with open(path, "rb") as f:
		return hashlib.sha256(f.read()).hexdigest()


# --- retrieval ---------------------------------------------------------------

def stem(word):
	"""Strip a few English suffixes, so "nightly" finds "night"."""
	for suffix in ("ly", "ing", "ed", "es", "s"):
		if len(word) > len(suffix) + 3 and word.endswith(suffix):
			return word[:-len(suffix)]
	return word


def tokens(text):
	words = re.findall(r"[a-z0-9]+", unicodedata.normalize("NFKC", text).lower())
	return [stem(w) for w in words if w not in STOP and len(w) > 1]


class BM25:
	"""Okapi BM25 over passages ({"id", "title", "text"})."""

	def __init__(self, passages, k1=1.5, b=0.75):
		self.passages = passages
		self.by_id = {p["id"]: p for p in passages}
		self.docs = [Counter(tokens(p["title"] + " " + p["text"])) for p in passages]
		self.lengths = [sum(d.values()) for d in self.docs]
		self.avg = sum(self.lengths) / len(self.docs)
		df = Counter(term for d in self.docs for term in d)
		n = len(self.docs)
		self.idf = {t: math.log(1 + (n - c + 0.5) / (c + 0.5)) for t, c in df.items()}
		self.k1, self.b = k1, b

	def score(self, query):
		q = tokens(query)
		out = []
		for doc, length in zip(self.docs, self.lengths):
			norm = self.k1 * (1 - self.b + self.b * length / self.avg)
			out.append(sum(self.idf.get(t, 0.0) * doc[t] * (self.k1 + 1) / (doc[t] + norm)
			               for t in q if doc[t]))
		return out

	def search(self, query, k):
		"""The k best passages, best first; ties keep corpus order."""
		scores = self.score(query)
		order = sorted(range(len(scores)), key=lambda i: (-scores[i], i))
		return [dict(self.passages[i], score=scores[i]) for i in order[:k]]


# --- generation --------------------------------------------------------------

def passage_text(p):
	return f"{p['title']}: {p['text']}"


def messages(question, passages, example_passage):
	"""The chat for one question: instructions, the worked example, the question."""
	def ask(q, shown):
		context = "\n".join(f"[{p['id']}] {passage_text(p)}" for p in shown)
		return {"role": "user", "content": f"Passages:\n{context}\n\nQuestion: {q}"}
	return [{"role": "system", "content": SYSTEM},
	        ask(EXAMPLE[1], [example_passage]),
	        {"role": "assistant", "content": EXAMPLE[2]},
	        ask(question, passages)]


class HFGenerator:
	"""A Hugging Face causal LM, greedy decoding, on one device."""

	backend = "transformers"

	def __init__(self, path, device, dtype):
		import torch
		from transformers import AutoModelForCausalLM, AutoTokenizer, GenerationConfig
		self.torch = torch
		self.device = torch.device(device)
		self.tokenizer = AutoTokenizer.from_pretrained(path, local_files_only=True)
		self.model = AutoModelForCausalLM.from_pretrained(
			path, dtype=getattr(torch, dtype), local_files_only=True).to(self.device).eval()
		# replaces the model's own defaults (which sample), so only GENERATION applies
		self.model.generation_config = GenerationConfig(
			**GENERATION, eos_token_id=self.model.generation_config.eos_token_id,
			pad_token_id=self.tokenizer.pad_token_id or self.tokenizer.eos_token_id)
		self.sync()

	def sync(self):
		"""Wait for queued GPU work, so a span's window covers it."""
		if self.device.type == "cuda":
			self.torch.cuda.synchronize(self.device)

	def generate(self, chat):
		"""Return (text, input tokens, output tokens)."""
		prompt = self.tokenizer.apply_chat_template(chat, tokenize=False,
		                                            add_generation_prompt=True)
		batch = self.tokenizer(prompt, return_tensors="pt").to(self.device)
		n_in = batch["input_ids"].shape[1]
		with self.torch.inference_mode():
			out = self.model.generate(**batch)
		self.sync()
		new = out[0, n_in:]
		return self.tokenizer.decode(new, skip_special_tokens=True).strip(), n_in, len(new)

	def describe(self):
		torch = self.torch
		info = {"backend": self.backend, "torch": torch.__version__,
		        "dtype": str(self.model.dtype).replace("torch.", ""),
		        "attention": getattr(self.model.config, "_attn_implementation", None),
		        "parameters": sum(p.numel() for p in self.model.parameters()),
		        "device": str(self.device)}
		from importlib.metadata import PackageNotFoundError, version
		for package in ("transformers", "tokenizers", "huggingface_hub", "safetensors"):
			try:
				info[package] = version(package)
			except PackageNotFoundError:
				info[package] = None
		if self.device.type == "cuda":
			info.update(cuda=torch.version.cuda, gpu=torch.cuda.get_device_name(self.device),
			            max_memory_allocated_bytes=torch.cuda.max_memory_allocated(self.device))
		return info


# --- verification ------------------------------------------------------------

def normalise(text):
	text = unicodedata.normalize("NFKC", text).lower()
	return " ".join(text.split())


def contains(text, phrase):
	"""Whole-word, case-insensitive match after normalisation."""
	pattern = r"(?<![\w])" + re.escape(normalise(phrase)) + r"(?![\w])"
	return re.search(pattern, normalise(text)) is not None


def verify(reply, question, retrieved_ids):
	"""Deterministic checks of one reply. Not a judgement of its quality.

	answer_match     a reference answer appears in the reply as whole words
	                 (citations removed first, so "[P12]" never matches "12")
	cites_source     the reply cites the passage that holds the answer
	citations_valid  it cites something, and only passages it was given
	retrieval_hit    that passage was among those retrieved
	verified         answer_match and cites_source and citations_valid
	"""
	cited = CITATION.findall(reply)
	body = CITATION.sub(" ", reply)
	result = {
		"answer_match": any(contains(body, a) for a in question["answers"]),
		"cited": cited,
		"cites_source": question["source"] in cited,
		"citations_valid": bool(cited) and all(c in retrieved_ids for c in cited),
		"retrieval_hit": question["source"] in retrieved_ids,
	}
	result["verified"] = (result["answer_match"] and result["cites_source"]
	                      and result["citations_valid"])
	return result


# --- the instrumented workflow ----------------------------------------------

def answer(session, index, generator, question, top_k):
	"""One question as one trace: retrieve, generate, verify."""
	with session.span("agent:answer", attributes={"question_id": question["id"]}) as root:
		with session.span("retrieval:search", attributes={"top_k": top_k}) as s:
			passages = index.search(question["question"], top_k)
			retrieved = [p["id"] for p in passages]
			s.set_attribute("retrieved", ",".join(retrieved))
		with session.span("llm:generate") as s:
			t0 = time.perf_counter()
			chat = messages(question["question"], passages, index.by_id[EXAMPLE[0]])
			text, n_in, n_out = generator.generate(chat)
			seconds = time.perf_counter() - t0      # generate() synchronised the GPU
			s.set_tokens(input=n_in, output=n_out)
		with session.span("verify:answer") as s:
			checks = verify(text, question, retrieved)
			s.set_attributes(**{k: v for k, v in checks.items() if isinstance(v, bool)})
		root.set_attribute("verified", checks["verified"])
	return {"id": question["id"], "question": question["question"], "retrieved": retrieved,
	        "answer": text, "input_tokens": n_in, "output_tokens": n_out,
	        "generate_s": seconds, **checks}


def run_workflow(session, load_generator, corpus, questions, top_k=3, repeats=1, warmup=2):
	"""Load, index, warm up, then answer every question `repeats` times.

	The one-off setup steps share a parent span, so reports show them as one
	branch apart from the repeated agent:answer traces.
	"""
	with session.span("setup:prepare"):
		with session.span("model:load"):
			generator = load_generator()
		with session.span("index:build", attributes={"passages": len(corpus)}):
			index = BM25(corpus)
		with session.span("llm:warmup", attributes={"generations": warmup}):
			for _ in range(warmup):
				generator.generate(messages(WARMUP, index.search(WARMUP, top_k),
				                            index.by_id[EXAMPLE[0]]))
	results = []
	for r in range(repeats):
		for q in questions:
			results.append(dict(answer(session, index, generator, q, top_k), repeat=r))
	return generator, results


def summarise(results):
	n = len(results)
	count = lambda key: sum(1 for r in results if r[key])
	return {"answers": n, "verified": count("verified"), "answer_match": count("answer_match"),
	        "cites_source": count("cites_source"), "retrieval_hit": count("retrieval_hit"),
	        "citations_valid": count("citations_valid")}


def describe_setup(model, revision, dtype, device, top_k, repeats, warmup=2):
	"""What a reader needs to reproduce the run, apart from the hardware."""
	return {
		"model": model, "revision": revision, "model_license": MODEL_LICENSE,
		"dtype": dtype, "device": device, "top_k": top_k, "repeats": repeats,
		"seed": SEED, "generation": dict(GENERATION), "warmup_generations": warmup,
		"corpus_sha256": sha256(os.path.join(DATA, "corpus.jsonl")),
		"questions_sha256": sha256(os.path.join(DATA, "questions.jsonl")),
		"python": platform.python_version(), "platform": platform.platform(),
	}


def write_results(directory, setup, generator, results):
	"""rag_results.json in the run directory: setup, summary and every answer."""
	record = {"setup": dict(setup, **generator.describe()), "summary": summarise(results),
	          "results": results}
	path = os.path.join(directory, "rag_results.json")
	with open(path, "w", encoding="utf-8") as f:
		json.dump(record, f, indent=1, ensure_ascii=False)
		f.write("\n")
	return path, record


def gpu_before_start(index=0):
	"""What else is on the GPU before the run (it is measured at device scope)."""
	try:
		import pynvml
		pynvml.nvmlInit()
	except Exception as e:
		return {"available": False, "reason": f"{type(e).__name__}: {e}"}
	try:
		h = pynvml.nvmlDeviceGetHandleByIndex(index)
		util = pynvml.nvmlDeviceGetUtilizationRates(h)
		mem = pynvml.nvmlDeviceGetMemoryInfo(h)
		try:
			procs = len(pynvml.nvmlDeviceGetComputeRunningProcesses(h))
		except Exception:
			procs = None                      # not supported under WSL2, for example
		return {"available": True, "utilization_percent": util.gpu,
		        "memory_used_bytes": mem.used, "compute_processes": procs}
	finally:
		pynvml.nvmlShutdown()


def model_path(model, revision, download):
	"""The local snapshot of `model` at `revision`; downloads only if asked."""
	from huggingface_hub import snapshot_download
	if download:
		return snapshot_download(model, revision=revision)
	try:
		return snapshot_download(model, revision=revision, local_files_only=True)
	except Exception:
		sys.exit(f"{model} at revision {revision[:12]} is not downloaded yet. Run once:\n"
		         f"  python {os.path.relpath(__file__)} --download")


def main():
	ap = argparse.ArgumentParser(description=__doc__,
	                             formatter_class=argparse.RawDescriptionHelpFormatter)
	ap.add_argument("--download", action="store_true",
	                help="download the model (about 3 GB) and exit; nothing is measured")
	ap.add_argument("--out-dir", default=None,
	                help="run directory when run on its own (default: a new "
	                     "agent-energy-profiler-runs/local_rag-<timestamp>/)")
	ap.add_argument("--hardware-report", default=None,
	                help="`agent-energy-profiler validate --counter-disturbance --json` "
	                     "output, when run on its own (under `run`, pass it to run)")
	ap.add_argument("--model", default=MODEL)
	ap.add_argument("--revision", default=REVISION)
	ap.add_argument("--dtype", default="bfloat16", choices=("bfloat16", "float16", "float32"))
	ap.add_argument("--device", default="cuda")
	ap.add_argument("--top-k", type=int, default=3)
	ap.add_argument("--repeats", type=int, default=1, help="passes over the 12 questions")
	args = ap.parse_args()

	try:
		import torch
		import transformers  # noqa: F401
	except ImportError:
		sys.exit("this example needs PyTorch and transformers: "
		         "pip install -r examples/local_rag/requirements.txt")
	path = model_path(args.model, args.revision, args.download)
	if args.download:
		print(f"downloaded {args.model} at {args.revision} to {path}")
		return
	if args.device.startswith("cuda") and not torch.cuda.is_available():
		sys.exit("no CUDA device visible to PyTorch. Check `nvidia-smi` and that the "
		         "installed torch is a CUDA build; --device cpu runs the same pipeline "
		         "on the CPU (slowly), with the GPU only idling.")

	random.seed(SEED)
	torch.manual_seed(SEED)
	corpus = load_jsonl(os.path.join(DATA, "corpus.jsonl"))
	questions = load_jsonl(os.path.join(DATA, "questions.jsonl"))
	setup = describe_setup(args.model, args.revision, args.dtype, args.device,
	                       args.top_k, args.repeats)
	setup["snapshot"] = os.path.basename(os.path.normpath(path))   # the commit loaded
	setup["gpu_before_start"] = (gpu_before_start() if args.device.startswith("cuda")
	                             else None)

	# Mode: the default (auto). Per-span GPU energy is measured only if the
	# hardware report shows that counter reads are harmless on this host;
	# otherwise it is estimated, and the run's counter is the measurement.
	attrs = {"example": "local_rag", **{k: v for k, v in setup.items()
	                                    if isinstance(v, (str, int))}}
	with Session.profile("local_rag", out_dir=args.out_dir,
	                     hardware_report=args.hardware_report, attributes=attrs) as session:
		generator, results = run_workflow(
			session, lambda: HFGenerator(path, args.device, args.dtype),
			corpus, questions, top_k=args.top_k, repeats=args.repeats)

	out, record = write_results(session.out_dir, setup, generator, results)
	summary = record["summary"]

	yes = lambda v: "yes" if v else "no"
	print(f"{'question':9}{'retrieved':>10}{'answer':>8}{'cites':>7}{'tokens':>8}{'time':>8}")
	for r in results:
		print(f"{r['id']:9}{yes(r['retrieval_hit']):>10}{yes(r['answer_match']):>8}"
		      f"{yes(r['cites_source'] and r['citations_valid']):>7}"
		      f"{r['output_tokens']:>8}{r['generate_s']:>7.2f}s")
	print(f"reference checks passed: {summary['verified']} of {summary['answers']} answers "
	      f"(a reference answer present, its source passage cited, nothing cited that "
	      f"was not retrieved; string checks, not a measure of factual accuracy)")
	print(f"answers: {out}")
	print(f"run directory: {session.out_dir}")


if __name__ == "__main__":
	main()
