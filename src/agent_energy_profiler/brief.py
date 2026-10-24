"""Brief report: which operations energy went to, on one screen.

    from agent_energy_profiler import brief
    print(brief.render(brief.build("agent-energy-profiler-runs/my-run")))

`agent-energy-profiler report <run> --brief` prints the same, and
Session.profile prints it when the session closes. It is a formatting layer
over report.build(): it computes no new measurement, and the full report
(report.render_text, report.json, operations.csv) is unchanged.

Ranking rules:

  * One basis for the whole table, never mixed: measured exclusive GPU
    energy; else estimated GPU energy; else measured exclusive CPU package
    energy; else no energy ranking (operations by total time, and why).
  * An operation is ranked only when every one of its spans has a value on
    that basis. The others are listed after the ranked ones with `n/a` and
    the reason, never as 0.
  * Measured energy is exclusive (a parent keeps only what its children do
    not cover), so ranked values never count anything twice. An estimate
    covers a span's whole window and has no exclusive form, so only leaf
    spans that no other span overlapped are ranked by estimate; an
    operation with child spans is listed, not ranked.
  * A span that overlapped other recorded work (`shared`) has no exclusive
    energy: device energy cannot be split between concurrent spans.
  * Energy is device-level. A ranking says where energy was used while an
    operation ran, not which operation is inefficient.
"""

import textwrap

from . import report as _report
from .report import _reasons_text, _s

DEFAULT_TOP = 10
WIDTH = 96
BASES = (("gpu", "measured"), ("gpu", "estimated"), ("cpu_package", "measured"))
DOMAIN_NAMES = {"gpu": "GPU", "cpu_package": "CPU package", "dram": "DRAM"}

LEGEND = {
	"shared": "shared: ran while other recorded spans ran; device energy cannot be split "
	          "between them, so it is not ranked",
	"parent": "parent: has child spans; an estimate covers the children too, so it is not "
	          "ranked (their own rows are)",
	"partial": "partial: some spans have no value (reason given); a sum over the others "
	           "would understate the operation, so it is not ranked",
	"none": "none: no span of the operation has a value on this basis",
	"short": "short: spans shorter than the {interval} power sampling interval; their "
	         "estimates are interpolated, not observed",
	"quantised": "quantised: spans shorter than the GPU counter's update interval; their "
	             "values are quantised and may not represent the span (not ranked when "
	             "all are)",
	"overlap": "overlap: schema-v1 events overlap in time, so their energy cannot be "
	           "compared without counting device energy twice",
}


#: Why an operation is not ranked, as the "not ranked" line says it.
REASONS = {
	"shared": "overlapped other recorded work (shared)",
	"parent": "have child spans, and an estimate covers the children too (parent)",
	"quantised": "have values only below the counter's update interval (quantised)",
	"partial": "lack a value for some spans (partial)",
	"none": "have no value on this basis (none)",
}


def build(path, resolution=None):
	"""The brief report dict for a run directory or a v1/v2 file."""
	rep, acc = _report._build(path, resolution)
	return _summarize(rep, acc, path)


def _sample_interval(rep):
	meas = rep.get("measurement") or {}
	timeline = meas.get("timeline_declared") or meas.get("timeline") or {}
	hz = timeline.get("power_hz")
	return 1.0 / hz if hz else None


def _value(op, det, basis, v1_overlap):
	"""The operation's energy on `basis`, or None when not every span has one."""
	if v1_overlap:
		return None
	domain, kind = basis
	if kind == "estimated":
		total, n = det["leaf_estimate"]
		return total if (n and n == det["n"]) else None
	e = op["energy"].get(domain)
	if e and e["n_included"] and not e["n_excluded"]:
		return e["energy_j"]
	return None


def _all_quantised(det, basis):
	"""Every span's measured GPU value is below the counter's resolution."""
	return basis == ("gpu", "measured") and det["below_resolution"] >= det["n"]


def _notes(op, det, basis, v1_overlap, interval, value):
	"""Short tags (see LEGEND) saying why a row is unranked or needs care."""
	notes = []
	if value is None:
		if v1_overlap:
			notes.append("overlap")
		elif det["shared"]:
			notes.append("shared")
		elif basis and basis[1] == "estimated" and det["parents"]:
			notes.append("parent")
		elif basis:
			domain, kind = basis
			if kind == "estimated":
				without = det["n"] - det["leaf_estimate"][1]
			else:
				e = op["energy"].get(domain) or {}
				without = e.get("n_excluded", det["n"])
			reasons = _reasons_text(det["missing"][domain]) or "no estimate"
			if without < det["n"]:
				notes.append(f"partial {without}/{det['n']} ({reasons})")
			else:
				notes.append(f"none ({reasons})")
	if basis == ("gpu", "estimated") and interval:
		if det["estimate_min_s"] is not None and det["estimate_min_s"] < interval:
			notes.append("short")
	if basis == ("gpu", "measured") and det["below_resolution"]:
		notes.append("quantised" if _all_quantised(det, basis)
		             else f"quantised {det['below_resolution']}/{det['n']}")
	if op["n_not_ok"]:
		notes.append(f"{op['n_not_ok']} not ok")
	return notes


def _fallback_reason(rep, acc, rows, v1_overlap, basis):
	"""Why no operation could be ranked by energy."""
	if not rows:
		return "no spans were recorded, so there is no operation-level data"
	if v1_overlap:
		return ("schema-v1 events overlap in time, so per-event energy would count "
		        "device energy more than once")
	if all(acc.detail[r["operation"]]["shared"] for r in rows):
		return ("every operation had spans that overlapped other recorded work, so device "
		        "energy cannot be split between operations")
	# each row's own reason, so the summary never names one cause for all
	tags = {}
	for r in rows:
		tag = "quantised" if r["energy_j"] is not None else next(
			(n.split(" ")[0] for n in r["notes"] if n.split(" ")[0] in REASONS), None)
		if tag is None:
			tags = None
			break
		tags[tag] = tags.get(tag, 0) + 1
	if tags and (basis or not set(tags) <= {"none", "partial"}):
		return "no operation can be ranked: " + "; ".join(
			f"{n} of {len(rows)} {REASONS[t]}"
			for t, n in sorted(tags.items(), key=lambda kv: (-kv[1], kv[0])))
	parts = []
	for domain in ("gpu", "cpu_package"):
		missing = {}
		for r in rows:
			for reason, n in acc.detail[r["operation"]]["missing"][domain].items():
				missing[reason] = missing.get(reason, 0) + n
		text = f"{DOMAIN_NAMES[domain]} {_reasons_text(missing) or 'not recorded'}"
		if domain == "gpu" and acc.estimate_only_gpu:
			text += ("; GPU estimates exist, but no operation has one for every span as a "
			         "leaf that nothing overlapped")
		elif (domain == "gpu" and rep["source"]["schema"] == 2
		      and not rep["window"].get("available")):
			text += ", and no hardware timeline to estimate from"
		parts.append(text)
	return "no operation has a value for every span (" + "; ".join(parts) + ")"


def _whole_run(rep):
	"""[(domain, energy_j or None, basis or reason)] of whole-run measurements."""
	rc = rep.get("run_counter") or {}
	totals = rep["totals"]["domains"]
	window = rep.get("window") or {}
	wdom = (window.get("domains") or {}) if window.get("available") else {}
	wbasis = ("trajectory windows" if rep["source"]["schema"] == 1
	          else "sampler, session window")
	out = []
	for domain in ("gpu", "cpu_package", "dram"):
		row = totals[domain]
		if domain == "gpu" and rc.get("status") == "measured":
			out.append((domain, rc["energy_j"], f"sampler counter, {rc['duration_s']:.2f} s"))
		elif row["total_j"] is not None:
			out.append((domain, row["total_j"], "all top-level spans"))
		elif (wdom.get(domain) or {}).get("energy_j") is not None:
			out.append((domain, wdom[domain]["energy_j"], wbasis))
		else:
			if row.get("missing_reasons"):
				why = ", ".join(row["missing_reasons"])
			elif domain == "gpu" and rc:
				why = rc.get("reason") or rc.get("status")
			else:
				why = row.get("reason") or "not recorded"
			if (domain == "gpu" and not rc and rep["source"]["schema"] == 2
			        and not window.get("available")):
				why += "; no hardware timeline"
			out.append((domain, None, why))
	return out


def _coverage(rep, acc):
	"""[(domain, text)]: how many spans have a value, and why the others do not."""
	n = rep["counts"]["spans"]
	out = []
	for domain in ("gpu", "cpu_package"):
		cov = rep["coverage"].get(domain)
		if not cov:
			out.append((domain, "not recorded"))
			continue
		states = cov["states"]
		measured = sum(v for k, v in states.items() if k.startswith("measured"))
		text = f"{measured} of {n} measured"
		if domain == "gpu" and acc.estimate_only_gpu:
			text += f", {acc.estimate_only_gpu} estimated"
		below = states.get("measured_below_resolution")
		if below:
			text += f", {below} below counter resolution"
		if measured < n and cov["reasons"]:
			text += f" (not measured: {_reasons_text(cov['reasons'])})"
		out.append((domain, text))
	return out


def _rows(ops, acc, basis, v1_overlap, interval):
	"""One row per operation (report.operations of `acc`), ranked on `basis`
	first, then the rest by total time. The view model calls this for
	hierarchy paths too, with the label-level basis."""
	# with nothing comparable but estimates present, notes still explain the
	# estimates (shared, parent)
	note_basis = basis or (("gpu", "estimated") if acc.estimate_only_gpu else None)
	rows = []
	for op in ops:
		det = acc.detail[op["operation"]]
		value = _value(op, det, basis, v1_overlap) if basis else None
		secondary = None
		if basis and basis[0] == "gpu":
			secondary = _value(op, det, ("cpu_package", "measured"), v1_overlap)
		rows.append({
			"operation": op["operation"], "n": det["n"], "n_not_ok": op["n_not_ok"],
			"duration_total_s": op["duration_total_s"], "duration_p50_s": op["duration_p50_s"],
			"duration_p95_s": op["duration_p95_s"], "energy_j": value,
			"cpu_package_j": secondary,
			"ranked": value is not None and not _all_quantised(det, basis),
			"notes": _notes(op, det, note_basis, v1_overlap, interval, value),
		})
	ranked = sorted((r for r in rows if r["ranked"]), key=lambda r: (-r["energy_j"],
	                                                                 str(r["operation"])))
	ranked_sum = sum(r["energy_j"] for r in ranked)
	for r in ranked:
		r["share"] = r["energy_j"] / ranked_sum if ranked_sum > 0 else None
	rest = sorted((r for r in rows if not r["ranked"]),
	              key=lambda r: (-(r["duration_total_s"] or 0), str(r["operation"])))
	for r in rest:
		r["share"] = None
	return ranked + rest


def _summarize(rep, acc, path=None):
	"""The brief report dict from report._build()'s report and accumulator."""
	v1_overlap = rep["source"]["schema"] == 1 and rep["totals"]["top_level_overlap"]
	interval = _sample_interval(rep)
	ops = rep["operations"]
	basis = next((b for b in BASES
	              if any(_value(op, acc.detail[op["operation"]], b, v1_overlap) is not None
	                     for op in ops)), None)
	rows = _rows(ops, acc, basis, v1_overlap, interval)
	ranked = [r for r in rows if r["ranked"]]
	meas = rep.get("measurement") or {}
	return {
		"input": path if path is not None else rep["source"]["path"],
		"schema": rep["source"]["schema"],
		"wall_s": rep["run"].get("wall_s"),
		"n_spans": rep["counts"]["spans"],
		"status": rep["counts"]["status"],
		"mode": ({k: meas.get(k) for k in ("mode", "requested_mode", "mode_source")}
		         if meas.get("recorded") else None),
		"whole_run": _whole_run(rep),
		"basis": basis,
		"fallback_reason": (None if ranked else
		                    _fallback_reason(rep, acc, rows, v1_overlap, basis)),
		"sample_interval_s": interval,
		"rows": rows,
		"has_cpu_column": any(r["cpu_package_j"] is not None for r in rows),
		"coverage": _coverage(rep, acc),
		"warnings": [w["code"] for w in rep["warnings"] if w["code"] != "DEVICE_LEVEL"],
	}


def _basis_text(basis, interval, schema):
	if schema == 1:
		return (f"{'estimated' if basis[1] == 'estimated' else 'measured'} "
		        f"{DOMAIN_NAMES[basis[0]]} energy, per event (schema v1 has no span "
		        f"hierarchy;\n  events do not overlap)")
	if basis == ("gpu", "measured"):
		return ("measured GPU energy (counter), exclusive:\n"
		        "  a parent keeps only what its children do not cover")
	if basis == ("gpu", "estimated"):
		rate = f"{1 / interval:g} Hz " if interval else ""
		return (f"ESTIMATED GPU energy (~): {rate}sampled power integrated over each span,\n"
		        f"  not a counter measurement; leaf spans only, so nothing is counted twice")
	return ("measured CPU package energy (RAPL), exclusive:\n"
	        "  a parent keeps only what its children do not cover")


def _wrap(text):
	return textwrap.fill(text, width=WIDTH, initial_indent="  ", subsequent_indent="    ",
	                     break_on_hyphens=False)


def _share(share):
	if share is None:
		return ""
	return "<0.1%" if 0 < share < 0.001 else f"{share * 100:.1f}%"


def render(b, top=DEFAULT_TOP):
	"""The brief report as text."""
	L = []
	status = b["status"]
	not_ok = sum(n for s, n in status.items() if s not in ("ok", None))
	if b["mode"]:
		mode = (f"{b['mode']['mode']} (requested {b['mode']['requested_mode']}"
		        + (f", {b['mode']['mode_source']}" if b["mode"].get("mode_source") else "") + ")")
	else:
		mode = ("schema v1 (GPU counter read at every event)" if b["schema"] == 1
		        else "not recorded")
	n_ops = len(b["rows"])
	L.append("ENERGY SUMMARY (brief)")
	L.append(f"  run        {_s(b['wall_s'])} (first span start to last span end), "
	         f"{b['n_spans']} spans, {n_ops} operations"
	         + (f", {not_ok} not ok" if not_ok else ""))
	L.append(f"  mode       {mode}")
	for i, (domain, value, how) in enumerate(b["whole_run"]):
		text = (f"{value:,.3f} J measured ({how})" if value is not None
		        else f"n/a, not measured ({how})")
		L.append(f"  {'whole run' if i == 0 else '':<11}{DOMAIN_NAMES[domain]:<13}{text}")
	L.append("")

	basis = b["basis"]
	interval = b["sample_interval_s"]
	estimated = basis == ("gpu", "estimated")
	col = None
	if basis:
		col = {"gpu": "GPU J", "cpu_package": "CPU pkg J"}[basis[0]] + (" est." if estimated else "")
	if b["fallback_reason"] is None:
		L.append("Operations ranked by " + _basis_text(basis, interval, b["schema"]))
	else:
		L.append("Operations by total time. Not ranked by energy:")
		L.append(_wrap(b["fallback_reason"] + "."))
	cpu_col = b["has_cpu_column"]
	ranked = [r for r in b["rows"] if r["ranked"]]
	rest = [r for r in b["rows"] if not r["ranked"]]
	share = f"{'share':>8}" if ranked else ""
	head = f"  {'#':>3}  {'operation':<24}{'n':>6}{'total':>10}{'p50':>10}{'p95':>10}"
	if col:
		head += f"{col:>14}{share}"
	if cpu_col:
		head += f"{'CPU pkg J':>12}"
	L.append(head + "  notes")
	shown = ranked[:top] + rest[:top]
	used = set()
	for i, r in enumerate(shown, 1):
		rank = str(i) if r["ranked"] else "-"
		line = (f"  {rank:>3}  {str(r['operation'])[:24]:<24}{r['n']:>6}"
		        f"{_s(r['duration_total_s']):>10}{_s(r['duration_p50_s']):>10}"
		        f"{_s(r['duration_p95_s']):>10}")
		if col:
			value = ("n/a" if r["energy_j"] is None else
			         ("~" if estimated else "") + format(r["energy_j"], ",.3f"))
			line += f"{value:>14}" + (f"{_share(r['share']):>8}" if ranked else "")
		if cpu_col:
			cpu = "n/a" if r["cpu_package_j"] is None else format(r["cpu_package_j"], ",.3f")
			line += f"{cpu:>12}"
		line += ("  " + ", ".join(r["notes"])) if r["notes"] else ""
		L.append(line.rstrip())
		used.update(n.split(" ")[0] for n in r["notes"])
	hidden = len(b["rows"]) - len(shown)
	if hidden:
		L.append(f"  ... {hidden} more operation{'s' if hidden > 1 else ''} in the full report")
	if ranked:
		L.append(f"  share: of the ranked operations' {col.replace(' est.', '')} only."
		         + (" ~ marks an estimate." if estimated else ""))
	elif estimated:
		L.append("  ~ marks an estimate.")
	if cpu_col:
		L.append("  CPU pkg J: measured exclusive CPU package energy, shown, not ranked.")
	if not ranked:
		L.append("  Total time is inclusive: a parent's includes its children's.")
	for tag in ("shared", "parent", "partial", "none", "overlap", "short", "quantised"):
		if tag in used:
			L.append(_wrap(LEGEND[tag].format(interval=_s(interval))))
	L.append("")
	L.append("Coverage per span")
	for domain, text in b["coverage"]:
		L.append(f"  {DOMAIN_NAMES[domain] + ':':<13}{text}")
	L.append("Device-level: energy covers every process on each device and is assigned to")
	L.append("  operations by time window. A large share shows where energy was used, not that")
	L.append("  an operation is inefficient: judge that with its output, throughput and workload.")
	L.append(f"Full report: agent-energy-profiler report {b['input']}")
	if b["warnings"]:
		L.append(f"  its warnings: {', '.join(dict.fromkeys(b['warnings']))}")
	return "\n".join(L)
