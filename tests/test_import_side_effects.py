"""M1.4: importing the profiler touches no hardware.

Each check runs in a fresh interpreter with a fake `pynvml` that records every
call and a `glob.glob` that records every sysfs scan, so the result does not
depend on whether this machine has a GPU or RAPL.
"""

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")

#: Prelude run before the code under test: a recording fake NVML with one
#: GPU, and a recording glob.
PRELUDE = textwrap.dedent("""
	import glob, json, sys, types
	sys.path.insert(0, %(src)r)
	calls = []
	fake = types.ModuleType("pynvml")
	def _rec(name, value=None):
		def f(*a, **k):
			calls.append(name)
			return value
		return f
	fake.nvmlInit = _rec("nvmlInit")
	fake.nvmlShutdown = _rec("nvmlShutdown")
	fake.nvmlDeviceGetCount = _rec("nvmlDeviceGetCount", 1)
	fake.nvmlDeviceGetHandleByIndex = _rec("nvmlDeviceGetHandleByIndex", "h0")
	fake.nvmlDeviceGetTotalEnergyConsumption = _rec("energy", 1000)
	fake.nvmlDeviceGetPowerUsage = _rec("power", 50000)
	fake.nvmlDeviceGetName = _rec("name", "FakeGPU")
	sys.modules["pynvml"] = fake
	_glob = glob.glob
	def recording_glob(pattern, *a, **k):
		calls.append("glob:" + pattern)
		return _glob(pattern, *a, **k)
	glob.glob = recording_glob
	def report(**extra):
		print(json.dumps(dict(calls=calls, **extra)))
""")


def run(code, cwd=None):
	out = subprocess.run(
		[sys.executable, "-c", PRELUDE % {"src": SRC} + textwrap.dedent(code)],
		capture_output=True, text=True, timeout=60, cwd=cwd)
	if out.returncode != 0:
		raise AssertionError(out.stderr)
	return json.loads(out.stdout.strip().splitlines()[-1])


class TestImportsTouchNoHardware(unittest.TestCase):

	def test_every_module_imports_without_hardware_access(self):
		result = run("""
			import importlib
			for name in ("agent_energy_profiler", "agent_energy_profiler.sampling",
			             "agent_energy_profiler.events", "agent_energy_profiler.profiler",
			             "agent_energy_profiler.attribution", "agent_energy_profiler.validation",
			             "agent_energy_profiler.hardware", "agent_energy_profiler.aggregate",
			             "agent_energy_profiler.trajectory", "agent_energy_profiler.visualize"):
				importlib.import_module(name)
			from agent_energy_profiler.hardware import nvml
			import threading
			report(nvml_ok=nvml.available(), threads=threading.active_count())
		""")
		self.assertEqual(result["calls"], [])
		self.assertFalse(result["nvml_ok"])
		self.assertEqual(result["threads"], 1, "an import started a thread")

	def test_import_works_without_pynvml_installed(self):
		result = run("""
			sys.modules["pynvml"] = None  # import pynvml -> ImportError
			import agent_energy_profiler.sampling as s
			from agent_energy_profiler.hardware import nvml
			report(available=nvml.available())
		""")
		self.assertFalse(result["available"])

	def test_sampler_initialises_lazily_on_first_use(self):
		result = run("""
			import agent_energy_profiler.sampling as s
			before = list(calls)
			header = s.build_header()
			report(before=before, header=header)
		""")
		self.assertEqual(result["before"], [])
		self.assertIn("nvmlInit", result["calls"])
		self.assertTrue(any(c.startswith("glob:") for c in result["calls"]))
		self.assertEqual(result["header"][:3], ["t", "gpu0_w", "gpu0_energy_mj"])

	def test_init_is_idempotent(self):
		result = run("""
			import agent_energy_profiler.sampling as s
			s.init(); s.init(); s.build_header(); s.warn_about_gaps()
			report()
		""")
		self.assertEqual(result["calls"].count("nvmlInit"), 1)
		self.assertEqual(result["calls"].count("glob:/sys/class/powercap/*/energy_uj"), 1)

	def test_sampler_main_writes_sidecar_and_shuts_nvml_down(self):
		with tempfile.TemporaryDirectory() as d:
			out = os.path.join(d, "power.csv")
			result = run("""
				import os, signal, time
				import agent_energy_profiler.sampling as s
				n = {"rows": 0}
				def fake_sleep(_):
					n["rows"] += 1
					if n["rows"] >= 2:
						os.kill(os.getpid(), signal.SIGINT)
				time.sleep = fake_sleep
				sys.argv = ["aep-sample", "--out", %r]
				s.main()
				report()
			""" % out)
			with open(out) as f:
				lines = f.read().splitlines()
			with open(out + ".meta.json") as f:
				meta = json.load(f)
		self.assertEqual(lines[0].split(",")[:3], ["t", "gpu0_w", "gpu0_energy_mj"])
		self.assertGreaterEqual(len(lines), 3)
		self.assertEqual(meta["format"], "agent_energy_profiler.power_meta")
		self.assertIsInstance(meta["rapl_max_energy_range_uj"], dict)
		self.assertEqual(result["calls"][-1], "nvmlShutdown")

	def test_shutdown_is_safe_without_init(self):
		result = run("""
			from agent_energy_profiler.hardware import nvml
			nvml.shutdown(); nvml.shutdown()
			report(available=nvml.available())
		""")
		self.assertEqual(result["calls"], [])
		self.assertFalse(result["available"])


if __name__ == "__main__":
	unittest.main()
