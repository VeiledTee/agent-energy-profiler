"""M6: explicit instrumentation inside LangGraph graphs (docs/LANGGRAPH.md).

The recommended LangGraph integration is no integration module: decorate the
node functions with `session.trace(label)` or open `session.span(label)`
inside them. These tests pin the behaviour that recommendation relies on,
against the installed LangGraph: node signatures (config injection) survive
the decorator, and parallel branches get the right parent span whether the
graph runs synchronously (thread pool) or asynchronously (tasks).

Skipped unless `langgraph` is installed.
"""

import asyncio
import json
import operator
import os
import tempfile
import unittest
from typing import Annotated, TypedDict

import pytest

pytest.importorskip("langgraph.graph", reason="needs langgraph")

from langchain_core.runnables import RunnableConfig  # noqa: E402
from langgraph.graph import END, START, StateGraph  # noqa: E402

from agent_energy_profiler.measurement import CallableReader  # noqa: E402
from agent_energy_profiler.session import Session  # noqa: E402


class State(TypedDict):
	question: str
	log: Annotated[list, operator.add]


class Counter_:
	def __init__(self):
		self.v, self.reads = 1_000_000, 0

	def read(self):
		self.reads += 1
		self.v += 100
		return [self.v]


def build(session, use_async):
	"""START -> (retrieve || calculate) -> answer -> END, all instrumented."""
	g = StateGraph(State)
	if use_async:
		@session.trace("retrieval:search")
		async def retrieve(state, config: RunnableConfig):
			await asyncio.sleep(0.005)
			return {"log": [("retrieve", config["configurable"].get("user"))]}

		@session.trace("tool:calculate")
		async def calculate(state):
			await asyncio.sleep(0.005)
			return {"log": [("calculate", None)]}

		async def answer(state):
			async with session.span("llm:answer") as s:
				s.set_tokens(input=len(state["log"]), output=1)
				return {"log": [("answer", None)]}
	else:
		@session.trace("retrieval:search")
		def retrieve(state, config: RunnableConfig):
			return {"log": [("retrieve", config["configurable"].get("user"))]}

		@session.trace("tool:calculate")
		def calculate(state):
			return {"log": [("calculate", None)]}

		def answer(state):
			with session.span("llm:answer") as s:
				s.set_tokens(input=len(state["log"]), output=1)
				return {"log": [("answer", None)]}
	g.add_node("retrieve", retrieve)
	g.add_node("calculate", calculate)
	g.add_node("answer", answer)
	g.add_edge(START, "retrieve")
	g.add_edge(START, "calculate")
	g.add_edge("retrieve", "answer")
	g.add_edge("calculate", "answer")
	g.add_edge("answer", END)
	return g.compile()


class TestLangGraph(unittest.TestCase):

	def setUp(self):
		self._tmp = tempfile.TemporaryDirectory()
		self.path = os.path.join(self._tmp.name, "spans.jsonl")

	def tearDown(self):
		self._tmp.cleanup()

	def spans(self):
		with open(self.path) as f:
			recs = [json.loads(line) for line in f if line.strip()]
		return [r for r in recs if r["record_type"] == "span"]

	def check(self, out, counter):
		self.assertIn(("retrieve", "u1"), out["log"])          # config still injected
		spans = self.spans()
		by_name = {s["name"]: s for s in spans}
		self.assertEqual(set(by_name), {"agent:request", "retrieval:search",
		                                "tool:calculate", "llm:answer"})
		root = by_name["agent:request"]
		for name in ("retrieval:search", "tool:calculate", "llm:answer"):
			self.assertEqual(by_name[name]["parent_span_id"], root["span_id"], name)
			self.assertEqual(by_name[name]["trace_id"], root["trace_id"], name)
			self.assertEqual(by_name[name]["energy"]["gpu"]["status"], "measured", name)
		self.assertEqual(by_name["llm:answer"]["input_tokens"], 2)
		self.assertEqual(counter.reads, 2 * len(spans))

	def test_sync_graph(self):
		c = Counter_()
		with Session(self.path, gpu=CallableReader(c.read), mode="direct") as session:
			graph = build(session, use_async=False)
			with session.span("agent:request"):
				out = graph.invoke({"question": "q", "log": []},
				                   config={"configurable": {"user": "u1"}})
		self.check(out, c)

	def test_async_graph(self):
		c = Counter_()
		with Session(self.path, gpu=CallableReader(c.read), mode="direct") as session:
			graph = build(session, use_async=True)

			async def main():
				async with session.span("agent:request"):
					return await graph.ainvoke({"question": "q", "log": []},
					                           config={"configurable": {"user": "u1"}})
			out = asyncio.run(main())
		self.check(out, c)
		parallel = [s for s in self.spans() if s["name"] in ("retrieval:search",
		                                                     "tool:calculate")]
		# the two branches overlap in time, so accounting will call them shared
		a, b = parallel
		self.assertLess(max(a["start_unix_s"], b["start_unix_s"]),
		                min(a["end_unix_s"], b["end_unix_s"]))


if __name__ == "__main__":
	unittest.main()
