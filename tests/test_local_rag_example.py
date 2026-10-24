"""The local RAG example (examples/local_rag): retrieval, verification and
instrumentation with a stand-in generator. The real model runs only where
PyTorch, a CUDA GPU and the downloaded model are all present."""

import importlib.util
import json
import os
import re
import subprocess
import sys
import unittest

from agent_energy_profiler import html_report, view_model
from agent_energy_profiler.measurement import CallableReader
from agent_energy_profiler.session import Session

from test_report import Counter_, V2Case

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "examples", "local_rag", "rag.py")

_spec = importlib.util.spec_from_file_location("local_rag_example", SCRIPT)
rag = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rag)

CORPUS = rag.load_jsonl(os.path.join(rag.DATA, "corpus.jsonl"))
QUESTIONS = rag.load_jsonl(os.path.join(rag.DATA, "questions.jsonl"))
BY_ID = {p["id"]: p for p in CORPUS}
SETUP = ["model:load", "index:build", "llm:warmup"]
LABELS = {"setup:prepare", *SETUP, "agent:answer", "retrieval:search", "llm:generate",
          "verify:answer"}


class FakeGenerator:
	"""Replies with the reference answer and its source; no model, no GPU."""

	def __init__(self):
		self.chats = []
		self.answers = {q["question"]: f"Answer: {q['answers'][0]}. [{q['source']}]"
		                for q in QUESTIONS}

	def generate(self, chat):
		self.chats.append(chat)
		question = chat[-1]["content"].rsplit("Question: ", 1)[1]
		return self.answers.get(question, "Answer: I do not know."), 120, 9

	def describe(self):
		return {"backend": "fake"}


def records(path):
	with open(path) as f:
		return [json.loads(line) for line in f]


class TestData(unittest.TestCase):

	def test_questions_are_answerable_from_their_source(self):
		self.assertEqual(len({q["id"] for q in QUESTIONS}), len(QUESTIONS))
		self.assertEqual(len(BY_ID), len(CORPUS))
		for q in QUESTIONS:
			source = rag.passage_text(BY_ID[q["source"]])
			self.assertTrue(any(rag.contains(source, a) for a in q["answers"]), q["id"])

	def test_the_worked_example_and_warmup_are_never_scored(self):
		self.assertNotIn(rag.EXAMPLE[0], {q["source"] for q in QUESTIONS})
		self.assertNotIn(rag.WARMUP, {q["question"] for q in QUESTIONS})
		self.assertIn(f"[{rag.EXAMPLE[0]}]", rag.EXAMPLE[2])

	def test_the_model_is_pinned_and_decoding_is_greedy(self):
		self.assertRegex(rag.REVISION, r"^[0-9a-f]{40}$")
		self.assertFalse(rag.GENERATION["do_sample"])
		self.assertEqual(rag.GENERATION["repetition_penalty"], 1.0)


class TestRetrieval(unittest.TestCase):

	def setUp(self):
		self.index = rag.BM25(CORPUS)

	def test_every_source_passage_ranks_first(self):
		for q in QUESTIONS:
			top = self.index.search(q["question"], 3)
			self.assertEqual(top[0]["id"], q["source"], q["id"])

	def test_results_are_ranked_and_deterministic(self):
		top = self.index.search("solar array battery", 5)
		self.assertEqual(len(top), 5)
		scores = [p["score"] for p in top]
		self.assertEqual(scores, sorted(scores, reverse=True))
		self.assertEqual(top, self.index.search("solar array battery", 5))
		# nothing matches: ties keep corpus order
		self.assertEqual([p["id"] for p in self.index.search("zebra", 3)], ["P01", "P02", "P03"])

	def test_suffixes_are_stripped(self):
		self.assertEqual(rag.tokens("nightly backups"), rag.tokens("night backup"))
		self.assertEqual(self.index.search("nightly backups", 1)[0]["id"], "P06")


class TestVerification(unittest.TestCase):

	def q(self, qid):
		return next(q for q in QUESTIONS if q["id"] == qid)

	def test_a_correct_cited_reply_is_verified(self):
		r = rag.verify("Answer: The Tern bunkhouse has twelve beds. [P15]", self.q("Q09"),
		               ["P15", "P01", "P02"])
		self.assertTrue(r["verified"])
		self.assertEqual(r["cited"], ["P15"])

	def test_a_citation_never_counts_as_the_answer(self):
		r = rag.verify("Answer: I do not know. [P12]", self.q("Q09"), ["P12"])
		self.assertFalse(r["answer_match"])

	def test_whole_words_only_after_normalisation(self):
		q12, q10, q01 = self.q("Q12"), self.q("Q10"), self.q("Q01")
		self.assertFalse(rag.verify("Answer: freezer F22. [P11]", q12, ["P11"])["answer_match"])
		self.assertTrue(rag.verify("Answer: transect k7. [P19]", q10, ["P19"])["answer_match"])
		# a no-break space (NFKC) and no space at all are both accepted
		self.assertTrue(rag.verify("Answer: 21 °C [P04]", q01, ["P04"])["answer_match"])
		self.assertTrue(rag.verify("Answer: 21°C [P04]", q01, ["P04"])["answer_match"])

	def test_citing_the_wrong_passage_fails(self):
		# Q06 as the real model once answered: right text, the worked example's id
		r = rag.verify("Answer: Formalin may only be handled in fume hood 3. [P03]",
		               self.q("Q06"), ["P12", "P01", "P02"])
		self.assertTrue(r["answer_match"])
		self.assertFalse(r["cites_source"])
		self.assertFalse(r["citations_valid"])
		self.assertFalse(r["verified"])

	def test_an_extra_citation_that_was_not_retrieved_fails(self):
		r = rag.verify("Answer: Heron. [P06] [P21]", self.q("Q03"), ["P06", "P04"])
		self.assertTrue(r["cites_source"])
		self.assertFalse(r["citations_valid"])
		self.assertFalse(r["verified"])

	def test_no_citation_fails(self):
		r = rag.verify("Answer: Heron.", self.q("Q03"), ["P06"])
		self.assertTrue(r["answer_match"])
		self.assertFalse(r["verified"])

	def test_retrieval_hit_is_reported_separately(self):
		r = rag.verify("Answer: Heron. [P06]", self.q("Q03"), ["P04", "P21"])
		self.assertFalse(r["retrieval_hit"])


class TestInstrumentation(V2Case):

	def record(self, gpu, repeats=1):
		self.gen = FakeGenerator()
		with Session(self.spans, mode="direct", gpu=gpu) as s:
			_, self.results = rag.run_workflow(s, lambda: self.gen, CORPUS, QUESTIONS,
			                                   repeats=repeats)
		return [r for r in records(self.spans) if r["record_type"] == "span"]

	def test_span_hierarchy(self):
		spans = self.record(CallableReader(Counter_().read))
		by_id = {s["span_id"]: s for s in spans}
		roots = sorted((s for s in spans if s["parent_span_id"] is None),
		               key=lambda s: s["start_unix_s"])
		# setup is one trace of its own, before and apart from every question
		self.assertEqual(roots[0]["name"], "setup:prepare")
		self.assertEqual({s["name"] for s in roots[1:]}, {"agent:answer"})
		setup = sorted((s for s in spans if s["parent_span_id"] == roots[0]["span_id"]),
		               key=lambda s: s["start_unix_s"])
		self.assertEqual([s["name"] for s in setup], SETUP)
		self.assertTrue(all(s["trace_id"] == roots[0]["trace_id"] for s in setup))
		self.assertLessEqual(roots[0]["end_unix_s"], roots[1]["start_unix_s"])
		answers = [s for s in spans if s["name"] == "agent:answer"]
		self.assertEqual(len(answers), len(QUESTIONS))
		for a in answers:
			children = sorted((s for s in spans if s["parent_span_id"] == a["span_id"]),
			                  key=lambda s: s["start_unix_s"])
			self.assertEqual([c["name"] for c in children],
			                 ["retrieval:search", "llm:generate", "verify:answer"])
			self.assertEqual(children[1]["input_tokens"], 120)
			self.assertEqual(children[1]["output_tokens"], 9)
			self.assertIs(children[2]["attributes"]["verified"], True)
			self.assertIs(a["attributes"]["verified"], True)
		for s in spans:
			if s["parent_span_id"]:
				self.assertIn(by_id[s["parent_span_id"]]["name"], {"agent:answer", "setup:prepare"})
		# two warm-up generations, then one per question; every chat shows the
		# worked example before the question
		self.assertEqual(len(self.gen.chats), 2 + len(QUESTIONS))
		for chat in self.gen.chats:
			self.assertEqual([m["role"] for m in chat],
			                 ["system", "user", "assistant", "user"])
		self.assertEqual(rag.summarise(self.results)["verified"], len(QUESTIONS))

	def test_repeats(self):
		spans = self.record(CallableReader(Counter_().read), repeats=2)
		self.assertEqual(sum(1 for s in spans if s["name"] == "agent:answer"),
		                 2 * len(QUESTIONS))
		self.assertEqual({r["repeat"] for r in self.results}, {0, 1})

	def test_report_and_html(self):
		self.record(CallableReader(Counter_().read))
		rep = self.attribute(power=True)
		ops = {op["operation"]: op for op in rep["operations"]}
		self.assertEqual(set(ops), LABELS)
		self.assertEqual(ops["llm:generate"]["n"], len(QUESTIONS))
		paths = {h["path"] for h in rep["hierarchy"]}
		self.assertIn("agent:answer > llm:generate", paths)
		self.assertIn("setup:prepare > llm:warmup", paths)
		self.assertNotIn("llm:warmup", paths)          # never at the level of a question
		view = view_model.build(self.d)
		page = html_report.render(view)
		self.assertIn("<!doctype html>", page.lower())
		self.assertIn("agent:answer", page)

	def test_missing_gpu_counter_is_unavailable_not_zero(self):
		spans = self.record(None)
		for s in spans:
			gpu = s["energy"]["gpu"]
			self.assertEqual((gpu["status"], gpu["reason"], gpu["energy_j"]),
			                 ("unavailable", "disabled", None), s["name"])
		rep = self.attribute(power=False)
		gpu = rep["totals"]["domains"]["gpu"]
		self.assertIsNone(gpu["total_j"])
		for op in rep["operations"]:
			self.assertIsNone(op["energy"]["gpu"]["energy_j"], op["operation"])
		view = view_model.build(self.d)
		html_report.render(view)               # renders, with n/a and reasons

	def test_results_file_records_how_to_reproduce(self):
		self.record(CallableReader(Counter_().read))
		setup = rag.describe_setup(rag.MODEL, rag.REVISION, "bfloat16", "cuda", 3, 1)
		path, record = rag.write_results(self.d, setup, self.gen, self.results)
		with open(path, encoding="utf-8") as f:
			saved = json.load(f)
		self.assertEqual(saved, json.loads(json.dumps(record)))
		s = saved["setup"]
		for key in ("model", "revision", "model_license", "dtype", "seed", "generation",
		            "top_k", "warmup_generations", "corpus_sha256", "questions_sha256",
		            "python", "backend"):
			self.assertIn(key, s)
		self.assertEqual(s["corpus_sha256"], rag.sha256(os.path.join(rag.DATA, "corpus.jsonl")))
		self.assertRegex(s["questions_sha256"], r"^[0-9a-f]{64}$")
		self.assertEqual(saved["summary"]["answers"], len(QUESTIONS))
		self.assertEqual(len(saved["results"]), len(QUESTIONS))


class TestCommandLine(unittest.TestCase):

	def test_help_needs_no_model_libraries(self):
		res = subprocess.run([sys.executable, SCRIPT, "--help"], capture_output=True,
		                     text=True, timeout=60)
		self.assertEqual(res.returncode, 0, res.stderr)
		for option in ("--download", "--hardware-report", "--revision", "--out-dir"):
			self.assertIn(option, res.stdout)

	@unittest.skipIf(importlib.util.find_spec("torch"), "PyTorch is installed")
	def test_without_pytorch_it_says_what_to_install(self):
		res = subprocess.run([sys.executable, SCRIPT], capture_output=True, text=True,
		                     timeout=60)
		self.assertNotEqual(res.returncode, 0)
		self.assertIn("requirements.txt", res.stderr)


def _real_model():
	"""The local model snapshot if PyTorch sees a CUDA GPU, else None."""
	if not (importlib.util.find_spec("torch") and importlib.util.find_spec("transformers")):
		return None
	import torch
	if not torch.cuda.is_available():
		return None
	from huggingface_hub import snapshot_download
	try:
		return snapshot_download(rag.MODEL, revision=rag.REVISION, local_files_only=True)
	except Exception:
		return None


@unittest.skipUnless(_real_model(), "needs PyTorch with CUDA and the downloaded model "
                                    "(python examples/local_rag/rag.py --download)")
class TestRealModelOnGpu(V2Case):

	def test_two_questions_on_the_gpu(self):
		gen = rag.HFGenerator(_real_model(), "cuda", "bfloat16")
		with Session(self.spans, mode="sampled", gpu=None) as s:
			_, results = rag.run_workflow(s, lambda: gen, CORPUS, QUESTIONS[:2], warmup=1)
		for r in results:
			self.assertTrue(r["answer"])
			self.assertGreater(r["output_tokens"], 0)
			self.assertTrue(r["retrieval_hit"])
		# greedy decoding: the same chat gives the same reply
		index = rag.BM25(CORPUS)
		chat = rag.messages(QUESTIONS[0]["question"], index.search(QUESTIONS[0]["question"], 3),
		                    index.by_id[rag.EXAMPLE[0]])
		self.assertEqual(gen.generate(chat)[0], gen.generate(chat)[0])
		info = gen.describe()
		self.assertEqual(info["dtype"], "bfloat16")
		self.assertTrue(info["gpu"])
		for package in ("transformers", "tokenizers", "huggingface_hub"):
			self.assertTrue(info[package], package)


if __name__ == "__main__":
	unittest.main()
