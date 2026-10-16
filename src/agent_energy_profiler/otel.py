"""OpenTelemetry integration (optional: pip install 'agent-energy-profiler[otel]').

Two independent parts. Neither changes what is measured, and neither touches
OpenTelemetry's global configuration (tracer provider, propagators, context
defaults): this module only reads the current context and, when asked, sends
spans to an exporter the caller supplies.

1. Correlation while recording (needs opentelemetry-api)

       from agent_energy_profiler import Session
       from agent_energy_profiler.otel import Correlation

       with Session("spans.jsonl", mode="auto", hooks=[Correlation()]) as session:
           ...

   A root span opened while an OpenTelemetry span is current joins that
   trace: it takes the trace id and records the OpenTelemetry span as its
   `external_parent`. With Correlation(propagate=True), each profiler span is
   also made the current OpenTelemetry span (as a non-recording span) while it
   is open, so application spans started inside it become its children. Those
   children then point at a span that exists only once you export the
   profiler's spans (part 2).

2. Export after attribution (needs opentelemetry-sdk)

       from agent_energy_profiler.otel import export_spans
       export_spans("run/spans_attributed.jsonl", exporter)

   Each recorded span becomes one OpenTelemetry span with the recorded
   trace/span/parent ids, timestamps, status and label, and its energy as
   `aep.*` attributes (docs/OPENTELEMETRY.md). Export runs after attribution
   because per-span CPU/DRAM energy, estimates, totals and accounting exist
   only then.

What the attributes are not: proof of accuracy. They carry the profiler's
values and provenance unchanged, including `aep.energy.external_meter_validated
= false`: no value has been checked against an external power meter
(docs/HARDWARE_VALIDATION_PROTOCOL.md).
"""

import hashlib
import json
import os

try:
	from opentelemetry import context as _otel_context
	from opentelemetry import trace as _otel_trace
except ImportError as e:  # pragma: no cover - exercised in the no-extra wheel test
	raise ImportError("agent_energy_profiler.otel needs OpenTelemetry: "
	                  "pip install 'agent-energy-profiler[otel]'") from e

from . import __version__
from . import measurement as m
from . import schema_v2

#: Version of the attribute mapping below. Bumped on any rename or meaning
#: change, so consumers can tell exports apart.
MAPPING_VERSION = 1

#: OpenTelemetry instrumentation scope of exported spans.
SCOPE_NAME = "agent_energy_profiler"

DOMAINS = ("gpu", "cpu_package", "dram", "cpu_core")

_SAMPLED = _otel_trace.TraceFlags(_otel_trace.TraceFlags.SAMPLED)


# --------------------------------------------------------------------------
# 1. correlation while recording
# --------------------------------------------------------------------------

class Correlation:
	"""Session hook linking profiler spans with the application's trace.

	propagate  also make each open profiler span the current OpenTelemetry
	           span, so application spans created inside it nest under it.
	           Off by default: those application spans then reference parents
	           that only exist after export_spans().
	"""

	def __init__(self, propagate=False):
		self.propagate = bool(propagate)
		self._tokens = {}

	def trace_context(self):
		"""The current OpenTelemetry span context, as the Session hook expects."""
		ctx = _otel_trace.get_current_span().get_span_context()
		if not ctx.is_valid:
			return None
		return {"system": "opentelemetry",
		        "trace_id": _otel_trace.format_trace_id(ctx.trace_id),
		        "span_id": _otel_trace.format_span_id(ctx.span_id),
		        "sampled": bool(ctx.trace_flags.sampled)}

	def on_span_start(self, span):
		if not self.propagate:
			return
		trace_id = int(span.trace_id, 16)
		current = _otel_trace.get_current_span().get_span_context()
		if current.is_valid and current.trace_id == trace_id:
			# same trace (the application's, or an enclosing profiler span):
			# keep its sampling decision and trace state
			flags, state = current.trace_flags, current.trace_state
		else:
			flags, state = _SAMPLED, None
		span_context = _otel_trace.SpanContext(trace_id, int(span.span_id, 16), is_remote=False,
		                                       trace_flags=flags, trace_state=state)
		token = _otel_context.attach(
			_otel_trace.set_span_in_context(_otel_trace.NonRecordingSpan(span_context)))
		self._tokens[(span.session_id, span.span_id)] = token

	def on_span_end(self, span, record):
		token = self._tokens.pop((span.session_id, span.span_id), None)
		if token is not None:
			_otel_context.detach(token)


# --------------------------------------------------------------------------
# 2. attribute mapping
# --------------------------------------------------------------------------

def _attr_value(value):
	"""An OpenTelemetry attribute value for `value`, or None to leave it out.

	Primitives pass through; homogeneous lists of primitives too; anything
	else (objects, mixed lists) becomes its JSON text.
	"""
	if value is None:
		return None
	if isinstance(value, (bool, str, int, float)):
		return value
	if isinstance(value, (list, tuple)):
		items = [v for v in value if v is not None]
		kinds = {type(v) for v in items}
		if len(kinds) <= 1 and kinds <= {bool, str, int, float}:
			return list(items)
	try:
		return json.dumps(value, sort_keys=True, default=str)
	except (TypeError, ValueError):
		return str(value)


def _put(attrs, key, value):
	value = _attr_value(value)
	if value is not None:
		attrs[key] = value


def _domain_attributes(attrs, prefix, d):
	"""One domain measurement: status always; energy only when measured."""
	status = d.get("status")
	_put(attrs, prefix + ".status", status)
	if status == m.MEASURED and d.get("energy_j") is not None:
		attrs[prefix + ".energy_j"] = float(d["energy_j"])
		below = d.get("below_resolution")
		attrs[prefix + ".resolution_known"] = below is not None
		if below is not None:
			attrs[prefix + ".below_resolution"] = bool(below)
		_put(attrs, prefix + ".update_intervals", d.get("update_intervals"))
	_put(attrs, prefix + ".reason", d.get("reason"))
	for key in ("source", "method", "scope"):
		_put(attrs, f"{prefix}.{key}", d.get(key))
	if d.get("diagnostic"):
		attrs[prefix + ".diagnostic"] = True


def session_attributes(header):
	"""Attributes describing how the session was measured (every span gets them)."""
	attrs = {"aep.otel.mapping_version": MAPPING_VERSION,
	         "aep.energy.scope": m.SCOPE_DEVICE,
	         "aep.energy.external_meter_validated": False}
	if not header:
		attrs["aep.measurement.recorded"] = False
		return attrs
	cfg = header.get("config") or {}
	_put(attrs, "aep.session.id", header.get("session_id"))
	_put(attrs, "aep.profiler.version", header.get("profiler_version"))
	meas = cfg.get("measurement")
	attrs["aep.measurement.recorded"] = bool(meas)
	if meas:
		for key in ("mode", "requested_mode", "mode_source", "model_version",
		            "span_counter_reads"):
			_put(attrs, f"aep.measurement.{key}", meas.get(key))
		_put(attrs, "aep.measurement.counter_disturbance",
		     (meas.get("counter_disturbance") or {}).get("verdict"))
	res = cfg.get("resolution") or {}
	_put(attrs, "aep.resolution.basis", res.get("basis"))
	_put(attrs, "aep.resolution.gpu_update_interval_s", res.get("gpu_update_interval_s"))
	_put(attrs, "aep.gpu.driver_version", (cfg.get("gpu") or {}).get("driver_version"))
	for key, value in (header.get("attributes") or {}).items():
		_put(attrs, f"aep.session.attributes.{key}", value)
	return attrs


def span_attributes(record, header=None):
	"""The OpenTelemetry attributes of one recorded (raw or attributed) span.

	Caller attributes come first and keep their keys; the profiler's own
	`aep.*` keys are written after them and win on a clash.
	"""
	attrs = {}
	for key, value in (record.get("attributes") or {}).items():
		_put(attrs, str(key), value)
	attrs.update(session_attributes(header))
	_put(attrs, "aep.operation.label", record.get("name"))
	_put(attrs, "aep.operation.type", record.get("operation_type"))
	_put(attrs, "aep.status", record.get("status"))
	_put(attrs, "error.type", record.get("error_type"))
	_put(attrs, "aep.span.duration_s", record.get("duration_s"))
	if record.get("clock_anomaly"):
		attrs["aep.span.clock_anomaly"] = True
	attrs["aep.span.other_sessions_active"] = bool(record.get("other_sessions_active"))
	_put(attrs, "aep.span.task", record.get("task"))
	_put(attrs, "thread.id", record.get("thread_id"))
	_put(attrs, "gen_ai.usage.input_tokens", record.get("input_tokens"))
	_put(attrs, "gen_ai.usage.output_tokens", record.get("output_tokens"))
	if record.get("upgraded_from_schema"):
		attrs["aep.upgraded_from_schema"] = record["upgraded_from_schema"]

	energy = record.get("energy") or {}
	for domain in DOMAINS:
		if isinstance(energy.get(domain), dict):
			_domain_attributes(attrs, f"aep.energy.{domain}", energy[domain])
	for domain, est in (record.get("estimates") or {}).items():
		if not isinstance(est, dict):
			continue
		prefix = f"aep.estimate.{domain}"
		_put(attrs, prefix + ".status", est.get("status"))
		if est.get("status") == m.ESTIMATED and est.get("energy_j") is not None:
			attrs[prefix + ".energy_j"] = float(est["energy_j"])
		_put(attrs, prefix + ".method", est.get("method"))
	total = record.get("total")
	if isinstance(total, dict):
		_put(attrs, "aep.energy.total.status", total.get("status"))
		if total.get("status") == m.MEASURED and total.get("energy_j") is not None:
			attrs["aep.energy.total.energy_j"] = float(total["energy_j"])
		_put(attrs, "aep.energy.total.boundary", total.get("boundary"))
		attrs["aep.energy.total.domains_measured"] = list(total.get("domains_measured") or [])
		attrs["aep.energy.total.domains_missing"] = list(total.get("domains_missing") or [])
		if total.get("below_resolution") is not None:
			attrs["aep.energy.total.below_resolution"] = bool(total["below_resolution"])
	acc = record.get("accounting")
	if isinstance(acc, dict):
		for key in ("attribution", "depth", "parent_missing", "n_children",
		            "children_overlap", "concurrent_spans", "exclusive_duration_s"):
			_put(attrs, f"aep.accounting.{key}", acc.get(key))
		for domain, ex in (acc.get("exclusive") or {}).items():
			if not isinstance(ex, dict):
				continue
			prefix = f"aep.accounting.exclusive.{domain}"
			_put(attrs, prefix + ".status", ex.get("status"))
			if ex.get("status") == m.MEASURED and ex.get("energy_j") is not None:
				attrs[prefix + ".energy_j"] = float(ex["energy_j"])
			_put(attrs, prefix + ".reason", ex.get("reason"))
	ext = record.get("external_parent")
	if isinstance(ext, dict):
		_put(attrs, "aep.external_parent.system", ext.get("system"))
	return attrs


# --------------------------------------------------------------------------
# 2. export
# --------------------------------------------------------------------------

def _synthetic_ids(record):
	"""Deterministic (trace_id, span_id) for a span recorded without them
	(a schema-v1 event): each becomes its own root trace."""
	digest = hashlib.sha256(
		f"aep-v1:{record.get('session_id')}:{record.get('span_id')}".encode()).hexdigest()
	trace_id, span_id = digest[:32], digest[32:48]
	return (trace_id if trace_id.strip("0") else "1".zfill(32),
	        span_id if span_id.strip("0") else "1".zfill(16))


def _int_id(value, length):
	if isinstance(value, str) and len(value) == length:
		try:
			n = int(value, 16)
		except ValueError:
			return None
		return n or None
	return None


def _header_for(path):
	"""The session header of a raw spans file, or of an attributed file's
	session_summary.json sidecar; None for v1 input."""
	header = schema_v2.session_header(path)
	if header is not None:
		return header
	summary = os.path.join(os.path.dirname(os.path.abspath(path)), "session_summary.json")
	try:
		with open(summary) as f:
			return (json.load(f) or {}).get("session")
	except (OSError, ValueError):
		return None


def to_readable_spans(path, resource=None, stats=None):
	"""Yield one opentelemetry.sdk ReadableSpan per span in `path`.

	`path` is a spans file (raw or attributed, schema v1 or v2) or a run
	directory. `resource` is the opentelemetry Resource to attach; pass your
	application's so the spans appear under its service. Default: a resource
	with service.name "agent-energy-profiler". A span without a start time
	cannot be placed on a timeline and is skipped (counted in
	stats["n_skipped"] when `stats` is a dict).
	"""
	from opentelemetry.sdk.resources import Resource
	from opentelemetry.sdk.trace import ReadableSpan
	from opentelemetry.sdk.util.instrumentation import InstrumentationScope
	from opentelemetry.trace import SpanContext, SpanKind, Status, StatusCode

	from .report import locate

	path = locate(path)
	header = _header_for(path)
	resource = resource or Resource.create({"service.name": "agent-energy-profiler"})
	scope = InstrumentationScope(SCOPE_NAME, __version__)
	for record in schema_v2.iter_records(path):
		start = record.get("start_unix_s")
		if start is None:
			if stats is not None:
				stats["n_skipped"] = stats.get("n_skipped", 0) + 1
			continue
		trace_id = _int_id(record.get("trace_id"), 32)
		span_id = _int_id(record.get("span_id"), 16)
		synthesized = trace_id is None or span_id is None
		if synthesized:
			t_hex, s_hex = _synthetic_ids(record)
			trace_id, span_id = int(t_hex, 16), int(s_hex, 16)
		parent = None
		parent_id = None if synthesized else _int_id(record.get("parent_span_id"), 16)
		ext = record.get("external_parent") if not synthesized else None
		if parent_id is not None:
			parent = SpanContext(trace_id, parent_id, is_remote=False, trace_flags=_SAMPLED)
		elif isinstance(ext, dict) and _int_id(ext.get("span_id"), 16):
			flags = _SAMPLED if ext.get("sampled", True) else _otel_trace.TraceFlags(0)
			parent = SpanContext(trace_id, _int_id(ext["span_id"], 16), is_remote=True,
			                     trace_flags=flags)
		start_ns = int(round(start * 1e9))
		duration = record.get("duration_s")
		if duration is not None:
			end_ns = start_ns + int(round(duration * 1e9))
		elif record.get("end_unix_s") is not None:
			end_ns = max(start_ns, int(round(record["end_unix_s"] * 1e9)))
		else:
			end_ns = start_ns
		status = record.get("status")
		if status in ("error", "timeout"):
			otel_status = Status(StatusCode.ERROR, record.get("error_type") or status)
		else:
			otel_status = Status(StatusCode.UNSET)
		attrs = span_attributes(record, header)
		if synthesized:
			attrs["aep.ids_synthesized"] = True
		yield ReadableSpan(
			name=record.get("name") or "unlabelled",
			context=SpanContext(trace_id, span_id, is_remote=False, trace_flags=_SAMPLED),
			parent=parent,
			resource=resource,
			attributes=attrs,
			kind=SpanKind.INTERNAL,
			status=otel_status,
			start_time=start_ns,
			end_time=end_ns,
			instrumentation_scope=scope,
		)


def export_spans(path, exporter, resource=None, batch_size=512):
	"""Send every span in `path` to an opentelemetry.sdk SpanExporter.

	Returns {"n_spans", "n_skipped", "n_batches", "n_failed_batches",
	"attributed"}. The exporter is not shut down: it is the caller's. Nothing
	global is configured; pass the exporter (and resource) your application
	uses.
	"""
	from opentelemetry.sdk.trace.export import SpanExportResult

	from .report import classify, locate

	path = locate(path)
	_, kind = classify(path)
	out = {"n_spans": 0, "n_skipped": 0, "n_batches": 0, "n_failed_batches": 0,
	       "attributed": kind == "attributed"}
	batch = []

	def flush():
		out["n_batches"] += 1
		if exporter.export(batch) != SpanExportResult.SUCCESS:
			out["n_failed_batches"] += 1
		batch.clear()

	for span in to_readable_spans(path, resource, stats=out):
		batch.append(span)
		out["n_spans"] += 1
		if len(batch) >= batch_size:
			flush()
	if batch:
		flush()
	return out


class JsonLinesSpanExporter:
	"""SpanExporter writing each span's to_json() as one line (offline export)."""

	def __init__(self, path):
		self._f = open(path, "w")

	def export(self, spans):
		from opentelemetry.sdk.trace.export import SpanExportResult
		for span in spans:
			self._f.write(json.dumps(json.loads(span.to_json())) + "\n")
		return SpanExportResult.SUCCESS

	def shutdown(self):
		self._f.close()

	def force_flush(self, timeout_millis=30000):
		self._f.flush()
		return True
