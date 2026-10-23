"""M3: the `agent-energy-profiler` command line (run, attribute, report, export).

M9 renamed the command from `aep`; the tests run it as
`python -m agent_energy_profiler`, which is the same entry point.
"""

import csv
import gzip
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
GOLDEN = os.path.join(HERE, "golden")
EXAMPLES = os.path.join(ROOT, "examples")

FAKE_PYNVML = textwrap.dedent("""
	import time
	_t0 = time.time()
	def nvmlInit(): pass
	def nvmlShutdown(): pass
	def nvmlDeviceGetCount(): return 1
	def nvmlDeviceGetHandleByIndex(i): return i
	def nvmlDeviceGetTotalEnergyConsumption(h): return int((time.time() - _t0) * 50_000)
	def nvmlDeviceGetPowerUsage(h): return 50_000
	def nvmlDeviceGetName(h): return "FakeGPU"
	def nvmlSystemGetDriverVersion(): return "fake"
""")


def cli(*args, env=None, cwd=None):
	return subprocess.run([sys.executable, "-m", "agent_energy_profiler", *args],
	                      capture_output=True, text=True, env=env, cwd=cwd, timeout=300)


class CliCase(unittest.TestCase):

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
		return dict(os.environ, PYTHONPATH=fake)


class TestRun(CliCase):

	def test_instrumented_program_gets_spans_attribution_and_report(self):
		out = os.path.join(self.d, "run")
		res = cli("run", "--out-dir", out, "--hz", "50", "--attr", "purpose=test", "--",
		          sys.executable, os.path.join(EXAMPLES, "agent_workflow", "agent.py"),
		          "--work", "50", env=self.fake_env())
		self.assertEqual(res.returncode, 0, res.stderr)
		for name in ("run.json", "spans.jsonl", "power.csv", "power.csv.meta.json",
		             "spans_attributed.jsonl", "session_summary.json"):
			self.assertTrue(os.path.exists(os.path.join(out, name)), name)
		with open(os.path.join(out, "run.json")) as f:
			run = json.load(f)
		self.assertTrue(run["instrumented"])
		self.assertEqual(run["exit_code"], 0)
		self.assertEqual(run["whole_program"]["gpu_in_band"]["status"], "measured")
		self.assertEqual(run["attributes"], {"purpose": "test"})
		with open(os.path.join(out, "spans.jsonl")) as f:
			header = json.loads(f.readline())
		self.assertEqual(header["attributes"]["purpose"], "test")
		self.assertEqual(header["attributes"]["example"], "agent_workflow")
		self.assertIn("ENERGY REPORT", res.stdout)
		self.assertIn("agent:question", res.stdout)

	def test_uninstrumented_program_gets_whole_program_energy_only(self):
		out = os.path.join(self.d, "run")
		script = os.path.join(self.d, "plain.py")
		with open(script, "w") as f:
			f.write("print(sum(range(10**6)))\n")
		res = cli("run", "--out-dir", out, "--hz", "50", "--", sys.executable, script,
		          env=self.fake_env())
		self.assertEqual(res.returncode, 0, res.stderr)
		with open(os.path.join(out, "run.json")) as f:
			run = json.load(f)
		self.assertFalse(run["instrumented"])
		self.assertIn("no spans were recorded", res.stdout)
		self.assertFalse(os.path.exists(os.path.join(out, "spans_attributed.jsonl")))
		self.assertEqual(run["whole_program"]["gpu_in_band"]["status"], "measured")

	def test_child_exit_code_is_propagated_and_gpu_off_works(self):
		out = os.path.join(self.d, "run")
		res = cli("run", "--out-dir", out, "--no-sampler", "--gpu", "off", "--",
		          sys.executable, "-c", "import sys; sys.exit(3)")
		self.assertEqual(res.returncode, 3)
		with open(os.path.join(out, "run.json")) as f:
			run = json.load(f)
		self.assertEqual(run["whole_program"]["gpu_in_band"]["reason"], "disabled")
		self.assertIsNone(run["whole_program"]["sampler_window"])

	def test_existing_output_is_not_overwritten(self):
		out = os.path.join(self.d, "run")
		os.makedirs(out)
		open(os.path.join(out, "spans.jsonl"), "w").close()
		res = cli("run", "--out-dir", out, "--no-sampler", "--", sys.executable, "-c", "pass")
		self.assertEqual(res.returncode, 2)

	def test_missing_command(self):
		self.assertEqual(cli("run", "--out-dir", self.d).returncode, 2)

	def test_readme_quick_start_runs(self):
		# the README's quick start is examples/quickstart.py, run as documented
		out = os.path.join(self.d, "runs", "quickstart")
		res = cli("run", "--out-dir", out, "--", sys.executable,
		          os.path.join(EXAMPLES, "quickstart.py"), env=self.fake_env(), cwd=self.d)
		self.assertEqual(res.returncode, 0, res.stderr)
		for label in ("agent:answer", "retrieval:search", "llm:generate", "verify:check"):
			self.assertIn(label, res.stdout)
		self.assertIn("ESTIMATED ENERGY", res.stdout)
		res = cli("report", out)
		self.assertEqual(res.returncode, 0, res.stderr)
		self.assertIn("llm:generate", res.stdout)

	def test_no_gpu_counter_says_why(self):
		fake = self.fake_env()
		with open(os.path.join(fake["PYTHONPATH"], "pynvml.py"), "a") as f:
			f.write("def nvmlDeviceGetCount(): return 0\n")
		res = cli("run", "--out-dir", os.path.join(self.d, "run"), "--no-sampler", "--",
		          sys.executable, "-c", "pass", env=fake)
		self.assertEqual(res.returncode, 0, res.stderr)
		self.assertIn("no GPU energy counter", res.stderr)
		self.assertIn("agent-energy-profiler validate", res.stderr)
		res = cli("run", "--out-dir", os.path.join(self.d, "off"), "--no-sampler",
		          "--gpu", "off", "--", sys.executable, "-c", "pass")
		self.assertNotIn("no GPU energy counter", res.stderr)

	def test_no_gpu_hint_names_the_extra_when_nvml_is_not_installed(self):
		from unittest import mock
		from agent_energy_profiler import cli as cli_module
		with mock.patch("importlib.util.find_spec", return_value=None):
			self.assertIn("agent-energy-profiler[gpu]", cli_module._no_gpu_hint())


class TestAttributeReportExport(CliCase):

	def unpack(self, case, kind, names):
		for name in names:
			with gzip.open(os.path.join(GOLDEN, case, kind, name + ".gz"), "rb") as g, \
			     open(os.path.join(self.d, name), "wb") as f:
				f.write(g.read())

	def test_v1_attribute_reproduces_the_golden_fixture(self):
		self.unpack("synthetic_rapl_multigpu", "input", ("events.jsonl", "power.csv"))
		res = cli("attribute", os.path.join(self.d, "events.jsonl"),
		          "--power", os.path.join(self.d, "power.csv"), "--out-dir", self.d)
		self.assertEqual(res.returncode, 0, res.stderr)
		for name in ("events_attributed.jsonl", "energy_summary.csv",
		             "trajectory_summary.csv", "trajectory_summary.json"):
			with gzip.open(os.path.join(GOLDEN, "synthetic_rapl_multigpu", "expected",
			                            name + ".gz"), "rb") as g, \
			     open(os.path.join(self.d, name), "rb") as f:
				self.assertTrue(f.read() == g.read(), name)

	def test_v1_report_formats(self):
		self.unpack("synthetic_rapl_multigpu", "expected",
		            ("events_attributed.jsonl", "energy_summary.csv", "trajectory_summary.json"))
		text = cli("report", self.d)
		self.assertEqual(text.returncode, 0, text.stderr)
		self.assertIn("ESTIMATED ENERGY", text.stdout)
		js = cli("report", self.d, "--format", "json")
		rep = json.loads(js.stdout)
		self.assertEqual(rep["source"]["schema"], 1)
		out = os.path.join(self.d, "ops.csv")
		self.assertEqual(cli("report", self.d, "--format", "csv", "--out", out).returncode, 0)
		with open(out) as f:
			self.assertEqual(len(list(csv.DictReader(f))), 4)

	def test_export_v2(self):
		self.unpack("synthetic_rapl_multigpu", "input", ("events.jsonl", "power.csv"))
		res = cli("attribute", os.path.join(self.d, "events.jsonl"), "--schema", "2",
		          "--power", os.path.join(self.d, "power.csv"), "--out-dir", self.d)
		self.assertEqual(res.returncode, 0, res.stderr)
		out = os.path.join(self.d, "export")
		res = cli("export", os.path.join(self.d, "spans_attributed.jsonl"), "--out-dir", out)
		self.assertEqual(res.returncode, 0, res.stderr)
		with open(os.path.join(out, "spans.csv")) as f:
			rows = list(csv.DictReader(f))
		self.assertEqual(len(rows), 8)
		self.assertIn("estimated_gpu_energy_j", rows[0])
		self.assertIn("gpu_exclusive_status", rows[0])
		integrated = [r for r in rows if r["estimated_gpu_energy_j"] and not r["gpu_energy_j"]]
		self.assertEqual(len(integrated), 3)
		for name in ("operations.csv", "report.json"):
			self.assertTrue(os.path.exists(os.path.join(out, name)))

	def test_export_refuses_raw_input(self):
		self.unpack("synthetic_rapl_multigpu", "input", ("events.jsonl",))
		res = cli("export", os.path.join(self.d, "events.jsonl"), "--out-dir", self.d)
		self.assertEqual(res.returncode, 2)

	def test_help_states_the_instrumentation_requirement(self):
		res = cli("run", "--help")
		self.assertIn("Session.from_env", res.stdout)
		self.assertIn("Whole-program energy", res.stdout)
		self.assertEqual(cli("--version").stdout.split()[0], "agent-energy-profiler")

	def test_command_is_named_after_the_distribution(self):
		res = cli("--help")
		self.assertTrue(res.stdout.startswith("usage: agent-energy-profiler "), res.stdout[:80])
		self.assertNotIn("aep ", res.stdout)

	def test_default_run_directory_is_named_after_the_command(self):
		res = cli("run", "--no-sampler", "--gpu", "off", "--", sys.executable, "-c", "pass",
		          cwd=self.d)
		self.assertEqual(res.returncode, 0, res.stderr)
		self.assertEqual(os.listdir(self.d), ["agent-energy-profiler-runs"])
		self.assertIn("agent-energy-profiler run: measurement mode", res.stderr)


if __name__ == "__main__":
	unittest.main()
