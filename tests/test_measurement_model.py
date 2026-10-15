"""M2: resolution awareness and in-band GPU counter validity (synthetic)."""

import json
import os
import tempfile
import unittest

from agent_energy_profiler import measurement as m

RES = m.Resolution.configured(0.1006, "synthetic test value")


class TestResolution(unittest.TestCase):

	def test_short_span_is_below_resolution_and_keeps_its_value(self):
		d = m.gpu_in_band([1000], [1150], 0.03, RES)
		self.assertEqual(d["status"], "measured")
		self.assertEqual(d["energy_j"], 0.15)
		self.assertTrue(d["below_resolution"])
		self.assertAlmostEqual(d["update_intervals"], 0.03 / 0.1006)

	def test_long_span_is_not_below_resolution(self):
		d = m.gpu_in_band([1000], [61000], 2.0, RES)
		self.assertEqual(d["energy_j"], 60.0)
		self.assertFalse(d["below_resolution"])
		self.assertAlmostEqual(d["update_intervals"], 2.0 / 0.1006)

	def test_zero_delta_is_a_measured_zero_not_missing(self):
		d = m.gpu_in_band([5000], [5000], 0.01, RES)
		self.assertEqual(d["status"], "measured")
		self.assertEqual(d["energy_j"], 0.0)
		self.assertIsNotNone(d["energy_j"])
		self.assertTrue(d["below_resolution"])

	def test_zero_delta_on_a_long_span_is_still_reported_as_zero(self):
		d = m.gpu_in_band([5000], [5000], 5.0, RES)
		self.assertEqual(d["energy_j"], 0.0)
		self.assertFalse(d["below_resolution"])

	def test_missing_reading_is_unavailable_never_zero(self):
		for start, end, reason in (([None], [100], "read_failed"),
		                           ([100], [None], "read_failed"),
		                           (None, None, "no_counter")):
			d = m.gpu_in_band(start, end, 1.0, RES)
			self.assertEqual(d["status"], "unavailable")
			self.assertIsNone(d["energy_j"])
			self.assertEqual(d["reason"], reason)
			self.assertIsNone(d["below_resolution"])

	def test_unknown_resolution_is_explicit(self):
		d = m.gpu_in_band([0], [100], 0.001, m.Resolution.unknown())
		self.assertEqual(d["energy_j"], 0.1)
		self.assertIsNone(d["below_resolution"])
		self.assertIsNone(d["update_intervals"])
		self.assertEqual(m.Resolution.unknown().to_dict()["basis"], "unknown")

	def test_interval_is_not_a_guaranteed_floor(self):
		"""A just-longer span is not 'resolved': the ratio is reported so
		analysis can choose its own cut-off."""
		d = m.gpu_in_band([0], [0], 0.11, RES)
		self.assertFalse(d["below_resolution"])
		self.assertLess(d["update_intervals"], 1.1)

	def test_resolution_records_how_it_was_established(self):
		r = RES.to_dict()
		self.assertEqual(r["basis"], "configured")
		self.assertEqual(r["source"], "synthetic test value")
		self.assertIsNone(r["rapl_update_interval_s"])
		self.assertEqual(m.Resolution.from_dict(r).to_dict(), r)

	def test_configured_resolution_requires_a_source(self):
		with self.assertRaises(ValueError):
			m.Resolution.configured(0.1, "")
		for bad in (0, -1, float("nan"), True, "0.1"):
			with self.assertRaises(ValueError):
				m.Resolution(bad, "configured", "x")
		with self.assertRaises(ValueError):
			m.Resolution(None, "configured", "x")

	def test_from_hardware_report(self):
		report = {"generated_at": 123.0,
		          "gpu_counter_resolution": {"status": "SUPPORTED",
		                                     "median_update_interval_s": 0.106,
		                                     "updates_observed": 80}}
		with tempfile.TemporaryDirectory() as d:
			path = os.path.join(d, "hw.json")
			with open(path, "w") as f:
				json.dump(report, f)
			r = m.Resolution.from_hardware_report(path)
		self.assertEqual(r.gpu_update_interval_s, 0.106)
		self.assertEqual(r.basis, "hardware_report")
		self.assertEqual(r.source, path)
		self.assertEqual(r.updates_observed, 80)
		self.assertEqual(r.characterized_at_unix_s, 123.0)

	def test_report_without_resolution_gives_unknown(self):
		for res in ({"status": "UNAVAILABLE"}, {"status": "UNKNOWN"}, {}):
			r = m.Resolution.from_hardware_report({"gpu_counter_resolution": res})
			self.assertEqual(r.basis, "unknown")
			self.assertIsNone(r.gpu_update_interval_s)


class TestInBandCounterValidity(unittest.TestCase):

	def test_counter_decrease_is_invalid_and_raw_is_preserved(self):
		d = m.gpu_in_band([90_000], [1_000], 1.0, RES)
		self.assertEqual(d["status"], "invalid")
		self.assertEqual(d["reason"], "counter_decrease")
		self.assertIsNone(d["energy_j"])
		self.assertEqual(d["raw"], [{"device": 0, "start_mj": 90_000, "end_mj": 1_000}])

	def test_one_decreasing_device_invalidates_the_domain(self):
		"""A sum across devices could hide a reset on one of them."""
		d = m.gpu_in_band([1000, 90_000], [50_000, 1_000], 1.0, RES)
		self.assertEqual(d["status"], "invalid")
		self.assertEqual(d["reason"], "counter_decrease")

	def test_multi_device_sum(self):
		d = m.gpu_in_band([1000, 2000], [3000, 5000], 1.0, RES)
		self.assertEqual(d["energy_j"], 5.0)
		self.assertEqual(len(d["raw"]), 2)

	def test_one_failed_device_read_makes_the_domain_unavailable(self):
		d = m.gpu_in_band([1000, None], [3000, 5000], 1.0, RES)
		self.assertEqual(d["status"], "unavailable")
		self.assertEqual(d["reason"], "read_failed")

	def test_device_count_change_is_invalid(self):
		d = m.gpu_in_band([1000], [3000, 5000], 1.0, RES)
		self.assertEqual(d["status"], "invalid")
		self.assertEqual(d["reason"], "devices_changed")

	def test_no_devices_is_unavailable(self):
		d = m.gpu_in_band([], [], 1.0, RES)
		self.assertEqual(d["status"], "unavailable")
		self.assertEqual(d["reason"], "no_counter")

	def test_every_record_is_device_scope(self):
		for d in (m.gpu_in_band([0], [1], 1.0, RES), m.unavailable_gpu("disabled")):
			self.assertEqual(d["scope"], "device")


if __name__ == "__main__":
	unittest.main()
