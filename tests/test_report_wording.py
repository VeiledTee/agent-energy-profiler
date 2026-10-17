"""M8: the report says only what the data supports.

Regression tests for statements that were misleading before 0.4.0: "every
top-level span with GPU energy was measured" when no GPU existed, counter
disturbance warnings for counters never read, and SPAN_ESTIMATES_ONLY naming
a run counter that did not exist.
"""

import json
import os
import threading
import time

from agent_energy_profiler import report
from agent_energy_profiler.measurement import CallableReader
from agent_energy_profiler.session import Session

from test_report import Counter_, V2Case, codes


class Reversing:
	"""A counter that goes backwards once (a reset): an invalid reading."""

	def __init__(self):
		self.values = iter([5_000_000, 10, 20, 30, 40, 50])

	def read(self):
		return [next(self.values)]


class TestWording(V2Case):

	def record(self, n_roots=1, **kw):
		with Session(self.spans, **kw) as s:
			for _ in range(n_roots):
				with s.span("agent:step"):
					with s.span("llm:x"):
						pass
		return self.spans

	def text(self, rep):
		return report.render_text(rep)

	def warning(self, rep, code):
		return [w["message"] for w in rep["warnings"] if w["code"] == code]

	def test_no_gpu_is_not_reported_as_measured(self):
		rep = report.build(self.record(mode="direct", gpu=False))
		text = self.text(rep)
		self.assertNotIn("was measured", text)
		self.assertNotIn("none needed", text)
		self.assertIn("none: 1 of 1 top-level spans have neither a GPU measurement nor an "
		              "estimate (disabled)", text)
		self.assertIn("lack a measured value (disabled)", text)
		self.assertEqual(rep["totals"]["estimated_gpu"]["without_gpu_value_reasons"],
		                 {"disabled": 1})
		gpu = [msg for msg in self.warning(rep, "DOMAIN_UNAVAILABLE") if msg.startswith("gpu")]
		self.assertEqual(len(gpu), 1)
		# a counter that was never read cannot have been disturbed
		self.assertFalse(codes(rep) & {"COUNTER_DISTURBANCE_UNKNOWN", "COUNTER_DISTURBED",
		                               "DIRECT_MODE_BY_DEFAULT"})

	def test_every_root_measured(self):
		rep = report.build(self.record(n_roots=2, mode="direct",
		                               gpu=CallableReader(Counter_().read)))
		self.assertIn("none needed: every top-level span has a measured GPU value",
		              self.text(rep))
		self.assertEqual(rep["totals"]["estimated_gpu"]["n_without_gpu_value"], 0)
		self.assertNotIn("missing_reasons", rep["totals"]["domains"]["gpu"])
		self.assertIn("COUNTER_DISTURBANCE_UNKNOWN", codes(rep))
		self.assertFalse([m for m in self.warning(rep, "DOMAIN_UNAVAILABLE")
		                  if m.startswith("gpu")])

	def test_default_sampled_without_a_timeline(self):
		rep = report.build(self.record(gpu=CallableReader(Counter_().read)))
		text = self.text(rep)
		self.assertIn("(not_read_by_mode)", text)
		self.assertIn("per-span GPU energy: estimated only", text)
		self.assertIn("whole-run GPU energy: measured", text)
		self.assertIn("(not available for this run: no sampler timeline)", text)
		[msg] = self.warning(rep, "NO_TIMELINE")
		self.assertIn("no energy is available for its spans", msg)
		self.assertNotIn("in-band GPU energy is available", msg)
		[msg] = self.warning(rep, "SPAN_ESTIMATES_ONLY")
		self.assertIn("No span has one (no hardware timeline was attributed)", msg)
		self.assertIn("No whole-run GPU counter measurement is available", msg)
		self.assertNotIn("The measured GPU figure is the run counter", msg)
		# a mode choice, not missing hardware; and the counter was never read
		self.assertFalse([m for m in self.warning(rep, "DOMAIN_UNAVAILABLE")
		                  if m.startswith("gpu")])
		self.assertNotIn("COUNTER_DISTURBANCE_UNKNOWN", codes(rep))

	def test_sampled_with_a_timeline_names_the_estimates(self):
		self.record(n_roots=2, mode="sampled", gpu=CallableReader(Counter_().read))
		rep = self.attribute(power=True)
		text = self.text(rep)
		self.assertIn("J over 2 of 2 top-level spans", text)
		self.assertNotIn("neither", text)
		[msg] = self.warning(rep, "SPAN_ESTIMATES_ONLY")
		self.assertIn("spans have one", msg)
		# measured totals never include the estimate
		self.assertIsNone(rep["totals"]["domains"]["gpu"]["total_j"])
		self.assertIsNotNone(rep["totals"]["estimated_gpu"]["sum_j"])

	def test_partial_coverage_names_the_missing_spans_and_why(self):
		# reads: root1 start/end ok, root2's 2nd read (llm:x start) fails
		reader = CallableReader(Counter_(fail_at=(5,)).read)
		rep = report.build(self.record(n_roots=2, mode="direct", gpu=reader))
		gpu = rep["totals"]["domains"]["gpu"]
		self.assertIsNone(gpu["total_j"])
		self.assertEqual(gpu["n_measured"], 1)
		self.assertEqual(gpu["missing_reasons"], {"read_failed": 1})
		text = self.text(rep)
		self.assertIn("(sum over the 1 of 2 measured", text)
		self.assertIn("none: 1 of 2 top-level spans have neither a GPU measurement nor an "
		              "estimate (read_failed)", text)

	def test_invalid_counter_readings(self):
		rep = report.build(self.record(mode="direct", gpu=CallableReader(Reversing().read)))
		self.assertIn("INVALID_MEASUREMENTS", codes(rep))
		self.assertIn("(counter_decrease)", self.text(rep))
		self.assertIn("counter_decrease", rep["totals"]["estimated_gpu"]["without_gpu_value_reasons"])
		# invalid readings were still reads of the counter
		self.assertIn("COUNTER_DISTURBANCE_UNKNOWN", codes(rep))

	def test_overlapping_roots_with_estimates_give_no_sums(self):
		barrier = threading.Barrier(2)
		with Session(self.spans, mode="sampled", gpu=CallableReader(Counter_().read)) as s:
			def root():
				with s.span("agent:step"):
					barrier.wait()
					time.sleep(0.01)
			ts = [threading.Thread(target=root) for _ in range(2)]
			[t.start() for t in ts]
			[t.join() for t in ts]
		rep = self.attribute(power=True)
		self.assertIn("OVERLAPPING_TOP_LEVEL", codes(rep))
		est = rep["totals"]["estimated_gpu"]
		self.assertIsNone(est["sum_j"])
		self.assertIn("overlap", est["reason"])
		text = self.text(rep)
		self.assertIn("2 of 2 top-level spans have one; no sum (top-level spans overlap", text)
		self.assertNotIn("(not_read_by_mode)", text.split("ESTIMATED ENERGY")[0])

	def test_window_names_why_a_domain_is_missing(self):
		self.record(mode="direct", gpu=False)
		rep = self.attribute(power=True)
		# the synthetic timeline has every domain; drop the GPU from it
		rep["window"]["domains"]["gpu"] = {"energy_j": None, "status": "unavailable",
		                                   "reason": "no_counter"}
		self.assertIn("gpu n/a (no_counter)", self.text(rep))

	def test_legacy_reports_unchanged_for_v1(self):
		# schema-v1 events: no hierarchy, every event top-level; wording rules
		# apply but nothing measured changes
		from test_report import unpack_v1
		rep = report.build(unpack_v1("cor_webqsp", self.d))
		self.assertEqual(rep["source"]["schema"], 1)
		self.assertIn("COUNTER_DISTURBANCE_UNKNOWN", codes(rep))
		self.assertIn("MEASUREMENT_NOT_RECORDED", codes(rep))
		self.assertTrue(json.dumps(rep))


if __name__ == "__main__":
	import unittest
	unittest.main()
