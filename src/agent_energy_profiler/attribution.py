"""Join semantic energy events to the hardware power timeline.

Inputs:
  events.jsonl  (written by agent_energy_profiler.events during the run)
  power.csv     (written by agent_energy_profiler.sampling on the same clock)

Outputs:
  events_attributed.jsonl  every source event, unchanged, plus attributed
                           energy fields, the list of domains actually
                           measured (available_energy_domains) and whether
                           the boundary is complete (measurement_complete).
                           This is the canonical per-event research artifact.
  energy_summary.csv       derived aggregate (by operation_label, by
                           operation_type, by question).

Method:
  GPU:  prefer the in-band NVML counter delta the event already carries;
        otherwise integrate sampled GPU power (trapezoid) inside the event's
        window. Events shorter than the sampling period get nearest-sample
        power x duration (an estimate, flagged as such below).
  RAPL: cumulative counters -> energy delta over the window, interpolated
        between samples. Wraparound is unwrapped with each counter's own
        max_energy_range_uj, read from the sampler's metadata sidecar
        (<power.csv>.meta.json); a decrease that cannot be resolved as one
        wrap makes the windows spanning it null.

Energy accounting boundary:

    measured_energy_j = gpu_energy_j + cpu_package_energy_j + dram_energy_j

The RAPL `core` domain is DIAGNOSTIC ONLY: it is a subdomain contained within
`package`, so adding both double-counts CPU energy. cpu_core_energy_j is
reported for diagnostics and never enters the total.

Missing counters stay null. A domain the hardware did not report is not zero:
on WSL2 there is no /sys/class/powercap at all, so cpu/dram come back null and
measured_energy_j is null too. Reporting 0.0 there would silently understate
the measurement boundary.

Honest units:
  Per-QUESTION energy is the robust primitive (long windows, many samples).
  Per-EVENT energy is a timestamp-attributed estimate; sub-100ms SPARQL events
  are order-of-magnitude, not precision, figures. Idle machine assumed:
  concurrent activity is attributed to whatever event overlaps it.

Usage:
  python -m agent_energy_profiler.attribution --events events.jsonl --power power.csv \
      --out energy_summary.csv [--out-events events_attributed.jsonl]
"""

import argparse
import bisect
import csv
import json
import os
import sys
from collections import defaultdict

from . import trajectory
from ._compat import sum  # 3.12 float summation on every Python (see _compat)

#: Format tag of the sampler's power-metadata sidecar (<power.csv>.meta.json).
POWER_META_FORMAT = "agent_energy_profiler.power_meta"

#: Semantic fields carried through from the raw event to the attributed one.
#: Attribution must never lose question id, iteration, traversal depth, step
#: index, operation label, tokens, status or provenance.
CARRIED_FIELDS = (
	"schema_version", "event_id",
	"run_id", "question_id", "dataset", "paradigm",
	"iteration", "traversal_depth", "step_index",
	"operation_type", "operation_label",
	"start_timestamp", "end_timestamp", "duration_s",
	"input_tokens", "output_tokens",
	"status",
	"model_name", "model_revision", "git_commit", "hardware_id",
	"meta",
)


def load_power(path):
	"""Read the hardware timeline.

	Returns (ts, gpu_power_cols, rapl_cols, gpu_energy_cols).

	Three distinct column families, kept separate because they are different
	instruments and must never be mixed:

	  gpu<i>_w           instantaneous power (W)   -> DIAGNOSTIC
	  gpu<i>_energy_mj   cumulative energy (mJ)    -> GPU measurement instrument
	  rapl_<domain>_uj   cumulative energy (uJ)    -> CPU/DRAM instrument

	A power.csv written before the GPU counter column existed simply yields an
	empty gpu_energy_cols, which is reported as unavailable rather than being
	silently replaced by integrated power.
	"""
	ts, gpu_power_cols, rapl_cols, gpu_energy_cols = [], {}, {}, {}
	with open(path) as f:
		r = csv.reader(f)
		header = next(r)
		gpu_pw_idx = [(i, h) for i, h in enumerate(header)
		              if h.startswith("gpu") and h.endswith("_w")]
		gpu_en_idx = [(i, h) for i, h in enumerate(header)
		              if h.startswith("gpu") and h.endswith("_energy_mj")]
		rapl_idx = [(i, h) for i, h in enumerate(header) if h.startswith("rapl_")]
		for _, h in gpu_pw_idx:
			gpu_power_cols[h] = []
		for _, h in gpu_en_idx:
			gpu_energy_cols[h] = []
		for _, h in rapl_idx:
			rapl_cols[h] = []
		for row in r:
			if not row or not row[0]:
				continue
			ts.append(float(row[0]))
			for i, h in gpu_pw_idx:
				# An unreadable power sample is missing, not 0 W: integrate_gpu
				# skips it rather than integrating a fabricated zero.
				gpu_power_cols[h].append(float(row[i]) if row[i] else None)
			for i, h in gpu_en_idx:
				gpu_energy_cols[h].append(float(row[i]) if row[i] else None)
			for i, h in rapl_idx:
				rapl_cols[h].append(float(row[i]) if row[i] else None)
	return ts, gpu_power_cols, rapl_cols, gpu_energy_cols


def power_meta_path(power_path):
	"""Where the sampler writes the metadata sidecar for a power.csv."""
	return power_path + ".meta.json"


def load_power_meta(path):
	"""{rapl column: max_energy_range_uj or None} from a metadata sidecar.

	The sidecar is optional: a power.csv written before it existed, or by a
	sampler that could not read the ranges, yields {} and every RAPL range is
	then unknown. A malformed file or entry is treated the same way. Unknown
	ranges never fall back to an assumed value (see unwrap_counter).
	"""
	try:
		with open(path) as f:
			meta = json.load(f)
	except (OSError, ValueError):
		return {}
	if not isinstance(meta, dict) or meta.get("format") != POWER_META_FORMAT:
		return {}
	zones = meta.get("rapl_max_energy_range_uj")
	if not isinstance(zones, dict):
		return {}
	return {str(column): valid_range(value) for column, value in zones.items()}


def classify_rapl(rapl_cols):
	"""Split RAPL columns into package / core / dram.

	Column names look like rapl_<domain>_<sysfs-node>_uj, e.g.
	rapl_package-0_intel-rapl:0_uj, rapl_core_intel-rapl:0:0_uj,
	rapl_dram_intel-rapl:0:1_uj.

	`core` is matched separately and deliberately excluded from the CPU total:
	it is contained within `package`. `psys` is dropped for the same reason in
	the other direction -- it is a platform-wide domain that *contains*
	package, so counting both would double-count as well.

	Match order matters:
	  * `uncore` is tested before `core`, because the string "uncore" contains
	    "core" and would otherwise be filed as the core domain, silently
	    inflating the cpu_core diagnostic. uncore is dropped: it is neither
	    additive nor the core diagnostic.
	  * `socket` is accepted as a package synonym, because some drivers label
	    the package-equivalent domain that way. Domains are classified by the
	    name the host reports; no CPU vendor is assumed. A host that exposes
	    no DRAM domain simply cannot complete the additive boundary, and
	    measured_energy_j stays null there.
	"""
	package, core, dram = {}, {}, {}
	for name, values in rapl_cols.items():
		lowered = name.lower()
		if "psys" in lowered:
			continue  # platform superset of package; would double-count
		if "dram" in lowered:
			dram[name] = values
		elif "package" in lowered or "socket" in lowered:
			package[name] = values
		elif "uncore" in lowered:
			continue  # not additive, and not the core diagnostic
		elif "core" in lowered:
			core[name] = values
	return package, core, dram


def backward_steps(ts):
	"""Adjacent sample pairs whose wall-clock timestamp went backwards.

	Returns [(index, t_before, t_after)] with t_after < t_before. The timeline
	is reported as-is: it is never sorted or repaired, because the samples'
	counter values belong to the order in which they were read.
	"""
	return [(i, ts[i], ts[i + 1]) for i in range(len(ts) - 1) if ts[i + 1] < ts[i]]


# --- prepared series ---------------------------------------------------------
# Every window lookup below works on a column's USABLE samples (value not
# None), in file order. Filtering is done once per column, not once per
# window: attribution used to rebuild these lists for every event and every
# trajectory, which made it O(windows x samples). The prepared lists are the
# exact lists the per-call code built, so bisection and arithmetic -- and
# therefore every output byte -- are unchanged. The public per-call functions
# remain and prepare their column on each call.

def usable_samples(ts, values):
	"""(timestamps, values) of the samples that carry a value, in file order."""
	tt, vv = [], []
	for t, v in zip(ts, values):
		if v is not None:
			tt.append(t)
			vv.append(v)
	return tt, vv


def integrate_gpu(ts, watts, t0, t1):
	"""Trapezoidal integral of power over [t0, t1] -> Joules, or None.

	None, never 0.0, when the window is impossible (t1 <= t0, e.g. a wall
	clock that stepped backwards mid-event) or when no usable power sample
	exists: there is then no measurement, and a zero would be fabricated.
	"""
	if t0 is None or t1 is None or t1 <= t0:
		return None
	return integrate_prepared(usable_samples(ts, watts), t0, t1)


def integrate_prepared(series, t0, t1):
	"""integrate_gpu over a series from usable_samples()."""
	if t0 is None or t1 is None or t1 <= t0:
		return None
	ts, watts = series
	if not ts:
		return None
	lo = bisect.bisect_left(ts, t0)
	hi = bisect.bisect_right(ts, t1)
	if lo >= len(ts):
		return watts[-1] * (t1 - t0)
	if hi <= 0:
		return watts[0] * (t1 - t0)
	if hi - lo < 2:  # window shorter than sampling period
		k = min(max(lo, 0), len(ts) - 1)
		return watts[k] * (t1 - t0)
	j = 0.0
	for k in range(lo, hi - 1):
		dt = ts[k + 1] - ts[k]
		j += 0.5 * (watts[k] + watts[k + 1]) * dt
	# edge slivers
	j += watts[lo] * max(0.0, ts[lo] - t0)
	j += watts[hi - 1] * max(0.0, t1 - ts[hi - 1])
	return j


def valid_range(max_range):
	"""A usable max_energy_range_uj (positive number), or None."""
	if isinstance(max_range, bool) or not isinstance(max_range, (int, float)):
		return None
	return max_range if max_range > 0 else None


def unwrap_counter(values, max_range):
	"""Unwrap a cumulative uJ counter, in the order its samples were read.

	Returns (unwrapped, segment): unwrapped[k] is values[k] plus every range
	the counter has wrapped through before k; segment[k] increases by one at
	each decrease that cannot be explained as exactly one wraparound. A delta
	is only defensible between samples of the same segment.

	A decrease (v[k] < v[k-1]) counts as one wrap only when all of:

	  * the counter's own max_energy_range_uj is known and valid;
	  * both readings lie inside [0, max_range] (otherwise the metadata does
	    not describe this counter);
	  * the wrap-corrected increment, max_range - v[k-1] + v[k], is below
	    half the range. A genuine wrap at ~10 Hz is a tiny increment; a large
	    one means a counter reset, a stalled sampler that may have wrapped
	    more than once, or wrong metadata, and the number of wraps is then
	    unknowable from the samples.

	Anything else starts a new segment: the energy across that interval is
	unavailable, never invented. A wrap hidden inside an increase (sampler
	stalled for more than a full range) cannot be detected from the samples
	at all; that is a documented limitation, not handled here.
	"""
	max_range = valid_range(max_range)
	unwrapped, segment = [], []
	offset, seg, prev = 0, 0, None
	for v in values:
		if prev is not None and v < prev:
			if (max_range is not None and 0 <= v <= max_range
			        and 0 <= prev <= max_range
			        and (max_range - prev + v) < max_range / 2):
				offset += max_range
			else:
				seg += 1
		unwrapped.append(v + offset if offset else v)
		segment.append(seg)
		prev = v
	return unwrapped, segment


def rapl_delta(ts, uj, t0, t1, max_range=None):
	"""Energy delta (J) from a cumulative uJ counter over [t0, t1].

	`max_range` is this counter's own max_energy_range_uj (see
	hardware.rapl.max_energy_range_uj), used to unwrap wraparound. Without it,
	a counter that never decreases is differenced as before, and an interval
	in which it decreases is unavailable (see unwrap_counter).

	Returns None when the counter has too few usable samples to difference, or
	when the window spans a decrease that cannot be resolved as one wrap: an
	absent or ambiguous counter is missing data, not zero energy.
	"""
	return rapl_delta_prepared(prepare_rapl(ts, uj, max_range), t0, t1)


def prepare_rapl(ts, uj, max_range=None):
	"""(timestamps, unwrapped values, segments) of a RAPL column's usable
	samples, for rapl_delta_prepared."""
	tt, vv = usable_samples(ts, uj)
	vals, seg = unwrap_counter(vv, max_range)
	return tt, vals, seg


def rapl_delta_prepared(series, t0, t1):
	"""rapl_delta over a series from prepare_rapl()."""
	d, _ = counter_window(series, t0, t1)
	if d is None:
		return None
	return d / 1e6  # uJ -> J


def counter_window(series, t0, t1):
	"""(difference in the counter's native unit, None) or (None, reason).

	The core of rapl_delta_prepared, with the reason a window is unusable:
	insufficient_samples, clock_anomaly (impossible window, or a negative
	difference that only a wall-clock step can produce) or
	counter_discontinuity (an unresolved decrease inside the window). Schema
	v2 reports the reason; v1 only ever saw None.
	"""
	tt, vals, seg = series
	if len(tt) < 2:
		return None, "insufficient_samples"
	if t1 <= t0:
		return None, "clock_anomaly"

	def bracket(t):
		i = bisect.bisect_left(tt, t)
		if i <= 0:
			return 0, 0
		if i >= len(tt):
			return len(tt) - 1, len(tt) - 1
		if seg[i - 1] != seg[i]:
			# Interpolating across an unresolved decrease is meaningless, but
			# a boundary that falls exactly on a sample needs only that sample.
			if t == tt[i]:
				return i, i
			if t == tt[i - 1]:
				return i - 1, i - 1
		return i - 1, i

	def value_at(a, b, t):
		if a == b:
			return vals[a]
		ta, tb = tt[a], tt[b]
		if tb == ta:
			return vals[b]
		return vals[a] + (vals[b] - vals[a]) * (t - ta) / (tb - ta)

	a0, b0 = bracket(t0)
	a1, b1 = bracket(t1)
	# Segments only grow with sample index, so equal segments at the lowest
	# and highest sample used mean no unresolved decrease lies in between.
	if seg[min(a0, a1)] != seg[max(b0, b1)]:
		return None, "counter_discontinuity"
	d = value_at(a1, b1, t1) - value_at(a0, b0, t0)
	if d < 0:
		# An unwrapped counter does not run backwards. A negative difference
		# can only come from a wall-clock step in the timeline, so the window
		# lookup is unreliable; the old code added 2**32 uJ here.
		return None, "clock_anomaly"
	return d, None


def counter_delta(ts, values, t0, t1, scale):
	"""Energy (J) from a cumulative counter over [t0, t1].

	`scale` converts the counter's native unit to Joules (1e3 for mJ, 1e6 for
	uJ). Interpolates linearly between the bracketing samples, which is well
	behaved for a monotone accumulator at locally-constant power.

	Returns None when fewer than two usable samples exist: an absent counter is
	missing data, never zero energy.
	"""
	return counter_delta_prepared(usable_samples(ts, values), t0, t1, scale)


def counter_delta_prepared(series, t0, t1, scale):
	"""counter_delta over a series from usable_samples()."""
	tt, vv = series
	if len(tt) < 2 or t1 <= t0:
		return None

	def value_at(t):
		i = bisect.bisect_left(tt, t)
		if i <= 0:
			return vv[0]
		if i >= len(tt):
			return vv[-1]
		ta, va, tb, vb = tt[i - 1], vv[i - 1], tt[i], vv[i]
		if tb == ta:
			return vb
		return va + (vb - va) * (t - ta) / (tb - ta)

	return (value_at(t1) - value_at(t0)) / scale


def _sum_complete(parts):
	"""Sum of a device group, or None unless every device yielded a value.

	A group (several GPUs, several RAPL sockets) is one measurement of the
	whole domain. If any member is unreadable, the readable members are only a
	subset of the hardware, and their sum would silently understate the domain
	as if it were complete. A measured 0.0 is a reading, not a gap.
	"""
	if not parts or any(p is None for p in parts):
		return None
	return sum(parts)


def sum_counter_domain(ts, cols, t0, t1, scale):
	"""Total energy (J) across a counter column group, or None if unavailable.

	None when the group is empty or any column cannot be differenced.
	"""
	return sum_counter_prepared(prepare_counter_group(ts, cols), t0, t1, scale)


def sum_domain(ts, cols, t0, t1, ranges=None):
	"""Total energy (J) across one RAPL domain group, or None if unavailable.

	None when the group is empty or any column cannot be differenced.
	`ranges` maps column name -> max_energy_range_uj (see load_power_meta);
	each socket/domain is unwrapped with its own range.
	"""
	return sum_rapl_prepared(prepare_rapl_group(ts, cols, ranges), t0, t1)


def prepare_counter_group(ts, cols):
	"""Prepared series for every column of a counter group (GPU mJ)."""
	return [usable_samples(ts, values) for values in cols.values()]


def prepare_rapl_group(ts, cols, ranges=None):
	"""Prepared series for every column of a RAPL domain group."""
	ranges = ranges or {}
	return [prepare_rapl(ts, values, ranges.get(name))
	        for name, values in cols.items()]


def sum_counter_prepared(group, t0, t1, scale):
	"""sum_counter_domain over prepare_counter_group() output."""
	if not group:
		return None
	return _sum_complete([counter_delta_prepared(s, t0, t1, scale) for s in group])


def sum_rapl_prepared(group, t0, t1):
	"""sum_domain over prepare_rapl_group() output."""
	if not group:
		return None
	return _sum_complete([rapl_delta_prepared(s, t0, t1) for s in group])


def measured_total(gpu_j, cpu_package_j, dram_j):
	"""gpu + cpu_package + dram, or None if any component is unmeasured.

	`core` is never a term here. A partial sum would misrepresent the
	measurement boundary, so an unmeasured component makes the total null.
	"""
	parts = (gpu_j, cpu_package_j, dram_j)
	if any(p is None for p in parts):
		return None
	return sum(parts)


def coverage_window(ts):
	"""(first, last) sample timestamp of the hardware timeline, or None.

	This is the interval during which hardware was actually being sampled. It
	is a property of the TIMELINE, not of which energy domains that timeline
	happened to contain: a GPU-only power.csv on a host with no RAPL still
	defines a perfectly good window.
	"""
	if not ts:
		return None
	return (ts[0], ts[-1])


def uncovered_events(events, ts):
	"""Events whose [start, end] is not fully inside the hardware timeline.

	Returns (uncovered, considered). An event is uncovered when the sampler was
	not running for all of it -- it started before the first sample, ended
	after the last, or straddles either edge. No hardware measurement exists
	for such an event, so its energy cannot be attributed and must not be
	invented, zeroed, or folded into a trajectory total.

	This is deliberately NOT about domain availability. Missing CPU-package or
	DRAM counters leave those fields null and measurement_complete false; they
	do not make an event uncovered. Events without usable timestamps are not
	considered, matching attribute_events, which skips them.
	"""
	window = coverage_window(ts)
	uncovered, considered = [], 0
	for e in events:
		t0 = e.get("start_timestamp", e.get("t_start"))
		t1 = e.get("end_timestamp", e.get("t_end"))
		if t0 is None or t1 is None:
			continue
		considered += 1
		if window is None or t0 < window[0] or t1 > window[1]:
			uncovered.append(e)
	return uncovered, considered


def render_coverage_failure(uncovered, considered, ts):
	"""Operator-facing explanation of a coverage failure."""
	pct = (100.0 * len(uncovered) / considered) if considered else 100.0
	window = coverage_window(ts)
	lines = [
		"ERROR: %d events (%.1f%%) fall outside hardware measurement coverage"
		% (len(uncovered), pct),
	]
	if window is None:
		lines.append("  hardware timeline: EMPTY - the sampler produced no "
		             "samples at all")
	else:
		lines.append("  hardware timeline : %.3f -> %.3f  (%.1f s, %d samples)"
		             % (window[0], window[1], window[1] - window[0], len(ts)))
		ev_t0 = min((e.get("start_timestamp", e.get("t_start")))
		            for e in uncovered)
		ev_t1 = max((e.get("end_timestamp", e.get("t_end")))
		            for e in uncovered)
		lines.append("  uncovered events  : %.3f -> %.3f" % (ev_t0, ev_t1))
		before = sum(1 for e in uncovered
		             if e.get("start_timestamp", e.get("t_start")) < window[0])
		after = sum(1 for e in uncovered
		            if e.get("end_timestamp", e.get("t_end")) > window[1])
		lines.append("  %d start before the timeline, %d end after it"
		             % (before, after))
	runs = sorted({str(e.get("run_id")) for e in uncovered})
	if runs:
		lines.append("  run_id(s) affected: %s" % ", ".join(runs[:5]))
	lines += [
		"",
		"  No energy is fabricated for these events and trajectory coverage is",
		"  not forced to 1.0, so this run is not a valid measurement.",
		"  Common causes: a reused run tag mixing two runs, the sampler",
		"  starting late or exiting early, or a truncated power log.",
		"  Re-run with a fresh --tag. There is no override: the measured-run",
		"  path will not attribute a run it cannot cover.",
	]
	return "\n".join(lines)


def attribute_events(events, ts, gpus, rapls, rapl_ranges=None):
	"""Attributed rows and GPU-source counts.

	`rapl_ranges` maps RAPL column -> max_energy_range_uj (load_power_meta);
	omitted, every range is unknown and wraps are reported unavailable.
	"""
	pkg_cols, core_cols, dram_cols = classify_rapl(rapls)
	pkg_p = prepare_rapl_group(ts, pkg_cols, rapl_ranges)
	core_p = prepare_rapl_group(ts, core_cols, rapl_ranges)
	dram_p = prepare_rapl_group(ts, dram_cols, rapl_ranges)
	gpu_power_p = [usable_samples(ts, w) for w in gpus.values()]
	gpu_src = {"counter": 0, "integrated": 0, "unavailable": 0}
	rows = []

	for e in events:
		# schema-v1 keys, falling back to the pre-v1 names so an old
		# events.jsonl still attributes rather than crashing.
		t0 = e.get("start_timestamp", e.get("t_start"))
		t1 = e.get("end_timestamp", e.get("t_end"))
		if t0 is None or t1 is None:
			continue

		# A valid in-band counter reading is kept whatever the wall clock did:
		# it is a difference of the monotonic hardware register, not of time.
		measured = e.get("gpu_energy_j")
		if measured is not None:
			gpu_j = measured
			gpu_source = "nvml_counter"
			gpu_src["counter"] += 1
		else:
			parts = [integrate_prepared(s, t0, t1) for s in gpu_power_p]
			if parts and all(p is not None for p in parts):
				gpu_j = sum(parts)
				gpu_source = "power_integration"
				gpu_src["integrated"] += 1
			else:
				# No counter, and no integrable window (reversed/empty interval,
				# no usable samples, or no GPU columns): unmeasured, not 0 J.
				gpu_j = None
				gpu_source = None
				if gpus:
					gpu_src["unavailable"] += 1

		cpu_package_j = sum_rapl_prepared(pkg_p, t0, t1)
		cpu_core_j = sum_rapl_prepared(core_p, t0, t1)  # diagnostic only
		dram_j = sum_rapl_prepared(dram_p, t0, t1)

		row = {key: e.get(key) for key in CARRIED_FIELDS}
		# Legacy readers (measurement/analyze.py, audit_runs.py) still look for
		# these; keep them until every consumer is on schema v1.
		row["category"] = e.get("category")
		row["label"] = e.get("label", e.get("operation_label"))

		total = measured_total(gpu_j, cpu_package_j, dram_j)
		# Which domains of the measurement boundary this event actually has.
		# core is excluded: it is diagnostic, not part of the boundary.
		domains = [name for name, value in (
			("gpu", gpu_j), ("cpu_package", cpu_package_j), ("dram", dram_j),
		) if value is not None]

		row.update({
			"gpu_energy_j": gpu_j,
			"gpu_energy_source": gpu_source,
			"cpu_package_energy_j": cpu_package_j,
			"cpu_core_energy_j": cpu_core_j,
			"dram_energy_j": dram_j,
			"measured_energy_j": total,
			"available_energy_domains": domains,
			"measurement_complete": total is not None,
			# The wall clock stepped backwards during this event. Its counter
			# energy (if any) is still valid; its duration and window are not.
			"clock_anomaly": t1 < t0,
		})
		rows.append(row)

	return rows, gpu_src


def _acc(table, key, row):
	bucket = table[key]
	bucket["n"] += 1
	bucket["duration_s"] += row.get("duration_s") or 0.0
	for field in ("gpu_energy_j", "cpu_package_energy_j", "cpu_core_energy_j",
	              "dram_energy_j", "measured_energy_j"):
		value = row.get(field)
		if value is None:
			bucket[f"{field}__missing"] += 1
		else:
			bucket[field] += value


def _fmt(bucket, field):
	"""Empty cell when no event in the bucket had the measurement."""
	if bucket[f"{field}__missing"] and bucket["n"] == bucket[f"{field}__missing"]:
		return ""
	return f"{bucket[field]:.3f}"


def main():
	ap = argparse.ArgumentParser()
	ap.add_argument("--events", required=True)
	ap.add_argument("--power", required=True)
	ap.add_argument("--out", default="energy_summary.csv")
	ap.add_argument("--out-events", default="",
	                help="per-event artifact (default: events_attributed.jsonl "
	                     "beside --events)")
	ap.add_argument("--power-meta", default="",
	                help="sampler metadata sidecar with per-counter RAPL "
	                     "max_energy_range_uj (default: <power>.meta.json if "
	                     "present)")
	args = ap.parse_args()

	out_events = args.out_events or os.path.join(
		os.path.dirname(os.path.abspath(args.events)), "events_attributed.jsonl")

	ts, gpus, rapls, gpu_energy_cols = load_power(args.power)
	rapl_ranges = load_power_meta(args.power_meta or power_meta_path(args.power))
	events = []
	for line in open(args.events):
		line = line.strip()
		if not line:
			continue
		try:
			events.append(json.loads(line))
		except json.JSONDecodeError:
			pass  # torn line from a crash; skip, don't die

	# Integrity gate: every event must lie inside the window during which
	# hardware was actually sampled. Checked BEFORE attribution so a broken
	# run cannot leave behind artifacts that look complete.
	# The failure is unconditional: there is no override. An event outside the
	# hardware timeline means the run is not a measurement, and the pipeline
	# must stop before any attributed or summary artifact is written.
	uncovered, considered = uncovered_events(events, ts)
	if uncovered:
		print(render_coverage_failure(uncovered, considered, ts), file=sys.stderr)
		return 1

	rows, gpu_src = attribute_events(events, ts, gpus, rapls, rapl_ranges)

	with open(out_events, "w") as f:
		for row in rows:
			f.write(json.dumps(row) + "\n")

	# Clock safety: the sampler and the event log stamp with the wall clock,
	# which can step backwards (e.g. WSL2 time sync). Reported, never repaired.
	steps = backward_steps(ts)
	n_reversed = sum(1 for row in rows if row.get("clock_anomaly"))
	if steps or n_reversed:
		largest = max((a - b for _, a, b in steps), default=0.0)
		print(f"WARNING: wall-clock anomaly: {len(steps)} backward step(s) in "
		      f"power.csv (largest {largest:.3f}s) and {n_reversed} event(s) with "
		      f"end < start. NVML counter energies are unaffected; affected "
		      f"trajectories are flagged clock_anomaly=true.", file=sys.stderr)

	pkg_cols, core_cols, dram_cols = classify_rapl(rapls)
	print(f"GPU energy source: {gpu_src['counter']} from NVML counter, "
	      f"{gpu_src['integrated']} integrated from power curve, "
	      f"{gpu_src['unavailable']} unavailable (null, not 0)")
	print(f"RAPL domains found: package={len(pkg_cols)} core={len(core_cols)} "
	      f"dram={len(dram_cols)}")
	if not pkg_cols and not dram_cols:
		print("NOTE: no RAPL package/dram counters in power.csv -> "
		      "cpu_package_energy_j, dram_energy_j and measured_energy_j are "
		      "null (not zero). GPU-only measurement for this run.")
	if core_cols:
		print("NOTE: RAPL core is reported as a diagnostic only and is NOT "
		      "added to measured_energy_j (it is contained within package).")
	no_range = sorted(c for c in rapls if rapl_ranges.get(c) is None)
	if no_range:
		print(f"NOTE: {len(no_range)} RAPL counter(s) without a known "
		      f"max_energy_range_uj -> a window in which such a counter "
		      f"decreases is null (unavailable), never unwrapped by guess.")

	by_label = defaultdict(lambda: defaultdict(float))
	by_type = defaultdict(lambda: defaultdict(float))
	by_q = defaultdict(lambda: defaultdict(float))
	for row in rows:
		_acc(by_label, row.get("operation_label") or row.get("label") or "", row)
		_acc(by_type, row.get("operation_type") or "", row)
		_acc(by_q, row.get("question_id") or "", row)

	fields = ("gpu_energy_j", "cpu_package_energy_j", "cpu_core_energy_j",
	          "dram_energy_j", "measured_energy_j")
	with open(args.out, "w", newline="") as f:
		w = csv.writer(f)
		w.writerow(["scope", "key", "n_events", "duration_s"] + list(fields))
		for scope, table in (("operation_type", by_type),
		                     ("operation_label", by_label),
		                     ("question", by_q)):
			for k, v in sorted(table.items()):
				w.writerow([scope, k, int(v["n"]), f"{v['duration_s']:.3f}"]
				           + [_fmt(v, field) for field in fields])

	# --- trajectory accounting ------------------------------------------
	# LIKE-FOR-LIKE INSTRUMENTS. Every trajectory-level energy reference is a
	# cumulative hardware counter differenced across the window, matching how
	# per-event energy is produced:
	#
	#   GPU          NVML gpu<i>_energy_mj      (same register the event log reads)
	#   CPU package  RAPL package cumulative uJ
	#   DRAM         RAPL dram cumulative uJ
	#
	# Integrated sampled power is NOT the reconciliation denominator. At ~10 Hz
	# it resolves fast inference power transients poorly and errs in both
	# directions, which inflated coverage above 1 on short trajectories. It is
	# retained only as the separately named diagnostic
	# sampled_gpu_energy_estimate_j, plus mean/peak power.
	#
	# When the GPU counter is absent, gpu_energy_j is reported as None and the
	# availability is stated explicitly -- never silently back-filled with the
	# sampled estimate.
	gpu_energy_p = prepare_counter_group(ts, gpu_energy_cols)
	pkg_p = prepare_rapl_group(ts, pkg_cols, rapl_ranges)
	dram_p = prepare_rapl_group(ts, dram_cols, rapl_ranges)
	gpu_power_p = [usable_samples(ts, w) for w in gpus.values()]

	def window_energy(t0, t1):
		if t0 is None or t1 is None:
			return None
		gpu_counter = sum_counter_prepared(gpu_energy_p, t0, t1, 1e3)
		pkg_c = sum_rapl_prepared(pkg_p, t0, t1)
		dram_c = sum_rapl_prepared(dram_p, t0, t1)

		# diagnostics from the sampled power curve
		sampled = None
		if gpus:
			parts = [integrate_prepared(s, t0, t1) for s in gpu_power_p]
			sampled = sum(parts) if all(p is not None for p in parts) else None
		mean_w = peak_w = None
		if gpus:
			lo = bisect.bisect_left(ts, t0)
			hi = bisect.bisect_right(ts, t1)
			window = [sum(vals[k] for vals in gpus.values())
			          for k in range(lo, max(lo, hi))
			          if all(vals[k] is not None for vals in gpus.values())]
			if window:
				mean_w = sum(window) / len(window)
				peak_w = max(window)

		return {
			"gpu_energy_j": gpu_counter,
			"cpu_package_energy_j": pkg_c,
			"dram_energy_j": dram_c,
			"measured_energy_j": measured_total(gpu_counter, pkg_c, dram_c),
			# --- diagnostics, never the reference ---
			"gpu_energy_source": ("nvml_cumulative_counter" if gpu_counter is not None
			                      else "unavailable"),
			"sampled_gpu_energy_estimate_j": sampled,
			"mean_gpu_power_w": mean_w,
			"peak_gpu_power_w": peak_w,
		}

	if ts and not gpu_energy_cols:
		print("NOTE: power.csv has no gpu*_energy_mj column -> trajectory GPU "
		      "energy is UNAVAILABLE (this log predates the cumulative GPU "
		      "counter). Integrated sampled power is reported only as the "
		      "diagnostic sampled_gpu_energy_estimate_j and is NOT substituted.")

	traj_rows = trajectory.accumulate(rows, window_energy=window_energy if ts else None,
	                                  clock_backsteps=[(a, b) for _, a, b in steps])
	traj_summary = trajectory.summarize(traj_rows, power_backward_steps=len(steps))
	traj_csv = os.path.join(os.path.dirname(os.path.abspath(out_events)),
	                        "trajectory_summary.csv")
	traj_json = os.path.join(os.path.dirname(os.path.abspath(out_events)),
	                         "trajectory_summary.json")
	trajectory.write_artifacts(traj_rows, traj_summary, traj_csv, traj_json)

	print(f"\n{len(rows)} events attributed")
	print(f"  per-event artifact -> {out_events}")
	print(f"  aggregate summary  -> {args.out}")
	print(f"  trajectory summary -> {traj_csv}")

	print("\n=== energy by operation_type ===")
	for k, v in sorted(by_type.items()):
		print(f"  {k:<10} n={int(v['n']):>6}  gpu={_fmt(v, 'gpu_energy_j'):>12}  "
		      f"pkg={_fmt(v, 'cpu_package_energy_j'):>10}  "
		      f"dram={_fmt(v, 'dram_energy_j'):>10}  "
		      f"measured={_fmt(v, 'measured_energy_j'):>12}")
	print("\n=== energy by operation_label ===")
	for k, v in sorted(by_label.items()):
		print(f"  {k:<22} n={int(v['n']):>6}  gpu={_fmt(v, 'gpu_energy_j'):>12}  "
		      f"pkg={_fmt(v, 'cpu_package_energy_j'):>10}  "
		      f"dram={_fmt(v, 'dram_energy_j'):>10}")

	print()
	print(trajectory.render(traj_rows, traj_summary))
	return 0


if __name__ == "__main__":
	sys.exit(main() or 0)
