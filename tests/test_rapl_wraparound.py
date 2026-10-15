"""M1.2: RAPL wraparound uses each counter's own max_energy_range_uj.

Synthetic only. These tests pin the arithmetic and the conservative policy;
they do not establish that the policy matches the behaviour of physical RAPL
hardware, which this project has not yet measured (docs/PROVENANCE.md).
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

from agent_energy_profiler import attribution
from agent_energy_profiler.hardware import rapl

R = 1_000_000_000      # a 1 kJ range, in uJ
R_SMALL = 400_000_000  # a different range on another socket
TS = [0.0, 1.0, 2.0, 3.0]
PKG = "rapl_package-0_intel-rapl:0_uj"
PKG1 = "rapl_package-1_intel-rapl:1_uj"
DRAM = "rapl_dram_intel-rapl:0:2_uj"


class TestRaplDelta(unittest.TestCase):

	def test_normal_delta(self):
		vals = [0.0, 10e6, 20e6, 30e6]
		self.assertAlmostEqual(attribution.rapl_delta(TS, vals, 0.0, 3.0, R), 30.0)
		self.assertAlmostEqual(attribution.rapl_delta(TS, vals, 0.5, 2.5, R), 20.0)

	def test_normal_delta_needs_no_metadata(self):
		vals = [0.0, 10e6, 20e6, 30e6]
		self.assertAlmostEqual(attribution.rapl_delta(TS, vals, 0.5, 2.5), 20.0)

	def test_one_wrap_uses_the_declared_range(self):
		# 10 J per second; wraps between t=1 and t=2.
		vals = [R - 15e6, R - 5e6, 5e6, 15e6]
		self.assertAlmostEqual(attribution.rapl_delta(TS, vals, 0.0, 3.0, R), 30.0)
		# window entirely inside the wrapping interval
		self.assertAlmostEqual(attribution.rapl_delta(TS, vals, 1.25, 1.75, R), 5.0)
		# window starting before and ending after the wrap
		self.assertAlmostEqual(attribution.rapl_delta(TS, vals, 0.5, 2.5, R), 20.0)

	def test_two_wraps_in_separate_intervals(self):
		ts = [0.0, 1.0, 2.0, 3.0, 4.0]
		# increments: 10 J (wrap), 395 J, 400 J, 300 J (wrap); each < R/2
		vals = [R - 5e6, 5e6, 0.4 * R, 0.8 * R, 0.1 * R]
		self.assertAlmostEqual(attribution.rapl_delta(ts, vals, 0.0, 4.0, R), 1105.0)

	def test_ranges_differ_per_counter(self):
		# The same raw readings mean different energy under different ranges.
		vals_small = [R_SMALL - 10e6, 0.0, 10e6, 20e6]
		vals_large = [R - 10e6, 0.0, 10e6, 20e6]
		self.assertAlmostEqual(
			attribution.rapl_delta(TS, vals_small, 0.0, 3.0, R_SMALL), 30.0)
		self.assertAlmostEqual(
			attribution.rapl_delta(TS, vals_large, 0.0, 3.0, R), 30.0)
		# The old hardcoded 2**32 uJ assumption would have read 4294.967 J of
		# wrap here for either counter.
		cols = {PKG: vals_small, PKG1: vals_large}
		self.assertAlmostEqual(
			attribution.sum_domain(TS, cols, 0.0, 3.0, {PKG: R_SMALL, PKG1: R}), 60.0)

	def test_missing_range_makes_a_spanning_window_unavailable(self):
		vals = [R - 15e6, R - 5e6, 5e6, 15e6]
		self.assertIsNone(attribution.rapl_delta(TS, vals, 0.0, 3.0))
		self.assertIsNone(attribution.rapl_delta(TS, vals, 0.0, 3.0, None))

	def test_missing_range_leaves_windows_away_from_the_decrease_measured(self):
		vals = [R - 15e6, R - 5e6, 5e6, 15e6]
		self.assertAlmostEqual(attribution.rapl_delta(TS, vals, 0.0, 1.0), 10.0)
		self.assertAlmostEqual(attribution.rapl_delta(TS, vals, 2.0, 3.0), 10.0)

	def test_invalid_range_is_treated_as_missing(self):
		vals = [R - 15e6, R - 5e6, 5e6, 15e6]
		for bad in (0, -R, "1000", True, float("nan")):
			with self.subTest(bad=bad):
				self.assertIsNone(attribution.rapl_delta(TS, vals, 0.0, 3.0, bad))

	def test_reading_above_the_declared_range_is_not_unwrapped(self):
		# The metadata cannot describe this counter: it read past its "max".
		vals = [R + 5e6, R + 15e6, 5e6, 15e6]
		self.assertIsNone(attribution.rapl_delta(TS, vals, 0.0, 3.0, R))

	def test_counter_reset_is_unavailable_not_invented(self):
		# Drop from below mid-range to near zero: a reset, not a wrap.
		# Unwrapping it would invent ~600 J in one interval.
		vals = [0.4 * R, 0.4 * R + 10e6, 1e6, 11e6]
		self.assertIsNone(attribution.rapl_delta(TS, vals, 0.0, 3.0, R))
		self.assertAlmostEqual(attribution.rapl_delta(TS, vals, 2.0, 3.0, R), 10.0)
		self.assertAlmostEqual(attribution.rapl_delta(TS, vals, 0.0, 1.0, R), 10.0)

	def test_reset_from_the_upper_half_is_indistinguishable_from_a_wrap(self):
		"""Documented limit of the policy, pinned so it cannot change silently:
		a reset from above mid-range reads exactly like a wrap."""
		vals = [0.9 * R, 0.9 * R + 10e6, 1e6, 11e6]
		self.assertAlmostEqual(attribution.rapl_delta(TS, vals, 1.0, 2.0, R),
		                       (0.1 * R - 10e6 + 1e6) / 1e6)

	def test_ambiguous_possible_multiple_wraps_are_unavailable(self):
		# Implied single-wrap increment is ~0.9 R in one interval. That many
		# Joules in one sample period is exactly when a second wrap could hide.
		vals = [0.1 * R, 0.2 * R, 0.1 * R, 0.2 * R]
		self.assertIsNone(attribution.rapl_delta(TS, vals, 0.0, 3.0, R))

	def test_a_clock_step_never_yields_negative_or_wrapped_energy(self):
		# Timeline stepped backwards mid-run; the window lookup is unreliable
		# there. The old code turned any negative difference into +4294.967 J.
		ts = [0.0, 1.0, 2.0, 1.5, 3.0, 4.0]
		vals = [0.0, 10e6, 20e6, 30e6, 40e6, 50e6]
		grid = [0.05 * k for k in range(0, 85)]
		for t0 in grid:
			for t1 in grid:
				if t1 <= t0:
					continue
				for max_range in (None, R):
					d = attribution.rapl_delta(ts, vals, t0, t1, max_range)
					self.assertTrue(d is None or 0.0 <= d <= 50.0, (t0, t1, d))

	def test_duplicate_timestamps_do_not_divide_by_zero(self):
		ts = [0.0, 1.0, 1.0, 2.0]
		vals = [0.0, 10e6, 10e6, 20e6]
		self.assertAlmostEqual(attribution.rapl_delta(ts, vals, 0.0, 2.0, R), 20.0)

	def test_too_few_samples_is_unavailable(self):
		self.assertIsNone(attribution.rapl_delta([0.0], [1.0], 0.0, 1.0, R))
		self.assertIsNone(attribution.rapl_delta(TS, [None, None, 5.0, None], 0.0, 3.0, R))


class TestUnwrapCounter(unittest.TestCase):

	def test_no_decrease_is_returned_unchanged(self):
		vals = [1.5, 2.5, 3.5]
		unwrapped, seg = attribution.unwrap_counter(vals, None)
		self.assertEqual(unwrapped, vals)
		self.assertEqual(seg, [0, 0, 0])
		for a, b in zip(unwrapped, vals):
			self.assertIs(type(a), type(b))

	def test_wrap_adds_one_range(self):
		unwrapped, seg = attribution.unwrap_counter([R - 2.0, 3.0, 8.0], R)
		self.assertEqual(unwrapped, [R - 2.0, R + 3.0, R + 8.0])
		self.assertEqual(seg, [0, 0, 0])

	def test_unresolvable_decrease_starts_a_segment(self):
		_, seg = attribution.unwrap_counter([R - 2.0, 3.0, 8.0], None)
		self.assertEqual(seg, [0, 1, 1])


class TestPowerMetaSidecar(unittest.TestCase):

	def write(self, d, payload):
		path = os.path.join(d, "power.csv.meta.json")
		with open(path, "w") as f:
			f.write(payload if isinstance(payload, str) else json.dumps(payload))
		return path

	def test_valid_sidecar(self):
		with tempfile.TemporaryDirectory() as d:
			path = self.write(d, {"format": attribution.POWER_META_FORMAT,
			                      "rapl_max_energy_range_uj": {PKG: R, DRAM: None}})
			self.assertEqual(attribution.load_power_meta(path), {PKG: R, DRAM: None})

	def test_absent_or_malformed_sidecar_means_unknown_ranges(self):
		with tempfile.TemporaryDirectory() as d:
			self.assertEqual(attribution.load_power_meta(os.path.join(d, "nope")), {})
			self.assertEqual(attribution.load_power_meta(self.write(d, "{not json")), {})
			self.assertEqual(attribution.load_power_meta(
				self.write(d, {"rapl_max_energy_range_uj": {PKG: R}})), {})
			self.assertEqual(attribution.load_power_meta(
				self.write(d, {"format": attribution.POWER_META_FORMAT,
				               "rapl_max_energy_range_uj": {PKG: "big", DRAM: -1}})),
				{PKG: None, DRAM: None})

	def test_power_meta_path_is_beside_the_power_log(self):
		self.assertEqual(attribution.power_meta_path("/x/power.csv"),
		                 "/x/power.csv.meta.json")


class TestRaplRangeDiscovery(unittest.TestCase):

	def test_reads_the_sibling_max_energy_range_file(self):
		with tempfile.TemporaryDirectory() as d:
			zone = os.path.join(d, "intel-rapl:0")
			os.makedirs(zone)
			for name, value in (("energy_uj", "123"), ("name", "package-0"),
			                    ("max_energy_range_uj", "262143328850\n")):
				with open(os.path.join(zone, name), "w") as f:
					f.write(value)
			path = os.path.join(zone, "energy_uj")
			self.assertEqual(rapl.max_energy_range_uj(path), 262143328850)
			self.assertEqual(rapl.column_ranges([path]),
			                 {"rapl_package-0_intel-rapl:0_uj": 262143328850})

	def test_missing_or_invalid_range_file_is_none(self):
		with tempfile.TemporaryDirectory() as d:
			path = os.path.join(d, "energy_uj")
			self.assertIsNone(rapl.max_energy_range_uj(path))
			for bad in ("", "abc", "0", "-5"):
				with open(os.path.join(d, "max_energy_range_uj"), "w") as f:
					f.write(bad)
				self.assertIsNone(rapl.max_energy_range_uj(path), bad)


class TestEndToEndWrap(unittest.TestCase):
	"""The CLI picks the sidecar up beside power.csv by default."""

	def run_cli(self, d, with_meta):
		power = os.path.join(d, "power.csv")
		with open(power, "w") as f:
			f.write(f"t,gpu0_energy_mj,{PKG},{DRAM}\n")
			pkg = [R - 15e6, R - 5e6, 5e6, 15e6]
			dram = [0, 1e6, 2e6, 3e6]
			for i, t in enumerate(TS):
				f.write(f"{t:.3f},{1000 * i},{int(pkg[i])},{int(dram[i])}\n")
		if with_meta:
			with open(attribution.power_meta_path(power), "w") as f:
				json.dump({"format": attribution.POWER_META_FORMAT,
				           "rapl_max_energy_range_uj": {PKG: R, DRAM: R}}, f)
		events = os.path.join(d, "events.jsonl")
		with open(events, "w") as f:
			f.write(json.dumps({"question_id": "q", "operation_label": "llm:x",
			                    "start_timestamp": 0.0, "end_timestamp": 3.0,
			                    "duration_s": 3.0, "gpu_energy_j": 3.0,
			                    "status": "ok"}) + "\n")
		out = subprocess.run(
			[sys.executable, "-m", "agent_energy_profiler.attribution",
			 "--events", events, "--power", power,
			 "--out", os.path.join(d, "energy_summary.csv"),
			 "--out-events", os.path.join(d, "events_attributed.jsonl")],
			capture_output=True, text=True, timeout=120)
		self.assertEqual(out.returncode, 0, out.stderr)
		with open(os.path.join(d, "events_attributed.jsonl")) as f:
			row = json.loads(f.readline())
		with open(os.path.join(d, "trajectory_summary.json")) as f:
			traj = json.load(f)["trajectories"][0]
		return row, traj, out.stdout

	def test_with_sidecar_the_wrap_is_measured(self):
		with tempfile.TemporaryDirectory() as d:
			row, traj, _ = self.run_cli(d, with_meta=True)
		self.assertAlmostEqual(row["cpu_package_energy_j"], 30.0)
		self.assertAlmostEqual(row["measured_energy_j"], 3.0 + 30.0 + 3.0)
		self.assertAlmostEqual(traj["trajectory_cpu_package_energy_j"], 30.0)

	def test_without_sidecar_the_wrap_is_unavailable(self):
		with tempfile.TemporaryDirectory() as d:
			row, traj, stdout = self.run_cli(d, with_meta=False)
		self.assertIsNone(row["cpu_package_energy_j"])
		self.assertAlmostEqual(row["dram_energy_j"], 3.0)
		self.assertIsNone(row["measured_energy_j"])
		self.assertFalse(row["measurement_complete"])
		self.assertIsNone(traj["trajectory_cpu_package_energy_j"])
		self.assertIn("max_energy_range_uj", stdout)


if __name__ == "__main__":
	unittest.main()
