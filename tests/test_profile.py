"""Session.profile: run directory, sampler, attribution and report managed for
the caller (0.4.0)."""

import asyncio
import contextlib
import importlib.util
import io
import json
import os
import platform
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest import mock

from agent_energy_profiler import report as report_module
from agent_energy_profiler.measurement import CallableReader
from agent_energy_profiler.sampling import Sampler
from agent_energy_profiler.session import Session

HERE = os.path.dirname(os.path.abspath(__file__))
EXAMPLES = os.path.join(os.path.dirname(HERE), "examples")

#: The sampler subprocess imports this instead of the real NVML.
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

#: An NVML that is installed but finds nothing (a host without an NVIDIA GPU).
NO_GPU_PYNVML = "def nvmlInit():\n\traise RuntimeError('NVML_ERROR_LIBRARY_NOT_FOUND')\n"

RUN_ENV = ("AEP_SPANS_FILE", "AEP_RESOLUTION", "AEP_GPU", "AEP_RUN_ATTRIBUTES",
           "AEP_MEASUREMENT")


class Counter_:
	"""A parent-side GPU counter for direct mode (CallableReader)."""

	def __init__(self):
		self.v = 1_000_000

	def read(self):
		self.v += 500
		return [self.v]


def records(path):
	with open(path) as f:
		return [json.loads(line) for line in f if line.strip()]


class ProfileCase(unittest.TestCase):

	def setUp(self):
		self._tmp = tempfile.TemporaryDirectory()
		self.d = self._tmp.name
		fake = os.path.join(self.d, "fake")
		os.makedirs(fake)
		with open(os.path.join(fake, "pynvml.py"), "w") as f:
			f.write(FAKE_PYNVML)
		self.fake_dir = fake
		# the sampler subprocess inherits os.environ; no `run` configuration leaks in
		env = {k: v for k, v in os.environ.items() if k not in RUN_ENV}
		env["PYTHONPATH"] = fake
		patcher = mock.patch.dict(os.environ, env, clear=True)
		patcher.start()
		self.addCleanup(patcher.stop)

	def tearDown(self):
		self._tmp.cleanup()

	def out(self, name="run"):
		return os.path.join(self.d, name)

	def profile(self, name="run", **kw):
		kw.setdefault("out_dir", self.out(name))
		kw.setdefault("gpu", CallableReader(Counter_().read))
		kw.setdefault("quiet", True)
		return Session.profile(name, **kw)

	def header(self, session):
		return records(session.path)[0]

	def spans(self, session):
		return {r["name"]: r for r in records(session.path) if r["record_type"] == "span"}


class TestStandalone(ProfileCase):

	def test_records_attributes_saves_and_prints_the_report(self):
		err, out = io.StringIO(), io.StringIO()
		with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
			with self.profile(quiet=False) as p:
				self.assertEqual(p.profile_status, "recording")
				self.assertTrue(p.sampler.running)
				with p.span("agent:answer"):
					with p.span("llm:generate") as s:
						time.sleep(0.05)
						s.set_tokens(input=10, output=2)
		self.assertEqual(p.profile_status, "complete")
		self.assertIsNone(p.profile_error)
		self.assertFalse(p.sampler.running)
		self.assertEqual(p.out_dir, self.out())
		for name in ("spans.jsonl", "power.csv", "power.csv.meta.json", "spans_attributed.jsonl",
		             "session_summary.json", "report.json", "report.txt"):
			self.assertTrue(os.path.exists(os.path.join(p.out_dir, name)), name)
		with open(os.path.join(p.out_dir, "report.json")) as f:
			self.assertEqual(json.load(f), p.report)
		labels = {op["operation"] for op in p.report["operations"]}
		self.assertEqual(labels, {"agent:answer", "llm:generate"})
		# the report goes to stderr; the program's stdout is left alone
		self.assertEqual(out.getvalue(), "")
		self.assertIn(f"profiling into {p.out_dir}", err.getvalue())
		self.assertIn("ENERGY REPORT", err.getvalue())
		self.assertIn("llm:generate", err.getvalue())

	def test_sampler_follows_the_mode_not_its_every_row_default(self):
		expected = {"direct": "boundaries", "sampled": "boundaries", "hybrid": "every_n_rows",
		            None: "boundaries", "auto": "boundaries"}
		for mode, policy in expected.items():
			with self.profile(f"m-{mode}", mode=mode, report=False) as p:
				with p.span("llm:x"):
					pass
			meas = self.header(p)["config"]["measurement"]
			self.assertEqual(meas["timeline"]["counter_policy"], policy, mode)
			self.assertEqual(self.header(p)["config"]["sampler"]["timeline"]["counter_policy"],
			                 policy, mode)
			self.assertEqual(meas["mode"], "sampled" if mode in (None, "auto") else mode)
			self.assertEqual(meas["mode_source"], "default" if mode is None else "caller")
			with open(p.sampler.power_file + ".meta.json") as f:
				self.assertEqual(json.load(f)["timeline"]["counter_policy"], policy)

	def test_measured_and_estimated_stay_separate(self):
		with self.profile("direct", mode="direct") as p:
			with p.span("llm:x"):
				time.sleep(0.02)
		g = self.spans(p)["llm:x"]["energy"]["gpu"]
		self.assertEqual(g["status"], "measured")
		with self.profile("sampled", mode="sampled") as p:
			with p.span("llm:x"):
				time.sleep(0.02)
		g = self.spans(p)["llm:x"]["energy"]["gpu"]
		self.assertEqual((g["status"], g["reason"], g["energy_j"]),
		                 ("unavailable", "not_read_by_mode", None))
		self.assertTrue(p.report["totals"]["estimated_gpu"])

	def test_hardware_report_sets_resolution_and_disturbance(self):
		hw = os.path.join(self.d, "hw.json")
		rep = {"generated_at": 1.0, "host": {"hostname": platform.node()},
		       "gpu": {"driver_version": None},
		       "gpu_counter_resolution": {"status": "SUPPORTED",
		                                  "median_update_interval_s": 0.1,
		                                  "updates_observed": 30},
		       "gpu_counter_disturbance": {"status": "SUPPORTED", "verdict": "NOT_DETECTED"}}
		with open(hw, "w") as f:
			json.dump(rep, f)
		with self.profile("ok", hardware_report=hw, report=False) as p:
			pass
		cfg = self.header(p)["config"]
		self.assertEqual(cfg["measurement"]["mode"], "direct")
		self.assertEqual(cfg["measurement"]["counter_disturbance"]["source"], hw)
		self.assertEqual(cfg["resolution"]["gpu_update_interval_s"], 0.1)
		rep["gpu_counter_disturbance"]["verdict"] = "DISTURBED"
		with open(hw, "w") as f:
			json.dump(rep, f)
		with self.profile("disturbed", hardware_report=hw, report=False) as p:
			pass
		self.assertEqual(self.header(p)["config"]["measurement"]["mode"], "sampled")
		with self.assertRaises(OSError):
			self.profile("missing", hardware_report=os.path.join(self.d, "nope.json"))

	def test_managed_arguments_are_refused(self):
		for kw in ({"sampler": None}, {"overwrite": True}, {"resolution": None},
		           {"disturbance": None}, {"path": "x"}, {"timeline": None}):
			with self.assertRaises(TypeError, msg=kw):
				Session.profile("x", **kw)

	def test_report_false_records_only_and_analyze_works_later(self):
		with self.profile(report=False) as p:
			with p.span("llm:x"):
				pass
		self.assertEqual(p.profile_status, "recorded")
		self.assertIsNone(p.report)
		self.assertFalse(os.path.exists(os.path.join(p.out_dir, "spans_attributed.jsonl")))
		rep = report_module.analyze(p)
		self.assertEqual([op["operation"] for op in rep["operations"]], ["llm:x"])

	def test_quiet_prints_nothing(self):
		err = io.StringIO()
		with contextlib.redirect_stderr(err):
			with self.profile(quiet=True) as p:
				with p.span("llm:x"):
					pass
		self.assertEqual(err.getvalue(), "")
		self.assertEqual(p.profile_status, "complete")

	def test_start_and_close_without_with(self):
		# the notebook pattern
		p = self.profile().start()
		with p.span("llm:x"):
			pass
		p.close()
		p.close()                                         # idempotent
		self.assertEqual(p.profile_status, "complete", repr(p.profile_error))
		self.assertFalse(p.sampler.running)


class TestOutputDirectories(ProfileCase):

	def setUp(self):
		super().setUp()
		cwd = os.getcwd()
		os.chdir(self.d)
		self.addCleanup(os.chdir, cwd)

	def test_generated_directories_are_unique(self):
		with mock.patch("time.strftime", return_value="20260101-000000"):
			a = Session.profile("my run/../x", gpu=False, quiet=True, report=False)
			b = Session.profile("my run/../x", gpu=False, quiet=True, report=False)
		# nothing exists before the session starts
		self.assertFalse(os.path.exists(Session.PROFILE_RUNS_DIR))
		with a, b:
			pass
		self.assertNotEqual(a.out_dir, b.out_dir)
		self.assertEqual(os.path.dirname(a.out_dir), Session.PROFILE_RUNS_DIR)
		self.assertEqual(b.out_dir, a.out_dir + "-2")
		self.assertNotIn(os.sep, os.path.basename(a.out_dir))
		self.assertTrue(os.path.basename(a.out_dir).startswith("my-run-"))
		self.assertTrue(os.path.exists(os.path.join(b.out_dir, "spans.jsonl")))

	def test_explicit_directory_is_never_overwritten(self):
		for existing in ("spans.jsonl", "power.csv"):
			out = os.path.join(self.d, f"taken-{existing}")
			os.makedirs(out)
			with open(os.path.join(out, existing), "w") as f:
				f.write("earlier run\n")
			p = Session.profile(out_dir=out, gpu=False, quiet=True)
			with self.assertRaises(FileExistsError):
				with p:
					self.fail("the workload must not run")
			with open(os.path.join(out, existing)) as f:
				self.assertEqual(f.read(), "earlier run\n")
			self.assertEqual(p.profile_status, "created")
			self.assertIsNone(p.sampler)

	def test_explicit_directory_may_exist_empty(self):
		out = os.path.join(self.d, "empty")
		os.makedirs(out)
		with Session.profile(out_dir=out, gpu=False, quiet=True) as p:
			with p.span("llm:x"):
				pass
		self.assertEqual(p.profile_status, "complete")

	def test_any_earlier_profile_file_blocks_an_explicit_directory(self):
		out = os.path.join(self.d, "exported")
		os.makedirs(out)
		with open(os.path.join(out, "report.json"), "w") as f:
			f.write("{}\n")
		with self.assertRaises(FileExistsError) as ctx:
			with Session.profile(out_dir=out, gpu=False, quiet=True):
				self.fail("the workload must not run")
		self.assertIn("report.json", str(ctx.exception))
		with open(os.path.join(out, "report.json")) as f:
			self.assertEqual(f.read(), "{}\n")


class TestNoSpans(ProfileCase):

	def test_no_operation_data_but_the_whole_session_measurement_is_reported(self):
		err = io.StringIO()
		with contextlib.redirect_stderr(err):
			with self.profile(quiet=False) as p:
				time.sleep(0.25)
		self.assertEqual(p.profile_status, "no_spans")
		self.assertIsNone(p.profile_error)
		self.assertIsNone(p.report)
		message = err.getvalue()
		self.assertIn("no spans were recorded, so there is no operation-level data", message)
		# the sampler's whole-session counter reading, labelled device-level
		self.assertRegex(message, r"GPU, sampler run counter \(2 readings, [0-9.]+ s\): "
		                          r"[0-9.]+ J")
		self.assertIn("device-level", message)
		self.assertIn("cpu_package, sampled counter: n/a", message)
		self.assertNotIn("ENERGY REPORT", message)
		# nothing attributed to an operation, and the raw data kept
		for name in ("spans_attributed.jsonl", "report.json", "report.txt"):
			self.assertFalse(os.path.exists(os.path.join(p.out_dir, name)), name)
		self.assertTrue(os.path.exists(p.sampler.power_file))

	def test_without_a_gpu_the_reading_is_missing_not_zero(self):
		with open(os.path.join(self.fake_dir, "pynvml.py"), "w") as f:
			f.write(NO_GPU_PYNVML)
		err = io.StringIO()
		with contextlib.redirect_stderr(err):
			with self.profile(quiet=False, gpu=False) as p:
				pass
		self.assertEqual(p.profile_status, "no_spans")
		self.assertIn("GPU, sampler run counter (0 readings): n/a", err.getvalue())
		self.assertNotIn(" 0.000 J", err.getvalue())

	def test_quiet_and_report_false(self):
		err = io.StringIO()
		with contextlib.redirect_stderr(err):
			with self.profile(quiet=True) as p:
				pass
		self.assertEqual((p.profile_status, err.getvalue()), ("no_spans", ""))
		with self.profile("offline", report=False) as p:
			pass
		self.assertEqual(p.profile_status, "no_spans")


@unittest.skipUnless(importlib.util.find_spec("opentelemetry") is not None,
                     "needs the otel extra")
class TestOpenTelemetry(ProfileCase):

	def test_hooks_correlate_and_the_profile_exports(self):
		from opentelemetry.sdk.trace import TracerProvider
		from opentelemetry.sdk.trace.export import SimpleSpanProcessor
		from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
		from agent_energy_profiler import otel
		memory = InMemorySpanExporter()
		provider = TracerProvider()
		provider.add_span_processor(SimpleSpanProcessor(memory))
		tracer = provider.get_tracer("test")
		with self.profile(hooks=[otel.Correlation(propagate=True)]) as p:
			with tracer.start_as_current_span("POST /ask") as http:
				app_trace = format(http.get_span_context().trace_id, "032x")
				with p.span("agent:request"):
					with p.span("retrieval:search"):
						with tracer.start_as_current_span("vector_db.query"):
							time.sleep(0.02)
		self.assertEqual(p.profile_status, "complete")
		header = self.header(p)
		self.assertEqual(header["config"]["hooks"],
		                 ["agent_energy_profiler.otel.Correlation"])
		spans = self.spans(p)
		self.assertEqual(spans["agent:request"]["trace_id"], app_trace)
		self.assertEqual(spans["agent:request"]["external_parent"]["trace_id"], app_trace)
		# after the profile: the attributed run exports into the application's pipeline
		result = otel.export_spans(p.out_dir, memory)
		self.assertEqual(result["n_spans"], 2)
		finished = {s.name: s for s in memory.get_finished_spans()}
		self.assertEqual(set(finished), {"POST /ask", "vector_db.query", "agent:request",
		                                 "retrieval:search"})
		retrieval = finished["retrieval:search"].attributes
		self.assertEqual(retrieval["aep.energy.gpu.status"], "unavailable")
		self.assertNotIn("aep.energy.gpu.energy_j", retrieval)      # not measured...
		self.assertIn("aep.estimate.gpu.energy_j", retrieval)       # ...estimated, and labelled
		# the application's span nested under the profiler span (propagate=True)
		self.assertEqual(finished["vector_db.query"].parent.span_id,
		                 finished["retrieval:search"].context.span_id)
		provider.shutdown()

	def test_otel_export_cli_reads_a_profile_directory(self):
		with self.profile() as p:
			with p.span("llm:x"):
				pass
		out = os.path.join(self.d, "otel.jsonl")
		res = subprocess.run([sys.executable, "-m", "agent_energy_profiler", "otel-export",
		                      p.out_dir, "--out", out], capture_output=True, text=True,
		                     timeout=120)
		self.assertEqual(res.returncode, 0, res.stderr)
		with open(out) as f:
			exported = [json.loads(line) for line in f]
		self.assertEqual([s["name"] for s in exported], ["llm:x"])


class TestExamples(ProfileCase):

	def example(self, *args, cwd=None):
		return subprocess.run([sys.executable, os.path.join(EXAMPLES, *args[0].split("/")),
		                       *args[1:]], capture_output=True, text=True, timeout=300,
		                      cwd=cwd or self.d)

	def test_agent_workflow_on_its_own_collects_energy(self):
		res = self.example("agent_workflow/agent.py", "--work", "20")
		self.assertEqual(res.returncode, 0, res.stderr)
		self.assertIn("answered 6 questions", res.stdout)
		runs = os.listdir(os.path.join(self.d, Session.PROFILE_RUNS_DIR))
		self.assertEqual(len(runs), 1)
		self.assertTrue(runs[0].startswith("agent_workflow-"))
		self.assertIn(f"run directory: {os.path.join(Session.PROFILE_RUNS_DIR, runs[0])}",
		              res.stdout)
		self.assertIn("ESTIMATED ENERGY", res.stderr)
		self.assertIn("RUN COUNTER MEASUREMENT", res.stderr)
		header = records(os.path.join(self.d, Session.PROFILE_RUNS_DIR, runs[0],
		                              "spans.jsonl"))[0]
		self.assertEqual(header["config"]["sampler"]["timeline"]["counter_policy"], "boundaries")

	def test_cpu_workflow_with_an_explicit_directory(self):
		out = os.path.join(self.d, "cpu")
		res = self.example("cpu_workflow/workflow.py", "--scale", "0.02", "--out-dir", out)
		self.assertEqual(res.returncode, 0, res.stderr)
		with open(os.path.join(out, "report.json")) as f:
			ops = {op["operation"] for op in json.load(f)["operations"]}
		self.assertEqual(ops, {"pipeline:run", "data:generate", "compute:sort",
		                       "compute:primes", "io:serialize"})
		res = self.example("cpu_workflow/workflow.py", "--scale", "0.02", "--out-dir", out)
		self.assertNotEqual(res.returncode, 0)                 # never overwritten
		self.assertIn("FileExistsError", res.stderr)

	def test_examples_under_run_use_one_sampler(self):
		for script, extra in (("agent_workflow/agent.py", ["--work", "20", "--parallel"]),
		                      ("cpu_workflow/workflow.py", ["--scale", "0.02"])):
			out = os.path.join(self.d, os.path.basename(script) + "-run")
			res = subprocess.run([sys.executable, "-m", "agent_energy_profiler", "run",
			                      "--out-dir", out, "--", sys.executable,
			                      os.path.join(EXAMPLES, *script.split("/")), *extra],
			                     capture_output=True, text=True, timeout=300, cwd=self.d)
			self.assertEqual(res.returncode, 0, res.stderr)
			self.assertEqual(res.stdout.count("ENERGY REPORT"), 1, script)
			self.assertNotIn("profiling into", res.stderr)
			self.assertIn(f"run directory: {out}", res.stdout)
			self.assertEqual(sorted(n for n in os.listdir(out) if n.startswith("power")),
			                 ["power.csv", "power.csv.meta.json", "power.csv.sampler.log"])
			self.assertEqual(records(os.path.join(out, "spans.jsonl"))[0]["config"]
			                 ["measurement"]["mode_source"], "aep_run")
		self.assertFalse(os.path.exists(os.path.join(self.d, Session.PROFILE_RUNS_DIR)))

	def test_gpu_inference_parses_its_arguments(self):
		# PyTorch is not a test dependency: only the profile wiring is checked
		res = self.example("gpu_inference/inference.py", "--help")
		self.assertEqual(res.returncode, 0, res.stderr)
		self.assertIn("--hardware-report", res.stdout)
		self.assertIn("--out-dir", res.stdout)


class TestConcurrency(ProfileCase):

	def test_async_nested_and_concurrent_spans(self):
		async def tool(p, i):
			async with p.span("tool:call", attributes={"i": i}):
				await asyncio.sleep(0.03)

		async def main():
			async with self.profile() as p:
				async with p.span("agent:plan"):
					await asyncio.gather(*(tool(p, i) for i in range(3)))
			return p
		p = asyncio.run(main())
		self.assertEqual(p.profile_status, "complete")
		spans = [r for r in records(p.path) if r["record_type"] == "span"]
		root = next(s for s in spans if s["name"] == "agent:plan")
		tools = [s for s in spans if s["name"] == "tool:call"]
		self.assertEqual(len(tools), 3)
		self.assertTrue(all(t["parent_span_id"] == root["span_id"] for t in tools))
		ops = {op["operation"]: op for op in p.report["operations"]}
		self.assertEqual(ops["tool:call"]["n"], 3)

	def test_threads(self):
		import threading
		with self.profile() as p:
			with p.span("agent:fanout") as parent:
				def work():
					with p.span("tool:thread", parent=parent):
						time.sleep(0.01)
				threads = [threading.Thread(target=work) for _ in range(2)]
				for t in threads:
					t.start()
				for t in threads:
					t.join()
		self.assertEqual(p.profile_status, "complete")
		self.assertEqual(sum(1 for r in records(p.path) if r.get("name") == "tool:thread"), 2)


class TestFailures(ProfileCase):
	"""Precedence: the workload's exception, then a failure to start (raised
	before any work), then stopping the recording and attribution (recorded on
	the session, raised only in strict mode and only when the workload
	succeeded)."""

	def test_workload_exception_propagates_and_the_run_is_still_reported(self):
		with self.assertRaises(KeyError):
			with self.profile() as p:
				with p.span("llm:x"):
					raise KeyError("workload")
		self.assertEqual(p.profile_status, "complete")
		self.assertEqual(self.spans(p)["llm:x"]["status"], "error")
		self.assertIsNotNone(p.report)

	def test_cancellation_propagates(self):
		async def main():
			async with self.profile() as p:
				async with p.span("llm:x"):
					raise asyncio.CancelledError()

		with self.assertRaises(asyncio.CancelledError):
			asyncio.run(main())
		out = self.out()
		spans = [r for r in records(os.path.join(out, "spans.jsonl"))
		         if r["record_type"] == "span"]
		self.assertEqual(spans[0]["status"], "cancelled")

	def test_attribution_failure_keeps_raw_files_and_says_how_to_recover(self):
		err = io.StringIO()
		with mock.patch.object(report_module, "analyze", side_effect=ValueError("boom")):
			with contextlib.redirect_stderr(err):
				with self.profile(quiet=False) as p:
					with p.span("llm:x"):
						pass
		self.assertEqual(p.profile_status, "incomplete")
		self.assertIsInstance(p.profile_error, ValueError)
		self.assertIsNone(p.report)
		for name in ("spans.jsonl", "power.csv"):
			self.assertTrue(os.path.exists(os.path.join(p.out_dir, name)), name)
		message = err.getvalue()
		self.assertIn("profile incomplete", message)
		self.assertIn("boom", message)
		self.assertIn(p.out_dir, message)
		self.assertIn(f"agent-energy-profiler attribute {p.path} --power", message)
		# the recovery command works on what was left behind
		rep = report_module.analyze(p.path, power=p.sampler.power_file)
		self.assertEqual([op["operation"] for op in rep["operations"]], ["llm:x"])

	def test_strict_raises_only_when_the_workload_succeeded(self):
		with mock.patch.object(report_module, "analyze", side_effect=ValueError("boom")):
			with self.assertRaises(RuntimeError) as ctx:
				with self.profile("strict-ok", strict=True) as p:
					with p.span("llm:x"):
						pass
			self.assertIsInstance(ctx.exception.__cause__, ValueError)
			self.assertEqual(p.profile_status, "incomplete")
			with self.assertRaises(KeyError):              # the workload's error wins
				with self.profile("strict-fail", strict=True) as p:
					with p.span("llm:x"):
						raise KeyError("workload")
			self.assertEqual(p.profile_status, "incomplete")
			p = self.profile("strict-close", strict=True).start()
			with p.span("llm:x"):
				pass
			with self.assertRaises(RuntimeError):          # close() with no workload error
				p.close()

	def test_sampler_stop_failure_is_recorded_not_raised(self):
		real_stop = Sampler.stop

		def failing_stop(sampler):
			real_stop(sampler)
			raise OSError("sampler stuck")
		with mock.patch.object(Sampler, "stop", failing_stop):
			with self.assertRaises(KeyError):
				with self.profile("with-error") as p:
					raise KeyError("workload")
			self.assertEqual(p.profile_status, "incomplete")
			self.assertIsInstance(p.profile_error, OSError)
			with self.profile("plain") as p:
				pass
			self.assertEqual(p.profile_status, "incomplete")
		self.assertEqual(records(p.path)[-1]["record_type"], "session_end")

	def test_sampler_exit_code_marks_the_profile_incomplete(self):
		real_stop = Sampler.stop

		def crashed(sampler):
			real_stop(sampler)
			return 1
		with mock.patch.object(Sampler, "stop", crashed):
			with self.profile() as p:
				pass
		self.assertEqual(p.profile_status, "incomplete")
		self.assertIn("exited with code 1", str(p.profile_error))

	def test_sampler_start_failure_raises_before_the_workload(self):
		calls = []
		with mock.patch.object(Sampler, "start", side_effect=RuntimeError("no samples")), \
		     mock.patch.object(report_module, "analyze", side_effect=calls.append):
			p = self.profile()
			with self.assertRaises(RuntimeError):
				with p:
					self.fail("the workload must not run")
		self.assertEqual(p.profile_status, "failed_to_start")
		self.assertEqual(calls, [])
		self.assertTrue(p.closed)


class TestUnderRun(ProfileCase):

	def run_env(self, spans):
		from agent_energy_profiler import measurement as m, sampling
		return {"AEP_SPANS_FILE": spans,
		        "AEP_MEASUREMENT": json.dumps({
		            "mode": "sampled", "requested_mode": "auto", "mode_reason": "from run",
		            "disturbance": m.CounterDisturbance.unknown().to_dict(),
		            "timeline": sampling.timeline_meta(10, 0)})}

	def test_records_into_the_run_without_a_sampler(self):
		spans = os.path.join(self.d, "run", "spans.jsonl")
		os.makedirs(os.path.dirname(spans))
		err = io.StringIO()
		with mock.patch.dict(os.environ, self.run_env(spans)), contextlib.redirect_stderr(err):
			with Session.profile("ignored", out_dir=os.path.join(self.d, "ignored"),
			                     hardware_report="hw.json", gpu=False) as p:
				self.assertIsNone(p.sampler)
				with p.span("llm:x"):
					pass
		self.assertEqual(p.path, spans)
		self.assertEqual(p.out_dir, os.path.dirname(spans))
		self.assertEqual(p.profile_status, "recorded_for_run")
		self.assertIsNone(p.report)
		self.assertFalse(os.path.exists(os.path.join(self.d, "ignored")))
		self.assertEqual(sorted(os.listdir(p.out_dir)), ["spans.jsonl"])
		meas = records(spans)[0]["config"]["measurement"]
		self.assertEqual((meas["mode"], meas["mode_source"]), ("sampled", "aep_run"))
		self.assertIn("hardware_report", err.getvalue())   # said once, not silently dropped
		self.assertNotIn("profiling into", err.getvalue())

	def test_the_cli_runs_the_quick_start_with_one_sampler(self):
		out = os.path.join(self.d, "cli-run")
		res = subprocess.run([sys.executable, "-m", "agent_energy_profiler", "run", "--out-dir",
		                      out, "--", sys.executable, os.path.join(EXAMPLES, "quickstart.py")],
		                     capture_output=True, text=True, timeout=300, cwd=self.d)
		self.assertEqual(res.returncode, 0, res.stderr)
		self.assertEqual(res.stdout.count("ENERGY REPORT"), 1)
		self.assertNotIn("profiling into", res.stderr)
		self.assertEqual([n for n in os.listdir(out) if n.endswith(".csv")], ["power.csv"])
		self.assertFalse(os.path.exists(os.path.join(self.d, Session.PROFILE_RUNS_DIR)))


class TestQuickStart(ProfileCase):

	def test_readme_quick_start_runs_on_its_own(self):
		# step 3 of the README: `python quickstart.py`, no Sampler written by hand
		res = subprocess.run([sys.executable, os.path.join(EXAMPLES, "quickstart.py")],
		                     capture_output=True, text=True, timeout=300, cwd=self.d)
		self.assertEqual(res.returncode, 0, res.stderr)
		self.assertEqual(res.stdout, "")
		runs = os.listdir(os.path.join(self.d, Session.PROFILE_RUNS_DIR))
		self.assertEqual(len(runs), 1)
		self.assertTrue(runs[0].startswith("quickstart-"))
		run = os.path.join(self.d, Session.PROFILE_RUNS_DIR, runs[0])
		self.assertIn(f"profiling into {os.path.join(Session.PROFILE_RUNS_DIR, runs[0])}",
		              res.stderr)
		for label in ("agent:answer", "retrieval:search", "llm:generate", "verify:check"):
			self.assertIn(label, res.stderr)
		self.assertIn("ESTIMATED ENERGY", res.stderr)
		with open(os.path.join(run, "report.json")) as f:
			self.assertEqual(len(json.load(f)["operations"]), 4)
		# step 4: the CLI reads the directory
		res = subprocess.run([sys.executable, "-m", "agent_energy_profiler", "report", run],
		                     capture_output=True, text=True, timeout=120)
		self.assertEqual(res.returncode, 0, res.stderr)
		self.assertIn("llm:generate", res.stdout)


class TestWithoutGpu(ProfileCase):

	def test_no_nvidia_gpu(self):
		with open(os.path.join(self.fake_dir, "pynvml.py"), "w") as f:
			f.write(NO_GPU_PYNVML)
		code = textwrap.dedent("""
			import sys
			from agent_energy_profiler import Session
			with Session.profile("nogpu", out_dir=sys.argv[1]) as p:
				with p.span("llm:x"):
					pass
			print(p.profile_status)
		""")
		out = os.path.join(self.d, "nogpu")
		res = subprocess.run([sys.executable, "-c", code, out], capture_output=True, text=True,
		                     timeout=120)
		self.assertEqual(res.returncode, 0, res.stderr)
		self.assertEqual(res.stdout.strip(), "complete")
		self.assertIn("no GPU energy counter", res.stderr)
		self.assertIn("ENERGY REPORT", res.stderr)
		g = {r["name"]: r for r in records(os.path.join(out, "spans_attributed.jsonl"))
		     if r.get("record_type") == "span"}["llm:x"]["energy"]["gpu"]
		self.assertEqual((g["status"], g["reason"], g["energy_j"]),
		                 ("unavailable", "no_counter", None))


@unittest.skipUnless(sys.platform.startswith("linux"), "parent watch uses Linux re-parenting")
class TestSamplerProcesses(ProfileCase):

	def gone(self, pid, timeout=5.0):
		deadline = time.monotonic() + timeout
		while time.monotonic() < deadline:
			try:
				with open(f"/proc/{pid}/stat") as f:
					if f.read().split(")")[-1].split()[0] == "Z":
						return True                     # exited, waiting to be reaped
			except FileNotFoundError:
				return True
			time.sleep(0.05)
		return False

	def test_sampler_stops_when_its_parent_is_killed(self):
		code = textwrap.dedent("""
			import os, signal, sys
			from agent_energy_profiler import Session
			p = Session.profile(out_dir=sys.argv[1], gpu=False, quiet=True).start()
			print(p.sampler._proc.pid, flush=True)
			os.kill(os.getpid(), signal.SIGKILL)       # no close, no exit handlers
		""")
		out = os.path.join(self.d, "killed")
		res = subprocess.run([sys.executable, "-c", code, out], capture_output=True, text=True,
		                     timeout=120)
		self.assertEqual(res.returncode, -signal.SIGKILL, res.stderr)
		pid = int(res.stdout.split()[0])
		self.assertTrue(self.gone(pid), "the sampler outlived its parent")
		with open(os.path.join(out, "power.csv")) as f:
			rows = f.read().splitlines()
		self.assertGreaterEqual(len(rows), 2)            # header and rows, closed cleanly
		with open(os.path.join(out, "power.csv.sampler.log")) as f:
			self.assertIn("parent process exited", f.read())

	def test_only_profile_sessions_watch_the_parent(self):
		self.assertFalse(Sampler("x.csv").watch_parent)
		with self.profile(report=False) as p:
			cmd = p.sampler._proc.args
		self.assertIn("--parent-pid", cmd)
		self.assertEqual(cmd[cmd.index("--parent-pid") + 1], str(os.getpid()))

	def test_a_wrong_parent_pid_is_not_watched(self):
		power = os.path.join(self.d, "power.csv")
		proc = subprocess.Popen([sys.executable, "-m", "agent_energy_profiler.sampling", "--out",
		                         power, "--hz", "50", "--parent-pid", "1"],
		                        stderr=subprocess.PIPE, text=True)
		time.sleep(1.0)
		self.assertIsNone(proc.poll(), "the sampler stopped although its parent is alive")
		proc.terminate()
		_, err = proc.communicate(timeout=10)
		self.assertIn("not watching it", err)

	def test_a_forked_child_does_not_stop_the_parents_sampler(self):
		with self.profile() as p:
			pid = os.fork()
			if pid == 0:
				try:
					p.close()
				finally:
					os._exit(0)
			os.waitpid(pid, 0)
			self.assertTrue(p.sampler.running)
			with p.span("llm:x"):
				pass
		self.assertEqual(p.profile_status, "complete")


if __name__ == "__main__":
	unittest.main()
