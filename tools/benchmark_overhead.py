"""Instrumentation overhead of a span, v1 profiler.span vs v2 Session.span.

Measures the wall-clock cost of entering and leaving an empty span, which is
the overhead added to every instrumented operation. Configurations:

  v1-disabled   profiler.span with no events file (measurement off)
  v1            profiler.span writing events.jsonl, GPU from NVML if present
  v2-nogpu      Session(gpu=False)
  v2-fake       Session with a constant in-memory counter (no NVML cost)
  v2-nvml       Session(gpu="auto"); skipped when NVML is unavailable

Usage:
  .venv/bin/python tools/benchmark_overhead.py [--n 20000] [--work-dir DIR]
"""

import argparse
import os
import statistics
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))

from agent_energy_profiler import events, profiler  # noqa: E402
from agent_energy_profiler.measurement import CallableReader  # noqa: E402
from agent_energy_profiler.session import Session  # noqa: E402


def time_spans(enter, n, repeats=5):
	"""Median over `repeats` of the mean per-span cost (microseconds)."""
	results = []
	for _ in range(repeats):
		started = time.perf_counter()
		for _ in range(n):
			with enter():
				pass
		results.append((time.perf_counter() - started) / n * 1e6)
	return statistics.median(results)


def main():
	ap = argparse.ArgumentParser()
	ap.add_argument("--n", type=int, default=20000)
	ap.add_argument("--work-dir", default="")
	args = ap.parse_args()
	work = args.work_dir or tempfile.mkdtemp(prefix="aep-overhead-")
	os.makedirs(work, exist_ok=True)
	rows = []

	os.environ.pop("ENERGY_EVENTS_FILE", None)
	events.reset_from_env()
	rows.append(("v1-disabled", time_spans(lambda: profiler.span("llm:x"), args.n)))

	os.environ["ENERGY_EVENTS_FILE"] = os.path.join(work, "events.jsonl")
	events.reset_from_env()
	rows.append(("v1", time_spans(lambda: profiler.span("llm:x"), args.n)))
	events.close()
	os.environ.pop("ENERGY_EVENTS_FILE", None)
	events.reset_from_env()

	configs = [("v2-nogpu", False), ("v2-fake", CallableReader(lambda: [1000])),
	           ("v2-nvml", "auto")]
	for name, gpu in configs:
		path = os.path.join(work, f"{name}.jsonl")
		with Session(path, gpu=gpu, overwrite=True) as s:
			header_gpu = s._reader is not None
			if name == "v2-nvml" and not header_gpu:
				rows.append((name, None))
				continue
			rows.append((name, time_spans(lambda: s.span("llm:x"), args.n)))

	print(f"empty-span overhead, {args.n} spans x 5 repeats (median of means)")
	for name, us in rows:
		print(f"  {name:<12} {'unavailable' if us is None else f'{us:8.1f} us/span'}")


if __name__ == "__main__":
	main()
