"""Example 3: a small agent loop with retrieval, reasoning and verification.

Everything runs locally in pure Python: no API keys, no downloads, no GPU.
Each question is one trace:

    agent:question
      retrieval:search      BM25 over a small built-in corpus
      llm:reason            a stand-in "reasoner" (deterministic text scoring)
      verify:check          does the answer appear in the retrieved evidence?
      (retry once with a widened search if verification fails)

The operation labels are chosen by this program, which knows why each step
runs. The profiler never infers them.

    aep run --out-dir runs/agent -- python examples/agent_workflow/agent.py
    aep run --out-dir runs/agent-par -- python examples/agent_workflow/agent.py --parallel

--parallel answers questions concurrently with asyncio. The spans then overlap,
and the report marks them `shared`: device energy cannot be split between
concurrent operations, so no exclusive energy is reported for them.
"""

import argparse
import asyncio
import math
import re
from collections import Counter

from agent_energy_profiler import Session

CORPUS = [
	("d1", "The Amazon River flows through Brazil, Peru and Colombia into the Atlantic Ocean."),
	("d2", "Mount Kilimanjaro is the highest mountain in Africa, located in Tanzania."),
	("d3", "The Danube flows through ten countries, including Germany, Austria and Hungary."),
	("d4", "Canberra is the capital city of Australia; Sydney is its largest city."),
	("d5", "The Atacama Desert in Chile is one of the driest places on Earth."),
	("d6", "Lake Baikal in Siberia, Russia, is the deepest lake in the world."),
	("d7", "Ottawa is the capital of Canada, located in the province of Ontario."),
	("d8", "The Nile is often considered the longest river, flowing through Egypt and Sudan."),
	("d9", "Mount Everest, on the border of Nepal and China, is the highest mountain on Earth."),
	("d10", "The Sahara is the largest hot desert, covering much of North Africa."),
]
QUESTIONS = [
	("What is the capital of Australia?", "canberra"),
	("Which lake is the deepest in the world?", "baikal"),
	("What is the highest mountain in Africa?", "kilimanjaro"),
	("What is the capital of Canada?", "ottawa"),
	("Which desert is one of the driest places?", "atacama"),
	("Which river flows into the Atlantic through Brazil?", "amazon"),
]
STOP = {"the", "is", "of", "in", "what", "which", "a", "and", "on", "into", "its", "one",
        "through", "places", "world"}


def tokens(text):
	return [t for t in re.findall(r"[a-z]+", text.lower()) if t not in STOP]


class BM25:
	def __init__(self, docs, k1=1.5, b=0.75):
		self.docs = [(doc_id, tokens(text), text) for doc_id, text in docs]
		self.avg = sum(len(t) for _, t, _ in self.docs) / len(self.docs)
		df = Counter(term for _, toks, _ in self.docs for term in set(toks))
		n = len(self.docs)
		self.idf = {t: math.log(1 + (n - c + 0.5) / (c + 0.5)) for t, c in df.items()}
		self.k1, self.b = k1, b

	def search(self, query, k):
		q = tokens(query)
		scored = []
		for doc_id, toks, text in self.docs:
			tf = Counter(toks)
			score = sum(self.idf.get(t, 0.0) * tf[t] * (self.k1 + 1)
			            / (tf[t] + self.k1 * (1 - self.b + self.b * len(toks) / self.avg))
			            for t in q)
			scored.append((score, doc_id, text))
		scored.sort(reverse=True)
		return scored[:k]


def reason(question, evidence, work):
	"""Stand-in reasoner: pick the capitalised candidate that best fits.

	`work` repeats the scoring to give the step a realistic, measurable
	duration; it does not change the answer.
	"""
	q = set(tokens(question))
	best = None
	for _ in range(work):
		candidates = Counter()
		for score, _, text in evidence:
			for word in re.findall(r"\b[A-Z][a-z]+\b", text):
				if word.lower() not in q and word.lower() not in STOP:
					candidates[word] += score
		best = candidates.most_common(1)[0][0] if candidates else None
	return best


def answer(session, index, question, expected, work):
	with session.span("agent:question", attributes={"question": question}) as q:
		for attempt, k in enumerate((1, 3), start=1):
			with session.span("retrieval:search", attributes={"k": k, "attempt": attempt}):
				evidence = index.search(question, k)
			with session.span("llm:reason", attributes={"attempt": attempt}) as r:
				result = reason(question, evidence, work)
				r.set_tokens(input=sum(len(t.split()) for _, _, t in evidence),
				             output=1 if result else 0)
			with session.span("verify:check", attributes={"attempt": attempt}) as v:
				ok = bool(result) and any(result in t for _, _, t in evidence)
				v.set_attribute("verified", ok)
			if ok:
				break
		q.set_attribute("answer", result)
		q.set_attribute("correct", (result or "").lower() == expected)
		return result


async def answer_async(session, index, question, expected, work):
	async with session.span("agent:question", attributes={"question": question}):
		async with session.span("retrieval:search"):
			evidence = index.search(question, 3)
			await asyncio.sleep(0)
		async with session.span("llm:reason"):
			result = await asyncio.to_thread(reason, question, evidence, work)
		async with session.span("verify:check"):
			await asyncio.sleep(0)
		return result


def main():
	ap = argparse.ArgumentParser(description=__doc__,
	                             formatter_class=argparse.RawDescriptionHelpFormatter)
	ap.add_argument("--out", default="agent_spans.jsonl")
	ap.add_argument("--work", type=int, default=3000,
	                help="reasoning repetitions per step (controls step duration)")
	ap.add_argument("--parallel", action="store_true",
	                help="answer all questions concurrently (asyncio)")
	args = ap.parse_args()
	index = BM25(CORPUS)

	with Session.from_env(args.out, overwrite=True,
	                      attributes={"example": "agent_workflow",
	                                  "parallel": args.parallel}) as session:
		if args.parallel:
			async def run_all():
				async with session.span("agent:batch"):
					return await asyncio.gather(*(answer_async(session, index, q, e, args.work)
					                              for q, e in QUESTIONS))
			results = asyncio.run(run_all())
		else:
			results = [answer(session, index, q, e, args.work) for q, e in QUESTIONS]
	correct = sum((r or "").lower() == e for r, (_, e) in zip(results, QUESTIONS))
	print(f"answered {len(QUESTIONS)} questions, {correct} correct")
	print(f"spans written to {session.path}")


if __name__ == "__main__":
	main()
