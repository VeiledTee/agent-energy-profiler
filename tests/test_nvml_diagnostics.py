"""M4: the NVML counter diagnostics (tools/nvml_diagnostics), with a fake GPU."""

import json
import os
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "tools", "nvml_diagnostics"))

import analyze  # noqa: E402
import experiment  # noqa: E402


class FakeGpu:
	"""Counter rising at 100 W, losing a fixed share per energy read."""

	def __init__(self, loss_mj_per_read=0, read_s=0.0):
		self.t0 = time.perf_counter()
		self.lost = 0
		self.loss = loss_mj_per_read
		self.read_s = read_s
		self.calls = []

	def _cost(self, kind):
		self.calls.append(kind)
		if self.read_s:
			end = time.perf_counter() + self.read_s
			while time.perf_counter() < end:
				pass

	def energy(self):
		self._cost("energy")
		self.lost += self.loss
		return int((time.perf_counter() - self.t0) * 100_000) - self.lost

	energy_field = energy

	def power_avg(self):
		self._cost("power_avg")
		return 100_000

	power_instant = power_avg

	def temperature(self):
		self._cost("temperature")
		return 40


class TestRunTrial(unittest.TestCase):

	def test_window_length_is_the_same_in_every_condition(self):
		for cond in ("endpoints", "energy_50hz", "combined_10hz", "energy_1000hz"):
			trial = experiment.run_trial(FakeGpu(read_s=0.0015), cond, 0.3)
			# tolerance: one in-flight read plus scheduler jitter; the defect
			# this guards against overran by ~25% of the window
			self.assertAlmostEqual(trial["window_s"], 0.3, delta=0.02, msg=cond)

	def test_slow_reads_do_not_build_a_backlog(self):
		gpu = FakeGpu(read_s=0.003)
		trial = experiment.run_trial(gpu, "energy_1000hz", 0.3)
		self.assertLess(trial["window_s"], 0.31)
		self.assertLess(len(trial["reads"]["energy"]), 120)

	def test_streams_follow_the_condition(self):
		trial = experiment.run_trial(FakeGpu(), "combined_10hz", 0.55)
		self.assertEqual(sorted(trial["reads"]), ["energy", "power_avg"])
		self.assertEqual(len(trial["reads"]["energy"]), 5)
		self.assertEqual(len(trial["reads"]["power_avg"]), 5)
		trial = experiment.run_trial(FakeGpu(), "endpoints", 0.2)
		self.assertEqual(trial["reads"], {})
		for name, streams in experiment.CONDITIONS.items():
			for kind, period in streams:
				self.assertTrue(hasattr(FakeGpu, kind), name)
				self.assertGreater(period, 0)


class TestAnalyze(unittest.TestCase):

	def make_run(self, d, workload="idle"):
		"""Two blocks; energy_50hz retains 50%, power reads retain 100%."""
		meta = {"args": {"workload": workload, "reps": 2, "duration": 8.0},
		        "conditions": {c: experiment.CONDITIONS[c]
		                       for c in ("endpoints", "energy_50hz", "power_avg_10hz")},
		        "gpu": {"name": "fake", "driver_version": "0"}, "host": {}}
		trials = []

		def trial(block, pos, cond, watts, reads):
			trials.append({"condition": cond, "block": block, "position": pos,
			               "window_s": 8.0, "energy_start_mj": 1_000_000,
			               "energy_end_mj": 1_000_000 + int(watts * 8000), "reads": reads,
			               "state_after": {"temperature_c": 40}})

		for block in range(2):
			base = 30.0 + block
			energy_reads = [[i * 0.02, i * 0.02 + 0.001, 1_000_000 + (i // 5) * 1500]
			                for i in range(1, 400)]
			power_reads = [[i * 0.1, i * 0.1 + 0.0002, int(base * 1000)] for i in range(1, 80)]
			trial(block, 0, "energy_50hz", base / 2, {"energy": energy_reads})
			trial(block, 1, "endpoints", base, {})
			trial(block, 2, "power_avg_10hz", base, {"power_avg": power_reads})
		with open(os.path.join(d, "meta.json"), "w") as f:
			json.dump(meta, f)
		with open(os.path.join(d, "trials.jsonl"), "w") as f:
			for t in trials:
				f.write(json.dumps(t) + "\n")

	def test_relative_power_ratio_and_carry_over(self):
		with tempfile.TemporaryDirectory() as d:
			self.make_run(d)
			r = analyze.analyze(d)
		c = r["conditions"]
		self.assertAlmostEqual(c["endpoints"]["relative_to_endpoints_mean"], 1.0)
		self.assertAlmostEqual(c["energy_50hz"]["relative_to_endpoints_mean"], 0.5, places=4)
		self.assertAlmostEqual(c["power_avg_10hz"]["relative_to_endpoints_mean"], 1.0, places=4)
		self.assertAlmostEqual(c["power_avg_10hz"]["counter_over_integrated_power_mean"], 1.0,
		                       places=2)
		self.assertAlmostEqual(c["energy_50hz"]["counter_updates"]["median_interval_ms"], 100.0)
		self.assertEqual(c["energy_50hz"]["counter_updates"]["median_step_mj"], 1500)
		self.assertEqual(len(r["carry_over_endpoints_w"]["after_fast_energy_polling"]), 2)
		self.assertEqual(r["endpoints_counter_w_per_block"], [30.0, 31.0])
		md = analyze.to_markdown([r])
		self.assertIn("| energy_50hz |", md)

	def test_confidence_interval(self):
		mean, half, n = analyze.ci([1.0, 2.0, 3.0])
		self.assertEqual((mean, n), (2.0, 3))
		self.assertAlmostEqual(half, 4.303 * 1.0 / 3 ** 0.5, places=3)
		self.assertEqual(analyze.ci([5.0])[1], None)
		self.assertEqual(analyze.ci([]), (None, None, 0))


FAKE_PYNVML = """
import time
_t0 = time.perf_counter()
_reads = [0]
LOSS_MJ = {loss}
def nvmlInit(): pass
def nvmlDeviceGetHandleByIndex(i): return i
def nvmlDeviceGetPerformanceState(h): return 8
def nvmlDeviceGetTotalEnergyConsumption(h):
    _reads[0] += 1
    return int((time.perf_counter() - _t0) * 30_000) - _reads[0] * LOSS_MJ
"""


class TestCounterDisturbanceCheck(unittest.TestCase):

	def test_verdicts(self):
		from agent_energy_profiler import validation as v
		self.assertEqual(v.counter_disturbance_verdict(
			[(25.0, 28.0, False), (24.0, 28.0, False)])["verdict"], v.DISTURBED)
		self.assertEqual(v.counter_disturbance_verdict(
			[(28.0, 28.0, False), (27.9, 28.0, False)])["verdict"], v.NOT_DETECTED)
		self.assertEqual(v.counter_disturbance_verdict(
			[(25.0, 28.0, False), (28.0, 28.0, False)])["verdict"], v.INCONCLUSIVE)
		# a pair where the GPU changed state is excluded, not counted
		r = v.counter_disturbance_verdict([(25.0, 28.0, False), (60.0, 28.0, True)])
		self.assertEqual((r["verdict"], r["n_valid"]), (v.INCONCLUSIVE, 1))

	def run_probe(self, loss):
		import subprocess
		with tempfile.TemporaryDirectory() as d:
			with open(os.path.join(d, "pynvml.py"), "w") as f:
				f.write(FAKE_PYNVML.format(loss=loss))
			code = ("import json; from agent_energy_profiler import validation as v; "
			        "print(json.dumps(v.probe_counter_disturbance(pairs=2, window_s=0.5, "
			        "rest_s=0.0)))")
			out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
			                     env=dict(os.environ, PYTHONPATH=d), timeout=60)
		self.assertEqual(out.returncode, 0, out.stderr)
		return json.loads(out.stdout)

	def test_probe_detects_a_counter_that_loses_energy_per_read(self):
		r = self.run_probe(loss=200)   # 10 reads/s x 0.2 J = 2 W of 30 W lost
		self.assertEqual(r["verdict"], "DISTURBED")
		self.assertLess(r["retained_median"], 0.95)

	def test_probe_reports_no_effect_for_an_undisturbed_counter(self):
		r = self.run_probe(loss=0)
		self.assertEqual(r["verdict"], "NOT_DETECTED")


if __name__ == "__main__":
	unittest.main()
