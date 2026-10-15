"""M2: the Session API -- nesting, isolation, lifecycle (synthetic GPU)."""

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest

from agent_energy_profiler import schema_v2
from agent_energy_profiler.measurement import CallableReader, Resolution
from agent_energy_profiler.session import Session

RES = Resolution.configured(0.1, "synthetic")


class FakeCounter:
	"""A monotonic per-device mJ counter that advances on every read."""

	def __init__(self, devices=1, step=10):
		self.value = [1_000_000] * devices
		self.step = step
		self.lock = threading.Lock()

	def read(self):
		with self.lock:
			self.value = [v + self.step for v in self.value]
			return list(self.value)

	def reader(self):
		return CallableReader(self.read, devices=[{"index": i, "name": "fake"}
		                                          for i in range(len(self.value))])


class SessionCase(unittest.TestCase):

	def setUp(self):
		self._tmp = tempfile.TemporaryDirectory()
		self.path = os.path.join(self._tmp.name, "spans.jsonl")

	def tearDown(self):
		self._tmp.cleanup()

	def session(self, **kw):
		kw.setdefault("gpu", FakeCounter().reader())
		kw.setdefault("resolution", RES)
		return Session(self.path, **kw)

	def records(self, path=None):
		with open(path or self.path) as f:
			out = [json.loads(line) for line in f if line.strip()]
		for r in out:
			self.assertEqual(schema_v2.validate(r), [], r)
		return out

	def spans(self, path=None):
		return {r["name"]: r for r in self.records(path) if r["record_type"] == "span"}


class TestLifecycle(SessionCase):

	def test_file_has_header_spans_and_footer(self):
		with self.session(attributes={"run_id": "r1"}) as s:
			with s.span("llm:plan"):
				pass
		recs = self.records()
		self.assertEqual([r["record_type"] for r in recs],
		                 ["session_start", "span", "session_end"])
		head = recs[0]
		self.assertEqual(head["profiler_version"], "0.2.0")
		self.assertEqual(head["config"]["energy_scope"], "device")
		self.assertEqual(head["config"]["resolution"]["basis"], "configured")
		self.assertEqual(head["attributes"], {"run_id": "r1"})
		self.assertEqual(recs[-1]["n_spans_written"], 1)
		self.assertEqual(recs[-1]["n_spans_dropped"], 0)

	def test_existing_file_is_refused_unless_overwrite(self):
		open(self.path, "w").close()
		with self.assertRaises(FileExistsError):
			self.session().start()
		with self.session(overwrite=True) as s:
			with s.span("llm:x"):
				pass
		self.assertIn("llm:x", self.spans())

	def test_span_outside_a_started_session_raises(self):
		s = self.session()
		with self.assertRaises(RuntimeError):
			with s.span("llm:x"):
				pass

	def test_close_is_idempotent_and_spans_after_close_are_rejected(self):
		s = self.session().start()
		s.close()
		s.close()
		with self.assertRaises(RuntimeError):
			with s.span("llm:x"):
				pass

	def test_session_closes_cleanly_when_the_body_raises(self):
		with self.assertRaises(KeyError):
			with self.session() as s:
				with s.span("llm:x"):
					raise KeyError("boom")
		recs = self.records()
		self.assertEqual(recs[-1]["record_type"], "session_end")
		self.assertEqual(self.spans()["llm:x"]["status"], "error")

	def test_label_is_required_and_never_inferred(self):
		with self.session() as s:
			with self.assertRaises(TypeError):
				s.span("")
			with self.assertRaises(TypeError):
				s.trace(lambda: None)  # @session.trace without a label
			with self.assertRaises(TypeError):
				s.span(None)

			@s.trace("tool:search")
			def some_function_name():
				return 3
			self.assertEqual(some_function_name(), 3)
		self.assertIn("tool:search", self.spans())
		self.assertNotIn("some_function_name", json.dumps(self.records()))


class TestNesting(SessionCase):

	def test_sync_nesting_ids_and_trace(self):
		with self.session() as s:
			with s.span("agent:step") as outer:
				with s.span("llm:plan") as inner:
					self.assertEqual(inner.parent_span_id, outer.span_id)
					self.assertEqual(s.current_span().span_id, inner.span_id)
				with s.span("tool:search"):
					pass
			with s.span("agent:step2"):
				pass
		sp = self.spans()
		root = sp["agent:step"]
		self.assertIsNone(root["parent_span_id"])
		for child in ("llm:plan", "tool:search"):
			self.assertEqual(sp[child]["parent_span_id"], root["span_id"])
			self.assertEqual(sp[child]["trace_id"], root["trace_id"])
		self.assertNotEqual(sp["agent:step2"]["trace_id"], root["trace_id"])
		self.assertEqual(len({r["span_id"] for r in sp.values()}), 4)

	def test_child_window_inside_parent_and_counter_bracketed(self):
		with self.session() as s:
			with s.span("agent:step"):
				with s.span("llm:plan"):
					pass
		sp = self.spans()
		p, c = sp["agent:step"], sp["llm:plan"]
		self.assertLessEqual(p["start_monotonic_s"], c["start_monotonic_s"])
		self.assertLessEqual(c["end_monotonic_s"], p["end_monotonic_s"])
		pr, cr = p["energy"]["gpu"]["raw"][0], c["energy"]["gpu"]["raw"][0]
		self.assertLess(pr["start_mj"], cr["start_mj"])
		self.assertLess(cr["end_mj"], pr["end_mj"])

	def test_exception_in_child_is_error_and_parent_keeps_its_own_status(self):
		with self.session() as s:
			with s.span("agent:step"):
				try:
					with s.span("tool:search"):
						raise ValueError("no results")
				except ValueError:
					pass
		sp = self.spans()
		self.assertEqual(sp["tool:search"]["status"], "error")
		self.assertEqual(sp["tool:search"]["error_type"], "ValueError")
		self.assertEqual(sp["agent:step"]["status"], "ok")
		self.assertIsNone(sp["agent:step"]["error_type"])

	def test_keyboard_interrupt_is_cancelled_through_the_stack(self):
		with self.assertRaises(KeyboardInterrupt):
			with self.session() as s:
				with s.span("agent:step"):
					with s.span("llm:plan"):
						raise KeyboardInterrupt
		sp = self.spans()
		self.assertEqual(sp["llm:plan"]["status"], "cancelled")
		self.assertEqual(sp["agent:step"]["status"], "cancelled")

	def test_explicit_status_wins(self):
		with self.session() as s:
			with self.assertRaises(TimeoutError):
				with s.span("llm:plan") as h:
					h.set_status("timeout")
					raise TimeoutError
			with self.assertRaises(ValueError):
				with s.span("llm:x") as h:
					h.set_status("nonsense")
		self.assertEqual(self.spans()["llm:plan"]["status"], "timeout")

	def test_tokens_attributes_and_v1_style_access(self):
		with self.session() as s:
			with s.span("llm:plan", attributes={"tool_attempt": 1}) as h:
				h.set_tokens(input=10, output=2)
				h["meta"] = {"attempts": 3}
				h.set_attribute("question_id", "q1")
			with s.span("llm:answer") as h:
				h["input_tokens"] = 5
				h["output_tokens"] = 1
		sp = self.spans()
		self.assertEqual((sp["llm:plan"]["input_tokens"], sp["llm:plan"]["output_tokens"]), (10, 2))
		self.assertEqual(sp["llm:plan"]["attributes"],
		                 {"tool_attempt": 1, "attempts": 3, "question_id": "q1"})
		self.assertEqual(sp["llm:answer"]["input_tokens"], 5)

	def test_explicit_parent_across_a_thread(self):
		with self.session() as s:
			with s.span("agent:step") as outer:
				def work(parent):
					with s.span("tool:worker", parent=parent):
						pass
					with s.span("tool:orphan"):
						pass
				t = threading.Thread(target=work, args=(outer,))
				t.start()
				t.join()
		sp = self.spans()
		self.assertEqual(sp["tool:worker"]["parent_span_id"], sp["agent:step"]["span_id"])
		self.assertIsNone(sp["tool:orphan"]["parent_span_id"],
		                  "a new thread must not silently inherit a parent")
		self.assertNotEqual(sp["tool:worker"]["thread_id"], sp["agent:step"]["thread_id"])


class TestAsync(SessionCase):

	def test_async_nesting_and_decorator(self):
		with self.session() as s:
			@s.trace("tool:search")
			async def search():
				await asyncio.sleep(0)
				return s.current_span().span_id

			async def main():
				async with s.span("agent:step") as outer:
					inner_id = await search()
					return outer.span_id, inner_id

			outer_id, inner_id = asyncio.run(main())
		sp = self.spans()
		self.assertEqual(sp["tool:search"]["parent_span_id"], outer_id)
		self.assertEqual(sp["tool:search"]["span_id"], inner_id)
		self.assertIsNotNone(sp["tool:search"]["task"])

	def test_concurrent_tasks_are_isolated_siblings(self):
		with self.session() as s:
			async def worker(label, delay):
				async with s.span(label):
					await asyncio.sleep(delay)
					async with s.span(label + "/inner"):
						await asyncio.sleep(delay)

			async def main():
				async with s.span("agent:fanout"):
					await asyncio.gather(worker("tool:a", 0.01), worker("tool:b", 0.005))

			asyncio.run(main())
		sp = self.spans()
		root = sp["agent:fanout"]["span_id"]
		self.assertEqual(sp["tool:a"]["parent_span_id"], root)
		self.assertEqual(sp["tool:b"]["parent_span_id"], root)
		self.assertEqual(sp["tool:a/inner"]["parent_span_id"], sp["tool:a"]["span_id"])
		self.assertEqual(sp["tool:b/inner"]["parent_span_id"], sp["tool:b"]["span_id"])

	def test_task_cancellation(self):
		with self.session() as s:
			async def main():
				started = asyncio.Event()

				async def work():
					async with s.span("llm:slow"):
						started.set()
						await asyncio.sleep(10)

				task = asyncio.ensure_future(work())
				await started.wait()
				task.cancel()
				with self.assertRaises(asyncio.CancelledError):
					await task
			asyncio.run(main())
		self.assertEqual(self.spans()["llm:slow"]["status"], "cancelled")
		self.assertEqual(self.spans()["llm:slow"]["error_type"], "CancelledError")

	def test_async_session_context_manager(self):
		async def main():
			async with self.session() as s:
				async with s.span("llm:x"):
					pass
		asyncio.run(main())
		self.assertIn("llm:x", self.spans())


class TestIsolation(SessionCase):

	def test_two_sessions_do_not_share_parents_or_files(self):
		other = os.path.join(self._tmp.name, "other.jsonl")
		with self.session() as a, Session(other, gpu=False) as b:
			with a.span("a:outer"):
				with b.span("b:inner") as bi:
					self.assertIsNone(bi.parent_span_id)
					self.assertIsNone(a.current_span().parent_span_id)
		sa, sb = self.spans(), self.spans(other)
		self.assertEqual(set(sa), {"a:outer"})
		self.assertEqual(set(sb), {"b:inner"})
		self.assertTrue(sa["a:outer"]["other_sessions_active"])
		self.assertTrue(sb["b:inner"]["other_sessions_active"])

	def test_sequential_sessions_are_not_flagged(self):
		other = os.path.join(self._tmp.name, "other.jsonl")
		with self.session() as a:
			with a.span("a:x"):
				pass
		with Session(other, gpu=False) as b:
			with b.span("b:x"):
				pass
		self.assertFalse(self.spans()["a:x"]["other_sessions_active"])
		self.assertFalse(self.spans(other)["b:x"]["other_sessions_active"])

	def test_parent_from_another_session_is_rejected(self):
		other = os.path.join(self._tmp.name, "other.jsonl")
		with self.session() as a, Session(other, gpu=False) as b:
			with a.span("a:x") as h:
				with self.assertRaises(ValueError):
					with b.span("b:x", parent=h):
						pass

	def test_threads_each_get_their_own_stack(self):
		errors = []
		with self.session() as s:
			def work(i):
				try:
					with s.span(f"t:{i}") as outer:
						for _ in range(20):
							with s.span(f"t:{i}/inner") as inner:
								if inner.parent_span_id != outer.span_id:
									errors.append(i)
				except Exception as e:  # pragma: no cover
					errors.append(e)
			threads = [threading.Thread(target=work, args=(i,)) for i in range(8)]
			for t in threads:
				t.start()
			for t in threads:
				t.join()
		self.assertEqual(errors, [])
		recs = [r for r in self.records() if r["record_type"] == "span"]
		self.assertEqual(len(recs), 8 * 21)

	@unittest.skipUnless(hasattr(os, "fork"), "needs fork")
	def test_forked_child_cannot_write_into_the_parent_file(self):
		with self.session() as s:
			pid = os.fork()
			if pid == 0:  # child
				try:
					with s.span("child:x"):
						pass
					os._exit(0 if s.n_foreign_process_writes_refused == 1 else 3)
				except BaseException:
					os._exit(4)
			_, status = os.waitpid(pid, 0)
			with s.span("parent:x"):
				pass
		self.assertEqual(os.WEXITSTATUS(status), 0)
		self.assertEqual(set(self.spans()), {"parent:x"})


class TestGpuAvailability(SessionCase):

	def test_gpu_disabled(self):
		with self.session(gpu=False) as s:
			with s.span("llm:x"):
				pass
		g = self.spans()["llm:x"]["energy"]["gpu"]
		self.assertEqual((g["status"], g["reason"], g["energy_j"]),
		                 ("unavailable", "disabled", None))
		self.assertEqual(self.records()[0]["config"]["gpu"]["reader"], "disabled")

	def test_gpu_absent_auto_mode(self):
		"""No NVML on the host: auto mode records unavailable, never zero."""
		code = textwrap.dedent("""
			import sys, json
			sys.modules["pynvml"] = None
			from agent_energy_profiler import Session
			with Session(sys.argv[1]) as s:
				with s.span("llm:x"):
					pass
		""")
		out = subprocess.run([sys.executable, "-c", code, self.path],
		                     capture_output=True, text=True, timeout=60)
		self.assertEqual(out.returncode, 0, out.stderr)
		g = self.spans()["llm:x"]["energy"]["gpu"]
		self.assertEqual((g["status"], g["reason"]), ("unavailable", "no_counter"))
		self.assertFalse(self.records()[0]["config"]["gpu"]["enabled"])

	def test_reader_failure_mid_session_is_read_failed(self):
		calls = {"n": 0}

		def flaky():
			calls["n"] += 1
			if calls["n"] == 2:
				raise OSError("NVML_ERROR_GPU_IS_LOST")
			return [calls["n"] * 100]
		with self.session(gpu=CallableReader(flaky)) as s:
			with s.span("llm:x"):
				pass
			with s.span("llm:y"):
				pass
		sp = self.spans()
		self.assertEqual(sp["llm:x"]["energy"]["gpu"]["reason"], "read_failed")
		self.assertEqual(sp["llm:y"]["energy"]["gpu"]["status"], "measured")

	def test_counter_reset_during_span_is_invalid(self):
		values = iter([[5_000_000], [10]])
		with self.session(gpu=CallableReader(lambda: next(values))) as s:
			with s.span("llm:x"):
				pass
		g = self.spans()["llm:x"]["energy"]["gpu"]
		self.assertEqual((g["status"], g["reason"]), ("invalid", "counter_decrease"))
		self.assertEqual(g["raw"], [{"device": 0, "start_mj": 5_000_000, "end_mj": 10}])

	@unittest.skipUnless(os.environ.get("AEP_REAL_GPU") == "1",
	                     "set AEP_REAL_GPU=1 to read the real NVML counter")
	def test_real_nvml(self):
		with Session(self.path, resolution=RES) as s:
			with s.span("llm:x"):
				sum(range(10**6))
		g = self.spans()["llm:x"]["energy"]["gpu"]
		self.assertEqual(g["status"], "measured")
		self.assertTrue(self.records()[0]["config"]["gpu"]["devices"])


FAKE_PYNVML = textwrap.dedent("""
	import time
	_t0 = time.time()
	def nvmlInit(): pass
	def nvmlShutdown(): pass
	def nvmlDeviceGetCount(): return 1
	def nvmlDeviceGetHandleByIndex(i): return i
	def nvmlDeviceGetTotalEnergyConsumption(h): return int((time.time() - _t0) * 100_000)
	def nvmlDeviceGetPowerUsage(h): return 100_000
	def nvmlDeviceGetName(h): return "FakeGPU"
""")


class TestSamplerLifecycle(SessionCase):

	def test_session_starts_and_stops_the_sampler_around_its_spans(self):
		from agent_energy_profiler.sampling import Sampler
		fake_dir = os.path.join(self._tmp.name, "fake")
		os.makedirs(fake_dir)
		with open(os.path.join(fake_dir, "pynvml.py"), "w") as f:
			f.write(FAKE_PYNVML)
		env = dict(os.environ, PYTHONPATH=fake_dir)
		power = os.path.join(self._tmp.name, "power.csv")
		sampler = Sampler(power, hz=50, env=env)
		with self.session(sampler=sampler) as s:
			self.assertTrue(sampler.running)
			with s.span("llm:x"):
				pass
		self.assertFalse(sampler.running)
		end = self.records()[-1]
		self.assertEqual(end["sampler_exit_code"], 0)
		cfg = self.records()[0]["config"]["sampler"]
		self.assertEqual((cfg["enabled"], cfg["hz"]), (True, 50.0))
		with open(power) as f:
			rows = [line.split(",") for line in f.read().splitlines()[1:]]
		span = self.spans()["llm:x"]
		self.assertLessEqual(float(rows[0][0]), span["start_unix_s"])
		self.assertGreaterEqual(float(rows[-1][0]), span["end_unix_s"])
		self.assertTrue(os.path.exists(power + ".meta.json"))

	def test_sampler_failure_to_start_closes_the_session(self):
		from agent_energy_profiler.sampling import Sampler
		sampler = Sampler(os.path.join(self._tmp.name, "nodir", "power.csv"),
		                  startup_timeout_s=2)
		s = self.session(sampler=sampler)
		with self.assertRaises((RuntimeError, OSError)):
			s.start()
		self.assertTrue(s.closed)


if __name__ == "__main__":
	unittest.main()
