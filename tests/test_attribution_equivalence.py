"""M1.5: the optimised attribution is numerically identical to the original.

The reference functions below are copied verbatim from the extraction commit
(69a7e24, src/agent_energy_profiler/attribution.py). Randomised timelines --
with empty cells, duplicate timestamps, backward clock steps and windows
outside, inside and across the timeline -- must give EXACTLY equal results
(==, no tolerance). The only intended difference is in rapl_delta's wrap
branch, which M1.2 replaced: there the original returned d + 2**32 uJ and the
corrected code returns None.

A structural test also pins the complexity fix: each column is filtered once
per attribution call, not once per event.
"""

import bisect
import random
import unittest
from unittest import mock

from agent_energy_profiler import attribution

# --- reference implementation (69a7e24), verbatim ---------------------------
RAPL_MAX = 2**32 * 1e0


def ref_integrate_gpu(ts, watts, t0, t1):
	if t0 is None or t1 is None or t1 <= t0:
		return None
	pts = [(t, w) for t, w in zip(ts, watts) if w is not None]
	if not pts:
		return None
	ts = [p[0] for p in pts]
	watts = [p[1] for p in pts]
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


def ref_rapl_delta(ts, uj, t0, t1):
	pts = [(t, v) for t, v in zip(ts, uj) if v is not None]
	if len(pts) < 2 or t1 <= t0:
		return None

	def value_at(t):
		tt = [p[0] for p in pts]
		i = bisect.bisect_left(tt, t)
		if i <= 0:
			return pts[0][1]
		if i >= len(pts):
			return pts[-1][1]
		(ta, va), (tb, vb) = pts[i - 1], pts[i]
		if vb < va:  # wraparound between samples
			vb += RAPL_MAX
		return va + (vb - va) * (t - ta) / (tb - ta)

	d = value_at(t1) - value_at(t0)
	if d < 0:
		d += RAPL_MAX
	return d / 1e6  # uJ -> J


def ref_counter_delta(ts, values, t0, t1, scale):
	pts = [(t, v) for t, v in zip(ts, values) if v is not None]
	if len(pts) < 2 or t1 <= t0:
		return None
	tt = [p[0] for p in pts]

	def value_at(t):
		i = bisect.bisect_left(tt, t)
		if i <= 0:
			return pts[0][1]
		if i >= len(pts):
			return pts[-1][1]
		(ta, va), (tb, vb) = pts[i - 1], pts[i]
		if tb == ta:
			return vb
		return va + (vb - va) * (t - ta) / (tb - ta)

	return (value_at(t1) - value_at(t0)) / scale
# -----------------------------------------------------------------------------


def timeline(rng, n, backsteps=True, dupes=True, gaps=0.1):
	ts, t = [], 100.0
	for _ in range(n):
		r = rng.random()
		if backsteps and r < 0.03:
			t -= rng.uniform(0.01, 0.3)
		elif dupes and r < 0.06:
			pass
		else:
			t += rng.uniform(0.05, 0.15)
		ts.append(round(t, 3))
	counter, acc = [], 0.0
	power = []
	for _ in range(n):
		acc += rng.uniform(0.0, 5e6)
		counter.append(None if rng.random() < gaps else float(int(acc)))
		power.append(None if rng.random() < gaps else round(rng.uniform(10, 300), 2))
	return ts, counter, power


def windows(rng, ts, k):
	lo, hi = min(ts) - 1.0, max(ts) + 1.0
	out = []
	for _ in range(k):
		a = rng.uniform(lo, hi)
		b = a + rng.choice((rng.uniform(-0.2, 0.0), rng.uniform(0.0, 0.05),
		                    rng.uniform(0.0, 2.0), rng.uniform(0.0, hi - lo)))
		out.append((a, b))
	# boundaries exactly on samples
	for _ in range(k // 4 if len(ts) >= 2 else 0):
		i, j = sorted(rng.sample(range(len(ts)), 2))
		out.append((ts[i], ts[j]))
	return out


class TestNumericalEquivalence(unittest.TestCase):

	def cases(self, dupes=True):
		rng = random.Random(1015)
		for trial in range(40):
			n = rng.choice((0, 1, 2, 3, 10, 200))
			ts, counter, power = timeline(rng, n, backsteps=trial % 2 == 0,
			                              dupes=dupes)
			if n == 0:
				yield ts, counter, power, [(0.0, 1.0), (1.0, 0.5)]
			else:
				yield ts, counter, power, windows(rng, ts, 60)

	def test_integrate_gpu_is_identical(self):
		for ts, _, power, wins in self.cases():
			series = attribution.usable_samples(ts, power)
			for t0, t1 in wins:
				ref = ref_integrate_gpu(ts, power, t0, t1)
				self.assertEqual(attribution.integrate_gpu(ts, power, t0, t1), ref)
				self.assertEqual(attribution.integrate_prepared(series, t0, t1), ref)

	def test_counter_delta_is_identical(self):
		for ts, counter, _, wins in self.cases():
			series = attribution.usable_samples(ts, counter)
			for t0, t1 in wins:
				ref = ref_counter_delta(ts, counter, t0, t1, 1e3)
				self.assertEqual(attribution.counter_delta(ts, counter, t0, t1, 1e3), ref)
				self.assertEqual(attribution.counter_delta_prepared(series, t0, t1, 1e3), ref)

	def test_rapl_delta_is_identical_outside_the_corrected_wrap_branch(self):
		checked = 0
		# no duplicate timestamps: the original divides by zero on them
		for ts, counter, _, wins in self.cases(dupes=False):
			if len(set(ts)) != len(ts):
				continue  # a backward step can still land on an earlier value
			series = attribution.prepare_rapl(ts, counter, None)
			for t0, t1 in wins:
				ref = ref_rapl_delta(ts, counter, t0, t1)
				new = attribution.rapl_delta_prepared(series, t0, t1)
				self.assertEqual(attribution.rapl_delta(ts, counter, t0, t1), new)
				if ref is not None and ref >= RAPL_MAX / 1e6 / 2:
					# original took its d < 0 -> +2**32 branch (a clock step)
					self.assertIsNone(new)
				else:
					self.assertEqual(new, ref)
					checked += 1
		self.assertGreater(checked, 1000)

	def test_group_sums_are_identical_when_every_column_is_present(self):
		rng = random.Random(7)
		ts, c0, _ = timeline(rng, 300, backsteps=False)
		_, c1, _ = timeline(rng, 300, backsteps=False)
		cols = {"a": c0, "b": c1}
		group = attribution.prepare_counter_group(ts, cols)
		for t0, t1 in windows(rng, ts, 200):
			parts = [ref_counter_delta(ts, c, t0, t1, 1e3) for c in (c0, c1)]
			ref = None if None in parts else sum(parts)
			self.assertEqual(attribution.sum_counter_domain(ts, cols, t0, t1, 1e3), ref)
			self.assertEqual(attribution.sum_counter_prepared(group, t0, t1, 1e3), ref)


class TestColumnsAreFilteredOncePerCall(unittest.TestCase):
	"""The O(events x samples) pattern was a per-event rebuild of each column's
	usable-sample list. Pin that it happens once per column instead."""

	def test_attribute_events_prepares_each_column_once(self):
		rng = random.Random(3)
		ts, counter, power = timeline(rng, 500, backsteps=False, gaps=0.0)
		rapls = {"rapl_package-0_x_uj": counter, "rapl_dram_x_uj": counter,
		         "rapl_core_x_uj": counter}
		gpus = {"gpu0_w": power, "gpu1_w": power}
		events = [{"start_timestamp": t0, "end_timestamp": t0 + 0.3,
		           "gpu_energy_j": None, "question_id": "q"}
		          for t0 in [ts[0] + 0.1 * k for k in range(300)]]
		real = attribution.usable_samples
		with mock.patch.object(attribution, "usable_samples",
		                       side_effect=real) as spy:
			rows, src = attribution.attribute_events(events, ts, gpus, rapls)
		self.assertEqual(len(rows), 300)
		self.assertEqual(src["integrated"], 300)
		self.assertEqual(spy.call_count, len(rapls) + len(gpus))


if __name__ == "__main__":
	unittest.main()
