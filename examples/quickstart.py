"""Quick start: profile a small three-step pipeline.

Pure Python: no GPU, API key, model or network needed. `busy()` stands in for
real work; replace it with your own retrieval, model and tool calls.

    python quickstart.py

The report is printed to stderr and saved, with the raw data, in
agent-energy-profiler-runs/quickstart-<timestamp>/.
"""

import time

from agent_energy_profiler import Session


def busy(seconds):
	"""Stand-in for real work: keep the CPU busy for `seconds`."""
	end = time.perf_counter() + seconds
	while time.perf_counter() < end:
		pass


with Session.profile("quickstart") as session:
	for question in ("q1", "q2", "q3"):
		with session.span("agent:answer", attributes={"question": question}):
			with session.span("retrieval:search"):
				busy(0.05)
			with session.span("llm:generate") as span:
				busy(0.30)
				span.set_tokens(input=120, output=40)
			with session.span("verify:check"):
				busy(0.02)
