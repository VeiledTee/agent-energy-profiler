"""M1.1: a multi-device aggregate is complete or it is null.

A group of counter columns (several GPUs, several RAPL sockets) sums to a
device total only when EVERY column in the group yields a delta. A partial sum
would silently represent a subset of the hardware as the whole, so it is
reported unavailable instead. A measured 0.0 is a reading, not a gap, and is
kept.
"""

import unittest

from agent_energy_profiler import attribution

TS = [0.0, 1.0, 2.0, 3.0]
GPU0 = [0.0, 1000.0, 2000.0, 3000.0]      # mJ, 1 W
GPU1 = [0.0, 2000.0, 4000.0, 6000.0]      # mJ, 2 W
FLAT = [5000.0, 5000.0, 5000.0, 5000.0]   # mJ, a counter that did not advance
MISSING = [None, None, None, None]
ONE_SAMPLE = [None, None, 1000.0, None]   # too few usable samples to difference

PKG0 = [0.0, 10e6, 20e6, 30e6]            # uJ, 10 W
PKG1 = [0.0, 5e6, 10e6, 15e6]             # uJ, 5 W


class TestMultiGpuCounterAggregation(unittest.TestCase):

	def total(self, *columns):
		cols = {f"gpu{i}_energy_mj": c for i, c in enumerate(columns)}
		return attribution.sum_counter_domain(TS, cols, 0.0, 2.0, 1e3)

	def test_complete_group_is_summed(self):
		self.assertAlmostEqual(self.total(GPU0, GPU1), 6.0)

	def test_single_device_is_unchanged(self):
		self.assertAlmostEqual(self.total(GPU0), 2.0)

	def test_one_missing_column_makes_the_total_unavailable(self):
		self.assertIsNone(self.total(GPU0, MISSING))
		self.assertIsNone(self.total(MISSING, GPU1))

	def test_too_few_samples_in_one_column_makes_the_total_unavailable(self):
		self.assertIsNone(self.total(GPU0, ONE_SAMPLE))

	def test_all_missing_is_unavailable(self):
		self.assertIsNone(self.total(MISSING, MISSING))

	def test_measured_zero_is_kept_and_not_mistaken_for_missing(self):
		self.assertEqual(self.total(FLAT), 0.0)
		self.assertAlmostEqual(self.total(GPU0, FLAT), 2.0)

	def test_measured_zero_does_not_rescue_a_missing_column(self):
		self.assertIsNone(self.total(FLAT, MISSING))

	def test_empty_group_is_unavailable(self):
		self.assertIsNone(attribution.sum_counter_domain(TS, {}, 0.0, 2.0, 1e3))


class TestMultiSocketRaplAggregation(unittest.TestCase):

	def total(self, *columns):
		cols = {f"rapl_package-{i}_intel-rapl:{i}_uj": c
		        for i, c in enumerate(columns)}
		return attribution.sum_domain(TS, cols, 0.0, 2.0)

	def test_complete_group_is_summed(self):
		self.assertAlmostEqual(self.total(PKG0, PKG1), 30.0)

	def test_single_socket_is_unchanged(self):
		self.assertAlmostEqual(self.total(PKG0), 20.0)

	def test_one_missing_socket_makes_the_total_unavailable(self):
		self.assertIsNone(self.total(PKG0, MISSING))
		self.assertIsNone(self.total(MISSING, PKG1))

	def test_too_few_samples_in_one_socket_makes_the_total_unavailable(self):
		self.assertIsNone(self.total(PKG0, ONE_SAMPLE))

	def test_measured_zero_is_kept(self):
		self.assertEqual(self.total([7e6] * 4), 0.0)
		self.assertAlmostEqual(self.total(PKG0, [7e6] * 4), 20.0)

	def test_empty_group_is_unavailable(self):
		self.assertIsNone(attribution.sum_domain(TS, {}, 0.0, 2.0))


class TestPartialGroupsReachTheEventBoundary(unittest.TestCase):
	"""A partial group must not leak into measured_energy_j either."""

	def event(self, gpu=7.0):
		return {"question_id": "q", "operation_label": "llm:reason",
		        "operation_type": "llm", "start_timestamp": 0.5,
		        "end_timestamp": 2.5, "duration_s": 2.0, "gpu_energy_j": gpu,
		        "status": "ok"}

	def test_missing_socket_nulls_package_and_total(self):
		rapl = {
			"rapl_package-0_intel-rapl:0_uj": PKG0,
			"rapl_package-1_intel-rapl:1_uj": MISSING,
			"rapl_dram_intel-rapl:0:2_uj": [0.0, 1e6, 2e6, 3e6],
		}
		row, = attribution.attribute_events([self.event()], TS, {}, rapl)[0]
		self.assertIsNone(row["cpu_package_energy_j"])
		self.assertAlmostEqual(row["dram_energy_j"], 2.0)
		self.assertIsNone(row["measured_energy_j"])
		self.assertEqual(row["available_energy_domains"], ["gpu", "dram"])
		self.assertFalse(row["measurement_complete"])

	def test_complete_sockets_still_measure(self):
		rapl = {
			"rapl_package-0_intel-rapl:0_uj": PKG0,
			"rapl_package-1_intel-rapl:1_uj": PKG1,
			"rapl_dram_intel-rapl:0:2_uj": [0.0, 1e6, 2e6, 3e6],
		}
		row, = attribution.attribute_events([self.event()], TS, {}, rapl)[0]
		self.assertAlmostEqual(row["cpu_package_energy_j"], 30.0)
		self.assertAlmostEqual(row["measured_energy_j"], 7.0 + 30.0 + 2.0)
		self.assertTrue(row["measurement_complete"])

	def test_power_integration_fallback_already_requires_every_gpu(self):
		"""Unchanged behaviour, pinned: one unreadable GPU power column nulls
		the integrated fallback rather than summing the readable one."""
		gpus = {"gpu0_w": [100.0] * 4, "gpu1_w": MISSING}
		row, = attribution.attribute_events([self.event(gpu=None)], TS, gpus, {})[0]
		self.assertIsNone(row["gpu_energy_j"])
		self.assertIsNone(row["gpu_energy_source"])


if __name__ == "__main__":
	unittest.main()
