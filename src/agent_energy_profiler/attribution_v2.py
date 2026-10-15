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

RAPL_SOURCE = "rapl"


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
		ranges = v1.load_power_meta(meta_path or v1.power_meta_path(power_path))
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

	def gpu_counter_domain(self, t0, t1, resolution, duration):
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


def attribute_spans(spans, timeline=None, resolution=None, allow_uncovered=False):
	"""Attributed + annotated spans. Raises ValueError on uncovered spans unless
	allow_uncovered, in which case their timeline domains are not_covered."""
	resolution = resolution or m.Resolution.unknown()
	if timeline is not None and not allow_uncovered:
		uncovered = [s for s in spans if s.get("start_unix_s") is not None
		             and not timeline.covers(s["start_unix_s"], s["end_unix_s"])]
		if uncovered:
			raise ValueError(f"{len(uncovered)} of {len(spans)} spans fall outside "
			                 f"the hardware timeline; no energy is attributed. "
			                 f"Use allow_uncovered to mark them not_covered.")
	out = []
	for span in spans:
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
		out.append(s)
	return accounting.annotate(out)


def session_window(spans, timeline, resolution):
	"""Whole-session window energy from the timeline (independent of spans)."""
	if timeline is None:
		return None
	starts = [s["start_unix_s"] for s in spans if s.get("start_unix_s") is not None]
	ends = [s["end_unix_s"] for s in spans if s.get("end_unix_s") is not None]
	if not starts or not ends:
		return None
	t0, t1 = min(starts), max(ends)
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

	header = schema_v2.session_header(args.spans)
	if args.resolution_report:
		resolution = m.Resolution.from_hardware_report(args.resolution_report)
	elif header:
		resolution = m.Resolution.from_dict(header["config"].get("resolution"))
	else:
		resolution = m.Resolution.unknown()
	spans = schema_v2.read_records(args.spans, resolution)
	timeline = Timeline(args.power, args.power_meta or None) if args.power else None

	try:
		rows = attribute_spans(spans, timeline, resolution, args.allow_uncovered)
	except ValueError as e:
		print(f"ERROR: {e}", file=sys.stderr)
		return 1

	os.makedirs(args.out_dir, exist_ok=True)
	with open(os.path.join(args.out_dir, "spans_attributed.jsonl"), "w") as f:
		for row in rows:
			f.write(json.dumps(row) + "\n")
	summary = accounting.summarize(rows, session_window(rows, timeline, resolution))
	summary.update({
		"schema_version": schema_v2.SCHEMA_VERSION,
		"input": os.path.abspath(args.spans),
		"input_schema": 2 if header else 1,
		"session": header,
		"resolution": resolution.to_dict(),
		"timeline": None if timeline is None else {
			"power_file": os.path.abspath(args.power),
			"samples": len(timeline.ts),
			"backward_steps": timeline.backward_steps,
			"rapl_columns_without_range": timeline.n_rapl_columns_without_range,
		},
	})
	with open(os.path.join(args.out_dir, "session_summary.json"), "w") as f:
		json.dump(summary, f, indent=2)
	print(f"{len(rows)} spans attributed -> {args.out_dir}")
	return 0


if __name__ == "__main__":
	sys.exit(main())
