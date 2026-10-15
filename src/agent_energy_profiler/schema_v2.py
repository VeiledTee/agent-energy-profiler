"""Schema v2: span records with explicit energy provenance.

Field definitions and semantics: docs/SCHEMA_V2.md. This module holds the
version constants, a shape validator, and the reader that loads both v1 and v2
files into the v2 shape. Schema v1 (schema.py) is untouched and still written
by events.py.
"""

import json

from . import measurement as m
from .schema import Status, type_of_label

SCHEMA_VERSION = 2

SESSION_START = "session_start"
SPAN = "span"
SESSION_END = "session_end"
RECORD_TYPES = (SESSION_START, SPAN, SESSION_END)

DOMAIN_STATUSES = (m.MEASURED, m.UNAVAILABLE, m.INVALID)
SPAN_STATUSES = tuple(s.value for s in Status)

SPAN_FIELDS = (
	"schema_version", "record_type", "session_id", "trace_id", "span_id",
	"parent_span_id", "name", "operation_type", "status", "error_type",
	"start_unix_s", "end_unix_s", "start_monotonic_s", "end_monotonic_s",
	"duration_s", "clock_anomaly", "process_id", "thread_id", "task",
	"other_sessions_active", "input_tokens", "output_tokens", "attributes",
	"energy",
)

DOMAIN_FIELDS = ("energy_j", "status", "reason", "source", "method", "scope",
                 "below_resolution", "update_intervals", "raw")

#: v1 fields that become v2 attributes when a v1 event is read.
V1_CONTEXT_FIELDS = ("run_id", "question_id", "dataset", "paradigm",
                     "iteration", "traversal_depth", "step_index",
                     "model_name", "model_revision", "git_commit", "hardware_id")


def validate_domain(name, d):
	"""Problems with one domain measurement (empty list when well formed)."""
	problems = []
	if not isinstance(d, dict):
		return [f"{name}: not an object"]
	for key in DOMAIN_FIELDS:
		if key not in d:
			problems.append(f"{name}: missing {key!r}")
	if problems:
		return problems
	if d["status"] not in DOMAIN_STATUSES:
		problems.append(f"{name}: unknown status {d['status']!r}")
	if d["scope"] != m.SCOPE_DEVICE:
		problems.append(f"{name}: scope {d['scope']!r} is not 'device'")
	if d["status"] == m.MEASURED:
		if not isinstance(d["energy_j"], (int, float)) or isinstance(d["energy_j"], bool):
			problems.append(f"{name}: measured without a numeric energy_j")
		if d["reason"] is not None:
			problems.append(f"{name}: measured with a reason")
	else:
		if d["energy_j"] is not None:
			problems.append(f"{name}: {d['status']} must have energy_j null")
		if d["reason"] not in m.REASONS:
			problems.append(f"{name}: unknown reason {d['reason']!r}")
		if d["below_resolution"] is not None:
			problems.append(f"{name}: below_resolution set on an unmeasured value")
	if d["below_resolution"] not in (True, False, None):
		problems.append(f"{name}: below_resolution must be bool or null")
	return problems


def validate(record):
	"""Problems with one v2 record (empty list when well formed)."""
	if not isinstance(record, dict):
		return ["not an object"]
	problems = []
	if record.get("schema_version") != SCHEMA_VERSION:
		problems.append("schema_version is not 2")
	rtype = record.get("record_type")
	if rtype not in RECORD_TYPES:
		return problems + [f"unknown record_type {rtype!r}"]
	if rtype != SPAN:
		if not record.get("session_id"):
			problems.append("missing session_id")
		return problems
	for key in SPAN_FIELDS:
		if key not in record:
			problems.append(f"missing {key!r}")
	if problems:
		return problems
	if record["status"] not in SPAN_STATUSES:
		problems.append(f"unknown status {record['status']!r}")
	if not isinstance(record["name"], str) or not record["name"]:
		problems.append("name must be a non-empty string")
	if record["operation_type"] != type_of_label(record["name"]):
		problems.append("operation_type disagrees with the name's prefix")
	if record["duration_s"] is not None and record["duration_s"] < 0:
		problems.append("negative duration_s")
	energy = record["energy"]
	if not isinstance(energy, dict):
		return problems + ["energy is not an object"]
	for name, d in energy.items():
		problems += validate_domain(f"energy.{name}", d)
	for name, est in (record.get("estimates") or {}).items():
		if est.get("status") not in (m.ESTIMATED, m.UNAVAILABLE):
			problems.append(f"estimates.{name}: bad status {est.get('status')!r}")
	return problems


# --------------------------------------------------------------------------
# reading v1 as v2
# --------------------------------------------------------------------------

def upgrade_v1(event, resolution=None):
	"""A v1 event (raw or attributed) in the v2 span shape.

	See docs/SCHEMA_V2.md "Reading v1 as v2". The input is not modified.
	"""
	resolution = resolution or m.Resolution.unknown()
	start = event.get("start_timestamp", event.get("t_start"))
	end = event.get("end_timestamp", event.get("t_end"))
	wall = (end - start) if (start is not None and end is not None) else None
	anomaly = wall is not None and wall < 0
	duration = None if (wall is None or anomaly) else wall
	label = event.get("operation_label", event.get("label")) or ""

	gpu_j = event.get("gpu_energy_j")
	source = event.get("gpu_energy_source")
	estimates = {}
	if gpu_j is not None and source == "power_integration":
		# v1 put this estimate in gpu_energy_j; v2 keeps it out of energy.
		estimates["gpu"] = {"energy_j": gpu_j, "status": m.ESTIMATED,
		                    "method": "power_integration",
		                    "detail": "v1 gpu_energy_source=power_integration"}
		gpu = m.unavailable_gpu("no_counter")
	elif gpu_j is not None:
		below, intervals = resolution.classify(duration)
		gpu = m.domain(gpu_j, m.MEASURED, None, m.GPU_SOURCE, m.IN_BAND,
		               below, intervals, [])
	else:
		gpu = m.unavailable_gpu("no_counter")

	energy = {"gpu": gpu}
	for v1_field, domain_name in (("cpu_package_energy_j", "cpu_package"),
	                              ("dram_energy_j", "dram"),
	                              ("cpu_core_energy_j", "cpu_core")):
		if v1_field not in event:
			continue
		value = event.get(v1_field)
		if value is None:
			d = m.domain(None, m.UNAVAILABLE, "no_counter", "rapl", m.SAMPLED)
		else:
			d = m.domain(value, m.MEASURED, None, "rapl", m.SAMPLED)
		if domain_name == "cpu_core":
			d["diagnostic"] = True
		energy[domain_name] = d

	attributes = {k: event.get(k) for k in V1_CONTEXT_FIELDS if k in event}
	attributes["meta"] = event.get("meta") or {}
	status = event.get("status") or Status.OK.value
	return {
		"schema_version": SCHEMA_VERSION,
		"record_type": SPAN,
		"upgraded_from_schema": 1,
		"session_id": event.get("run_id"),
		"trace_id": None,
		"span_id": event.get("event_id"),
		"parent_span_id": None,
		"name": label,
		"operation_type": event.get("operation_type", type_of_label(label)),
		"status": status if status in SPAN_STATUSES else Status.ERROR.value,
		"error_type": (event.get("meta") or {}).get("exception_type"),
		"start_unix_s": start,
		"end_unix_s": end,
		"start_monotonic_s": None,
		"end_monotonic_s": None,
		"duration_s": duration,
		"clock_anomaly": anomaly,
		"process_id": None,
		"thread_id": None,
		"task": None,
		"other_sessions_active": False,
		"input_tokens": event.get("input_tokens"),
		"output_tokens": event.get("output_tokens"),
		"attributes": attributes,
		"energy": energy,
		"estimates": estimates,
	}


def read_records(path, resolution=None, spans_only=True):
	"""Records from a v1 or v2 JSON-lines file, in the v2 shape.

	v1 events are upgraded with upgrade_v1. Torn lines (a crash mid-write) are
	skipped. A record with schema_version above 2 raises ValueError: a newer
	format is not guessed at. With spans_only, session_start/session_end
	records are left out.
	"""
	out = []
	with open(path) as f:
		for number, line in enumerate(f, 1):
			line = line.strip()
			if not line:
				continue
			try:
				record = json.loads(line)
			except json.JSONDecodeError:
				continue
			version = record.get("schema_version", 1)
			if version == 1 or version is None:
				out.append(upgrade_v1(record, resolution))
			elif version == SCHEMA_VERSION:
				if spans_only and record.get("record_type") != SPAN:
					continue
				out.append(record)
			else:
				raise ValueError(f"{path}:{number}: unsupported schema_version {version!r}")
	return out


def session_header(path):
	"""The session_start record of a v2 file, or None (v1 file, or none written)."""
	with open(path) as f:
		for line in f:
			line = line.strip()
			if not line:
				continue
			try:
				record = json.loads(line)
			except json.JSONDecodeError:
				continue
			if record.get("record_type") == SESSION_START:
				return record
	return None
