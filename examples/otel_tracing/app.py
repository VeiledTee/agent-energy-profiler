"""Example 4: energy profiling inside an OpenTelemetry-traced application.

A small question-answering "service" that is already traced with
OpenTelemetry. Each request is an application span (`POST /ask`); inside it
the agent's operations are profiler spans; inside those, the application's
own client spans (a fake vector database) keep nesting:

    POST /ask                 application span (OpenTelemetry)
      agent:request           profiler span
        retrieval:search      profiler span
          vector_db.query     application span, child of the profiler span
        llm:reason            profiler span (CPU work standing in for a model)
        verify:check          profiler span

The profiler joins the application's trace (Correlation) and, with
propagate=True, becomes the current OpenTelemetry span while its spans are
open. After the run the profiler's spans are attributed and exported to the
SAME exporter, so a trace backend shows one tree with energy on the profiler
spans. Nothing global is configured: the application owns its
TracerProvider and the profiler only uses what it is handed.

Run on its own (Session.profile starts the sampler and attributes the run when
the session closes; this program then prints a short report and exports):

    python examples/otel_tracing/app.py --out-dir runs/otel

or under `agent-energy-profiler run`, then export the attributed run:

    agent-energy-profiler run --out-dir runs/otel -- python examples/otel_tracing/app.py
    agent-energy-profiler otel-export runs/otel --out runs/otel/otel_spans.jsonl

Needs the otel extra: pip install 'agent-energy-profiler[otel]'.
"""

import argparse
import hashlib
import json
import os
import time

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind

from agent_energy_profiler import Session, report
from agent_energy_profiler.otel import Correlation, JsonLinesSpanExporter, export_spans

QUESTIONS = ["What is the capital of Australia?", "Which lake is the deepest?",
             "What is the highest mountain in Africa?"]
DOCS = {"australia": "Canberra is the capital of Australia.",
        "lake": "Lake Baikal is the deepest lake.",
        "mountain": "Kilimanjaro is the highest mountain in Africa."}


def reason(question, evidence, work):
	"""Stand-in for a model call: hashing loops sized by `work`."""
	digest = question.encode()
	for _ in range(work):
		digest = hashlib.sha256(digest + evidence.encode()).digest()
	return evidence.split(" ")[0]


def handle(tracer, session, request_id, question, work):
	with tracer.start_as_current_span("POST /ask", kind=SpanKind.SERVER) as http:
		http.set_attribute("http.request.method", "POST")
		with session.bind(request_id=request_id):
			with session.span("agent:request", attributes={"question": question}):
				with session.span("retrieval:search"):
					with tracer.start_as_current_span("vector_db.query", kind=SpanKind.CLIENT):
						time.sleep(0.02)
						key = next((k for k in DOCS if k in question.lower()), "lake")
						evidence = DOCS[key]
				with session.span("llm:reason") as s:
					answer = reason(question, evidence, work)
					s.set_tokens(input=len(question.split()) + len(evidence.split()), output=1)
				with session.span("verify:check") as s:
					s.set_attribute("verified", answer in evidence)
		http.set_attribute("http.response.status_code", 200)
	return answer


def print_tree(spans):
	"""Indented trace tree with the energy the profiler attached."""
	by_parent = {}
	ids = {sp.context.span_id for sp in spans}
	for sp in spans:
		parent = sp.parent.span_id if sp.parent is not None and sp.parent.span_id in ids else None
		by_parent.setdefault(parent, []).append(sp)

	def show(sp, depth):
		a = sp.attributes
		ms = (sp.end_time - sp.start_time) / 1e6
		if "aep.operation.label" in a:
			gpu = (f"gpu {a['aep.energy.gpu.energy_j']:.3f} J measured"
			       if "aep.energy.gpu.energy_j" in a else
			       f"gpu {a.get('aep.energy.gpu.status')} ({a.get('aep.energy.gpu.reason')})")
			est = (f", est. {a['aep.estimate.gpu.energy_j']:.3f} J"
			       if "aep.estimate.gpu.energy_j" in a else "")
			cpu = (f", cpu_package {a['aep.energy.cpu_package.energy_j']:.3f} J"
			       if "aep.energy.cpu_package.energy_j" in a else "")
			tag = f"   [{gpu}{est}{cpu}; {a.get('aep.accounting.attribution', '?')}]"
		else:
			tag = "   (application span)"
		print(f"  {'  ' * depth}{sp.name:<{24 - 2 * depth}} {ms:8.1f} ms{tag}")
		for child in sorted(by_parent.get(sp.context.span_id, []), key=lambda s: s.start_time):
			show(child, depth + 1)

	for root in sorted(by_parent.get(None, []), key=lambda s: s.start_time):
		show(root, 0)


def main():
	ap = argparse.ArgumentParser(description=__doc__,
	                             formatter_class=argparse.RawDescriptionHelpFormatter)
	ap.add_argument("--out-dir", default=None,
	                help="where spans, power and exports go when run on its own (default: "
	                     "a new agent-energy-profiler-runs/otel_tracing-<timestamp>/)")
	ap.add_argument("--work", type=int, default=1_000_000,
	                help="hashing iterations per llm:reason (controls its duration)")
	args = ap.parse_args()
	under_aep_run = bool(os.environ.get(Session.ENV_SPANS_FILE))

	# -- the application's own tracing setup (not the profiler's) ---------
	memory = InMemorySpanExporter()
	provider = TracerProvider(resource=Resource.create({"service.name": "qa-service"}))
	provider.add_span_processor(SimpleSpanProcessor(memory))
	tracer = provider.get_tracer("qa-service")

	# -- the profiler ------------------------------------------------------
	# On its own: a sampler (power every row, GPU counter at the first and final
	# row) and attribution when the session closes; quiet, because this program
	# prints its own short report below. Under `agent-energy-profiler run`:
	# run's sampler, mode and report.
	with Session.profile("otel_tracing", out_dir=args.out_dir, quiet=True,
	                     hooks=[Correlation(propagate=True)],
	                     attributes={"example": "otel_tracing"}) as session:
		for i, question in enumerate(QUESTIONS):
			handle(tracer, session, f"req-{i}", question, args.work)
	print(f"{len(QUESTIONS)} requests handled; profiler mode {session.mode} "
	      f"({session.mode_reason})")

	if under_aep_run:
		print("under `agent-energy-profiler run`: it attributes and reports when this program exits; then "
		      "export with `agent-energy-profiler otel-export <run dir>`")
		provider.shutdown()
		return
	if session.report is None:
		raise SystemExit(f"profile {session.profile_status}: {session.profile_error}")

	# -- after the run: report, export into the same pipeline -------------
	print(report.render_text(session.report, top=5))
	result = export_spans(session.out_dir, memory, resource=provider.resource)
	print(f"\nexported {result['n_spans']} profiler spans into the application's exporter")
	print("\none trace per request (application spans + profiler spans):")
	print_tree(memory.get_finished_spans())

	out = os.path.join(session.out_dir, "otel_spans.jsonl")
	file_exporter = JsonLinesSpanExporter(out)
	file_exporter.export(memory.get_finished_spans())
	file_exporter.shutdown()
	print(f"\nall spans as OpenTelemetry JSON lines: {out}")
	provider.shutdown()


if __name__ == "__main__":
	main()
