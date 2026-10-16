"""Nested-span accounting for schema-v2 spans: inclusive vs exclusive.

Rules (docs/SCHEMA_V2.md section 4):

  * inclusive energy of a span is the device's energy over its window;
  * exclusive energy = inclusive - sum(direct children inclusive), reported
    only for a `sole` span (nothing outside its own subtree overlapped it)
    whose children are sequential, inside its window and measured;
  * summary totals add ROOT spans only, and only when roots do not overlap.

Nothing here allocates shared device energy between concurrent spans. Where
that would be needed the value is reported as not defensible instead.

Memory (M3): accounting needs every span's window, ids and measured values,
but not the spans themselves. `Index` keeps only that compact projection, so
attribution can stream spans through (attribution_v2) instead of holding two
full copies of every span. `annotate` and `summarize` keep their M2 behaviour
and output byte for byte; they are thin wrappers over `Index` and
`SummaryBuilder`.
"""

import bisect
import math
import sys
from collections import Counter, defaultdict

from . import measurement as m
from ._compat import sum  # 3.12 float summation on every Python (see _compat)

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


def _raw_delta_mj(d):
	"""Per-device integer mJ deltas from in-band raw reads, or None."""
	raw = d.get("raw") or []
	if not raw or any(not isinstance(r, dict) or r.get("start_mj") is None
	                  or r.get("end_mj") is None for r in raw):
		return None
	return tuple(r["end_mj"] - r["start_mj"] for r in raw)


def _exclusive_value(parent, children):
	"""(energy_j, status, reason) for parent minus children of one domain.

	`parent` and each child are (energy_j, raw_deltas_or_None) of a measured
	domain (see compact()).
	"""
	p_raw = parent[1]
	c_raws = [c[1] for c in children]
	if p_raw is not None and all(c is not None and len(c) == len(p_raw) for c in c_raws):
		# exact integer arithmetic on the counter's own unit
		per_device = [p - sum(c[i] for c in c_raws) for i, p in enumerate(p_raw)]
		if any(v < 0 for v in per_device):
			return None, m.INVALID, "negative_exclusive"
		return sum(per_device) / 1000.0, m.MEASURED, None
	value_p = parent[0]
	values_c = [c[0] for c in children]
	value = math.fsum([value_p] + [-c for c in values_c])
	if value < 0:
		# Within floating round-off of the subtraction the result is
		# indistinguishable from zero; beyond it, the inputs disagree.
		bound = 4 * _EPS * (abs(value_p) + math.fsum(abs(c) for c in values_c))
		if -value <= bound:
			return 0.0, m.MEASURED, None
		return None, m.INVALID, "negative_exclusive"
	return value, m.MEASURED, None


class Node:
	"""What accounting needs from one span, and nothing else."""

	__slots__ = ("sid", "parent", "window", "duration", "other", "domains")

	def __init__(self, sid, parent, window, duration, other, domains):
		self.sid = sid
		self.parent = parent
		self.window = window
		self.duration = duration
		self.other = other
		# {domain: (energy_j, raw_deltas_or_None) if measured else None}
		self.domains = domains


def compact(span):
	"""A Node from a (attributed) v2 span dict."""
	domains = {}
	for name, d in (span.get("energy") or {}).items():
		name = sys.intern(name)
		if d and d.get("status") == m.MEASURED:
			domains[name] = (d["energy_j"], _raw_delta_mj(d))
		else:
			domains[name] = None
	return Node(span.get("span_id"), span.get("parent_span_id"), _window(span),
	            span.get("duration_s"), bool(span.get("other_sessions_active")),
	            domains)


class Index:
	"""Structural index over all spans of a file; `account(i)` per span."""

	def __init__(self, nodes):
		self.nodes = nodes
		by_id = {}
		for i, n in enumerate(nodes):
			if n.sid is not None:
				by_id.setdefault(n.sid, i)
		children = defaultdict(list)
		for i, n in enumerate(nodes):
			p = n.parent
			if p is not None and p in by_id and by_id[p] != i:
				children[p].append(i)
		self.by_id = by_id
		self.children = children
		valid = [n.window for n in nodes if n.window is not None]
		self.starts = sorted(w[0] for w in valid)
		self.ends = sorted(w[1] for w in valid)
		self.zero_points = Counter(w[0] for w in valid if w[0] == w[1])
		self.domains = sorted({k for n in nodes for k in n.domains})

	def __len__(self):
		return len(self.nodes)

	def _overlap_count(self, w):
		a = bisect.bisect_left(self.starts, w[1])                # s_j < e_i
		b = bisect.bisect_right(self.ends, w[0])                 # e_j <= s_i
		z = self.zero_points[w[0]] if w[0] == w[1] else 0         # degenerate both-sides
		self_term = 1 if w[1] > w[0] else 0
		return a - (b - z) - self_term

	def _ancestors(self, i):
		out, seen, cur = [], set(), self.nodes[i]
		while True:
			p = cur.parent
			if p is None or p not in self.by_id or p in seen:
				return out
			seen.add(p)
			j = self.by_id[p]
			cur = self.nodes[j]
			out.append(j)

	def _descendants(self, i):
		out, stack = [], list(self.children.get(self.nodes[i].sid, []))
		while stack:
			c = stack.pop()
			out.append(c)
			stack.extend(self.children.get(self.nodes[c].sid, []))
		return out

	def depth(self, i):
		return len(self._ancestors(i))

	def account(self, i):
		"""The `accounting` object of span i (docs/SCHEMA_V2.md section 4)."""
		nodes = self.nodes
		n = nodes[i]
		w = n.window
		kids = sorted(self.children.get(n.sid, []),
		              key=lambda c: (nodes[c].window or (math.inf, math.inf)))
		kid_windows = [nodes[c].window for c in kids]
		acc = {
			"depth": self.depth(i),
			"parent_missing": n.parent is not None and n.parent not in self.by_id,
			"n_children": len(kids),
			"children_overlap": False,
			"concurrent_spans": None,
			"attribution": None,
			"inclusive_duration_s": n.duration,
			"exclusive_duration_s": None,
			"exclusive": {},
		}
		if w is not None and all(kw is not None for kw in kid_windows):
			acc["children_overlap"] = any(kid_windows[k + 1][0] < kid_windows[k][1]
			                              for k in range(len(kid_windows) - 1))
			related = sum(1 for a in self._ancestors(i) if nodes[a].window and
			              _overlaps(nodes[a].window, w))
			related += sum(1 for d in self._descendants(i) if nodes[d].window and
			               _overlaps(nodes[d].window, w))
			acc["concurrent_spans"] = self._overlap_count(w) - related
			acc["exclusive_duration_s"] = (w[1] - w[0]) - _union_length(kid_windows, *w)
		other = n.other
		sole = acc["concurrent_spans"] == 0 and not other
		acc["attribution"] = "sole" if sole else "shared"
		outside = w is not None and any(kw is not None and (kw[0] < w[0] or kw[1] > w[1])
		                                for kw in kid_windows)

		for domain in self.domains:
			if domain not in n.domains:
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
			elif n.domains[domain] is None:
				ex = (None, m.UNAVAILABLE, "parent_unmeasured")
			elif any(nodes[c].domains.get(domain) is None for c in kids):
				ex = (None, m.UNAVAILABLE, "child_unmeasured")
			else:
				ex = _exclusive_value(n.domains[domain],
				                      [nodes[c].domains[domain] for c in kids])
			acc["exclusive"][domain] = {"energy_j": ex[0], "status": ex[1],
			                            "reason": ex[2]}
		return acc


def annotate(spans):
	"""Copies of `spans` (v2 span dicts) each with an `accounting` object."""
	spans = [dict(s) for s in spans]
	index = Index([compact(s) for s in spans])
	for i, s in enumerate(spans):
		s["accounting"] = index.account(i)
	return spans


class SummaryBuilder:
	"""Session summary built one annotated span at a time (span order)."""

	def __init__(self):
		self.n_spans = 0
		self.roots = []           # (window, {domain: measured tuple or None})
		self.domains = set()
		self.status = Counter()
		self.attribution = Counter()
		self.energy_counts = defaultdict(Counter)
		self.by_label = {}

	def add(self, span):
		acc = span["accounting"]
		energy = span.get("energy") or {}
		self.n_spans += 1
		self.domains.update(energy)
		self.status[span.get("status")] += 1
		self.attribution[acc["attribution"]] += 1
		if acc["depth"] == 0:
			self.roots.append((_window(span), compact(span).domains))
		for domain, d in energy.items():
			if not d:
				continue
			c = self.energy_counts[domain]
			if d.get("status") != m.MEASURED:
				c[d.get("status")] += 1
			elif d.get("below_resolution") is True:
				c["measured_below_resolution"] += 1
			elif d.get("below_resolution") is False:
				c["measured"] += 1
			else:
				c["measured_resolution_unknown"] += 1
		row = self.by_label.setdefault(span.get("name"), {"n": 0, "duration_s": 0.0,
		                                                  "exclusive_duration_s": 0.0,
		                                                  "exclusive": {}})
		row["n"] += 1
		row["duration_s"] += span.get("duration_s") or 0.0
		row["exclusive_duration_s"] += acc.get("exclusive_duration_s") or 0.0
		for domain, ex in acc["exclusive"].items():
			slot = row["exclusive"].setdefault(domain, {"energy_j": 0.0, "n_included": 0,
			                                            "n_excluded": 0})
			if ex["status"] == m.MEASURED:
				slot["energy_j"] += ex["energy_j"]
				slot["n_included"] += 1
			else:
				slot["n_excluded"] += 1

	def result(self, window=None):
		root_windows = sorted(w for w, _ in self.roots if w is not None)
		root_overlap = any(root_windows[i + 1][0] < root_windows[i][1]
		                   for i in range(len(root_windows) - 1))
		edges = sorted([(w[0], 1) for w in root_windows] + [(w[1], -1) for w in root_windows],
		               key=lambda x: (x[0], x[1]))
		depth = peak = 0
		for _, delta in edges:
			depth += delta
			peak = max(peak, depth)

		domains = sorted(self.domains)
		totals = {}
		for domain in domains:
			if domain == "cpu_core":
				continue  # diagnostic, never a total
			if root_overlap:
				totals[domain] = {"energy_j": None, "reason": "concurrent_root_spans"}
				continue
			unmeasured = [r for r in self.roots if r[1].get(domain) is None]
			if unmeasured or not self.roots:
				totals[domain] = {"energy_j": None, "reason": "root_unmeasured",
				                  "n_roots_unmeasured": len(unmeasured)}
				continue
			totals[domain] = {"energy_j": math.fsum(r[1][domain][0] for r in self.roots),
			                  "reason": None}

		by_label = {k: v for k, v in self.by_label.items()}
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
			"n_spans": self.n_spans,
			"n_roots": len(self.roots),
			"status_counts": dict(sorted(self.status.items())),
			"attribution_counts": dict(sorted(self.attribution.items())),
			"root_spans_overlap": root_overlap,
			"max_root_concurrency": peak,
			"energy_status_counts": {d: dict(sorted(self.energy_counts[d].items()))
			                         for d in domains},
			"sum_root_inclusive": totals,
			"window": window,
			"window_minus_roots": unattributed,
			"by_label": dict(sorted(by_label.items(), key=lambda kv: str(kv[0]))),
		}


def summarize(spans, window=None):
	"""Session summary over ANNOTATED spans. Never adds a parent to its child.

	`window` is an optional {"start_unix_s", "end_unix_s", "domains": {domain:
	domain-measurement}} for the whole session window from an independent
	instrument (attribution_v2 supplies it from the sampler timeline).
	`window_minus_roots` is that window energy minus the root total: energy
	inside the session that no root span covers. Not clamped.
	"""
	builder = SummaryBuilder()
	for span in spans:
		builder.add(span)
	return builder.result(window)
