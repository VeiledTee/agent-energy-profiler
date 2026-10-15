"""Clock safety, git provenance and the CPU/DRAM regression guard.

Ported from Chain-of-Relations-Emissions tests/test_profiler_hardening.py
(GitProvenance, ClockSafety, and the profiler half of CpuDramPreserved). The
source file's preflight, model-revision, measured-run and param.json tests
exercise research tooling and stay in the research repository.
"""

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from agent_energy_profiler import attribution, trajectory
from agent_energy_profiler import events as profiler_events


class TempDirCase(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.mkdtemp()
		self.proc = os.path.join(self.tmp, "proc")
		os.makedirs(self.proc)

	def tearDown(self):
		shutil.rmtree(self.tmp, ignore_errors=True)


# ==========================================================================
# 1. code provenance
# ==========================================================================

class GitProvenance(TempDirCase):

	def git(self, *args):
		subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
		               cwd=self.repo, check=True, capture_output=True)

	def setUp(self):
		super().setUp()
		self.repo = os.path.join(self.tmp, "repo")
		os.makedirs(self.repo)
		self.git("init", "-q")
		with open(os.path.join(self.repo, "a.txt"), "w") as f:
			f.write("a\n")
		self.git("add", "a.txt")
		self.git("commit", "-q", "-m", "init")

	def test_clean_tree_is_not_dirty(self):
		self.assertIs(profiler_events.detect_git_dirty(self.repo), False)
		self.assertTrue(profiler_events.detect_git_commit(self.repo))

	def test_modified_tracked_file_is_dirty(self):
		with open(os.path.join(self.repo, "a.txt"), "a") as f:
			f.write("b\n")
		self.assertIs(profiler_events.detect_git_dirty(self.repo), True)

	def test_untracked_file_is_dirty(self):
		open(os.path.join(self.repo, "new.py"), "w").close()
		self.assertIs(profiler_events.detect_git_dirty(self.repo), True)

	def test_ignored_file_is_not_dirty(self):
		with open(os.path.join(self.repo, ".gitignore"), "w") as f:
			f.write("results/\n")
		self.git("add", ".gitignore")
		self.git("commit", "-q", "-m", "ignore")
		os.makedirs(os.path.join(self.repo, "results"))
		open(os.path.join(self.repo, "results", "x.json"), "w").close()
		self.assertIs(profiler_events.detect_git_dirty(self.repo), False)

	def test_not_a_repository_is_unknown_not_clean(self):
		self.assertIsNone(profiler_events.detect_git_dirty(self.tmp + "/proc"))




class ClockSafety(unittest.TestCase):
	TS = [0.0, 1.0, 2.0, 3.0, 4.0]
	W = [100.0, 100.0, 100.0, 100.0, 100.0]

	def event(self, t0, t1, gpu=None, q="q1", label="llm:reason"):
		return {"question_id": q, "operation_label": label, "operation_type": label.split(":")[0],
		        "start_timestamp": t0, "end_timestamp": t1, "duration_s": t1 - t0,
		        "gpu_energy_j": gpu, "status": "ok"}

	def test_reversed_and_empty_windows_integrate_to_null_not_zero(self):
		self.assertIsNone(attribution.integrate_gpu(self.TS, self.W, 2.5, 1.5))
		self.assertIsNone(attribution.integrate_gpu(self.TS, self.W, 2.0, 2.0))
		self.assertIsNone(attribution.integrate_gpu([], [], 1.0, 2.0))
		self.assertIsNone(attribution.integrate_gpu(self.TS, [None] * 5, 1.0, 2.0))

	def test_normal_window_integrates_as_before(self):
		self.assertAlmostEqual(attribution.integrate_gpu(self.TS, self.W, 0.5, 3.5), 300.0)
		# a missing sample is skipped, not read as 0 W
		self.assertAlmostEqual(
			attribution.integrate_gpu(self.TS, [100.0, None, 100.0, 100.0, 100.0], 0.5, 3.5), 300.0)

	def test_reversed_event_without_counter_is_null_and_flagged(self):
		rows, src = attribution.attribute_events([self.event(2.5, 1.5)], self.TS, {"gpu0_w": self.W}, {})
		self.assertIsNone(rows[0]["gpu_energy_j"])
		self.assertIsNone(rows[0]["gpu_energy_source"])
		self.assertTrue(rows[0]["clock_anomaly"])
		self.assertEqual(src["unavailable"], 1)

	def test_reversed_event_keeps_valid_counter_energy(self):
		rows, _ = attribution.attribute_events([self.event(2.5, 1.5, gpu=42.0)], self.TS,
		                                       {"gpu0_w": self.W}, {})
		self.assertEqual(rows[0]["gpu_energy_j"], 42.0)
		self.assertEqual(rows[0]["gpu_energy_source"], "nvml_counter")
		self.assertTrue(rows[0]["clock_anomaly"])

	def test_normal_event_behaviour_unchanged(self):
		rows, src = attribution.attribute_events([self.event(0.5, 3.5)], self.TS, {"gpu0_w": self.W}, {})
		self.assertAlmostEqual(rows[0]["gpu_energy_j"], 300.0)
		self.assertEqual(rows[0]["gpu_energy_source"], "power_integration")
		self.assertFalse(rows[0]["clock_anomaly"])
		self.assertEqual(src, {"counter": 0, "integrated": 1, "unavailable": 0})

	def test_backward_power_steps_detected_not_repaired(self):
		ts = [0.0, 1.0, 0.6, 2.0]
		self.assertEqual(attribution.backward_steps(ts), [(1, 1.0, 0.6)])
		self.assertEqual(ts, [0.0, 1.0, 0.6, 2.0])
		self.assertEqual(attribution.backward_steps(self.TS), [])

	def test_trajectory_flags_backstep_inside_window_only(self):
		events = [self.event(0.0, 1.0, gpu=1.0, q="a"), self.event(5.0, 6.0, gpu=1.0, q="b")]
		rows = {r["question_id"]: r for r in trajectory.accumulate(events, clock_backsteps=[(0.8, 0.5)])}
		self.assertTrue(rows["a"]["clock_anomaly"])
		self.assertEqual(rows["a"]["power_backsteps_in_window"], 1)
		self.assertFalse(rows["b"]["clock_anomaly"])
		summary = trajectory.summarize(list(rows.values()), power_backward_steps=1)
		self.assertTrue(summary["clock_anomaly"])
		self.assertEqual(summary["n_clock_anomaly_trajectories"], 1)

	def test_reversed_event_is_not_reported_as_overlap(self):
		events = [self.event(0.0, 2.0, gpu=1.0), self.event(1.5, 1.2, gpu=1.0)]
		row, = trajectory.accumulate(events)
		self.assertFalse(row["events_overlap"])
		self.assertEqual(row["max_concurrency"], 1)
		self.assertEqual(row["n_reversed_events"], 1)
		self.assertTrue(row["clock_anomaly"])
		self.assertTrue(row["coverage_valid"] is False)  # no independent window here

	def test_real_overlap_still_detected(self):
		events = [self.event(0.0, 2.0, gpu=1.0), self.event(1.0, 3.0, gpu=1.0)]
		row, = trajectory.accumulate(events)
		self.assertTrue(row["events_overlap"])
		self.assertEqual(row["max_concurrency"], 2)
		self.assertFalse(row["clock_anomaly"])

	def test_monotonic_run_has_no_anomaly(self):
		events = [self.event(0.0, 1.0, gpu=1.0), self.event(1.0, 2.0, gpu=2.0)]
		row, = trajectory.accumulate(events, clock_backsteps=[])
		self.assertFalse(row["clock_anomaly"])
		self.assertEqual(row["sum_attributed_gpu_energy_j"], 3.0)
		self.assertFalse(trajectory.summarize([row], power_backward_steps=0)["clock_anomaly"])

	def test_empty_power_cell_loads_as_missing(self):
		with tempfile.TemporaryDirectory() as tmp:
			p = os.path.join(tmp, "power.csv")
			with open(p, "w") as f:
				f.write("t,gpu0_w,gpu0_energy_mj\n0.0,,100\n1.0,50.0,150\n")
			ts, gpus, _, energy = attribution.load_power(p)
		self.assertEqual(gpus["gpu0_w"], [None, 50.0])
		self.assertEqual(energy["gpu0_energy_mj"], [100.0, 150.0])

	def test_attribution_main_warns_and_writes_run_level_flag(self):
		with tempfile.TemporaryDirectory() as tmp:
			power = os.path.join(tmp, "power.csv")
			with open(power, "w") as f:
				f.write("t,gpu0_w,gpu0_energy_mj\n")
				for t, e in ((0.0, 0), (1.0, 100), (0.7, 200), (2.0, 300), (3.0, 400)):
					f.write(f"{t},100.0,{e}\n")
			events = os.path.join(tmp, "events.jsonl")
			with open(events, "w") as f:
				f.write(json.dumps(self.event(0.2, 1.9, gpu=5.0)) + "\n")
			err, out = io.StringIO(), io.StringIO()
			argv = ["attribution", "--events", events, "--power", power,
			        "--out", os.path.join(tmp, "s.csv"), "--out-events", os.path.join(tmp, "a.jsonl")]
			with mock.patch.object(sys, "argv", argv), contextlib.redirect_stderr(err), \
			     contextlib.redirect_stdout(out):
				self.assertEqual(attribution.main(), 0)
			with open(os.path.join(tmp, "trajectory_summary.json")) as f:
				saved = json.load(f)
			with open(os.path.join(tmp, "a.jsonl")) as f:
				row = json.loads(f.readline())
		self.assertIn("wall-clock anomaly: 1 backward step", err.getvalue())
		self.assertTrue(saved["summary"]["clock_anomaly"])
		self.assertEqual(saved["summary"]["power_backward_steps"], 1)
		self.assertTrue(saved["trajectories"][0]["clock_anomaly"])
		self.assertEqual(row["gpu_energy_j"], 5.0)  # counter energy untouched




class CpuDramPreserved(unittest.TestCase):
	TS = [0.0, 1.0, 2.0, 3.0]
	RAPL = {
		"rapl_package-0_intel-rapl:0_uj": [0.0, 10e6, 20e6, 30e6],     # 10 J/s
		"rapl_core_intel-rapl:0:0_uj": [0.0, 6e6, 12e6, 18e6],         # diagnostic
		"rapl_dram_intel-rapl:0:1_uj": [0.0, 2e6, 4e6, 6e6],           # 2 J/s
	}

	def event(self, gpu=7.0):
		return {"question_id": "q", "operation_label": "llm:reason", "operation_type": "llm",
		        "start_timestamp": 0.5, "end_timestamp": 2.5, "duration_s": 2.0,
		        "gpu_energy_j": gpu, "status": "ok"}

	def test_mocked_rapl_domains_are_measured_and_summed(self):
		row, = attribution.attribute_events([self.event()], self.TS, {}, self.RAPL)[0]
		self.assertAlmostEqual(row["cpu_package_energy_j"], 20.0)
		self.assertAlmostEqual(row["dram_energy_j"], 4.0)
		self.assertAlmostEqual(row["cpu_core_energy_j"], 12.0)
		self.assertAlmostEqual(row["measured_energy_j"], 7.0 + 20.0 + 4.0)  # core not added
		self.assertEqual(row["available_energy_domains"], ["gpu", "cpu_package", "dram"])
		self.assertTrue(row["measurement_complete"])

	def test_unavailable_rapl_stays_null(self):
		row, = attribution.attribute_events([self.event()], self.TS, {}, {})[0]
		for key in ("cpu_package_energy_j", "dram_energy_j", "cpu_core_energy_j", "measured_energy_j"):
			self.assertIsNone(row[key], key)
		self.assertEqual(row["available_energy_domains"], ["gpu"])
		self.assertFalse(row["measurement_complete"])

	def test_rapl_trajectory_window_still_measured(self):
		rows, _ = attribution.attribute_events([self.event()], self.TS, {}, self.RAPL)
		pkg, core, dram = attribution.classify_rapl(self.RAPL)
		window = {"gpu_energy_j": 7.0, "cpu_package_energy_j": attribution.sum_domain(self.TS, pkg, 0.5, 2.5),
		          "dram_energy_j": attribution.sum_domain(self.TS, dram, 0.5, 2.5)}
		window["measured_energy_j"] = attribution.measured_total(*window.values())
		row, = trajectory.accumulate(rows, window_energy=lambda t0, t1: window)
		self.assertAlmostEqual(row["trajectory_measured_energy_j"], 31.0)
		self.assertAlmostEqual(row["coverage_measured_energy_j"], 1.0)
		self.assertFalse(row["clock_anomaly"])



if __name__ == "__main__":
	unittest.main()
