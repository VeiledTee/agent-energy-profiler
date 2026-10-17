"""M8 (0.4.0): Session's default mode is `auto`.

`auto` selects `direct` only for a NOT_DETECTED disturbance verdict measured
on this configuration (host, driver, GPU models), and `sampled` otherwise. It
never runs the disturbance test. Explicit modes resolve exactly as before, and
the 0.3.0 SessionDefaultModeWarning is no longer emitted.
"""

import json
import os
import platform
import subprocess
import sys
import tempfile
import unittest
import warnings
from unittest import mock

from agent_energy_profiler import measurement as m
from agent_energy_profiler import report, sampling, validation
from agent_energy_profiler.hardware import nvml
from agent_energy_profiler.measurement import (CallableReader, CounterDisturbance,
                                               NvmlReader, SessionDefaultModeWarning)
from agent_energy_profiler.session import Session

HERE = os.path.abspath(__file__)
SRC = os.path.join(os.path.dirname(os.path.dirname(HERE)), "src")

#: The header keys of counter_disturbance before 0.4.0.
DISTURBANCE_KEYS_V03 = {"verdict", "source", "retained_median", "read_hz",
                        "measured_at_unix_s", "driver_version"}


class Counter:

	def __init__(self):
		self.v = 1_000_000
		self.reads = 0

	def read(self):
		self.reads += 1
		self.v += 250
		return [self.v]


class FakeNvml(NvmlReader):
	"""An NvmlReader that needs no GPU: a fixed model name, a counting counter."""

	def __init__(self, models=("FakeGPU",)):
		super().__init__()
		self.models = list(models)
		self.counter = Counter()

	def start(self):
		self._handles = list(range(len(self.models)))
		return True

	def devices(self):
		return [{"index": i, "name": n} for i, n in enumerate(self.models)]

	def read(self):
		return self.counter.read() * len(self.models)


def not_detected(**config):
	return CounterDisturbance("NOT_DETECTED", "hw.json", 1.0, 10.0, 1.0,
	                          config.get("driver_version"), config.get("hostname"),
	                          config.get("gpu_models"))


class Base(unittest.TestCase):

	def setUp(self):
		self._tmp = tempfile.TemporaryDirectory()
		self.d = self._tmp.name
		self.n = 0
		self._saved_env = {k: os.environ.pop(k, None)
		                   for k in (Session.ENV_SPANS_FILE, Session.ENV_MEASUREMENT)}

	def tearDown(self):
		for k, v in self._saved_env.items():
			if v is not None:
				os.environ[k] = v
			else:
				os.environ.pop(k, None)
		self._tmp.cleanup()

	def path(self):
		self.n += 1
		return os.path.join(self.d, f"spans{self.n}.jsonl")

	@staticmethod
	def records(path):
		with open(path) as f:
			return [json.loads(line) for line in f if line.strip()]

	def record(self, gpu=None, **kw):
		"""(counter reads, [(name, gpu energy without timing)], measurement header)"""
		reader = gpu if gpu is not None else CallableReader(Counter().read)
		counter = getattr(reader, "counter", None) or reader._read.__self__
		p = self.path()
		with Session(p, gpu=reader, **kw) as s:
			for _ in range(3):
				with s.span("agent:step"):
					with s.span("tool:x"):
						pass
		spans = []
		for r in self.records(p):
			if r["record_type"] == "span":
				g = dict(r["energy"]["gpu"])
				g.pop("update_intervals", None)      # timing-dependent
				spans.append((r["name"], g))
		return counter.reads, spans, self.records(p)[0]["config"]["measurement"]


class TestDefaultIsAuto(Base):

	def test_without_evidence_the_default_runs_sampled_and_reads_no_counter(self):
		reads, spans, meas = self.record()
		self.assertEqual((meas["mode"], meas["requested_mode"], meas["mode_source"]),
		                 ("sampled", "auto", "default"))
		self.assertFalse(meas["span_counter_reads"])
		self.assertTrue(meas["mode_reason"].startswith("default:"))
		self.assertIn("auto: counter disturbance UNKNOWN", meas["mode_reason"])
		self.assertEqual(meas["counter_disturbance"]["verdict"], "UNKNOWN")
		self.assertIn("estimated", meas["semantics"]["gpu_span_energy"])
		self.assertEqual(reads, 0)
		for _, g in spans:
			self.assertEqual((g["status"], g["reason"], g["energy_j"]),
			                 ("unavailable", "not_read_by_mode", None))

	def test_the_default_equals_explicit_auto(self):
		for disturbance in (None, CounterDisturbance("DISTURBED", "t"),
		                    CounterDisturbance("INCONCLUSIVE", "t"), not_detected()):
			kw = {"disturbance": disturbance} if disturbance else {}
			reads_d, spans_d, meas_d = self.record(**kw)
			reads_a, spans_a, meas_a = self.record(mode="auto", **kw)
			self.assertEqual((reads_d, spans_d), (reads_a, spans_a))
			drop = ("mode_source", "mode_reason")
			self.assertEqual({k: v for k, v in meas_d.items() if k not in drop},
			                 {k: v for k, v in meas_a.items() if k not in drop})
			self.assertEqual((meas_d["mode_source"], meas_a["mode_source"]),
			                 ("default", "caller"))
			self.assertTrue(meas_d["mode_reason"].endswith(meas_a["mode_reason"]))

	def test_not_detected_on_this_configuration_selects_direct(self):
		reads, spans, meas = self.record(disturbance=not_detected(hostname=platform.node()))
		self.assertEqual((meas["mode"], meas["requested_mode"]), ("direct", "auto"))
		self.assertEqual(reads, 12)
		self.assertEqual(meas["counter_disturbance"]["hostname"], platform.node())

	def test_a_test_from_another_configuration_does_not_select_direct(self):
		cases = [
			(CallableReader(Counter().read), {"hostname": "elsewhere"}, "host"),
			(FakeNvml(), {"driver_version": "1.0"}, "driver"),
			(FakeNvml(), {"gpu_models": ["OtherGPU"]}, "GPU models"),
		]
		for reader, config, what in cases:
			with mock.patch.object(nvml, "driver_version", return_value="2.0"):
				reads, spans, meas = self.record(gpu=reader, disturbance=not_detected(**config))
			self.assertEqual(meas["mode"], "sampled", what)
			self.assertIn("another configuration", meas["mode_reason"])
			self.assertIn(what, meas["mode_reason"])
			self.assertEqual(reads, 0, what)
		# matching driver and models: direct
		with mock.patch.object(nvml, "driver_version", return_value="2.0"):
			reads, _, meas = self.record(gpu=FakeNvml(), disturbance=not_detected(
				driver_version="2.0", gpu_models=["FakeGPU"], hostname=platform.node()))
		self.assertEqual((meas["mode"], reads), ("direct", 12))

	def test_the_disturbance_test_is_never_run(self):
		with mock.patch.object(validation, "probe_counter_disturbance",
		                       side_effect=AssertionError("diagnostic ran")):
			for kw in ({}, {"mode": "auto"}, {"disturbance": not_detected()}):
				self.record(**kw)
			self.record(gpu=FakeNvml(), disturbance=not_detected())

	def test_from_env(self):
		s = Session.from_env(self.path(), gpu=False)
		self.assertEqual((s.requested_mode, s.mode, s.mode_source),
		                 ("auto", "sampled", "default"))
		s = Session.from_env(self.path(), gpu=False, default_mode="direct")
		self.assertEqual((s.mode, s.mode_source), ("direct", "caller"))
		os.environ[Session.ENV_MEASUREMENT] = json.dumps({
			"mode": "direct", "requested_mode": "auto", "mode_reason": "auto: t",
			"disturbance": not_detected().to_dict(),
			"timeline": sampling.timeline_meta(10, 0)})
		p = self.path()
		with Session.from_env(p, gpu=CallableReader(Counter().read)) as s:
			pass
		# `agent-energy-profiler run` checked the configuration in the parent; the child keeps it
		self.assertEqual((s.requested_mode, s.mode, s.mode_source),
		                 ("auto", "direct", "aep_run"))


class TestExplicitModesUnchanged(Base):

	def test_explicit_direct_reproduces_the_previous_behaviour(self):
		# Same disturbance-free host, every configuration check failing: an
		# explicit direct still reads the counter at every span boundary.
		reads, spans, meas = self.record(mode="direct",
		                                 disturbance=not_detected(hostname="elsewhere"))
		self.assertEqual((meas["mode"], meas["requested_mode"], meas["mode_source"],
		                  meas["mode_reason"]),
		                 ("direct", "direct", "caller", "requested explicitly"))
		self.assertEqual(reads, 12)
		energies = [(name, g["status"], g["energy_j"]) for name, g in spans]
		self.assertEqual(energies, [("tool:x", "measured", 0.25),
		                            ("agent:step", "measured", 0.75)] * 3)

	def test_every_explicit_mode_resolves_as_before(self):
		for mode in ("direct", "sampled", "hybrid"):
			for d in (None, not_detected(hostname="elsewhere"), CounterDisturbance("DISTURBED")):
				self.assertEqual(m.resolve_mode(mode, d, {"hostname": "here"}),
				                 (mode, "requested explicitly"))
		self.assertEqual(m.resolve_mode("auto", not_detected())[0], "direct")
		self.assertEqual(m.resolve_mode("auto", not_detected(), {})[0], "direct")
		with self.assertRaises(ValueError):
			m.resolve_mode("fast")


class TestConfiguration(unittest.TestCase):

	def test_mismatch_compares_only_what_both_sides_know(self):
		d = not_detected(hostname="a", driver_version="1", gpu_models=["X", "Y"])
		self.assertEqual(m.configuration_mismatch(d, None), [])
		self.assertEqual(m.configuration_mismatch(d, {"hostname": "a"}), [])
		self.assertEqual(m.configuration_mismatch(
			d, {"hostname": "a", "driver_version": "1", "gpu_models": ["Y", "X"]}), [])
		out = m.configuration_mismatch(
			d, {"hostname": "b", "driver_version": "2", "gpu_models": ["X"]})
		self.assertEqual(len(out), 3)
		self.assertEqual(m.configuration_mismatch(not_detected(), {"hostname": "b"}), [])

	def test_from_hardware_report_records_the_tested_configuration(self):
		rep = {"generated_at": 5.0, "host": {"hostname": "box"},
		       "gpu": {"driver_version": "610.88", "devices": [{"index": 0, "name": "G"}]},
		       "gpu_counter_disturbance": {"status": "SUPPORTED", "verdict": "NOT_DETECTED",
		                                   "retained_median": 1.0, "read_hz": 10.0}}
		d = CounterDisturbance.from_hardware_report(rep, "hw.json")
		self.assertEqual((d.hostname, d.driver_version, d.gpu_models), ("box", "610.88", ["G"]))
		self.assertEqual(CounterDisturbance.from_dict(d.to_dict()).to_dict(), d.to_dict())
		self.assertEqual(m.resolve_mode("auto", d, {"hostname": "other"})[0], "sampled")

	def test_header_form_is_unchanged_when_nothing_new_is_known(self):
		self.assertEqual(set(CounterDisturbance.unknown().to_dict()), DISTURBANCE_KEYS_V03)
		self.assertEqual(set(CounterDisturbance("DISTURBED", "t", driver_version="1").to_dict()),
		                 DISTURBANCE_KEYS_V03)

	def test_current_configuration_does_not_touch_nvml_without_a_started_reader(self):
		with mock.patch.object(nvml, "init", side_effect=AssertionError("NVML touched")), \
		     mock.patch.object(nvml, "driver_version", side_effect=AssertionError("NVML touched")):
			cfg = m.current_configuration(None)
			self.assertEqual(cfg["driver_version"], None)
			m.current_configuration(CallableReader(lambda: [0]))
			m.current_configuration(NvmlReader())          # never started


class TestNoWarning(Base):

	def test_no_warning_and_the_category_still_exists(self):
		self.assertTrue(issubclass(SessionDefaultModeWarning, FutureWarning))
		self.assertTrue(issubclass(m.SessionDefaultModeWarning, FutureWarning))
		with warnings.catch_warnings(record=True) as w:
			warnings.simplefilter("always")
			Session(self.path(), gpu=False)
			Session.from_env(self.path(), gpu=False)
			Session(self.path(), gpu=False, mode="direct")
		self.assertEqual([str(x.message) for x in w], [])

	def test_a_default_session_runs_under_warnings_as_errors(self):
		code = ("import os, tempfile\n"
		        "from agent_energy_profiler import Session\n"
		        "d = tempfile.mkdtemp()\n"
		        "with Session(os.path.join(d, 'x.jsonl'), gpu=False) as s:\n"
		        "    with s.span('agent:x'):\n"
		        "        pass\n"
		        "print(s.mode, s.requested_mode, s.mode_source, len(os.listdir(d)))\n")
		env = dict(os.environ, PYTHONPATH=SRC + os.pathsep + os.environ.get("PYTHONPATH", ""))
		env.pop("PYTHONWARNINGS", None)
		env.pop(Session.ENV_MEASUREMENT, None)
		r = subprocess.run([sys.executable, "-W", "error", "-c", code], capture_output=True,
		                   text=True, env=env, timeout=60)
		self.assertEqual((r.returncode, r.stderr), (0, ""))
		self.assertEqual(r.stdout.split(), ["sampled", "auto", "default", "1"])


class TestReportOfTheDefault(Base):

	def test_the_report_says_per_span_gpu_energy_is_estimated(self):
		p = self.path()
		with Session(p, gpu=CallableReader(Counter().read)) as s:
			with s.span("agent:x"):
				pass
		rep = report.build(p)
		codes = {w["code"] for w in rep["warnings"]}
		self.assertIn("SPAN_ESTIMATES_ONLY", codes)
		self.assertNotIn("DIRECT_MODE_BY_DEFAULT", codes)
		text = report.render_text(rep)
		self.assertIn("mode sampled (requested auto, default)", text)


if __name__ == "__main__":
	unittest.main()
