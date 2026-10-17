"""M5: measurement modes, sampler counter policies, disturbance-aware reporting."""

import asyncio
import csv
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest

from agent_energy_profiler import attribution_v2, report
from agent_energy_profiler import measurement as m
from agent_energy_profiler import sampling
from agent_energy_profiler.measurement import CallableReader, CounterDisturbance, Resolution
from agent_energy_profiler.session import Session

HERE = os.path.dirname(os.path.abspath(__file__))
EXAMPLES = os.path.join(os.path.dirname(HERE), "examples")

NOT_DETECTED = CounterDisturbance("NOT_DETECTED", "test")
DISTURBED = CounterDisturbance("DISTURBED", "test", 0.89, 10.0)

#: A fake NVML that counts energy-counter calls (written to a file at exit).
FAKE_PYNVML = textwrap.dedent("""
	import atexit, os, time
	_t0 = time.time()
	_calls = {"energy": 0, "power": 0}
	def _dump():
		path = os.environ.get("FAKE_NVML_CALLS")
		if path:
			with open(path + "." + str(os.getpid()), "w") as f:
				f.write("%d %d" % (_calls["energy"], _calls["power"]))
	atexit.register(_dump)
	def nvmlInit(): pass
	def nvmlShutdown(): pass
	def nvmlDeviceGetCount(): return 1
	def nvmlDeviceGetHandleByIndex(i): return i
	def nvmlDeviceGetTotalEnergyConsumption(h):
		_calls["energy"] += 1
		return int((time.time() - _t0) * 30_000)
	def nvmlDeviceGetPowerUsage(h):
		_calls["power"] += 1
		return 30_000
	def nvmlDeviceGetName(h): return "FakeGPU"
	def nvmlSystemGetDriverVersion(): return "fake-1.0"
""")


class Counter_:
	def __init__(self, step=100):
		self.v, self.reads, self.step = 1_000_000, 0, step

	def read(self):
		self.reads += 1
		self.v += self.step
		return [self.v]


class Tmp(unittest.TestCase):

	def setUp(self):
		self._tmp = tempfile.TemporaryDirectory()
		self.d = self._tmp.name

	def tearDown(self):
		self._tmp.cleanup()

	def fake_env(self):
		fake = os.path.join(self.d, "fake")
		os.makedirs(fake, exist_ok=True)
		with open(os.path.join(fake, "pynvml.py"), "w") as f:
			f.write(FAKE_PYNVML)
		return dict(os.environ, PYTHONPATH=fake,
		            FAKE_NVML_CALLS=os.path.join(self.d, "calls"))

	def calls(self):
		total = [0, 0]
		for name in os.listdir(self.d):
			if name.startswith("calls."):
				with open(os.path.join(self.d, name)) as f:
					e, p = map(int, f.read().split())
				total[0] += e
				total[1] += p
		return total

	def records(self, path):
		with open(path) as f:
			return [json.loads(line) for line in f if line.strip()]


class TestModeResolution(unittest.TestCase):

	def test_explicit_modes(self):
		for mode in ("direct", "sampled", "hybrid"):
			self.assertEqual(m.resolve_mode(mode, DISTURBED)[0], mode)
		with self.assertRaises(ValueError):
			m.resolve_mode("fast")

	def test_auto_is_conservative(self):
		self.assertEqual(m.resolve_mode("auto", NOT_DETECTED)[0], "direct")
		for verdict in ("DISTURBED", "INCONCLUSIVE", "UNKNOWN"):
			mode, reason = m.resolve_mode("auto", CounterDisturbance(verdict, "x"))
			self.assertEqual(mode, "sampled", verdict)
			self.assertIn(verdict, reason)
		mode, reason = m.resolve_mode("auto")          # nothing known
		self.assertEqual(mode, "sampled")
		self.assertIn("no test result", reason)

	def test_disturbance_from_hardware_report(self):
		rep = {"generated_at": 5.0, "gpu": {"driver_version": "610.88"},
		       "gpu_counter_disturbance": {"status": "SUPPORTED", "verdict": "DISTURBED",
		                                   "retained_median": 0.893, "read_hz": 10.0}}
		d = CounterDisturbance.from_hardware_report(rep, "hw.json")
		self.assertEqual((d.verdict, d.retained_median, d.driver_version, d.source),
		                 ("DISTURBED", 0.893, "610.88", "hw.json"))
		d = CounterDisturbance.from_hardware_report({"gpu": {}}, "hw.json")
		self.assertEqual(d.verdict, "UNKNOWN")
		self.assertIn("no disturbance test", d.source)
		self.assertEqual(CounterDisturbance.from_dict(DISTURBED.to_dict()).to_dict(),
		                 DISTURBED.to_dict())


class TestCounterPolicy(unittest.TestCase):

	def test_policies(self):
		self.assertEqual(sampling.counter_policy(10, None), ("every_row", 1))
		self.assertEqual(sampling.counter_policy(10, 10), ("every_row", 1))
		self.assertEqual(sampling.counter_policy(10, 50), ("every_row", 1))
		self.assertEqual(sampling.counter_policy(10, 0), ("boundaries", None))
		self.assertEqual(sampling.counter_policy(10, 1), ("every_n_rows", 10))
		self.assertEqual(sampling.counter_policy(50, 2), ("every_n_rows", 25))
		meta = sampling.timeline_meta(10, 3)
		self.assertEqual(meta["counter_every_n_rows"], 3)
		self.assertAlmostEqual(meta["counter_hz_effective"], 10 / 3)
		self.assertEqual(sampling.timeline_meta(10, 0)["counter_hz_effective"], 0.0)
		with self.assertRaises(ValueError):
			sampling.Sampler("x.csv", counter_hz=-1)


class TestSessionModes(Tmp):

	def run_session(self, **kw):
		path = os.path.join(self.d, "spans.jsonl")
		with Session(path, overwrite=True, resolution=Resolution.configured(0.1, "t"),
		             **kw) as s:
			with s.span("agent:step"):
				with s.span("llm:plan"):
					pass
		return self.records(path)

	def test_direct_reads_the_counter_at_each_span_boundary(self):
		c = Counter_()
		recs = self.run_session(gpu=CallableReader(c.read), mode="direct")
		self.assertEqual(c.reads, 4)                       # 2 spans x 2 boundaries
		meas = recs[0]["config"]["measurement"]
		self.assertEqual((meas["mode"], meas["span_counter_reads"], meas["model_version"]),
		                 ("direct", True, 2))
		self.assertEqual(meas["counter_disturbance"]["verdict"], "UNKNOWN")
		spans = [r for r in recs if r["record_type"] == "span"]
		self.assertTrue(all(s["energy"]["gpu"]["status"] == "measured" for s in spans))

	def test_sampled_and_hybrid_never_read_per_span(self):
		for mode in ("sampled", "hybrid"):
			c = Counter_()
			recs = self.run_session(gpu=CallableReader(c.read), mode=mode)
			self.assertEqual(c.reads, 0, mode)
			spans = [r for r in recs if r["record_type"] == "span"]
			for s in spans:
				g = s["energy"]["gpu"]
				self.assertEqual((g["status"], g["reason"], g["energy_j"]),
				                 ("unavailable", "not_read_by_mode", None))
			self.assertFalse(recs[0]["config"]["measurement"]["span_counter_reads"])

	def test_auto_records_its_reason(self):
		recs = self.run_session(gpu=CallableReader(Counter_().read), mode="auto",
		                        disturbance=DISTURBED)
		meas = recs[0]["config"]["measurement"]
		self.assertEqual((meas["mode"], meas["requested_mode"]), ("sampled", "auto"))
		self.assertIn("DISTURBED", meas["mode_reason"])
		self.assertEqual(meas["counter_disturbance"]["retained_median"], 0.89)

	def test_gpu_unavailable_wins_over_mode(self):
		recs = self.run_session(gpu=False, mode="sampled")
		spans = [r for r in recs if r["record_type"] == "span"]
		self.assertEqual(spans[0]["energy"]["gpu"]["reason"], "disabled")

	def test_default_is_auto_since_0_4_0(self):
		c = Counter_()
		recs = self.run_session(gpu=CallableReader(c.read))
		meas = recs[0]["config"]["measurement"]
		self.assertEqual((meas["mode"], meas["requested_mode"]), ("sampled", "auto"))
		self.assertEqual(c.reads, 0)

	def test_from_env_keeps_the_parents_mode_choice(self):
		env = {"AEP_SPANS_FILE": os.path.join(self.d, "env.jsonl"),
		       "AEP_MEASUREMENT": json.dumps({
		           "mode": "sampled", "requested_mode": "auto",
		           "mode_reason": "auto: counter disturbance UNKNOWN (no test result available)",
		           "disturbance": CounterDisturbance.unknown().to_dict(),
		           "timeline": sampling.timeline_meta(10, 0)})}
		old = {k: os.environ.get(k) for k in env}
		os.environ.update(env)
		try:
			with Session.from_env(gpu=CallableReader(Counter_().read)) as s:
				with s.span("llm:x"):
					pass
		finally:
			for k, v in old.items():
				if v is None:
					os.environ.pop(k, None)
				else:
					os.environ[k] = v
		meas = self.records(env["AEP_SPANS_FILE"])[0]["config"]["measurement"]
		self.assertEqual((meas["mode"], meas["requested_mode"]), ("sampled", "auto"))
		self.assertEqual(meas["timeline"]["counter_policy"], "boundaries")


class TestSamplerPolicies(Tmp):

	def sample(self, counter_hz, hz=50):
		from agent_energy_profiler.sampling import Sampler
		power = os.path.join(self.d, f"power_{counter_hz}.csv")
		s = Sampler(power, hz=hz, counter_hz=counter_hz, env=self.fake_env())
		s.start()
		import time
		time.sleep(0.6)
		code = s.stop()
		self.assertEqual(code, 0)
		self.assertFalse(s.running)
		with open(power) as f:
			rows = list(csv.DictReader(f))
		with open(power + ".meta.json") as f:
			meta = json.load(f)
		return rows, meta

	def test_every_row_is_the_unchanged_default(self):
		rows, meta = self.sample(None)
		self.assertTrue(all(r["gpu0_energy_mj"] for r in rows))
		self.assertEqual(meta["timeline"]["counter_policy"], "every_row")
		self.assertEqual(meta["version"], 2)

	def test_boundaries_reads_the_counter_on_first_and_final_rows_only(self):
		rows, meta = self.sample(0)
		filled = [i for i, r in enumerate(rows) if r["gpu0_energy_mj"]]
		self.assertEqual(filled, [0, len(rows) - 1])
		self.assertTrue(all(r["gpu0_w"] for r in rows))       # power on every row
		self.assertEqual(meta["timeline"]["counter_policy"], "boundaries")
		energy_calls, power_calls = self.calls()
		self.assertLessEqual(energy_calls, 4)               # init probe + 2 boundaries
		self.assertGreaterEqual(power_calls, len(rows))

	def test_sparse_rate(self):
		rows, meta = self.sample(10, hz=50)                  # every 5th row
		filled = [i for i, r in enumerate(rows) if r["gpu0_energy_mj"]]
		self.assertEqual(filled[:-1], list(range(0, len(rows) - 1, 5)))
		self.assertEqual(filled[-1], len(rows) - 1)
		self.assertEqual(meta["timeline"]["counter_every_n_rows"], 5)


def write_timeline(d, t0, t1, policy, n=None, gpu_reset_at=None, drop_power=(), hz=10):
	"""A synthetic power.csv + sidecar: 100 W GPU, counter per `policy`."""
	path = os.path.join(d, "power.csv")
	rows = int((t1 - t0) * hz) + 1
	lines = ["t,gpu0_w,gpu0_energy_mj,rapl_package-0_intel-rapl:0_uj,rapl_dram_intel-rapl:0:2_uj"]
	for i in range(rows):
		t = t0 + i / hz
		read = (policy == "every_row" or i in (0, rows - 1)
		        or (policy == "every_n_rows" and i % n == 0))
		e = int(100_000 * (t - t0)) + 5_000_000
		if gpu_reset_at is not None and t >= gpu_reset_at:
			e = int(100_000 * (t - gpu_reset_at))
		w = "" if i in drop_power else "100.0"
		lines.append(f"{t:.6f},{w},{e if read else ''},{2_000_000 * i},{200_000 * i}")
	with open(path, "w") as f:
		f.write("\n".join(lines) + "\n")
	meta = {"format": "agent_energy_profiler.power_meta", "version": 2,
	        "rapl_max_energy_range_uj": {},
	        "timeline": sampling.timeline_meta(hz, 0 if policy == "boundaries" else
	                                           (None if policy == "every_row" else hz / n))}
	with open(path + ".meta.json", "w") as f:
		json.dump(meta, f)
	return path


class TestSparseAttribution(Tmp):

	def record(self, mode="sampled"):
		path = os.path.join(self.d, "spans.jsonl")
		with Session(path, gpu=CallableReader(Counter_().read), mode=mode,
		             resolution=Resolution.configured(0.1, "t")) as s:
			with s.span("agent:step"):
				with s.span("llm:short"):
					pass
				with s.span("llm:short"):
					pass

			async def fan():
				async def w(name):
					async with s.span(name):
						await asyncio.sleep(0.01)
				await asyncio.gather(w("tool:a"), w("tool:b"))
			asyncio.run(fan())
		spans = [r for r in self.records(path) if r["record_type"] == "span"]
		return path, min(x["start_unix_s"] for x in spans), max(x["end_unix_s"] for x in spans)

	def attribute(self, spans, power):
		out = os.path.join(self.d, "out")
		self.assertEqual(attribution_v2.main(["--spans", spans, "--power", power,
		                                      "--out-dir", out]), 0)
		with open(os.path.join(out, "session_summary.json")) as f:
			summary = json.load(f)
		with open(os.path.join(out, "spans_attributed.jsonl")) as f:
			rows = [json.loads(l) for l in f]
		return summary, rows, report.build(out)

	def test_boundaries_timeline(self):
		spans, t0, t1 = self.record("sampled")
		power = write_timeline(self.d, t0 - 1, t1 + 1, "boundaries")
		summary, rows, rep = self.attribute(spans, power)
		gpu_window = summary["window"]["domains"]["gpu"]
		self.assertEqual((gpu_window["status"], gpu_window["reason"]),
		                 ("unavailable", "counter_not_sampled"))
		rc = summary["run_counter"]
		self.assertEqual((rc["status"], rc["n_readings"]), ("measured", 2))
		self.assertTrue(rc["brackets_all_spans"])
		self.assertAlmostEqual(rc["energy_j"], 100.0 * rc["duration_s"], delta=1.0)
		# per-span GPU: estimates only, never in energy.gpu
		for r in rows:
			self.assertEqual(r["energy"]["gpu"]["reason"], "not_read_by_mode")
			self.assertEqual(r["estimates"]["gpu"]["status"], "estimated")
			self.assertIsNone(r["total"]["energy_j"])
		# RAPL is unaffected by the GPU counter policy
		self.assertEqual(rows[0]["energy"]["cpu_package"]["status"], "measured")
		codes = {w["code"] for w in rep["warnings"]}
		self.assertIn("SPAN_ESTIMATES_ONLY", codes)
		self.assertNotIn("COUNTER_DISTURBANCE_UNKNOWN", codes)
		self.assertEqual(rep["measurement"]["mode"], "sampled")
		self.assertIn("RUN COUNTER MEASUREMENT", report.render_text(rep))
		# nested and overlapping spans: no exclusive GPU energy without measurement
		root = next(r for r in rows if r["name"] == "agent:step")
		self.assertEqual(root["accounting"]["exclusive"]["gpu"]["reason"], "parent_unmeasured")
		shared = [r for r in rows if r["name"] in ("tool:a", "tool:b")]
		self.assertTrue(all(r["accounting"]["attribution"] == "shared" for r in shared))
		self.assertIsNone(rep["totals"]["domains"]["gpu"]["total_j"])
		self.assertGreater(rep["totals"]["estimated_gpu"]["n_spans"], 0)

	def test_hybrid_intervals_and_missing_samples(self):
		spans, t0, t1 = self.record("hybrid")
		power = write_timeline(self.d, t0 - 2, t1 + 2, "every_n_rows", n=10,
		                       drop_power=(3, 4, 17))
		summary, rows, rep = self.attribute(spans, power)
		ci = summary["counter_intervals"]
		self.assertGreaterEqual(ci["n_intervals"], 3)
		self.assertAlmostEqual(ci["counter_over_estimate_median"], 1.0, places=2)
		self.assertEqual(summary["run_counter"]["status"], "measured")
		self.assertEqual(summary["timeline"]["declared"]["counter_policy"], "every_n_rows")

	def test_counter_discontinuity_invalidates_the_run_counter(self):
		spans, t0, t1 = self.record("sampled")
		power = write_timeline(self.d, t0 - 2, t1 + 2, "every_n_rows", n=5,
		                       gpu_reset_at=t0)
		summary, _, rep = self.attribute(spans, power)
		rc = summary["run_counter"]
		self.assertEqual((rc["status"], rc["reason"], rc["energy_j"]),
		                 ("invalid", "counter_discontinuity", None))

	def test_direct_with_unknown_and_detected_disturbance_is_warned(self):
		for disturbance, code in ((None, "COUNTER_DISTURBANCE_UNKNOWN"),
		                          (DISTURBED, "COUNTER_DISTURBED")):
			sub = tempfile.mkdtemp(dir=self.d)
			path = os.path.join(sub, "spans.jsonl")
			with Session(path, gpu=CallableReader(Counter_().read), mode="direct",
			             disturbance=disturbance) as s:
				with s.span("llm:x"):
					pass
			rep = report.build(path)
			self.assertIn(code, {w["code"] for w in rep["warnings"]})

	def test_direct_with_no_effect_detected_is_not_warned(self):
		path = os.path.join(self.d, "spans.jsonl")
		with Session(path, gpu=CallableReader(Counter_().read), mode="auto",
		             disturbance=NOT_DETECTED) as s:
			with s.span("llm:x"):
				pass
		rep = report.build(path)
		codes = {w["code"] for w in rep["warnings"]}
		self.assertEqual(rep["measurement"]["mode"], "direct")
		self.assertFalse(codes & {"COUNTER_DISTURBANCE_UNKNOWN", "COUNTER_DISTURBED",
		                          "SPAN_ESTIMATES_ONLY"})

	def test_legacy_artifacts_say_the_mode_was_not_recorded(self):
		import gzip
		src = os.path.join(HERE, "golden_v2", "session_mixed", "input", "spans.jsonl.gz")
		path = os.path.join(self.d, "old.jsonl")
		with gzip.open(src, "rb") as g, open(path, "wb") as f:
			f.write(g.read())
		rep = report.build(path)
		self.assertFalse(rep["measurement"]["recorded"])
		codes = {w["code"] for w in rep["warnings"]}
		self.assertTrue({"MEASUREMENT_NOT_RECORDED", "COUNTER_DISTURBANCE_UNKNOWN"} <= codes)


class TestAepRunModes(Tmp):

	def aep_run(self, *args, hw=None):
		out = os.path.join(self.d, "run")
		cmd = [sys.executable, "-m", "agent_energy_profiler", "run", "--out-dir", out,
		       "--power-hz", "50", *args]
		if hw:
			path = os.path.join(self.d, "hw.json")
			with open(path, "w") as f:
				json.dump(hw, f)
			cmd += ["--hardware-report", path]
		cmd += ["--", sys.executable, os.path.join(EXAMPLES, "agent_workflow", "agent.py"),
		        "--work", "20"]
		res = subprocess.run(cmd, capture_output=True, text=True, env=self.fake_env(),
		                     timeout=300)
		self.assertEqual(res.returncode, 0, res.stderr)
		with open(os.path.join(out, "run.json")) as f:
			run = json.load(f)
		with open(os.path.join(out, "spans.jsonl")) as f:
			header = json.loads(f.readline())
		with open(os.path.join(out, "power.csv")) as f:
			rows = list(csv.DictReader(f))
		return run, header, rows, res.stdout

	def test_auto_without_evidence_runs_sampled_with_boundary_counter_reads(self):
		run, header, rows, stdout = self.aep_run()
		self.assertEqual(run["measurement"]["mode"], "sampled")
		self.assertEqual(header["config"]["measurement"]["requested_mode"], "auto")
		# M6: who chose. The example's own default_mode="direct" does not apply
		# under aep run.
		self.assertEqual(run["measurement"]["mode_source"], "default")
		self.assertEqual(header["config"]["measurement"]["mode_source"], "aep_run")
		self.assertEqual(header["config"]["measurement"]["mode"], "sampled")
		self.assertEqual(header["config"]["measurement"]["timeline"]["counter_policy"],
		                 "boundaries")
		filled = [r for r in rows if r["gpu0_energy_mj"]]
		self.assertEqual(len(filled), 2)
		self.assertEqual(run["whole_program"]["sampler_run_counter"]["status"], "measured")
		self.assertIn("SPAN_ESTIMATES_ONLY", stdout)
		energy_calls, _ = self.calls()
		# sampler: init probe + 2 boundaries; parent: 2 whole-program reads;
		# the child never reads per span
		self.assertLessEqual(energy_calls, 8)

	def test_auto_with_no_effect_detected_runs_direct(self):
		hw = {"generated_at": 1.0, "gpu": {"driver_version": "fake-1.0"},
		      "gpu_counter_resolution": {"status": "SUPPORTED",
		                                 "median_update_interval_s": 0.1},
		      "gpu_counter_disturbance": {"status": "SUPPORTED", "verdict": "NOT_DETECTED",
		                                  "retained_median": 1.0, "read_hz": 10.0}}
		run, header, rows, _ = self.aep_run(hw=hw)
		self.assertEqual(run["measurement"]["mode"], "direct")
		self.assertEqual(header["config"]["measurement"]["counter_disturbance"]["verdict"],
		                 "NOT_DETECTED")
		self.assertEqual(header["config"]["resolution"]["gpu_update_interval_s"], 0.1)
		energy_calls, _ = self.calls()
		self.assertGreater(energy_calls, 40)                 # per-span reads happened

	def test_auto_ignores_a_verdict_from_another_driver(self):
		# 0.4.0: a NOT_DETECTED test only counts for the configuration it ran on
		hw = {"generated_at": 1.0, "gpu": {"driver_version": "fake-0.9"},
		      "gpu_counter_disturbance": {"status": "SUPPORTED", "verdict": "NOT_DETECTED",
		                                  "retained_median": 1.0, "read_hz": 10.0}}
		run, header, rows, _ = self.aep_run(hw=hw)
		self.assertEqual(run["measurement"]["mode"], "sampled")
		self.assertIn("another configuration", run["measurement"]["mode_reason"])
		self.assertIn("fake-0.9", header["config"]["measurement"]["mode_reason"])
		self.assertEqual(header["config"]["measurement"]["mode"], "sampled")

	def test_hybrid_with_an_explicit_counter_rate(self):
		run, header, rows, stdout = self.aep_run("--mode", "hybrid", "--counter-hz", "10")
		self.assertEqual(run["measurement"]["timeline"]["counter_every_n_rows"], 5)
		self.assertIn("RUN COUNTER MEASUREMENT", stdout)
		self.assertEqual(run["measurement"]["mode_source"], "caller")
		self.assertEqual(header["config"]["measurement"]["mode"], "hybrid")


if __name__ == "__main__":
	unittest.main()
