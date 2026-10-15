"""M1.3: interrupted work is recorded as cancelled, not as failed work.

`cancelled` is already part of the schema-v1 status vocabulary
(schema.Status.CANCELLED), so no reader sees a new value. Before M1.3 span()
recorded KeyboardInterrupt and asyncio.CancelledError as "error".
"""

import asyncio
import json
import os
import tempfile
import unittest

from agent_energy_profiler import events as aep_events
from agent_energy_profiler import profiler, schema


class SpanCase(unittest.TestCase):

	def setUp(self):
		self._tmp = tempfile.TemporaryDirectory()
		self.path = os.path.join(self._tmp.name, "events.jsonl")
		os.environ["ENERGY_EVENTS_FILE"] = self.path
		aep_events.reset_from_env()

	def tearDown(self):
		aep_events.close()
		os.environ.pop("ENERGY_EVENTS_FILE", None)
		aep_events.reset_from_env()
		self._tmp.cleanup()

	def read(self):
		aep_events.close()
		if not os.path.exists(self.path):
			return []
		with open(self.path) as f:
			return [json.loads(line) for line in f if line.strip()]


class TestSyncSpanStatus(SpanCase):

	def test_normal_completion_is_ok(self):
		with profiler.span("llm:reason"):
			pass
		event, = self.read()
		self.assertEqual(event["status"], "ok")
		self.assertNotIn("exception_type", event["meta"])

	def test_exception_is_error_and_propagates(self):
		with self.assertRaises(ValueError):
			with profiler.span("kg:query"):
				raise ValueError("failed")
		event, = self.read()
		self.assertEqual(event["status"], "error")
		self.assertEqual(event["meta"]["exception_type"], "ValueError")

	def test_keyboard_interrupt_is_cancelled_and_propagates(self):
		with self.assertRaises(KeyboardInterrupt):
			with profiler.span("llm:reason") as s:
				s["input_tokens"] = 5
				raise KeyboardInterrupt
		event, = self.read()
		self.assertEqual(event["status"], schema.Status.CANCELLED.value)
		self.assertEqual(event["meta"]["exception_type"], "KeyboardInterrupt")
		self.assertEqual(event["input_tokens"], 5)
		self.assertGreaterEqual(event["end_timestamp"], event["start_timestamp"])
		self.assertGreaterEqual(event["duration_s"], 0.0)

	def test_cancelled_error_raised_synchronously_is_cancelled(self):
		with self.assertRaises(asyncio.CancelledError):
			with profiler.span("llm:reason"):
				raise asyncio.CancelledError
		event, = self.read()
		self.assertEqual(event["status"], "cancelled")

	def test_system_exit_is_still_recorded_as_error(self):
		"""Only interruption is reclassified; other BaseExceptions keep their
		existing status."""
		with self.assertRaises(SystemExit):
			with profiler.span("llm:reason"):
				raise SystemExit(3)
		event, = self.read()
		self.assertEqual(event["status"], "error")
		self.assertEqual(event["meta"]["exception_type"], "SystemExit")

	def test_operation_scope_is_restored_after_interrupt(self):
		with self.assertRaises(KeyboardInterrupt):
			with profiler.span("llm:reason"):
				raise KeyboardInterrupt
		self.assertIsNone(aep_events.current_operation())

	def test_the_same_exception_object_propagates(self):
		exc = KeyboardInterrupt("stop")
		with self.assertRaises(KeyboardInterrupt) as caught:
			with profiler.span("llm:reason"):
				raise exc
		self.assertIs(caught.exception, exc)


class TestAsyncSpanStatus(SpanCase):

	def test_task_cancellation_is_cancelled_and_propagates(self):
		started = []

		async def work():
			with profiler.span("llm:reason"):
				started.append(True)
				await asyncio.sleep(10)

		async def main():
			task = asyncio.ensure_future(work())
			while not started:
				await asyncio.sleep(0)
			task.cancel()
			with self.assertRaises(asyncio.CancelledError):
				await task
			self.assertTrue(task.cancelled())

		asyncio.run(main())
		event, = self.read()
		self.assertEqual(event["status"], "cancelled")
		self.assertEqual(event["meta"]["exception_type"], "CancelledError")
		self.assertGreaterEqual(event["duration_s"], 0.0)

	def test_timeout_cancellation_is_cancelled(self):
		async def main():
			with self.assertRaises(asyncio.TimeoutError):
				await asyncio.wait_for(work(), timeout=0.01)

		async def work():
			with profiler.span("llm:reason"):
				await asyncio.sleep(10)

		asyncio.run(main())
		event, = self.read()
		self.assertEqual(event["status"], "cancelled")

	def test_async_exception_is_error(self):
		async def work():
			with profiler.span("kg:query"):
				await asyncio.sleep(0)
				raise RuntimeError("boom")

		with self.assertRaises(RuntimeError):
			asyncio.run(work())
		event, = self.read()
		self.assertEqual(event["status"], "error")

	def test_async_completion_is_ok(self):
		async def work():
			with profiler.span("llm:reason"):
				await asyncio.sleep(0)

		asyncio.run(work())
		event, = self.read()
		self.assertEqual(event["status"], "ok")

	def test_cancelled_sibling_does_not_disturb_the_operation_scope(self):
		async def work(label, cancel_me):
			with profiler.span(label):
				await asyncio.sleep(10 if cancel_me else 0)
				return aep_events.current_operation()

		async def main():
			doomed = asyncio.ensure_future(work("llm:doomed", True))
			await asyncio.sleep(0)
			doomed.cancel()
			survivor = await work("llm:survivor", False)
			with self.assertRaises(asyncio.CancelledError):
				await doomed
			return survivor

		self.assertEqual(asyncio.run(main()), "llm:survivor")
		statuses = {e["operation_label"]: e["status"] for e in self.read()}
		self.assertEqual(statuses, {"llm:survivor": "ok", "llm:doomed": "cancelled"})


if __name__ == "__main__":
	unittest.main()
