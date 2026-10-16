"""Version-aware run report for schema-v1 and schema-v2 artifacts.

    from agent_energy_profiler import report
    r = report.build("out/")              # a directory or a file, v1 or v2
    print(report.render_text(r))

Rules, applied identically to both schemas:

  * Measured and estimated energy are never added together. Measured totals
    come from `energy.*` (counter differences); estimates are listed in their
    own section. A v1 GPU value that came from power integration is read as
    an estimate (schema_v2.upgrade_v1), exactly as in v2.
  * No double counting. Run totals add top-level spans only (v2 roots; every
    v1 event, since v1 has no hierarchy) and are refused when those overlap
    in time. Per-operation energy is v2 EXCLUSIVE energy (a parent's own
    share, children removed) and, for v1, each event's own energy.
  * A total is reported only when every contributing span was measured. The
    partial sum over measured spans is shown beside it, labelled with how
    many spans it covers, never under the total's name.
  * Missing stays missing: unavailable and invalid values are counted, with
    their reasons, not zero-filled.

The report reads the attributed files line by line and keeps only compact
per-span tuples, so it runs in bounded memory on large runs.
"""

import csv
import io
import json
import math
import os
import statistics
import tempfile
from collections import Counter, defaultdict

from . import __version__
from . import attribution_v2
from . import measurement as m
from . import schema_v2
from . import trajectory

REPORT_VERSION = 1
BOUNDARY = ("gpu", "cpu_package", "dram")
DOMAINS = ("gpu", "cpu_package", "dram", "cpu_core")


# --------------------------------------------------------------------------
# locating and classifying inputs
# --------------------------------------------------------------------------

def _first_record(path):
	with open(path) as f:
		for line in f:
			line = line.strip()
			if not line:
				continue
			try:
				return json.loads(line)
			except json.JSONDecodeError:
				continue
	return None


def classify(path):
	"""(schema, kind) of a JSON-lines file: kind is 'attributed' or 'raw'."""
	first = _first_record(path)
	if first is None:
		raise ValueError(f"{path}: no records")
	version = first.get("schema_version", 1)
	if version == schema_v2.SCHEMA_VERSION:
		if first.get("record_type") == schema_v2.SPAN and "accounting" in first:
			return 2, "attributed"
		return 2, "raw"
	if version in (1, None):
		return 1, ("attributed" if "measured_energy_j" in first else "raw")
	raise ValueError(f"{path}: unsupported schema_version {version!r}")


def locate(path):
	"""The record file to report on, for a file or a run directory."""
	if os.path.isfile(path):
		return path
	if not os.path.isdir(path):
		raise FileNotFoundError(path)
	for name in ("spans_attributed.jsonl", "events_attributed.jsonl",
	             "spans.jsonl", "events.jsonl"):
		candidate = os.path.join(path, name)
		if os.path.isfile(candidate):
			return candidate
	raise FileNotFoundError(f"{path}: no spans_attributed.jsonl, events_attributed.jsonl, "
	                        f"spans.jsonl or events.jsonl")


def _sidecar(path, name):
	candidate = os.path.join(os.path.dirname(os.path.abspath(path)), name)
	if not os.path.isfile(candidate):
		return None
	try:
		with open(candidate) as f:
			return json.load(f)
	except (OSError, ValueError):
		return None


# --------------------------------------------------------------------------
# accumulation
# --------------------------------------------------------------------------

def _domain_state(d):
	"""(bucket, reason) of one domain measurement for coverage counts."""
	if not d:
		return "absent", None
	status = d.get("status")
	if status != m.MEASURED:
		return status, d.get("reason")
	if d.get("below_resolution") is True:
		return "measured_below_resolution", None
	if d.get("below_resolution") is False:
		return "measured", None
	return "measured_resolution_unknown", None


def _window(span):
	s, e = span.get("start_monotonic_s"), span.get("end_monotonic_s")
	if s is None or e is None:
		s, e = span.get("start_unix_s"), span.get("end_unix_s")
	if s is None or e is None or e < s:
		return None
	return (s, e)


def _quantile(sorted_values, q):
	if not sorted_values:
		return None
	return sorted_values[min(len(sorted_values) - 1, int(q * (len(sorted_values) - 1) + 0.5))]


class _Accumulator:

	def __init__(self, schema):
		self.schema = schema
		self.n = 0
		self.status = Counter()
		self.coverage = {d: Counter() for d in DOMAINS}
		self.reasons = {d: Counter() for d in DOMAINS}
		self.zero_gpu = 0
		self.roots = []      # (window, {domain: value or None}, total, estimate)
		self.labels = defaultdict(lambda: {"durations": [], "errors": 0,
		                                   "energy": defaultdict(lambda: [0.0, 0, 0]),
		                                   "estimated_gpu": [0.0, 0]})
		self.tree = []       # (span_id, parent, name, duration, excl gpu, incl gpu, shared)
		self.shared = 0
		self.other_sessions = 0
		self.clock_anomalies = 0
		self.estimate_only_gpu = 0
		self.run_ids = Counter()
		self.hardware_ids = Counter()
		self.models = Counter()
		self.git_commits = Counter()
		self.start = self.end = None

	def add(self, span):
		self.n += 1
		self.status[span.get("status")] += 1
		energy = span.get("energy") or {}
		estimates = span.get("estimates") or {}
		acc = span.get("accounting")
		attrs = span.get("attributes") or {}
		for key, counter in (("run_id", self.run_ids), ("hardware_id", self.hardware_ids),
		                     ("model_name", self.models), ("git_commit", self.git_commits)):
			if attrs.get(key):
				counter[attrs[key]] += 1
		if span.get("clock_anomaly"):
			self.clock_anomalies += 1
		if span.get("other_sessions_active"):
			self.other_sessions += 1
		t0, t1 = span.get("start_unix_s"), span.get("end_unix_s")
		if t0 is not None:
			self.start = t0 if self.start is None else min(self.start, t0)
		if t1 is not None:
			self.end = t1 if self.end is None else max(self.end, t1)

		for domain in DOMAINS:
			bucket, reason = _domain_state(energy.get(domain))
			if bucket != "absent":
				self.coverage[domain][bucket] += 1
				if reason:
					self.reasons[domain][reason] += 1
		gpu = energy.get("gpu") or {}
		if gpu.get("status") == m.MEASURED and gpu.get("energy_j") == 0.0:
			self.zero_gpu += 1
		def measured(d):
			return d.get("energy_j") if (d and d.get("status") == m.MEASURED) else None

		# An estimate matters to the reader only where there is no counter
		# measurement. Where both exist it is a diagnostic comparison.
		est = estimates.get("gpu") or {}
		est_any = est.get("energy_j") if est.get("status") == m.ESTIMATED else None
		gpu_measured = measured(gpu)
		est_value = est_any if gpu_measured is None else None
		if est_value is not None:
			self.estimate_only_gpu += 1

		depth = acc["depth"] if acc else 0
		if depth == 0:
			total = span.get("total") or attribution_v2._total(
				{k: v for k, v in energy.items() if v})
			self.roots.append((_window(span),
			                   {d: measured(energy.get(d)) for d in BOUNDARY},
			                   total.get("energy_j"), est_value,
			                   (est_any, gpu_measured) if (est_any is not None and
			                                               gpu_measured is not None) else None))

		label = self.labels[span.get("name")]
		if span.get("duration_s") is not None:
			label["durations"].append(span["duration_s"])
		if span.get("status") not in (None, "ok"):
			label["errors"] += 1
		if est_value is not None:
			label["estimated_gpu"][0] += est_value
			label["estimated_gpu"][1] += 1
		if acc is not None:
			shared = acc.get("attribution") == "shared"
			self.shared += shared
			for domain, ex in acc.get("exclusive", {}).items():
				slot = label["energy"][domain]
				if ex.get("status") == m.MEASURED:
					slot[0] += ex["energy_j"]
					slot[1] += 1
				else:
					slot[2] += 1
			ex_gpu = acc.get("exclusive", {}).get("gpu", {})
			self.tree.append((span.get("span_id"), span.get("parent_span_id"),
			                  span.get("name"), span.get("duration_s"),
			                  ex_gpu.get("energy_j") if ex_gpu.get("status") == m.MEASURED else None,
			                  None if shared else measured(gpu), shared))
		else:
			# v1: no hierarchy, so an event's own energy is its operation energy
			for domain in DOMAINS:
				slot = label["energy"][domain]
				value = measured(energy.get(domain))
				if value is not None:
					slot[0] += value
					slot[1] += 1
				else:
					slot[2] += 1


# --------------------------------------------------------------------------
# building the report
# --------------------------------------------------------------------------

def _totals(acc):
	windows = sorted(w for w, *_ in acc.roots if w is not None)
	overlap = any(windows[i + 1][0] < windows[i][1] for i in range(len(windows) - 1))
	n_roots = len(acc.roots)
	basis = "root spans" if acc.schema == 2 else "events (schema v1 has no hierarchy)"
	out = {"basis": basis, "n_top_level": n_roots, "top_level_overlap": overlap,
	       "domains": {}}
	columns = [(d, [r[1][d] for r in acc.roots]) for d in BOUNDARY]
	columns.append(("total", [r[2] for r in acc.roots]))
	for domain, values in columns:
		present = [v for v in values if v is not None]
		row = {"n_measured": len(present), "n_spans": n_roots}
		if overlap:
			row.update(total_j=None, sum_measured_j=None,
			           reason="top-level spans overlap in time: device energy would be "
			                  "counted more than once")
		else:
			row["sum_measured_j"] = math.fsum(present) if present else None
			row["total_j"] = row["sum_measured_j"] if (present and len(present) == n_roots) else None
			row["reason"] = (None if row["total_j"] is not None else
			                 f"{n_roots - len(present)} of {n_roots} top-level spans "
			                 f"lack a measured value")
		out["domains"][domain] = row
	est = [r[3] for r in acc.roots if r[3] is not None]
	out["estimated_gpu"] = {
		"sum_j": (math.fsum(est) if (est and not overlap) else None),
		"n_spans": len(est),
		"scope": "top-level spans with NO GPU counter measurement",
		"method": "power_integration (integrated sampled board power)",
		"note": "an estimate; never added to the measured totals",
	}
	pairs = [r[4] for r in acc.roots if r[4] is not None]
	out["gpu_estimate_vs_counter"] = {
		"n_spans": len(pairs),
		"estimate_sum_j": math.fsum(p[0] for p in pairs) if (pairs and not overlap) else None,
		"counter_sum_j": math.fsum(p[1] for p in pairs) if (pairs and not overlap) else None,
		"note": "diagnostic only: how far integrated ~10 Hz power sits from the counter "
		        "on spans that have both",
	}
	return out


def _operations(acc):
	rows = []
	for name, lab in sorted(acc.labels.items(), key=lambda kv: str(kv[0])):
		durations = sorted(lab["durations"])
		energy = {}
		for domain in DOMAINS:
			s, n_in, n_out = lab["energy"].get(domain, (0.0, 0, 0))
			if n_in == 0 and n_out == 0:
				continue
			energy[domain] = {"energy_j": s if n_in else None, "n_included": n_in,
			                  "n_excluded": n_out}
		rows.append({
			"operation": name,
			"n": len(lab["durations"]) or None,
			"n_not_ok": lab["errors"],
			"duration_total_s": math.fsum(durations) if durations else None,
			"duration_mean_s": statistics.fmean(durations) if durations else None,
			"duration_p50_s": _quantile(durations, 0.5),
			"duration_p95_s": _quantile(durations, 0.95),
			"energy_basis": "exclusive" if acc.schema == 2 else "per_event",
			"energy": energy,
			# only spans of this operation that have no GPU measurement
			"estimated_gpu_j": lab["estimated_gpu"][0] if lab["estimated_gpu"][1] else None,
			"n_estimated_gpu": lab["estimated_gpu"][1],
		})
	return rows


def _hierarchy(acc, max_depth=4):
	"""Aggregate spans by their label path (root > ... > span)."""
	if not acc.tree:
		return []
	by_id = {}
	for row in acc.tree:
		if row[0] is not None:
			by_id.setdefault(row[0], row)
	paths = {}

	def path_of(row):
		chain, cur, seen = [], row, set()
		while cur is not None and id(cur) not in seen and len(chain) < 64:
			seen.add(id(cur))
			chain.append(str(cur[2]))
			cur = by_id.get(cur[1]) if cur[1] is not None else None
		return tuple(reversed(chain))

	agg = {}
	for row in acc.tree:
		key = paths.get(id(row))
		if key is None:
			key = paths[id(row)] = path_of(row)
		if len(key) > max_depth:
			continue
		a = agg.setdefault(key, {"n": 0, "duration_s": 0.0, "excl": [0.0, 0],
		                         "incl": [0.0, 0], "shared": 0})
		a["n"] += 1
		a["duration_s"] += row[3] or 0.0
		if row[4] is not None:
			a["excl"][0] += row[4]
			a["excl"][1] += 1
		if row[5] is not None:
			a["incl"][0] += row[5]
			a["incl"][1] += 1
		a["shared"] += row[6]
	out = []
	for key in sorted(agg):
		a = agg[key]
		out.append({
			"path": " > ".join(key), "depth": len(key) - 1, "n": a["n"],
			"inclusive_duration_s": a["duration_s"],
			"exclusive_gpu_energy_j": a["excl"][0] if a["excl"][1] else None,
			"n_exclusive_gpu": a["excl"][1],
			"inclusive_gpu_energy_j_sole": a["incl"][0] if a["incl"][1] else None,
			"n_inclusive_gpu_sole": a["incl"][1],
			"n_shared": a["shared"],
		})
	return out


def _warn(out, code, message):
	out.append({"code": code, "message": message})


def _warnings(acc, rep):
	w = []
	_warn(w, "DEVICE_LEVEL", "All energy is whole-device energy (every process on the "
	      "GPU/CPU package), attributed to spans by time window. It is most defensible "
	      "on an otherwise idle machine running non-concurrent work.")
	res = rep["resolution"]
	gpu_cov = acc.coverage["gpu"]
	n_gpu_measured = sum(v for k, v in gpu_cov.items() if k.startswith("measured"))
	if n_gpu_measured and res.get("gpu_update_interval_s") is None:
		_warn(w, "RESOLUTION_UNKNOWN", "The GPU counter's update interval was not "
		      "characterised for this run, so short spans cannot be flagged as below "
		      "resolution. Run `aep validate --json hw.json` and pass the report.")
	below = gpu_cov.get("measured_below_resolution", 0)
	if below:
		_warn(w, "BELOW_RESOLUTION", f"{below} of {n_gpu_measured} measured GPU values "
		      f"({100.0 * below / n_gpu_measured:.1f}%) come from spans shorter than the "
		      f"counter's update interval (~{res['gpu_update_interval_s'] * 1000:.1f} ms): "
		      f"quantised readings, biased low.")
	if acc.zero_gpu:
		_warn(w, "ZERO_READINGS", f"{acc.zero_gpu} spans read exactly 0 J of GPU energy. "
		      f"For short spans this is counter quantisation, not evidence that no "
		      f"energy was used.")
	if acc.shared:
		_warn(w, "SHARED_ATTRIBUTION", f"{acc.shared} spans overlapped other recorded "
		      f"work; their inclusive energy includes that work and they have no "
		      f"exclusive energy.")
	if rep["totals"]["top_level_overlap"]:
		_warn(w, "OVERLAPPING_TOP_LEVEL", "Top-level spans overlap in time, so no "
		      "span-based total is reported. Use the independent window measurement.")
	if acc.other_sessions:
		_warn(w, "OTHER_SESSIONS", f"{acc.other_sessions} spans ran while another "
		      f"Session in the same process was active; device energy cannot be split.")
	if acc.estimate_only_gpu:
		_warn(w, "ESTIMATES_PRESENT", f"{acc.estimate_only_gpu} spans have only an "
		      f"integrated-power GPU estimate. They are reported separately and excluded "
		      f"from measured totals.")
	invalid = {d: acc.coverage[d].get("invalid", 0) for d in DOMAINS}
	if any(invalid.values()):
		detail = ", ".join(f"{d}: {n}" for d, n in invalid.items() if n)
		_warn(w, "INVALID_MEASUREMENTS", f"Counter readings were invalid ({detail}); see "
		      f"coverage reasons.")
	for domain in ("cpu_package", "dram"):
		cov = acc.coverage[domain]
		if cov and not any(k.startswith("measured") for k in cov):
			_warn(w, "DOMAIN_UNAVAILABLE", f"{domain} was not measured for any span "
			      f"(reasons: {dict(acc.reasons[domain]) or 'none recorded'}); totals "
			      f"over the gpu+cpu_package+dram boundary are unavailable.")
	if acc.clock_anomalies:
		_warn(w, "CLOCK_ANOMALY", f"{acc.clock_anomalies} spans saw the wall clock step "
		      f"backwards; their window lookups are unreliable.")
	meas = rep.get("measurement") or {}
	verdict = (meas.get("counter_disturbance") or {}).get("verdict", "UNKNOWN")
	timeline = meas.get("timeline_declared") or meas.get("timeline") or {}
	dense_timeline = (timeline.get("counter_hz_effective") or 0) >= 2
	frequent_reads = (not meas.get("recorded") or meas.get("span_counter_reads")
	                  or dense_timeline)
	if not meas.get("recorded"):
		_warn(w, "MEASUREMENT_NOT_RECORDED", meas.get("note", "measurement mode not recorded"))
	if frequent_reads and verdict in ("UNKNOWN", "INCONCLUSIVE"):
		_warn(w, "COUNTER_DISTURBANCE_UNKNOWN", f"The GPU energy counter was read often "
		      f"(per span and/or by the sampler) and counter disturbance on this host is "
		      f"{verdict}. On at least one host, each read lowered the counter's reported "
		      f"energy (docs/NVML_COUNTER_VALIDATION.md); measured GPU values may be biased "
		      f"low. Test with `aep validate --counter-disturbance`.")
	if frequent_reads and verdict == "DISTURBED":
		_warn(w, "COUNTER_DISTURBED", "Counter disturbance was DETECTED on this host and the "
		      "counter was read often during this run: measured GPU values are biased low "
		      "by an amount that depends on the read rate. Prefer --mode sampled or hybrid.")
	if (meas.get("mode_source") == m.MODE_SOURCE_DEFAULT and meas.get("span_counter_reads")
	        and verdict != "NOT_DETECTED"):
		_warn(w, "DIRECT_MODE_BY_DEFAULT", f"The session used Session's default mode "
		      f"(direct: per-span GPU counter reads) without a disturbance test showing "
		      f"that is harmless here (verdict {verdict}). Pass mode='auto' with a hardware "
		      f"report, or choose a mode explicitly (docs/MEASUREMENT_MODES.md).")
	if meas.get("recorded") and not meas.get("span_counter_reads"):
		_warn(w, "SPAN_ESTIMATES_ONLY", f"Mode {meas.get('mode')}: spans were not measured "
		      f"with the GPU counter. Per-span GPU values are estimates (integrated averaged "
		      f"power); the measured GPU figure is the run counter, which covers the whole "
		      f"sampler window and cannot be split between spans.")
	if rep["source"].get("attributed_on_the_fly"):
		_warn(w, "NO_TIMELINE", "The input was not attributed; it was attributed without "
		      "a hardware timeline, so only in-band GPU energy is available.")
	return w


def _measurement_section(schema, summary):
	"""How the run was measured (Milestone 5 provenance), or what is unknown."""
	header = (summary or {}).get("session") or {}
	cfg = header.get("config") or {}
	meas = cfg.get("measurement")
	if schema == 1:
		out = {"recorded": False, "model_version": 1,
		       "note": "schema-v1 run: GPU counter read in-band at every event boundary; "
		               "the sampler's counter rate and counter disturbance were not recorded"}
	elif not meas:
		out = {"recorded": False, "model_version": 1,
		       "note": "session recorded before measurement modes: per-span GPU counter "
		               "reads were on; the sampler's counter rate and counter disturbance "
		               "were not recorded"}
	else:
		out = dict(meas, recorded=True)
	declared = ((summary or {}).get("timeline") or {}).get("declared")
	if declared:
		out["timeline_declared"] = declared
	out["driver_version"] = (cfg.get("gpu") or {}).get("driver_version")
	return out


def _metadata_v2(summary):
	header = (summary or {}).get("session") or {}
	cfg = header.get("config") or {}
	return {
		"session_id": header.get("session_id"),
		"profiler_version_recorded": header.get("profiler_version"),
		"started_unix_s": header.get("started_unix_s"),
		"attributes": header.get("attributes"),
		"host": header.get("host"),
		"gpu": cfg.get("gpu"),
		"sampler": cfg.get("sampler"),
		"energy_scope": cfg.get("energy_scope"),
	}


def build(path, resolution=None):
	"""The report dict for a run directory or a v1/v2 JSON-lines file."""
	record_file = locate(path)
	schema, kind = classify(record_file)
	source = {"path": os.path.abspath(record_file), "schema": schema, "kind": kind}
	cleanup = None
	if kind == "raw":
		tmp = tempfile.TemporaryDirectory()
		cleanup = tmp
		attributed = os.path.join(tmp.name, "spans_attributed.jsonl")
		header = schema_v2.session_header(record_file)
		res = resolution or (m.Resolution.from_dict(header["config"].get("resolution"))
		                     if header else m.Resolution.unknown())
		summary = attribution_v2.attribute_file(record_file, attributed, None, res)
		summary["session"] = header
		source["attributed_on_the_fly"] = True
		read_path, read_schema = attributed, 2
	else:
		read_path, read_schema = record_file, schema
		summary = _sidecar(record_file, "session_summary.json") if schema == 2 else None
		header = (summary or {}).get("session")

	try:
		if read_schema == 2 and schema == 2:
			res = resolution or (m.Resolution.from_dict(header["config"].get("resolution"))
			                     if header else m.Resolution.unknown())
		elif schema == 1:
			res = resolution or m.Resolution.unknown()
		acc = _Accumulator(schema)
		if read_schema == 2:
			with open(read_path) as f:
				for line in f:
					line = line.strip()
					if line:
						record = json.loads(line)
						if record.get("record_type") == schema_v2.SPAN:
							acc.add(record)
		else:
			for span in schema_v2.iter_records(read_path, res):
				acc.add(span)
	finally:
		if cleanup is not None:
			cleanup.cleanup()

	rep = {
		"report_version": REPORT_VERSION,
		"generated_by": f"agent_energy_profiler {__version__}",
		"source": source,
		"run": {
			"run_ids": dict(acc.run_ids), "hardware_ids": dict(acc.hardware_ids),
			"models": dict(acc.models), "git_commits": dict(acc.git_commits),
			"start_unix_s": acc.start, "end_unix_s": acc.end,
			"wall_s": (acc.end - acc.start) if (acc.start is not None and acc.end is not None) else None,
		},
		"resolution": res.to_dict(),
		"counts": {"spans": acc.n, "status": dict(sorted(acc.status.items(), key=str))},
	}
	if schema == 2:
		rep["run"].update(_metadata_v2(summary))
	rep["measurement"] = _measurement_section(schema, summary)
	rep["totals"] = _totals(acc)
	rep["window"] = _window_section(schema, record_file, summary)
	rep["run_counter"] = (summary or {}).get("run_counter")
	rep["counter_intervals"] = (summary or {}).get("counter_intervals")
	rep["operations"] = _operations(acc)
	rep["hierarchy"] = _hierarchy(acc) if schema == 2 else []
	rep["coverage"] = {d: {"states": dict(sorted(acc.coverage[d].items())),
	                       "reasons": dict(sorted(acc.reasons[d].items()))}
	                   for d in DOMAINS if acc.coverage[d]}
	rep["warnings"] = _warnings(acc, rep)
	return rep


def analyze(spans, power=None, out_dir=None, resolution_report=None, allow_uncovered=False):
	"""Attribute a recorded session and return its report.

	    with Session("run/spans.jsonl", sampler=Sampler("run/power.csv", ...)) as s:
	        ...
	    rep = analyze(s)                  # or analyze("run/spans.jsonl", power=...)
	    print(report.render_text(rep))

	`spans` is a closed Session or a raw spans file. A Session's own sampler
	file is used as `power` unless one is given. Writes spans_attributed.jsonl
	and session_summary.json to `out_dir` (default: beside the spans file),
	exactly as `aep attribute` does, then builds the report from them.
	"""
	from .session import Session
	if isinstance(spans, Session):
		if not spans.closed:
			raise RuntimeError("analyze() needs a closed Session (leave its `with` block first)")
		if power is None and spans.sampler is not None:
			power = spans.sampler.power_file
		spans = spans.path
	_, kind = classify(spans)
	if kind == "attributed":
		raise ValueError(f"{spans} is already attributed; use report.build()")
	out_dir = out_dir or os.path.dirname(os.path.abspath(spans))
	attribution_v2.attribute_run(spans, out_dir, power=power or "",
	                             resolution_report=resolution_report or "",
	                             allow_uncovered=allow_uncovered)
	return build(os.path.join(out_dir, "spans_attributed.jsonl"))


def _window_section(schema, record_file, summary):
	"""Independent whole-window measurement, if the attribution produced one."""
	if schema == 2:
		window = (summary or {}).get("window")
		if not window:
			return {"available": False,
			        "reason": "no hardware timeline was supplied to attribution"}
		return {"available": True, "basis": "sampler timeline over the session window",
		        "start_unix_s": window.get("start_unix_s"),
		        "end_unix_s": window.get("end_unix_s"),
		        "domains": {d: {"energy_j": v.get("energy_j"), "status": v.get("status"),
		                        "reason": v.get("reason")}
		                    for d, v in (window.get("domains") or {}).items()},
		        "window_minus_roots": (summary or {}).get("window_minus_roots")}
	traj = _sidecar(record_file, "trajectory_summary.json")
	if not traj:
		return {"available": False, "reason": "no trajectory_summary.json beside the events"}
	rows = traj.get("trajectories") or []
	windows = sorted((r["trajectory_start"], r["trajectory_end"]) for r in rows
	                 if r.get("trajectory_start") is not None and r.get("trajectory_end") is not None)
	overlap = any(windows[i + 1][0] < windows[i][1] for i in range(len(windows) - 1))
	out = {"available": True, "basis": "per-trajectory cumulative counter windows "
	       "(schema-v1 trajectory_summary.json)", "n_trajectories": len(rows),
	       "trajectories_overlap": overlap, "domains": {}}
	for d, name in (("gpu_energy_j", "gpu"), ("cpu_package_energy_j", "cpu_package"),
	                ("dram_energy_j", "dram"), ("measured_energy_j", "total")):
		values = [r.get(f"trajectory_{d}") for r in rows]
		ok = values and all(v is not None for v in values) and not overlap
		out["domains"][name] = {
			"energy_j": math.fsum(values) if ok else None,
			"n_trajectories_measured": sum(1 for v in values if v is not None)}
	summ = traj.get("summary") or {}
	out["gpu_coverage_median"] = summ.get("gpu_coverage_median")
	out["clock_anomaly"] = summ.get("clock_anomaly")
	return out


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

def _j(value, width=12):
	if value is None:
		return "n/a".rjust(width)
	return f"{value:,.3f}".rjust(width)


def _s(value):
	if value is None:
		return "n/a"
	if value < 1e-3:
		return f"{value * 1e6:.0f} us"
	return f"{value * 1000:.1f} ms" if value < 1 else f"{value:.2f} s"


def render_text(rep, top=20):
	L = []
	src = rep["source"]
	L.append("=" * 78)
	L.append(f"ENERGY REPORT  schema v{src['schema']} ({src['kind']})  {src['path']}")
	L.append("=" * 78)
	run = rep["run"]
	if run.get("session_id"):
		L.append(f"session      {run['session_id']}  recorded by "
		         f"{run.get('profiler_version_recorded')}")
	if run.get("run_ids"):
		L.append(f"run id(s)    {', '.join(map(str, run['run_ids']))}")
	gpu = run.get("gpu") or {}
	if gpu.get("devices"):
		names = ", ".join(str(d.get("name")) for d in gpu["devices"])
		L.append(f"gpu          {names}  (reader {gpu.get('reader')})")
	elif gpu:
		L.append(f"gpu          not read ({gpu.get('unavailable_reason') or gpu.get('reader')})")
	if run.get("hardware_ids"):
		L.append(f"hardware     {', '.join(map(str, run['hardware_ids']))}")
	if run.get("models"):
		L.append(f"model(s)     {', '.join(map(str, run['models']))}")
	L.append(f"spans        {rep['counts']['spans']}   status {rep['counts']['status']}"
	         f"   wall {_s(run.get('wall_s'))}")
	res = rep["resolution"]
	if res.get("gpu_update_interval_s"):
		L.append(f"resolution   GPU counter updates every ~{res['gpu_update_interval_s'] * 1000:.1f} ms "
		         f"({res['basis']}: {res.get('source')})")
	else:
		L.append("resolution   GPU counter update interval UNKNOWN (not characterised)")
	meas = rep.get("measurement") or {}
	if meas.get("recorded"):
		tl = meas.get("timeline_declared") or meas.get("timeline") or {}
		dist = meas.get("counter_disturbance") or {}
		source = f", {meas['mode_source']}" if meas.get("mode_source") else ""
		L.append(f"measurement  mode {meas.get('mode')} (requested {meas.get('requested_mode')}"
		         f"{source}); per-span GPU counter reads "
		         f"{'ON' if meas.get('span_counter_reads') else 'off'}")
		if tl:
			L.append(f"             sampler: power every row at {tl.get('power_hz')} Hz "
			         f"({tl.get('power_api')}); GPU counter {tl.get('counter_policy')} "
			         f"(~{tl.get('counter_hz_effective')} Hz)")
		L.append(f"             counter disturbance {dist.get('verdict')} "
		         f"(source: {dist.get('source') or 'none'})"
		         + (f", driver {meas['driver_version']}" if meas.get("driver_version") else ""))
	else:
		L.append(f"measurement  not recorded: {meas.get('note')}")
	L.append("")

	t = rep["totals"]
	L.append(f"MEASURED ENERGY (hardware counters; basis: {t['basis']})")
	for d in ("gpu", "cpu_package", "dram", "total"):
		row = t["domains"][d]
		label = "gpu+cpu_package+dram" if d == "total" else d
		if row["total_j"] is not None:
			L.append(f"  {label:<22}{_j(row['total_j'])} J")
		elif row.get("sum_measured_j") is not None:
			L.append(f"  {label:<22}{'n/a':>12}    (sum over the {row['n_measured']} of "
			         f"{row['n_spans']} measured: {row['sum_measured_j']:,.3f} J)")
		else:
			L.append(f"  {label:<22}{'n/a':>12}    {row.get('reason') or ''}")
	est = t["estimated_gpu"]
	L.append("ESTIMATED ENERGY (spans with no GPU measurement; never added above)")
	if est["n_spans"]:
		L.append(f"  gpu (power integration){_j(est['sum_j'], 10)} J over {est['n_spans']} spans")
	else:
		L.append("  none: every top-level span with GPU energy was measured")
	cmp_ = t.get("gpu_estimate_vs_counter") or {}
	if cmp_.get("n_spans") and cmp_.get("counter_sum_j"):
		L.append(f"  (diagnostic: on {cmp_['n_spans']} measured spans, integrated power gives "
		         f"{cmp_['estimate_sum_j']:,.3f} J vs counter {cmp_['counter_sum_j']:,.3f} J)")
	win = rep["window"]
	L.append("INDEPENDENT WINDOW MEASUREMENT")
	if win.get("available"):
		parts = []
		for d, v in win["domains"].items():
			parts.append(f"{d} {_j(v.get('energy_j'), 0).strip()} J")
		L.append(f"  {win['basis']}: " + ", ".join(parts))
	else:
		L.append(f"  n/a ({win.get('reason')})")
	rc = rep.get("run_counter")
	if rc:
		L.append("RUN COUNTER MEASUREMENT (sampler's GPU counter, its own first to last reading)")
		if rc.get("status") == "measured":
			L.append(f"  gpu {rc['energy_j']:,.3f} J over {rc['duration_s']:.2f} s, "
			         f"{rc['n_readings']} readings; brackets all spans: "
			         f"{rc.get('brackets_all_spans')}")
		else:
			L.append(f"  n/a ({rc.get('status')}: {rc.get('reason')})")
		ci = rep.get("counter_intervals") or {}
		if ci.get("counter_over_estimate_median") is not None:
			L.append(f"  (diagnostic: on {ci['n_intervals']} counter intervals, counter / "
			         f"integrated power median {ci['counter_over_estimate_median']:.3f}, "
			         f"p10-p90 {ci['counter_over_estimate_p10']:.3f}-"
			         f"{ci['counter_over_estimate_p90']:.3f})")
	L.append("")

	ops = sorted(rep["operations"], key=lambda r: -(r["duration_total_s"] or 0))[:top]
	basis = "exclusive" if src["schema"] == 2 else "per event"
	L.append(f"BY OPERATION (energy: {basis}; top {len(ops)} by total time)")
	L.append(f"  {'operation':<26}{'n':>7}{'total':>10}{'p50':>10}{'p95':>10}"
	         f"{'gpu J':>12}{'cpu_pkg J':>12}{'est. gpu J':>12}")
	for r in ops:
		e = r["energy"]
		L.append(f"  {str(r['operation'])[:26]:<26}{r['n'] or 0:>7}"
		         f"{_s(r['duration_total_s']):>10}{_s(r['duration_p50_s']):>10}"
		         f"{_s(r['duration_p95_s']):>10}"
		         f"{_j((e.get('gpu') or {}).get('energy_j'))}"
		         f"{_j((e.get('cpu_package') or {}).get('energy_j'))}"
		         f"{_j(r['estimated_gpu_j'])}")
	if rep["hierarchy"]:
		L.append("")
		L.append("HIERARCHY (by label path; exclusive GPU energy)")
		for h in rep["hierarchy"][:top * 2]:
			indent = "  " * h["depth"]
			name = h["path"].split(" > ")[-1]
			L.append(f"  {indent}{name:<{max(1, 30 - len(indent))}}{h['n']:>7}"
			         f"{_s(h['inclusive_duration_s']):>10}"
			         f"{_j(h['exclusive_gpu_energy_j'])} J"
			         f"{'   shared x' + str(h['n_shared']) if h['n_shared'] else ''}")
	L.append("")
	L.append("COVERAGE")
	for d, cov in rep["coverage"].items():
		L.append(f"  {d:<12} {cov['states']}" + (f"  reasons {cov['reasons']}" if cov["reasons"] else ""))
	L.append("")
	L.append("WARNINGS")
	for warning in rep["warnings"]:
		L.append(f"  [{warning['code']}] {warning['message']}")
	return "\n".join(L)


def operations_csv(rep):
	"""The by-operation table as CSV text (one row per operation)."""
	out = io.StringIO()
	w = csv.writer(out)
	header = ["operation", "n", "n_not_ok", "duration_total_s", "duration_mean_s",
	          "duration_p50_s", "duration_p95_s", "energy_basis"]
	for d in DOMAINS:
		header += [f"{d}_energy_j", f"{d}_n_included", f"{d}_n_excluded"]
	header += ["estimated_gpu_j", "n_estimated_gpu"]
	w.writerow(header)
	for r in rep["operations"]:
		row = [r["operation"], r["n"], r["n_not_ok"], r["duration_total_s"],
		       r["duration_mean_s"], r["duration_p50_s"], r["duration_p95_s"],
		       r["energy_basis"]]
		for d in DOMAINS:
			e = r["energy"].get(d) or {}
			row += [e.get("energy_j"), e.get("n_included"), e.get("n_excluded")]
		row += [r["estimated_gpu_j"], r["n_estimated_gpu"]]
		w.writerow(["" if v is None else v for v in row])
	return out.getvalue()
