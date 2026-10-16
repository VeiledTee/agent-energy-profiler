"""Schema-v2 attribution: spans + hardware timeline -> attributed spans.

Inputs:
  spans.jsonl     written by Session (schema v2), or a v1 events.jsonl, which
                  is read through schema_v2.upgrade_v1
  power.csv       optional; written by the sampler on the same wall clock
Outputs (in --out-dir):
  spans_attributed.jsonl   every span, unchanged, plus energy.cpu_package,
                           energy.dram, energy.cpu_core (diagnostic),
                           estimates.gpu, total and accounting
  session_summary.json     accounting.summarize(), plus the session window
                           measured independently from the timeline

Differences from v1 attribution (attribution.py), all deliberate:

  * integrated sampled power is an ESTIMATE and goes to estimates.gpu only;
    it never fills energy.gpu and never enters total;
  * every unusable window says why (reason codes, docs/SCHEMA_V2.md 2.5);
  * the GPU counter in the timeline is checked for decreases: a window that
    spans one is counter_discontinuity, not a (possibly negative) energy.
    v1's counter_delta is unchanged and still differences it blindly;
  * nested spans get exclusive energy where it is defensible (accounting.py).

The numerical methods are v1's: same prepared series, same interpolation,
same trapezoid. A v2 cpu_package/dram value equals the v1 value for the same
window whenever both exist.

Usage:
  python -m agent_energy_profiler.attribution_v2 --spans spans.jsonl \
      [--power power.csv] --out-dir out/ [--allow-uncovered]
"""

import argparse
import bisect
import json
import os
import sys

from . import accounting
from . import attribution as v1
from . import measurement as m
from . import schema_v2
from ._compat import sum  # 3.12 float summation on every Python (see _compat)

RAPL_SOURCE = "rapl"


def _load_timeline_meta(path):
	"""The sidecar's `timeline` block (sampler v2), or None."""
	try:
		with open(path) as f:
			meta = json.load(f)
	except (OSError, ValueError):
		return None
	if not isinstance(meta, dict) or meta.get("format") != v1.POWER_META_FORMAT:
		return None
	timeline = meta.get("timeline")
	return timeline if isinstance(timeline, dict) else None


def _group_window(group, t0, t1, scale):
	"""(joules, None) for a complete device group, else (None, reason)."""
	if not group:
		return None, "no_counter"
	parts = []
	for series in group:
		d, reason = v1.counter_window(series, t0, t1)
		if d is None:
			return None, reason
		parts.append(d / scale)
	return sum(parts), None


def _status_for(reason):
	return m.INVALID if reason in ("counter_discontinuity", "clock_anomaly") \
		else m.UNAVAILABLE


def _samples_in(series_list, names, t0, t1):
	"""Usable samples per column inside [t0, t1] (by bisection, so exact on a
	monotonic timeline and approximate across a backward clock step)."""
	out = []
	for name, series in zip(names, series_list):
		tt = series[0]
		n = max(0, bisect.bisect_right(tt, t1) - bisect.bisect_left(tt, t0))
		out.append({"column": name, "samples_in_window": n})
	return out


class Timeline:
	"""The hardware timeline, prepared once (see attribution.py M1.5)."""

	def __init__(self, power_path, meta_path=None):
		self.ts, gpus, rapls, gpu_energy = v1.load_power(power_path)
		meta_path = meta_path or v1.power_meta_path(power_path)
		ranges = v1.load_power_meta(meta_path)
		#: what the sampler declared it read (sidecar v2); None for timelines
		#: written before Milestone 5, which read the counter on every row
		self.timeline_meta = _load_timeline_meta(meta_path)
		policy = (self.timeline_meta or {}).get("counter_policy", "every_row")
		#: sparse: the counter was deliberately not read on every row. Window
		#: values are then measured only between actual readings (run_counter),
		#: never interpolated across long unread gaps.
		self.sparse_counter = policy in ("boundaries", "every_n_rows")
		self.gpu_energy_raw = list(gpu_energy.values())
		self.n_rapl_columns_without_range = sum(1 for c in rapls if ranges.get(c) is None)
		pkg, core, dram = v1.classify_rapl(rapls)
		self.groups = {}
		for domain, cols in (("cpu_package", pkg), ("cpu_core", core), ("dram", dram)):
			self.groups[domain] = (list(cols), v1.prepare_rapl_group(self.ts, cols, ranges))
		# GPU cumulative counter, checked for decreases (no range: NVML's
		# register does not wrap, so any decrease is a discontinuity).
		self.gpu_counter = (list(gpu_energy),
		                    [v1.prepare_rapl(self.ts, v, None) for v in gpu_energy.values()])
		self.gpu_power = [v1.usable_samples(self.ts, w) for w in gpus.values()]
		self.window = v1.coverage_window(self.ts)
		self.backward_steps = len(v1.backward_steps(self.ts))

	def covers(self, t0, t1):
		return (self.window is not None and t0 is not None and t1 is not None
		        and t0 >= self.window[0] and t1 <= self.window[1])

	def rapl_domain(self, domain, t0, t1):
		names, group = self.groups[domain]
		value, reason = _group_window(group, t0, t1, 1e6)
		status = m.MEASURED if value is not None else _status_for(reason)
		d = m.domain(value, status, reason, RAPL_SOURCE, m.SAMPLED,
		             raw=_samples_in(group, names, t0, t1))
		if domain == "cpu_core":
			d["diagnostic"] = True
		return d

	def counter_readings(self):
		"""[(t, total_mj, segment)] on rows where every GPU's counter was read.

		segment changes where any device's counter went down (reset, driver
		reload): energy across a segment change is not defensible.
		"""
		if not self.gpu_energy_raw:
			return []
		rows = [i for i in range(len(self.ts))
		        if all(col[i] is not None for col in self.gpu_energy_raw)]
		seg, out, prev = 0, [], None
		for i in rows:
			values = [col[i] for col in self.gpu_energy_raw]
			if prev is not None and any(v < p for v, p in zip(values, prev)):
				seg += 1
			out.append((self.ts[i], sum(values), seg))
			prev = values
		return out

	def run_counter(self, span_t0=None, span_t1=None):
		"""Measured GPU energy between the first and last counter readings.

		The window is the readings' own: it is not stretched or shrunk to the
		spans by interpolation. `brackets_all_spans` says whether it contains
		every span; energy outside the spans (sampler start-up, idle tail) is
		inside it.
		"""
		reads = self.counter_readings()
		base = {"source": m.GPU_SOURCE, "method": "sampled_counter_readings",
		        "scope": m.SCOPE_DEVICE, "n_readings": len(reads)}
		if len(reads) < 2:
			return dict(base, energy_j=None, status=m.UNAVAILABLE,
			            reason="insufficient_samples" if self.gpu_energy_raw else "no_counter")
		(t0, e0, s0), (t1, e1, s1) = reads[0], reads[-1]
		out = dict(base, start_unix_s=t0, end_unix_s=t1, duration_s=t1 - t0,
		           brackets_all_spans=(span_t0 is not None and span_t1 is not None
		                               and t0 <= span_t0 and t1 >= span_t1))
		if s0 != s1:
			return dict(out, energy_j=None, status=m.INVALID, reason="counter_discontinuity")
		return dict(out, energy_j=(e1 - e0) / 1000.0, status=m.MEASURED, reason=None)

	def counter_intervals(self):
		"""Counter vs integrated averaged power on each interval between readings.

		Diagnostic only, never a correction: averaged power lags load changes
		(docs/NVML_COUNTER_VALIDATION.md section 4), so short intervals around
		load steps disagree for reasons that are not counter error.
		"""
		reads = self.counter_readings()
		pairs = []
		for (ta, ea, sa), (tb, eb, sb) in zip(reads, reads[1:]):
			if sa != sb or tb <= ta:
				continue
			parts = [v1.integrate_prepared(s, ta, tb) for s in self.gpu_power]
			est = sum(parts) if parts and all(p is not None for p in parts) else None
			pairs.append((tb - ta, (eb - ea) / 1000.0, est))
		usable = [(d, c, e) for d, c, e in pairs if e]
		ratios = sorted(c / e for d, c, e in usable)

		def q(p):
			return ratios[min(len(ratios) - 1, int(p * (len(ratios) - 1) + 0.5))] if ratios else None
		return {
			"n_intervals": len(pairs),
			"median_interval_s": sorted(d for d, _, _ in pairs)[len(pairs) // 2] if pairs else None,
			"counter_j": sum(c for _, c, _ in pairs) if pairs else None,
			"estimate_j_same_intervals": sum(e for _, _, e in usable) if usable else None,
			"counter_over_estimate_median": q(0.5),
			"counter_over_estimate_p10": q(0.1),
			"counter_over_estimate_p90": q(0.9),
			"note": "diagnostic: integrated averaged power vs the counter between consecutive "
			        "counter readings; averaged power lags load changes",
		}

	def gpu_counter_domain(self, t0, t1, resolution, duration):
		if self.sparse_counter:
			# the counter was read too sparsely (by configuration) for an
			# interpolated window value to be a measurement; see run_counter
			return m.domain(None, m.UNAVAILABLE, "counter_not_sampled", m.GPU_SOURCE, m.SAMPLED)
		names, group = self.gpu_counter
		value, reason = _group_window(group, t0, t1, 1e3)
		status = m.MEASURED if value is not None else _status_for(reason)
		below, intervals = (resolution.classify(duration) if value is not None
		                    else (None, None))
		return m.domain(value, status, reason, m.GPU_SOURCE, m.SAMPLED, below,
		                intervals, raw=_samples_in(group, names, t0, t1))

	def gpu_estimate(self, t0, t1):
		parts = [v1.integrate_prepared(s, t0, t1) for s in self.gpu_power]
		if parts and all(p is not None for p in parts):
			return {"energy_j": sum(parts), "status": m.ESTIMATED,
			        "method": "power_integration",
			        "detail": "trapezoid over sampled board power; an estimate, "
			                  "not a counter measurement"}
		return {"energy_j": None, "status": m.UNAVAILABLE,
		        "method": "power_integration",
		        "detail": "no usable power samples" if self.gpu_power
		                  else "no gpu power columns"}


def _unavailable(domain, reason):
	d = m.domain(None, m.UNAVAILABLE, reason, RAPL_SOURCE, m.SAMPLED)
	if domain == "cpu_core":
		d["diagnostic"] = True
	return d


def _total(energy):
	terms = ("gpu", "cpu_package", "dram")
	measured = [t for t in terms if energy.get(t, {}).get("status") == m.MEASURED]
	missing = [t for t in terms if t not in measured]
	flags = [energy[t].get("below_resolution") for t in measured]
	if any(f is True for f in flags):
		below = True
	elif any(f is None for f in flags):
		below = None
	else:
		below = False
	complete = not missing
	return {
		"energy_j": sum(energy[t]["energy_j"] for t in terms) if complete else None,
		"status": m.MEASURED if complete else "incomplete",
		"boundary": "gpu+cpu_package+dram",
		"domains_measured": measured,
		"domains_missing": missing,
		"below_resolution": below if complete else None,
	}


def _uncovered(span, timeline):
	return (timeline is not None and span.get("start_unix_s") is not None
	        and not timeline.covers(span["start_unix_s"], span["end_unix_s"]))


def _uncovered_error(n_uncovered, n_spans):
	return ValueError(f"{n_uncovered} of {n_spans} spans fall outside the hardware "
	                  f"timeline; no energy is attributed. Use allow_uncovered to "
	                  f"mark them not_covered.")


def attribute_one(span, timeline=None):
	"""One span with the timeline domains, estimates and total added (a copy;
	the input is not modified). No accounting: that needs every span."""
	s = dict(span)
	energy = dict(s.get("energy") or {})
	estimates = dict(s.get("estimates") or {})
	t0, t1 = s.get("start_unix_s"), s.get("end_unix_s")
	if timeline is None:
		for domain in ("cpu_package", "dram", "cpu_core"):
			energy.setdefault(domain, _unavailable(domain, "no_counter"))
		estimates.setdefault("gpu", {"energy_j": None, "status": m.UNAVAILABLE,
		                             "method": "power_integration",
		                             "detail": "no hardware timeline supplied"})
	elif not timeline.covers(t0, t1):
		for domain in ("cpu_package", "dram", "cpu_core"):
			energy[domain] = _unavailable(domain, "not_covered")
		estimates.setdefault("gpu", {"energy_j": None, "status": m.UNAVAILABLE,
		                             "method": "power_integration",
		                             "detail": "span outside the hardware timeline"})
	else:
		for domain in ("cpu_package", "dram", "cpu_core"):
			energy[domain] = timeline.rapl_domain(domain, t0, t1)
		estimates.setdefault("gpu", timeline.gpu_estimate(t0, t1))
	energy.setdefault("gpu", m.unavailable_gpu("no_counter"))
	s["energy"] = energy
	s["estimates"] = estimates
	s["total"] = _total(energy)
	return s


def attribute_spans(spans, timeline=None, resolution=None, allow_uncovered=False):
	"""Attributed + annotated spans, all in memory. Raises ValueError on
	uncovered spans unless allow_uncovered, in which case their timeline
	domains are not_covered. `attribute_file` streams instead."""
	spans = list(spans)
	if not allow_uncovered:
		n = sum(1 for s in spans if _uncovered(s, timeline))
		if n:
			raise _uncovered_error(n, len(spans))
	return accounting.annotate([attribute_one(s, timeline) for s in spans])


def attribute_file(spans_path, out_path, timeline=None, resolution=None,
                   allow_uncovered=False):
	"""Stream a spans file to spans_attributed.jsonl; returns the summary.

	Two passes over the input so that memory holds only the accounting
	index, never all spans: pass 1 attributes each span to build its compact
	accounting node (and checks coverage before anything is written); pass 2
	attributes it again -- the same deterministic computation -- adds its
	accounting and writes it out. Output is byte-identical to the in-memory
	attribute_spans + accounting.summarize path.
	"""
	nodes, n_uncovered = [], 0
	t_min = t_max = None
	for span in schema_v2.iter_records(spans_path, resolution):
		if _uncovered(span, timeline):
			n_uncovered += 1
		row = attribute_one(span, timeline)
		nodes.append(accounting.compact(row))
		if row.get("start_unix_s") is not None:
			t_min = row["start_unix_s"] if t_min is None else min(t_min, row["start_unix_s"])
		if row.get("end_unix_s") is not None:
			t_max = row["end_unix_s"] if t_max is None else max(t_max, row["end_unix_s"])
	if n_uncovered and not allow_uncovered:
		raise _uncovered_error(n_uncovered, len(nodes))

	index = accounting.Index(nodes)
	builder = accounting.SummaryBuilder()
	with open(out_path, "w") as f:
		for i, span in enumerate(schema_v2.iter_records(spans_path, resolution)):
			row = attribute_one(span, timeline)
			row["accounting"] = index.account(i)
			f.write(json.dumps(row) + "\n")
			builder.add(row)
	return builder.result(_window_from_bounds(t_min, t_max, timeline, resolution))


def session_window(spans, timeline, resolution):
	"""Whole-session window energy from the timeline (independent of spans)."""
	if timeline is None:
		return None
	starts = [s["start_unix_s"] for s in spans if s.get("start_unix_s") is not None]
	ends = [s["end_unix_s"] for s in spans if s.get("end_unix_s") is not None]
	if not starts or not ends:
		return None
	return _window_from_bounds(min(starts), max(ends), timeline, resolution)


def _window_from_bounds(t0, t1, timeline, resolution):
	if timeline is None or t0 is None or t1 is None:
		return None
	resolution = resolution or m.Resolution.unknown()
	if not timeline.covers(t0, t1):
		domains = {"gpu": m.domain(None, m.UNAVAILABLE, "not_covered",
		                           m.GPU_SOURCE, m.SAMPLED)}
		domains.update({d: _unavailable(d, "not_covered")
		                for d in ("cpu_package", "dram")})
	else:
		domains = {
			"gpu": timeline.gpu_counter_domain(t0, t1, resolution, t1 - t0),
			"cpu_package": timeline.rapl_domain("cpu_package", t0, t1),
			"dram": timeline.rapl_domain("dram", t0, t1),
		}
	return {"start_unix_s": t0, "end_unix_s": t1, "domains": domains}


def main(argv=None):
	ap = argparse.ArgumentParser(description="schema-v2 attribution")
	ap.add_argument("--spans", required=True, help="v2 spans.jsonl or v1 events.jsonl")
	ap.add_argument("--power", default="", help="sampler power.csv (optional)")
	ap.add_argument("--power-meta", default="")
	ap.add_argument("--out-dir", required=True)
	ap.add_argument("--resolution-report", default="",
	                help="aep-validate --json report; overrides the session's "
	                     "recorded resolution (needed for v1 input)")
	ap.add_argument("--allow-uncovered", action="store_true",
	                help="mark spans outside the timeline not_covered instead "
	                     "of refusing to attribute the run")
	args = ap.parse_args(argv)
	try:
		summary = attribute_run(args.spans, args.out_dir, power=args.power,
		                        power_meta=args.power_meta,
		                        resolution_report=args.resolution_report,
		                        allow_uncovered=args.allow_uncovered)
	except ValueError as e:
		print(f"ERROR: {e}", file=sys.stderr)
		return 1
	print(f"{summary['n_spans']} spans attributed -> {args.out_dir}")
	return 0


def attribute_run(spans, out_dir, power="", power_meta="", resolution_report="",
                  allow_uncovered=False):
	"""Attribute a spans file into out_dir (spans_attributed.jsonl and
	session_summary.json); returns the summary. What `aep attribute` runs.

	Raises ValueError when spans fall outside the timeline (unless
	allow_uncovered); nothing is written then.
	"""
	header = schema_v2.session_header(spans)
	if resolution_report:
		resolution = m.Resolution.from_hardware_report(resolution_report)
	elif header:
		resolution = m.Resolution.from_dict(header["config"].get("resolution"))
	else:
		resolution = m.Resolution.unknown()
	timeline = Timeline(power, power_meta or None) if power else None

	os.makedirs(out_dir, exist_ok=True)
	out_path = os.path.join(out_dir, "spans_attributed.jsonl")
	summary = attribute_file(spans, out_path, timeline, resolution, allow_uncovered)
	summary.update({
		"schema_version": schema_v2.SCHEMA_VERSION,
		"input": os.path.abspath(spans),
		"input_schema": 2 if header else 1,
		"session": header,
		"resolution": resolution.to_dict(),
		"timeline": None if timeline is None else {
			"power_file": os.path.abspath(power),
			"samples": len(timeline.ts),
			"backward_steps": timeline.backward_steps,
			"rapl_columns_without_range": timeline.n_rapl_columns_without_range,
		},
	})
	# Milestone 5, only for timelines that declare what they read (sidecar
	# v2), so earlier artifacts attribute exactly as before.
	if timeline is not None and timeline.timeline_meta is not None:
		summary["timeline"]["declared"] = timeline.timeline_meta
		window = summary.get("window") or {}
		summary["run_counter"] = timeline.run_counter(window.get("start_unix_s"),
		                                              window.get("end_unix_s"))
		summary["counter_intervals"] = timeline.counter_intervals()
	with open(os.path.join(out_dir, "session_summary.json"), "w") as f:
		json.dump(summary, f, indent=2)
	return summary


if __name__ == "__main__":
	sys.exit(main())
