"""`agent-energy-profiler`: one command for the whole workflow.

    agent-energy-profiler validate [--json hw.json]       what can this host measure?
    agent-energy-profiler run [options] -- python app.py  run a program under profiling
    agent-energy-profiler attribute <spans|events> --power power.csv --out-dir out/
    agent-energy-profiler report <run dir or file> [--format text|json|csv]
    agent-energy-profiler export <run dir or file> --out-dir exported/
    agent-energy-profiler otel-export <run dir or file> [--endpoint URL | --out FILE]
    agent-energy-profiler sample --out power.csv          the sampler on its own

What `agent-energy-profiler run` can and cannot see
---------------------------------------------------
`agent-energy-profiler run` measures the WHOLE program: it starts the hardware
sampler, reads the GPU energy counter before and after, and records the
program's start and end. That gives the program's total device energy and
nothing finer.

Operation-level energy (which LLM call, which retrieval, which tool) exists
only for spans the program records itself through the SDK:

    from agent_energy_profiler import Session
    with Session.from_env("spans.jsonl") as session:
        with session.span("llm:plan"):
            ...

`agent-energy-profiler run` tells such a program where to write (environment
variables read by Session.from_env) and attributes the spans afterwards. It never inspects an
uninstrumented process to guess what its operations were.
"""

import argparse
import contextlib
import csv
import json
import os
import subprocess
import sys
import time

from . import __version__

#: The command's name (console script `agent-energy-profiler`; also
#: `python -m agent_energy_profiler`).
PROG = "agent-energy-profiler"


@contextlib.contextmanager
def _argv(argv):
	saved = sys.argv
	sys.argv = argv
	try:
		yield
	finally:
		sys.argv = saved


def _resolution(args):
	from .measurement import Resolution
	report = getattr(args, "resolution_report", "") or getattr(args, "hardware_report", "")
	if report:
		return Resolution.from_hardware_report(report)
	if getattr(args, "probe_resolution", False):
		print(f"{PROG}: probing the GPU counter update interval (~5 s)...", file=sys.stderr)
		return Resolution.probe()
	return Resolution.unknown()


# --------------------------------------------------------------------------
# agent-energy-profiler validate / sample
# --------------------------------------------------------------------------

def cmd_validate(args, rest):
	from . import validation
	with _argv([f"{PROG} validate"] + rest):
		return validation.main()


def cmd_sample(args, rest):
	from . import sampling
	with _argv([f"{PROG} sample"] + rest):
		sampling.main()
	return 0


# --------------------------------------------------------------------------
# agent-energy-profiler run
# --------------------------------------------------------------------------

def _no_gpu_hint():
	"""Why `run` found no GPU counter and what to do (stderr only; nothing recorded)."""
	import importlib.util
	if importlib.util.find_spec("pynvml") is None:
		return ("For an NVIDIA GPU, install the gpu extra: "
		        "pip install 'agent-energy-profiler[gpu]'.")
	return f"NVML found no GPU with a readable energy counter; `{PROG} validate` shows why."


def _gpu_reading(reader):
	if reader is None:
		return None
	try:
		return reader.read()
	except Exception:
		return None


def cmd_run(args, rest):
	from . import attribution_v2, report
	from . import measurement as m
	from .sampling import Sampler

	command = rest[1:] if rest and rest[0] == "--" else rest
	if not command:
		print(f"{PROG} run: give the program after `--`, e.g. {PROG} run -- python app.py",
		      file=sys.stderr)
		return 2
	out_dir = args.out_dir or os.path.join(f"{PROG}-runs", time.strftime("%Y%m%d-%H%M%S"))
	os.makedirs(out_dir, exist_ok=True)
	spans_file = os.path.join(out_dir, "spans.jsonl")
	power_file = os.path.join(out_dir, "power.csv")
	for existing in (spans_file, power_file):
		if os.path.exists(existing):
			print(f"{PROG} run: {existing} already exists; use a new --out-dir", file=sys.stderr)
			return 2

	resolution = _resolution(args)
	disturbance = (m.CounterDisturbance.from_hardware_report(args.hardware_report)
	               if args.hardware_report else m.CounterDisturbance.unknown())
	mode_source = m.MODE_SOURCE_DEFAULT if args.mode is None else m.MODE_SOURCE_CALLER
	requested_mode = args.mode or m.MODE_AUTO
	reader = None
	if args.gpu != "off":
		candidate = m.NvmlReader()
		try:
			if candidate.start():
				reader = candidate
		except Exception:
			reader = None
	# auto applies a NOT_DETECTED verdict only to the configuration it was
	# measured on (0.4.0)
	mode, mode_reason = m.resolve_mode(requested_mode, disturbance,
	                                   m.current_configuration(reader))
	counter_hz = (args.counter_hz if args.counter_hz is not None
	              else m.MODES[mode]["timeline_counter_hz"])
	if args.no_sampler:
		timeline = None
	else:
		from .sampling import timeline_meta
		timeline = timeline_meta(args.power_hz, counter_hz)
	print(f"{PROG} run: measurement mode {mode} ({mode_reason})", file=sys.stderr)
	if reader is None and args.gpu != "off":
		print(f"{PROG} run: no GPU energy counter, so GPU values will be n/a. "
		      f"{_no_gpu_hint()}", file=sys.stderr)
	attributes = dict(kv.split("=", 1) for kv in args.attr)
	env = dict(os.environ)
	env.update({
		"AEP_SPANS_FILE": os.path.abspath(spans_file),
		"AEP_RESOLUTION": json.dumps(resolution.to_dict()),
		"AEP_GPU": "off" if args.gpu == "off" else "auto",
		"AEP_RUN_ATTRIBUTES": json.dumps(attributes),
		"AEP_MEASUREMENT": json.dumps({"mode": mode, "requested_mode": requested_mode,
		                               "mode_reason": mode_reason,
		                               "disturbance": disturbance.to_dict(),
		                               "timeline": timeline}),
	})

	sampler = (None if args.no_sampler
	           else Sampler(power_file, hz=args.power_hz, counter_hz=counter_hz))

	if sampler is not None:
		sampler.start()
	try:
		gpu_start = _gpu_reading(reader)
		start_unix, start_mono = time.time(), time.monotonic()
		proc = subprocess.run(command, env=env)
		end_mono, end_unix = time.monotonic(), time.time()
		gpu_end = _gpu_reading(reader)
	finally:
		sampler_exit = sampler.stop() if sampler is not None else None
		if reader is not None:
			reader.stop()

	duration = end_mono - start_mono
	gpu = (m.gpu_in_band(gpu_start, gpu_end, duration, resolution) if reader is not None
	       else m.unavailable_gpu("disabled" if args.gpu == "off" else "no_counter"))
	window = run_counter = None
	if sampler is not None:
		tl = attribution_v2.Timeline(power_file)
		window = attribution_v2._window_from_bounds(start_unix, end_unix, tl, resolution)
		if tl.timeline_meta is not None:
			run_counter = tl.run_counter(start_unix, end_unix)
	instrumented = os.path.exists(spans_file)
	run = {
		"aep_run_version": 2,
		"measurement": m.measurement_config(mode, requested_mode, mode_reason, disturbance,
		                                    timeline, source=mode_source),
		"profiler_version": __version__,
		"command": command,
		"exit_code": proc.returncode,
		"start_unix_s": start_unix,
		"end_unix_s": end_unix,
		"duration_s": duration,
		"whole_program": {
			"gpu_in_band": gpu,
			"sampler_window": window,
			"sampler_run_counter": run_counter,
			"note": "device-level energy over the program's lifetime, including "
			        "interpreter start-up and anything else running on the devices",
		},
		"resolution": resolution.to_dict(),
		"sampler": None if sampler is None else dict(sampler.describe(), exit_code=sampler_exit),
		"instrumented": instrumented,
		"spans_file": spans_file if instrumented else None,
		"attributes": attributes,
	}
	with open(os.path.join(out_dir, "run.json"), "w") as f:
		json.dump(run, f, indent=2)

	print(f"\n{PROG} run: exit code {proc.returncode}, {duration:.2f} s -> {out_dir}")
	print(f"  whole program, GPU (in-band counter): "
	      f"{_fmt_domain(gpu)}")
	if run_counter:
		span = (f", {run_counter['duration_s']:.2f} s" if run_counter.get("duration_s")
		        is not None else "")
		print(f"  sampler run counter, GPU ({run_counter['n_readings']} readings{span}): "
		      f"{_fmt_domain(run_counter)}")
	if window:
		for d, v in window["domains"].items():
			print(f"  whole program, {d} (sampled counter): {_fmt_domain(v)}")
	if not instrumented:
		print("  no spans were recorded: the program does not use the SDK "
		      "(Session.from_env), so only whole-program energy is available.")
		return proc.returncode
	if not _has_spans(spans_file):
		# nothing to attribute; the report cannot be built from zero spans
		print("  no spans were recorded: the program opened a session but no span, so "
		      "there is no operation-level data; only whole-program energy is available.")
		return proc.returncode
	code = attribution_v2.main(["--spans", spans_file, "--out-dir", out_dir]
	                           + (["--power", power_file] if sampler is not None else []))
	if code != 0:
		print(f"{PROG} run: attribution failed; raw files are kept in {out_dir}", file=sys.stderr)
		return code
	print()
	print(report.render_text(report.build(out_dir)))
	return proc.returncode


def _has_spans(spans_file):
	"""Whether a v2 spans file holds at least one span record."""
	with open(spans_file) as f:
		for line in f:
			try:
				if json.loads(line).get("record_type") == "span":
					return True
			except ValueError:
				continue
	return False


def _fmt_domain(d):
	if d.get("status") == "measured":
		flag = ""
		if d.get("below_resolution") is True:
			flag = " (below counter resolution)"
		return f"{d['energy_j']:.3f} J{flag}"
	return f"n/a ({d.get('status')}: {d.get('reason')})"


# --------------------------------------------------------------------------
# agent-energy-profiler attribute / report / export
# --------------------------------------------------------------------------

def cmd_attribute(args, rest):
	from . import attribution, attribution_v2, report
	schema, kind = report.classify(args.input)
	if kind == "attributed":
		print(f"{PROG} attribute: {args.input} is already attributed", file=sys.stderr)
		return 2
	out_dir = args.out_dir or os.path.dirname(os.path.abspath(args.input))
	if schema == 1 and args.schema != 2:
		# The v1 pipeline, unchanged: the research artifacts' own path.
		if not args.power:
			print(f"{PROG} attribute: schema-v1 attribution needs --power", file=sys.stderr)
			return 2
		os.makedirs(out_dir, exist_ok=True)
		with _argv([f"{PROG} attribute", "--events", args.input, "--power", args.power,
		            "--out", os.path.join(out_dir, "energy_summary.csv"),
		            "--out-events", os.path.join(out_dir, "events_attributed.jsonl")]
		           + (["--power-meta", args.power_meta] if args.power_meta else [])):
			return attribution.main()
	argv = ["--spans", args.input, "--out-dir", out_dir]
	if args.power:
		argv += ["--power", args.power]
	if args.power_meta:
		argv += ["--power-meta", args.power_meta]
	if args.resolution_report:
		argv += ["--resolution-report", args.resolution_report]
	if args.allow_uncovered:
		argv.append("--allow-uncovered")
	return attribution_v2.main(argv)


def cmd_report(args, rest):
	from . import report
	resolution = _resolution(args) if args.resolution_report else None
	try:
		rep = report.build(args.input, resolution)
	except (OSError, ValueError) as e:
		print(f"{PROG} report: {e}", file=sys.stderr)
		return 2
	if args.format == "json":
		text = json.dumps(rep, indent=2)
	elif args.format == "csv":
		text = report.operations_csv(rep)
	else:
		text = report.render_text(rep, top=args.top)
	if args.out:
		with open(args.out, "w") as f:
			f.write(text if text.endswith("\n") else text + "\n")
		print(f"report -> {args.out}")
	else:
		print(text)
	return 0


SPAN_COLUMNS = ["span_id", "parent_span_id", "trace_id", "name", "operation_type", "status",
                "start_unix_s", "end_unix_s", "duration_s", "input_tokens", "output_tokens",
                "attribution", "depth", "exclusive_duration_s"]


def _span_row(span):
	acc = span.get("accounting") or {}
	row = {k: span.get(k) for k in SPAN_COLUMNS if k in span}
	row["attribution"] = acc.get("attribution")
	row["depth"] = acc.get("depth")
	row["exclusive_duration_s"] = acc.get("exclusive_duration_s")
	for d in ("gpu", "cpu_package", "dram", "cpu_core"):
		dom = (span.get("energy") or {}).get(d) or {}
		row[f"{d}_energy_j"] = dom.get("energy_j")
		row[f"{d}_status"] = dom.get("status")
		row[f"{d}_reason"] = dom.get("reason")
		row[f"{d}_below_resolution"] = dom.get("below_resolution")
		ex = (acc.get("exclusive") or {}).get(d) or {}
		row[f"{d}_exclusive_energy_j"] = ex.get("energy_j")
		row[f"{d}_exclusive_status"] = ex.get("status")
	total = span.get("total") or {}
	row["total_energy_j"] = total.get("energy_j")
	row["total_status"] = total.get("status")
	est = (span.get("estimates") or {}).get("gpu") or {}
	row["estimated_gpu_energy_j"] = est.get("energy_j")
	row["estimated_gpu_status"] = est.get("status")
	return row


def cmd_export(args, rest):
	from . import report, schema_v2
	try:
		path = report.locate(args.input)
		schema, kind = report.classify(path)
	except (OSError, ValueError) as e:
		print(f"{PROG} export: {e}", file=sys.stderr)
		return 2
	if kind != "attributed":
		print(f"{PROG} export: attribute the run first ({PROG} attribute)", file=sys.stderr)
		return 2
	os.makedirs(args.out_dir, exist_ok=True)
	rep = report.build(path)
	with open(os.path.join(args.out_dir, "report.json"), "w") as f:
		json.dump(rep, f, indent=2)
	with open(os.path.join(args.out_dir, "operations.csv"), "w", newline="") as f:
		f.write(report.operations_csv(rep))
	columns = None
	n = 0
	with open(os.path.join(args.out_dir, "spans.csv"), "w", newline="") as f:
		writer = None
		for span in schema_v2.iter_records(path):
			row = _span_row(span)
			if writer is None:
				columns = list(row)
				writer = csv.DictWriter(f, fieldnames=columns)
				writer.writeheader()
			writer.writerow({k: "" if v is None else v for k, v in row.items()})
			n += 1
	print(f"exported {n} spans -> {args.out_dir}/spans.csv, operations.csv, report.json")
	return 0


def cmd_otel_export(args, rest):
	"""Spans as OpenTelemetry spans: OTLP/HTTP, console, or JSON lines."""
	try:
		from . import otel
		from opentelemetry.sdk.resources import Resource
	except ImportError as e:
		hint = "agent-energy-profiler[otel]"
		print(f"{PROG} otel-export: {e}" + ("" if hint in str(e) else f" (pip install '{hint}')"),
		      file=sys.stderr)
		return 2
	from . import report
	try:
		path = report.locate(args.input)
		_, kind = report.classify(path)
	except (OSError, ValueError) as e:
		print(f"{PROG} otel-export: {e}", file=sys.stderr)
		return 2
	if kind != "attributed":
		print(f"{PROG} otel-export: the input is not attributed, so only in-band GPU energy "
		      f"is exported (no CPU/DRAM, estimates, totals or accounting). Run `{PROG} "
		      f"attribute` first for the full record.", file=sys.stderr)
	if args.endpoint:
		try:
			from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
		except ImportError:
			print(f"{PROG} otel-export: --endpoint needs the OTLP exporter: "
			      "pip install 'agent-energy-profiler[otlp]'", file=sys.stderr)
			return 2
		exporter = OTLPSpanExporter(endpoint=args.endpoint)
		where = args.endpoint
	elif args.out:
		exporter = otel.JsonLinesSpanExporter(args.out)
		where = args.out
	else:
		from opentelemetry.sdk.trace.export import ConsoleSpanExporter
		exporter = ConsoleSpanExporter()
		where = "stdout"
	resource = Resource.create({"service.name": args.service_name})
	try:
		result = otel.export_spans(path, exporter, resource=resource)
	finally:
		exporter.shutdown()
	print(f"{PROG} otel-export: {result['n_spans']} spans -> {where} "
	      f"({result['n_batches']} batches, {result['n_failed_batches']} failed"
	      + (f", {result['n_skipped']} without a start time skipped" if result["n_skipped"]
	         else "") + ")", file=sys.stderr)
	return 1 if result["n_failed_batches"] else 0


# --------------------------------------------------------------------------

def build_parser():
	ap = argparse.ArgumentParser(
		prog=PROG, description="Agent Energy Profiler: hardware-grounded energy "
		"profiling for instrumented Python programs.",
		epilog=f"Operation-level energy requires SDK spans in the program; "
		       f"`{PROG} run` alone measures the whole program. See `{PROG} run --help`.")
	ap.add_argument("--version", action="version", version=f"{PROG} {__version__}")
	sub = ap.add_subparsers(dest="command", required=True)

	p = sub.add_parser("validate", help="report what this host can measure "
	                   "(all further options go to the validator, e.g. --json hw.json)",
	                   add_help=False)
	p.set_defaults(func=cmd_validate, passthrough=True)

	p = sub.add_parser("sample", help="run the hardware sampler on its own "
	                   "(options: --out power.csv --hz 10)", add_help=False)
	p.set_defaults(func=cmd_sample, passthrough=True)

	p = sub.add_parser("run", help="run a program under profiling",
	                   description="Run COMMAND (after `--`) with the sampler on and the "
	                   "GPU counter read around it. Whole-program energy is always "
	                   "measured; per-operation energy only for spans the program records "
	                   "with Session.from_env().")
	p.add_argument("--out-dir", default="", help=f"default: {PROG}-runs/<timestamp>")
	p.add_argument("--mode", choices=("auto", "direct", "sampled", "hybrid"), default=None,
	               help="measurement mode (default auto: per-span GPU counter reads only "
	                    "if a disturbance test from --hardware-report found no effect on "
	                    "this host, driver and GPU; otherwise sampled). See "
	                    "docs/MEASUREMENT_MODES.md")
	p.add_argument("--hardware-report", default="",
	               help=f"`{PROG} validate --counter-disturbance --json` report: the "
	                    "GPU counter interval and the counter-disturbance verdict")
	p.add_argument("--power-hz", "--hz", dest="power_hz", type=float, default=10.0,
	               help="sampler row rate: averaged power (and RAPL) read on every row "
	                    "(default 10)")
	p.add_argument("--counter-hz", type=float, default=None,
	               help="sampler GPU energy-counter read rate; default from the mode "
	                    "(0 = first and final row only; hybrid: 1)")
	p.add_argument("--no-sampler", action="store_true",
	               help="no CPU/DRAM timeline; GPU from in-band reads only")
	p.add_argument("--gpu", choices=("auto", "off"), default="auto")
	p.add_argument("--resolution-report", default="",
	               help=f"`{PROG} validate --json` report giving the GPU counter interval "
	                    "(--hardware-report also provides it)")
	p.add_argument("--probe-resolution", action="store_true",
	               help="measure the GPU counter interval before running (~5 s)")
	p.add_argument("--attr", action="append", default=[], metavar="KEY=VALUE",
	               help="run attribute recorded in the session (repeatable)")
	p.set_defaults(func=cmd_run, passthrough=True)

	p = sub.add_parser("attribute", help="join recorded spans/events with power.csv")
	p.add_argument("input", help="spans.jsonl (v2) or events.jsonl (v1)")
	p.add_argument("--power", default="")
	p.add_argument("--power-meta", default="")
	p.add_argument("--out-dir", default="", help="default: beside the input")
	p.add_argument("--schema", type=int, choices=(1, 2), default=None,
	               help="force v2 attribution of a v1 file with --schema 2 "
	                    "(default: v1 files use the v1 pipeline)")
	p.add_argument("--resolution-report", default="")
	p.add_argument("--allow-uncovered", action="store_true")
	p.set_defaults(func=cmd_attribute)

	p = sub.add_parser("report", help="readable summary of an attributed run (v1 or v2)")
	p.add_argument("input", help="run directory or attributed/raw file")
	p.add_argument("--format", choices=("text", "json", "csv"), default="text")
	p.add_argument("--out", default="")
	p.add_argument("--top", type=int, default=20)
	p.add_argument("--resolution-report", default="",
	               help="GPU counter interval for v1 input (v2 records its own)")
	p.set_defaults(func=cmd_report)

	p = sub.add_parser("export", help="structured export: spans.csv, operations.csv, "
	                   "report.json")
	p.add_argument("input")
	p.add_argument("--out-dir", required=True)
	p.set_defaults(func=cmd_export)

	p = sub.add_parser("otel-export", help="send spans to OpenTelemetry (needs the otel extra)",
	                   description="Convert recorded spans to OpenTelemetry spans with energy "
	                   "attributes (docs/OPENTELEMETRY.md). Default destination: the console.")
	p.add_argument("input", help="run directory or spans file (attributed preferred)")
	dest = p.add_mutually_exclusive_group()
	dest.add_argument("--endpoint", default="",
	                  help="OTLP/HTTP traces endpoint, e.g. http://localhost:4318/v1/traces "
	                       "(needs the otlp extra)")
	dest.add_argument("--out", default="", help="write one JSON span per line to this file")
	p.add_argument("--service-name", default="agent-energy-profiler",
	               help="service.name of the exported spans' resource")
	p.set_defaults(func=cmd_otel_export)
	return ap


def main(argv=None):
	argv = list(sys.argv[1:] if argv is None else argv)
	ap = build_parser()
	# validate/sample/run pass everything after their name through
	if argv and argv[0] in ("validate", "sample"):
		args = ap.parse_args(argv[:1])
		return args.func(args, argv[1:])
	if argv and argv[0] == "run":
		if "--" in argv:
			split = argv.index("--")
			args = ap.parse_args(argv[:split])
			return args.func(args, argv[split:])
		args, rest = ap.parse_known_args(argv)
		return args.func(args, rest)
	args = ap.parse_args(argv)
	return args.func(args, [])


if __name__ == "__main__":
	sys.exit(main())
