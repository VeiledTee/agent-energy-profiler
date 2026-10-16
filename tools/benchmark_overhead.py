"""Instrumentation overhead of a span, v1 profiler.span vs v2 Session.span.

Measures the wall-clock cost of entering and leaving an empty span, which is
the overhead added to every instrumented operation. Configurations:

  v1-disabled   profiler.span with no events file (measurement off)
  v1            profiler.span writing events.jsonl, GPU from NVML if present
  v2-nogpu      Session(gpu=False)
  v2-fake       Session with a constant in-memory counter (no NVML cost)
  v2-nvml       Session(gpu="auto"); skipped when NVML is unavailable

Milestone 6 adds, all on v2-nogpu so only the added cost is visible:

  bind          inside session.bind(request_id=...)
  otel-corr     hooks=[otel.Correlation()], under an application span
  otel-prop     hooks=[otel.Correlation(propagate=True)], same
  ...-nvml      the two OpenTelemetry rows again with NVML (direct mode)

and the throughput of otel.export_spans over an attributed file (--export-n
spans), to an exporter that discards them. The OpenTelemetry rows need the
otel extra and are skipped without it.

Usage:
  .venv/bin/python tools/benchmark_overhead.py [--n 20000] [--export-n 50000]
      [--work-dir DIR]
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
	ap.add_argument("--export-n", type=int, default=50000)
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
		with Session(path, gpu=gpu, overwrite=True, mode="direct") as s:
			header_gpu = s._reader is not None
			if name == "v2-nvml" and not header_gpu:
				rows.append((name, None))
				continue
			rows.append((name, time_spans(lambda: s.span("llm:x"), args.n)))

	with Session(os.path.join(work, "bind.jsonl"), gpu=False, mode="sampled",
	             overwrite=True) as s:
		with s.bind(request_id="r1", user="u"):
			rows.append(("bind", time_spans(lambda: s.span("llm:x"), args.n)))

	try:
		from opentelemetry.sdk.trace import TracerProvider
		from agent_energy_profiler import otel
	except ImportError:
		otel = None
	export = None
	if otel is not None:
		tracer = TracerProvider().get_tracer("bench")
		configs = [("otel-corr", False, False), ("otel-prop", False, True),
		           ("otel-corr-nvml", "auto", False), ("otel-prop-nvml", "auto", True)]
		for name, gpu, propagate in configs:
			path = os.path.join(work, f"{name}.jsonl")
			with Session(path, gpu=gpu, mode="direct" if gpu else "sampled", overwrite=True,
			             hooks=[otel.Correlation(propagate=propagate)]) as s:
				if gpu and s._reader is None:
					rows.append((name, None))
					continue
				with tracer.start_as_current_span("app"):
					rows.append((name, time_spans(lambda: s.span("llm:x"), args.n)))
		export = benchmark_export(otel, work, args.export_n)

	print(f"empty-span overhead, {args.n} spans x 5 repeats (median of means)")
	for name, us in rows:
		print(f"  {name:<15} {'unavailable' if us is None else f'{us:8.1f} us/span'}")
	if export:
		print(f"otel.export_spans: {export[0]} attributed spans in {export[1]:.2f} s "
		      f"({export[0] / export[1]:,.0f} spans/s), peak RSS {export[2]:.0f} MB")


def benchmark_export(otel, work, n):
	"""(spans, seconds, peak RSS MB) of exporting an attributed nested session."""
	import resource
	from agent_energy_profiler import attribution_v2
	from opentelemetry.sdk.trace.export import SpanExportResult

	class Discard:
		def export(self, spans):
			return SpanExportResult.SUCCESS

		def shutdown(self):
			pass

	path = os.path.join(work, "export.jsonl")
	with Session(path, gpu=False, mode="sampled", overwrite=True) as s:
		for _ in range(n // 4):
			with s.span("agent:step"):
				with s.span("retrieval:search"):
					pass
				with s.span("llm:reason") as r:
					r.set_tokens(input=100, output=10)
				with s.span("tool:x", attributes={"k": 3}):
					pass
	out = os.path.join(work, "export_out")
	attribution_v2.attribute_run(path, out)
	started = time.perf_counter()
	result = otel.export_spans(out, Discard())
	elapsed = time.perf_counter() - started
	rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
	return result["n_spans"], elapsed, rss


if __name__ == "__main__":
	main()
