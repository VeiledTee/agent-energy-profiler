"""Nested-span accounting for schema-v2 spans: inclusive vs exclusive.

Rules (docs/SCHEMA_V2.md section 4):

  * inclusive energy of a span is the device's energy over its window;
  * exclusive energy = inclusive - sum(direct children inclusive), reported
    only for a `sole` span (nothing outside its own subtree overlapped it)
    whose children are sequential, inside its window and measured;
  * summary totals add ROOT spans only, and only when roots do not overlap.

Nothing here allocates shared device energy between concurrent spans. Where
that would be needed the value is reported as not defensible instead.
"""

import bisect
import math
import sys
from collections import Counter, defaultdict

from . import measurement as m

_EPS = sys.float_info.epsilon


def _window(span):
	"""(start, end) on the monotonic clock, else wall clock; None if unusable."""
	s, e = span.get("start_monotonic_s"), span.get("end_monotonic_s")
	if s is None or e is None:
		s, e = span.get("start_unix_s"), span.get("end_unix_s")
	if s is None or e is None or e < s:
		return None
	return (s, e)


def _overlaps(a, b):
	return a[0] < b[1] and b[0] < a[1]


def _union_length(intervals, lo, hi):
	clipped = sorted((max(s, lo), min(e, hi)) for s, e in intervals)
	total, cur_s, cur_e = 0.0, None, None
	for s, e in clipped:
		if e <= s:
			continue
		if cur_e is None or s > cur_e:
			if cur_e is not None:
				total += cur_e - cur_s
			cur_s, cur_e = s, e
		else:
			cur_e = max(cur_e, e)
	if cur_e is not None:
		total += cur_e - cur_s
	return total


def _measured(span, domain):
	d = (span.get("energy") or {}).get(domain)
	return d if (d and d.get("status") == m.MEASURED) else None


def _raw_delta_mj(d):
	"""Per-device integer mJ deltas from in-band raw reads, or None."""
	raw = d.get("raw") or []
	if not raw or any(not isinstance(r, dict) or r.get("start_mj") is None
	                  or r.get("end_mj") is None for r in raw):
		return None
	return [r["end_mj"] - r["start_mj"] for r in raw]


def _exclusive_value(parent_d, child_ds):
	"""(energy_j, status, reason) for parent minus children of one domain."""
	p_raw = _raw_delta_mj(parent_d)
	c_raws = [_raw_delta_mj(c) for c in child_ds]
	if p_raw is not None and all(c is not None and len(c) == len(p_raw) for c in c_raws):
		# exact integer arithmetic on the counter's own unit
		per_device = [p - sum(c[i] for c in c_raws) for i, p in enumerate(p_raw)]
		if any(v < 0 for v in per_device):
			return None, m.INVALID, "negative_exclusive"
		return sum(per_device) / 1000.0, m.MEASURED, None
	parent = parent_d["energy_j"]
	children = [c["energy_j"] for c in child_ds]
	value = math.fsum([parent] + [-c for c in children])
	if value < 0:
		# Within floating round-off of the subtraction the result is
		# indistinguishable from zero; beyond it, the inputs disagree.
		bound = 4 * _EPS * (abs(parent) + math.fsum(abs(c) for c in children))
		if -value <= bound:
			return 0.0, m.MEASURED, None
		return None, m.INVALID, "negative_exclusive"
	return value, m.MEASURED, None


def annotate(spans):
	"""Copies of `spans` (v2 span dicts) each with an `accounting` object."""
	spans = [dict(s) for s in spans]
	by_id = {}
	for s in spans:
		if s.get("span_id") is not None:
			by_id.setdefault(s["span_id"], s)
	children = defaultdict(list)
	for s in spans:
		p = s.get("parent_span_id")
		if p is not None and p in by_id and by_id[p] is not s:
			children[p].append(s)

	windows = [_window(s) for s in spans]
	win_of = {id(s): w for s, w in zip(spans, windows)}
	valid = [w for w in windows if w is not None]
	starts = sorted(w[0] for w in valid)
	ends = sorted(w[1] for w in valid)
	zero_points = Counter(w[0] for w in valid if w[0] == w[1])

	def overlap_count(w):
		a = bisect.bisect_left(starts, w[1])          # s_j < e_i
		b = bisect.bisect_right(ends, w[0])           # e_j <= s_i
		z = zero_points[w[0]] if w[0] == w[1] else 0  # degenerate both-sides
		self_term = 1 if w[1] > w[0] else 0
		return a - (b - z) - self_term

	def depth_of(s):
		d, seen, cur = 0, set(), s
		while True:
			p = cur.get("parent_span_id")
			if p is None or p not in by_id or p in seen:
				return d
			seen.add(p)
			cur = by_id[p]
			d += 1

	def ancestors(s):
		out, seen, cur = [], set(), s
		while True:
			p = cur.get("parent_span_id")
			if p is None or p not in by_id or p in seen:
				return out
			seen.add(p)
			cur = by_id[p]
			out.append(cur)

	def descendants(s):
		out, stack = [], list(children.get(s.get("span_id"), []))
		while stack:
			c = stack.pop()
			out.append(c)
			stack.extend(children.get(c.get("span_id"), []))
		return out

	domains = sorted({k for s in spans for k in (s.get("energy") or {})})
	for s in spans:
		w = win_of[id(s)]
		kids = sorted(children.get(s.get("span_id"), []),
		              key=lambda c: (win_of[id(c)] or (math.inf, math.inf)))
		kid_windows = [win_of[id(c)] for c in kids]
		acc = {
			"depth": depth_of(s),
			"parent_missing": (s.get("parent_span_id") is not None
			                   and s.get("parent_span_id") not in by_id),
			"n_children": len(kids),
			"children_overlap": False,
			"concurrent_spans": None,
			"attribution": None,
			"inclusive_duration_s": s.get("duration_s"),
			"exclusive_duration_s": None,
			"exclusive": {},
		}
		if w is not None and all(kw is not None for kw in kid_windows):
			acc["children_overlap"] = any(kid_windows[i + 1][0] < kid_windows[i][1]
			                              for i in range(len(kid_windows) - 1))
			related = sum(1 for a in ancestors(s) if win_of[id(a)] and
			              _overlaps(win_of[id(a)], w))
			related += sum(1 for d in descendants(s) if win_of[id(d)] and
			               _overlaps(win_of[id(d)], w))
			acc["concurrent_spans"] = overlap_count(w) - related
			acc["exclusive_duration_s"] = (w[1] - w[0]) - _union_length(kid_windows, *w)
		other = bool(s.get("other_sessions_active"))
		sole = acc["concurrent_spans"] == 0 and not other
		acc["attribution"] = "sole" if sole else "shared"
		outside = w is not None and any(kw is not None and (kw[0] < w[0] or kw[1] > w[1])
		                                for kw in kid_windows)

		for domain in domains:
			if domain not in (s.get("energy") or {}):
				continue
			if acc["concurrent_spans"] is None:
				ex = (None, m.UNAVAILABLE, "clock_anomaly")
			elif other:
				ex = (None, m.NOT_DEFENSIBLE, "other_sessions_active")
			elif not sole:
				ex = (None, m.NOT_DEFENSIBLE, "concurrent_spans")
			elif acc["children_overlap"]:
				ex = (None, m.NOT_DEFENSIBLE, "children_overlap")
			elif outside:
				ex = (None, m.NOT_DEFENSIBLE, "child_outside_parent")
			elif _measured(s, domain) is None:
				ex = (None, m.UNAVAILABLE, "parent_unmeasured")
			elif any(_measured(c, domain) is None for c in kids):
				ex = (None, m.UNAVAILABLE, "child_unmeasured")
			else:
				ex = _exclusive_value(_measured(s, domain),
				                      [_measured(c, domain) for c in kids])
			acc["exclusive"][domain] = {"energy_j": ex[0], "status": ex[1],
			                            "reason": ex[2]}
		s["accounting"] = acc
	return spans


def summarize(spans, window=None):
	"""Session summary over ANNOTATED spans. Never adds a parent to its child.

	`window` is an optional {"start_unix_s", "end_unix_s", "domains": {domain:
	domain-measurement}} for the whole session window from an independent
	instrument (attribution_v2 supplies it from the sampler timeline).
	`window_minus_roots` is that window energy minus the root total: energy
	inside the session that no root span covers. Not clamped.
	"""
	roots = [s for s in spans if s["accounting"]["depth"] == 0]
	root_windows = sorted(w for w in (_window(s) for s in roots) if w is not None)
	root_overlap = any(root_windows[i + 1][0] < root_windows[i][1]
	                   for i in range(len(root_windows) - 1))
	edges = sorted([(w[0], 1) for w in root_windows] + [(w[1], -1) for w in root_windows],
	               key=lambda x: (x[0], x[1]))
	depth = peak = 0
	for _, delta in edges:
		depth += delta
		peak = max(peak, depth)

	domains = sorted({k for s in spans for k in (s.get("energy") or {})})
	totals = {}
	for domain in domains:
		if domain == "cpu_core":
			continue  # diagnostic, never a total
		if root_overlap:
			totals[domain] = {"energy_j": None, "reason": "concurrent_root_spans"}
			continue
		unmeasured = [r for r in roots if _measured(r, domain) is None]
		if unmeasured or not roots:
			totals[domain] = {"energy_j": None, "reason": "root_unmeasured",
			                  "n_roots_unmeasured": len(unmeasured)}
			continue
		totals[domain] = {"energy_j": math.fsum(_measured(r, domain)["energy_j"]
		                                        for r in roots), "reason": None}

	def below_counts(domain):
		c = Counter()
		for s in spans:
			d = (s.get("energy") or {}).get(domain)
			if not d:
				continue
			if d.get("status") != m.MEASURED:
				c[d.get("status")] += 1
			elif d.get("below_resolution") is True:
				c["measured_below_resolution"] += 1
			elif d.get("below_resolution") is False:
				c["measured"] += 1
			else:
				c["measured_resolution_unknown"] += 1
		return dict(sorted(c.items()))

	by_label = {}
	for s in spans:
		row = by_label.setdefault(s.get("name"), {"n": 0, "duration_s": 0.0,
		                                          "exclusive_duration_s": 0.0,
		                                          "exclusive": {}})
		row["n"] += 1
		row["duration_s"] += s.get("duration_s") or 0.0
		row["exclusive_duration_s"] += s["accounting"].get("exclusive_duration_s") or 0.0
		for domain, ex in s["accounting"]["exclusive"].items():
			slot = row["exclusive"].setdefault(domain, {"energy_j": 0.0, "n_included": 0,
			                                            "n_excluded": 0})
			if ex["status"] == m.MEASURED:
				slot["energy_j"] += ex["energy_j"]
				slot["n_included"] += 1
			else:
				slot["n_excluded"] += 1
	for row in by_label.values():
		for slot in row["exclusive"].values():
			if slot["n_included"] == 0:
				slot["energy_j"] = None

	sum_roots = {d: t["energy_j"] for d, t in totals.items()}
	unattributed = {}
	for domain, d in ((window or {}).get("domains") or {}).items():
		w_j = d.get("energy_j") if d.get("status") == m.MEASURED else None
		r_j = sum_roots.get(domain)
		unattributed[domain] = (w_j - r_j) if (w_j is not None and r_j is not None) else None

	return {
		"n_spans": len(spans),
		"n_roots": len(roots),
		"status_counts": dict(sorted(Counter(s.get("status") for s in spans).items())),
		"attribution_counts": dict(sorted(Counter(
			s["accounting"]["attribution"] for s in spans).items())),
		"root_spans_overlap": root_overlap,
		"max_root_concurrency": peak,
		"energy_status_counts": {d: below_counts(d) for d in domains},
		"sum_root_inclusive": totals,
		"window": window,
		"window_minus_roots": unattributed,
		"by_label": dict(sorted(by_label.items(), key=lambda kv: str(kv[0]))),
	}
