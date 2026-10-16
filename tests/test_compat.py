"""0.3.0: identical float summation on every supported Python.

Python 3.12 made sum() of floats compensated; the reference artifacts were
produced on 3.12. agent_energy_profiler._compat.sum is the built-in on 3.12
and 3.13 and a replica of 3.12's algorithm elsewhere (3.9-3.11, and 3.14,
which changed sum() again).
"""

import builtins
import math
import random
import sys
import unittest

from agent_energy_profiler import _compat
from agent_energy_profiler._compat import neumaier_sum

SAME_AS_312 = (3, 12) <= sys.version_info < (3, 14)


def same(a, b):
	if type(a) is not type(b):
		return False
	if isinstance(a, float):
		if math.isnan(a):
			return math.isnan(b)
		return a == b and math.copysign(1, a) == math.copysign(1, b)
	return a == b


class TestCompatSum(unittest.TestCase):

	def test_builtin_where_it_already_is_312s(self):
		self.assertIs(_compat.sum is builtins.sum, SAME_AS_312)

	def test_known_312_results_on_every_version(self):
		cases = [
			([0.1] * 10, 1.0),                      # naive: 0.9999999999999999
			([1e16, 1.0, -1e16], 1.0),              # naive: 0.0
			([0.1, 0.2, 0.3], 0.6),                 # naive: 0.6000000000000001
			([1, 2, 3], 6),
			([], 0),
			([-0.0], 0.0),                          # int start 0 + -0.0
			([1e308, 1e308, -1e308], math.inf),
		]
		for items, want in cases:
			self.assertTrue(same(_compat.sum(items), want), (items, _compat.sum(items)))
			self.assertTrue(same(neumaier_sum(items), want), items)
		self.assertTrue(same(neumaier_sum([-0.0], -0.0), -0.0))
		self.assertTrue(same(neumaier_sum([0.5, 2**70, 0.25]), 2.0**70))

	@unittest.skipUnless(SAME_AS_312, "compares with the built-in sum of 3.12/3.13")
	def test_replica_matches_the_builtin(self):
		rng = random.Random(7)

		def item():
			k = rng.random()
			if k < 0.6:
				return rng.uniform(-1, 1) * 10.0 ** rng.choice([-30, -3, 0, 6, 16, 300])
			if k < 0.8:
				return rng.randint(-10**6, 10**6)
			if k < 0.85:
				return rng.choice([True, False, 2**63 + 1, -2**64])
			return rng.choice([0.0, -0.0, 0.1, 1e16, math.inf, -math.inf, 1e308])

		for n in range(30_000):
			items = [item() for _ in range(rng.randint(0, 10))]
			start = (0, 0.0, -0.0, 3)[n % 4]
			try:
				want = builtins.sum(items, start)
			except OverflowError:
				with self.assertRaises(OverflowError):
					neumaier_sum(items, start)
				continue
			self.assertTrue(same(neumaier_sum(items, start), want), (start, items))


if __name__ == "__main__":
	unittest.main()
