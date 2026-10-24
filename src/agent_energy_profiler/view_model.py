"""View model: everything the HTML report shows, computed and checked here.

    from agent_energy_profiler import view_model
    view = view_model.build("agent-energy-profiler-runs/my-run")
    view_model.validate(view)          # build() already did; raises ViewModelError

The HTML report (html_report) only displays, sorts and filters this dict. No
energy is added, compared or judged in the browser: every aggregate, every
"rankable" decision and every share is made here, by the same code as the
reports:

  * label rows are brief._rows() over report._build(), as in the brief report;
  * hierarchy-path rows are the same rows over report._build(key=path), so a
    path is accounted exactly as a label is (exclusive measured energy; leaf
    spans that nothing overlapped for estimates), on the label-level basis;
  * per-span values are copied from the attributed spans as cells.

Contract (VIEW_VERSION 1):

  * Energy appears only inside a cell: {domain, scope, kind, value_j, reason,
    qualifiers, method}. kind is measured | estimated | unavailable |
    invalid | not_defensible; value_j is a number only for measured and
    estimated, never 0 for something missing. scope is inclusive (the span's
    own window, children included), exclusive (children removed; accounting),
    window (an estimate over the span's window) or whole_run.
  * Aggregate rows carry the brief's fields (energy_j, ranked, share, notes)
    on the one basis in view["basis"]; a ranked row is never shared or a
    parent of an estimate.
  * Every count, aggregate, coverage figure and warning covers the whole run.
    Only the list of individual span instances may be limited
    (view["instances_info"]).

Privacy (the file is meant to be shareable):

  * span and session attributes are omitted unless allowed by name
    (`attributes=`) or all at once (`all_attributes=True`);
  * no host name, no absolute or relative file path (hardware-report sources
    are reduced to their file name), no span, trace or session id: spans
    are numbered in file order;
  * text copied from the recording is scrubbed of the run's directory path
    and the home directory, and the mode's free-text reason of host names
    (the recorded test host and this machine; that is where a recording
    names hosts); build() fails if any of them still appears there.
    Operation labels and allowed attributes are the caller's own text and
    are copied as they are, unless `redact_labels=True` replaces each label
    with "operation N" (numbered in order of first appearance). Labels that
    look like program input (long, multi-line or several words, or one
    label per span) are counted in view["privacy"] so the caller can be
    warned; they are not changed.
"""

import heapq
import json
import os
import platform
import re
import socket

from . import __version__
from . import brief
from . import measurement as m
from . import report

VIEW_VERSION = 1
DEFAULT_MAX_INSTANCES = 10000
KINDS = (m.MEASURED, m.ESTIMATED, m.UNAVAILABLE, m.INVALID, m.NOT_DEFENSIBLE)
SCOPES = ("inclusive", "exclusive", "window", "whole_run")
REDACTED = "[redacted]"
FREE_TEXT_LABEL_CHARS = 64
FREE_TEXT_LABEL_WORDS = 4
MANY_LABELS = 50

# plain-language text for the reason codes the attribution records; the page
# shows it next to the code
REASON_TEXT = {
	"no_counter": "no energy counter available on this machine",
	"not_read_by_mode": "not read in this measurement mode (see Provenance)",
	"not_covered": "outside the power sampler's recording",
	"insufficient_samples": "too few power samples in the span's window",
	"counter_not_sampled": "the sampler did not read the energy counter",
	"counter_discontinuity": "the counter reset or went backwards",
	"clock_anomaly": "the wall clock stepped during the span",
	"disabled": "GPU reading was disabled",
	"negative_exclusive": "its children read more than the span itself (counter noise)",
	"parent_unmeasured": "the span's own value was not measured, so nothing to split",
	"child_unmeasured": "a child span has no measured value",
	"concurrent_spans": "it overlapped other recorded work",
	"children_overlap": "its children overlapped each other",
	"child_outside_parent": "a child span ran outside this span's window",
	"other_sessions_active": "another session in this process was active",
	"root_unmeasured": "a top-level span has no measured value",
	"concurrent_root_spans": "top-level spans overlapped, so their sum would count time twice",
}


class ViewModelError(ValueError):
	"""The view would break the contract (or leak something it must not)."""


# --------------------------------------------------------------------------
# cells
# --------------------------------------------------------------------------

def _cell(domain, scope, d):
	"""One energy value with its kind; missing stays missing."""
	status = d.get("status")
	kind = status if status in KINDS else m.UNAVAILABLE
	value = d.get("energy_j") if kind in (m.MEASURED, m.ESTIMATED) else None
	reason = d.get("reason") or (None if status in KINDS else (status or "not recorded"))
	if kind in (m.MEASURED, m.ESTIMATED) and value is None:
		kind, reason = m.UNAVAILABLE, "no value"
	qualifiers = []
	if kind == m.MEASURED and domain == "gpu" and scope == "inclusive":
		if d.get("below_resolution") is True:
			qualifiers.append("below_resolution")
		elif d.get("below_resolution") is None:
			qualifiers.append("resolution_unknown")
	return _compact({"domain": domain, "scope": scope, "kind": kind, "value_j": value,
	                 "reason": reason, "qualifiers": qualifiers, "method": d.get("method")})


def _compact(record):
	"""Drop null, false and empty fields: an absent key means exactly that."""
	return {k: v for k, v in record.items() if v is not None and v is not False and v != []
	        and v != {}}


def _span_cells(span):
	energy = span.get("energy") or {}
	acc = span.get("accounting")
	cells = []
	for domain in report.BOUNDARY:
		if energy.get(domain):
			cells.append(_cell(domain, "inclusive", energy[domain]))
		ex = ((acc or {}).get("exclusive") or {}).get(domain)
		if ex:
			cells.append(_cell(domain, "exclusive", ex))
	est = (span.get("estimates") or {}).get("gpu")
	if est:
		cell = _cell("gpu", "window", est)
		if (energy.get("gpu") or {}).get("status") == m.MEASURED:
			# a counter value exists: the estimate is only a cross-check
			cell["qualifiers"] = cell.get("qualifiers", []) + ["diagnostic_only"]
		cells.append(cell)
	return cells


# --------------------------------------------------------------------------
# privacy
# --------------------------------------------------------------------------

def _filter_attributes(attrs, allow, everything):
	attrs = attrs or {}
	if everything:
		return dict(attrs), 0
	kept = {k: v for k, v in attrs.items() if k in allow}
	return kept, len(attrs) - len(kept)


def _basename(path):
	return os.path.basename(str(path).rstrip("/\\")) if path else path


class _Scrubber:
	"""Removes host names and run paths from free text copied into the view."""

	def __init__(self, rep, path):
		hosts, paths = set(), set()
		meas = rep.get("measurement") or {}
		dist = meas.get("counter_disturbance") or {}
		for value in (dist.get("hostname"), _safe_hostname(), platform.node()):
			if value:
				hosts.add(str(value))
		host = rep["run"].get("host") or {}
		if isinstance(host, dict):
			for key in ("hostname", "host", "node"):
				if host.get(key):
					hosts.add(str(host[key]))
		for p in (path, rep["source"].get("path")):
			if p:
				absolute = os.path.abspath(p)
				paths.update({absolute, os.path.dirname(absolute)})
				if os.sep in str(p).rstrip(os.sep):
					paths.add(str(p).rstrip(os.sep))
		home = os.path.expanduser("~")
		if home and home not in ("/", "~"):
			paths.add(home)
		# paths anywhere (longest first, before their own prefixes). Host
		# names reach the recording only in the mode's free-text reason
		# (measurement.configuration_mismatch), so only that text is
		# scrubbed of them, as whole words: a host called "gpu" must not
		# erase "gpu" from the rest of the report.
		self.paths = [re.compile(re.escape(p)) for p in
		              sorted((p for p in paths if len(p) > 1), key=len, reverse=True)]
		self.hosts = [re.compile(r"(?<![\w.-])" + re.escape(h) + r"(?![\w-])")
		              for h in sorted(hosts, key=len, reverse=True)]

	def __call__(self, value, patterns=None):
		patterns = self.paths if patterns is None else patterns
		if isinstance(value, str):
			for pattern in patterns:
				value = pattern.sub(REDACTED, value)
			return value
		if isinstance(value, dict):
			return {k: self(v, patterns) for k, v in value.items()}
		if isinstance(value, list):
			return [self(v, patterns) for v in value]
		return value

	def free_text(self, value):
		"""Recorded prose that may name hosts as well as paths."""
		return self(value, self.paths + self.hosts)


def _safe_hostname():
	try:
		return socket.gethostname()
	except OSError:
		return None


def _free_text_labels(labels):
	"""Labels that read like program input rather than a chosen name."""
	return [label for label in labels if isinstance(label, str) and (
		len(label) > FREE_TEXT_LABEL_CHARS or any(c in label for c in "\n\r\t")
		or len(label.split()) >= FREE_TEXT_LABEL_WORDS)]


# --------------------------------------------------------------------------
# paths and instances
# --------------------------------------------------------------------------

class _Structure:
	"""span_id -> (parent_span_id, name, number); spans numbered in file order."""

	def __init__(self):
		self.by_id = {}
		self.n = 0
		self.labels = {}         # label -> number of spans, in order of first use

	def visit(self, span):
		label = span.get("name")
		self.labels[label] = self.labels.get(label, 0) + 1
		sid = span.get("span_id")
		if sid is not None and sid not in self.by_id:
			self.by_id[sid] = (span.get("parent_span_id"), span.get("name"), self.n)
		self.n += 1

	def path(self, span):
		"""Label path root > ... > span, as report._hierarchy builds it."""
		chain, seen = [span.get("name")], {span.get("span_id")}
		cur = span.get("parent_span_id")
		while cur is not None and cur in self.by_id and cur not in seen and len(chain) < 64:
			seen.add(cur)
			parent, name, _ = self.by_id[cur]
			chain.append(name)
			cur = parent
		return tuple(reversed(chain))


class _Instances:
	"""Every span if they fit in `limit`; otherwise, per path, the ones most
	worth inspecting (not ok, shared, slowest), up to an equal share each."""

	def __init__(self, structure, n_total, n_paths, limit, start, allow, everything):
		self.structure = structure
		self.keep_all = n_total <= limit
		self.quota = max(1, limit // max(1, n_paths))
		self.limit = limit
		self.start = start
		self.allow, self.everything = allow, everything
		self.heaps = {}
		self.all = []
		self.number = 0

	def visit(self, span, path):
		number = self.number
		self.number += 1
		acc = span.get("accounting") or {}
		priority = (span.get("status") not in (None, "ok"), acc.get("attribution") == "shared",
		            span.get("duration_s") or 0.0, -number)
		if self.keep_all:
			self.all.append(self._record(span, path, number))
			return
		heap = self.heaps.setdefault(path, [])
		if len(heap) < self.quota:
			heapq.heappush(heap, (priority, number, self._record(span, path, number)))
		elif priority > heap[0][0]:
			heapq.heapreplace(heap, (priority, number, self._record(span, path, number)))

	def records(self):
		if self.keep_all:
			return self.all
		chosen = [entry for heap in self.heaps.values() for entry in heap]
		if len(chosen) > self.limit:            # more paths than the limit
			chosen = sorted(chosen, reverse=True)[:self.limit]
		return sorted((entry[2] for entry in chosen), key=lambda r: r["n"])

	def _record(self, span, path, number):
		acc = span.get("accounting") or {}
		parent = self.structure.by_id.get(span.get("parent_span_id"))
		attrs, omitted = _filter_attributes(span.get("attributes"), self.allow, self.everything)
		t0, t1 = span.get("start_unix_s"), span.get("end_unix_s")
		rec = {
			"n": number,
			"path": path,                       # replaced by the path's id
			"name": span.get("name"),
			"parent": parent[2] if parent else None,
			"start_s": (t0 - self.start) if (t0 is not None and self.start is not None) else None,
			"end_s": (t1 - self.start) if (t1 is not None and self.start is not None) else None,
			"duration_s": span.get("duration_s"),
			"status": span.get("status"),
			"error_type": span.get("error_type"),
			"depth": acc.get("depth"),
			"n_children": acc.get("n_children"),
			"attribution": acc.get("attribution"),
			"concurrent_spans": acc.get("concurrent_spans"),
			"children_overlap": acc.get("children_overlap"),
			"clock_anomaly": bool(span.get("clock_anomaly")),
			"other_sessions_active": bool(span.get("other_sessions_active")),
			"input_tokens": span.get("input_tokens"),
			"output_tokens": span.get("output_tokens"),
			"attributes": attrs,
			"n_attributes_omitted": omitted,
			"cells": _span_cells(span),
		}
		return dict(_compact(rec), n=number, path=path, name=span.get("name"))


# --------------------------------------------------------------------------
# provenance
# --------------------------------------------------------------------------

def _provenance(rep, scrub):
	meas = rep.get("measurement") or {}
	run = rep["run"]
	timeline = meas.get("timeline_declared") or meas.get("timeline") or {}
	dist = meas.get("counter_disturbance") or {}
	res = rep.get("resolution") or {}
	gpu = run.get("gpu") or {}
	rc = rep.get("run_counter") or {}
	ci = rep.get("counter_intervals") or {}
	return scrub({
		"schema": rep["source"]["schema"],
		"kind": rep["source"]["kind"],
		"attributed_on_the_fly": bool(rep["source"].get("attributed_on_the_fly")),
		"profiler_version_recorded": run.get("profiler_version_recorded"),
		"generated_by": rep.get("generated_by"),
		"measurement_recorded": bool(meas.get("recorded")),
		"measurement_note": scrub.free_text(meas.get("note")),
		"mode": meas.get("mode"),
		"requested_mode": meas.get("requested_mode"),
		"mode_source": meas.get("mode_source"),
		"mode_reason": scrub.free_text(meas.get("mode_reason")),
		"span_counter_reads": meas.get("span_counter_reads"),
		"semantics": {k: v for k, v in (meas.get("semantics") or {}).items()
		              if isinstance(v, str)},
		"sampler": {k: timeline.get(k) for k in ("power_hz", "power_api", "counter_policy",
		                                         "counter_hz_effective", "counter_api")
		            if k in timeline},
		"counter_disturbance": {"verdict": dist.get("verdict"),
		                        "source": _basename(dist.get("source")),
		                        "read_hz": dist.get("read_hz"),
		                        "retained_median": dist.get("retained_median"),
		                        "driver_version": dist.get("driver_version"),
		                        "gpu_models": dist.get("gpu_models")} if dist else None,
		"resolution": {"gpu_update_interval_s": res.get("gpu_update_interval_s"),
		               "basis": res.get("basis"), "source": _basename(res.get("source"))},
		"gpu": {"reader": gpu.get("reader"), "enabled": gpu.get("enabled"),
		        "unavailable_reason": gpu.get("unavailable_reason"),
		        "driver_version": meas.get("driver_version") or gpu.get("driver_version"),
		        "devices": [{"index": d.get("index"), "name": d.get("name")}
		                    for d in (gpu.get("devices") or [])]} if gpu else None,
		"energy_scope": run.get("energy_scope"),
		"run_counter": {k: rc.get(k) for k in ("status", "energy_j", "duration_s", "n_readings",
		                                       "brackets_all_spans", "reason")} if rc else None,
		"counter_intervals": {k: ci.get(k) for k in (
			"n_intervals", "counter_over_estimate_median", "counter_over_estimate_p10",
			"counter_over_estimate_p90")} if ci else None,
		"window": rep.get("window"),
		"totals": rep.get("totals"),
		"coverage": rep.get("coverage"),
		"counts": rep.get("counts"),
	})


def _whole_run(summary):
	cells = []
	for domain, value, how in summary["whole_run"]:
		if value is None:
			cells.append({"domain": domain, "scope": "whole_run", "kind": m.UNAVAILABLE,
			              "reason": how})
		else:
			cells.append({"domain": domain, "scope": "whole_run", "kind": m.MEASURED,
			              "value_j": value, "method": how})
	return cells


def _basis(summary):
	basis = summary["basis"]
	if not basis:
		return None
	domain, kind = basis
	schema = summary["schema"]
	if schema == 1:
		scope = "per_event"
		text = (f"{kind} {brief.DOMAIN_NAMES[domain]} energy per event (schema v1 has no "
		        f"span hierarchy)")
	elif kind == m.ESTIMATED:
		scope = "window"
		text = ("estimated GPU energy: sampled power integrated over each span's window; "
		        "ranked for leaf spans that nothing overlapped, where it cannot count "
		        "anything twice")
	else:
		scope = "exclusive"
		text = (f"measured {brief.DOMAIN_NAMES[domain]} energy, exclusive: a parent keeps "
		        f"only what its children do not cover")
	return {"domain": domain, "kind": kind, "scope": scope, "text": text}


# --------------------------------------------------------------------------
# build
# --------------------------------------------------------------------------

def build(path, *, attributes=(), all_attributes=False, max_instances=DEFAULT_MAX_INSTANCES,
          resolution=None, name=None, redact_labels=False):
	"""The view dict of a run directory or a v1/v2 file (see module docstring).

	attributes      attribute names to include (span and session attributes)
	all_attributes  include every attribute; the file then holds your data
	max_instances   individual spans listed at most (aggregates are complete)
	name            the run's display name (default: its directory's name)
	redact_labels   replace every operation label with "operation N"
	"""
	allow = frozenset(attributes or ())
	structure = _Structure()
	# pass 1: the label-level report and brief rows, plus the span tree
	rep, acc = report._build(path, resolution, visit=structure.visit)
	summary = brief._summarize(rep, acc, path)
	scrub = _Scrubber(rep, path)
	start = rep["run"].get("start_unix_s")

	paths = set()
	for sid, (parent, label, _) in structure.by_id.items():
		paths.add(structure.path({"span_id": sid, "parent_span_id": parent, "name": label}))
	n_without_id = structure.n - len(structure.by_id)
	instances = _Instances(structure, structure.n, len(paths) + n_without_id,
	                       max_instances, start, allow, all_attributes)

	def path_key(span):
		return structure.path(span)

	def visit(span):
		instances.visit(span, structure.path(span))

	# pass 2: the same accounting grouped by hierarchy path
	rep_p, acc_p = report._build(path, resolution, key=path_key, visit=visit)
	v1_overlap = rep["source"]["schema"] == 1 and rep["totals"]["top_level_overlap"]
	path_rows = brief._rows(rep_p["operations"], acc_p, summary["basis"], v1_overlap,
	                        summary["sample_interval_s"])
	order = {label: i for i, label in enumerate(structure.labels)}
	if redact_labels:
		def label_of(label):
			return f"operation {order[label] + 1}"

		def path_order(key):
			return [order[x] for x in key]
	else:
		def label_of(label):
			return label

		def path_order(key):
			return [str(x) for x in key]
	ids = {tuple(r["operation"]): i for i, r in enumerate(
		sorted(path_rows, key=lambda r: path_order(r["operation"])))}
	records = instances.records()
	shown = {}
	for rec in records:
		rec["path"] = ids[tuple(rec["path"])]
		rec["name"] = label_of(rec["name"])
		shown[rec["path"]] = shown.get(rec["path"], 0) + 1
	path_out = []
	for r in path_rows:
		key = tuple(r["operation"])
		row = {k: v for k, v in r.items() if k != "operation"}
		row.update(id=ids[key], path=[label_of(x) for x in key], name=label_of(key[-1]),
		           depth=len(key) - 1,
		           parent=ids.get(key[:-1]), n_instances_shown=shown.get(ids[key], 0))
		path_out.append(row)
	path_out.sort(key=lambda r: r["id"])

	header_attrs = (rep["run"].get("attributes") or {})
	session_attrs, session_omitted = _filter_attributes(header_attrs, allow, all_attributes)
	interval = summary["sample_interval_s"]
	free_text = _free_text_labels(structure.labels)
	view = {
		"view_version": VIEW_VERSION,
		"generated_by": f"agent_energy_profiler {__version__}",
		"privacy": {
			"attributes": ("all" if all_attributes else "allowlist" if allow else "none"),
			"allowlist": sorted(allow),
			"omitted": ["host names", "file paths", "span, trace and session ids"]
			           + ([] if all_attributes else ["attributes not allowed by name"])
			           + (["operation labels"] if redact_labels else []),
			"labels": "redacted" if redact_labels else "as_written",
			"n_labels": len(structure.labels),
			"n_free_text_labels": len(free_text),
			"many_labels": (len(structure.labels) > MANY_LABELS
			                and len(structure.labels) > structure.n / 2),
		},
		"run": {
			"name": name or _basename(os.path.abspath(path if os.path.isdir(path)
			                                          else os.path.dirname(path) or ".")),
			"schema": rep["source"]["schema"],
			"wall_s": rep["run"].get("wall_s"),
			"n_spans": rep["counts"]["spans"],
			"status": rep["counts"]["status"],
			"n_operations": len(summary["rows"]),
			"n_paths": len(path_out),
			"attributes": session_attrs,
			"n_attributes_omitted": session_omitted,
		},
		"mode": summary["mode"],
		"basis": _basis(summary),
		"fallback_reason": scrub(summary["fallback_reason"]),
		"whole_run": _whole_run(summary),
		"coverage": [{"domain": d, "text": t} for d, t in summary["coverage"]],
		"operations": [dict({k: v for k, v in r.items() if k != "operation"},
		                    name=label_of(r["operation"])) for r in summary["rows"]],
		"paths": path_out,
		"instances": records,
		"instances_info": {
			"shown": len(records), "total": structure.n,
			"policy": ("every span" if len(records) == structure.n else
			           f"at most {max_instances} spans: per hierarchy path, those not ok, "
			           f"then shared, then the slowest; every count, aggregate, coverage "
			           f"figure and warning still covers all {structure.n} spans"),
		},
		"legend": {tag: text.format(interval=report._s(interval))
		           for tag, text in brief.LEGEND.items()},
		"reasons": REASON_TEXT,
		"warnings": scrub([{"code": w["code"], "message": w["message"]}
		                   for w in rep["warnings"]]),
		"provenance": _provenance(rep, scrub),
	}
	validate(view, forbidden=scrub.paths, forbidden_hosts=scrub.hosts)
	return view


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------

def _check_cell(cell, where):
	if cell.get("kind") not in KINDS:
		raise ViewModelError(f"{where}: unknown kind {cell.get('kind')!r}")
	if cell.get("scope") not in SCOPES:
		raise ViewModelError(f"{where}: unknown scope {cell.get('scope')!r}")
	has_value = cell.get("value_j") is not None
	if has_value != (cell["kind"] in (m.MEASURED, m.ESTIMATED)):
		raise ViewModelError(f"{where}: {cell['kind']} with value {cell.get('value_j')!r}")


def validate(view, forbidden=(), forbidden_hosts=()):
	"""Raise ViewModelError where `view` breaks the contract. `forbidden`:
	regular expressions (paths) that must not match the text the view copies
	from the recording (provenance, warnings, mode, basis);
	`forbidden_hosts`: host names that must not match its free text (the
	mode's reason, the measurement note)."""
	if view.get("view_version") != VIEW_VERSION:
		raise ViewModelError(f"view_version {view.get('view_version')!r}")
	for i, cell in enumerate(view["whole_run"]):
		_check_cell(cell, f"whole_run[{i}]")
	for rec in view["instances"]:
		for cell in rec.get("cells", ()):
			_check_cell(cell, f"span #{rec['n']}")
	basis = view["basis"]
	for kind, rows in (("operations", view["operations"]), ("paths", view["paths"])):
		ranked = [r for r in rows if r["ranked"]]
		if ranked and basis is None:
			raise ViewModelError(f"{kind}: ranked rows without a basis")
		for r in rows:
			where = f"{kind} {r.get('path') or r.get('name')!r}"
			if r["ranked"]:
				if r["energy_j"] is None:
					raise ViewModelError(f"{where}: ranked without a value")
				tags = {n.split(" ")[0] for n in r["notes"]}
				if tags & {"shared", "parent", "partial", "none", "overlap"}:
					raise ViewModelError(f"{where}: ranked although {sorted(tags)}")
			elif r.get("share") is not None:
				raise ViewModelError(f"{where}: a share without a rank")
		shares = [r["share"] for r in ranked if r["share"] is not None]
		if shares and abs(sum(shares) - 1.0) > 1e-9:
			raise ViewModelError(f"{kind}: shares add up to {sum(shares)}")
	if view["privacy"].get("labels") == "redacted":
		names = ([r["name"] for r in view["operations"]] + [r["name"] for r in view["instances"]]
		         + [x for r in view["paths"] for x in r["path"]])
		if any(not re.fullmatch(r"operation \d+", str(x)) for x in names):
			raise ViewModelError("an operation label remains although labels were redacted")
	if view["privacy"]["attributes"] == "none":
		if view["run"]["attributes"] or any(rec.get("attributes") for rec in view["instances"]):
			raise ViewModelError("attributes present although none were allowed")
	if forbidden:
		# labels and allowed attributes are the caller's own; what the
		# recording and the report add must not carry a host name or a path
		text = json.dumps({k: view.get(k) for k in ("provenance", "warnings", "mode", "basis",
		                                            "fallback_reason", "whole_run", "coverage")})
		for pattern in forbidden:
			if pattern.search(text):
				raise ViewModelError("the view would contain a file path; it was not written")
	prov = view.get("provenance") or {}
	prose = " ".join(str(prov.get(k) or "") for k in ("mode_reason", "measurement_note"))
	for pattern in forbidden_hosts:
		if pattern.search(prose):
			raise ViewModelError("the view would contain a host name; it was not written")
