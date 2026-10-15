"""Attribution's hardware-coverage gate.

Ported from Chain-of-Relations-Emissions tests/test_run_integrity.py
(TestHardwareCoverageGate). The source file's other half, the measured-run
tag-reuse guard, tests the research run wrapper and stays in the research
repository.

Attribution must fail when any event falls outside the window during which
hardware was actually sampled. This is about the TIMELINE, and is distinct
from a domain (CPU-package, DRAM) simply being unavailable on the host.
"""

import unittest

from agent_energy_profiler.attribution import (
	attribute_events,
	coverage_window,
	render_coverage_failure,
	uncovered_events,
)



def ev(t0, t1, **kw):
	e = {"start_timestamp": t0, "end_timestamp": t1,
	     "operation_label": "kg:id2name", "operation_type": "kg",
	     "run_id": kw.pop("run_id", "r1")}
	e.update(kw)
	return e


class TestHardwareCoverageGate(unittest.TestCase):
	def setUp(self):
		# hardware sampled from t=100 to t=200
		self.ts = [100.0 + i for i in range(101)]

	def test_fully_covered_events_pass(self):
		events = [ev(110, 120), ev(150, 160), ev(190, 199)]
		unc, considered = uncovered_events(events, self.ts)
		self.assertEqual(unc, [])
		self.assertEqual(considered, 3)

	def test_event_entirely_before_timeline_fails(self):
		events = [ev(10, 20), ev(150, 160)]
		unc, considered = uncovered_events(events, self.ts)
		self.assertEqual(len(unc), 1)
		self.assertEqual(unc[0]["start_timestamp"], 10)
		self.assertEqual(considered, 2)

	def test_event_entirely_after_timeline_fails(self):
		events = [ev(150, 160), ev(500, 510)]
		unc, _ = uncovered_events(events, self.ts)
		self.assertEqual(len(unc), 1)
		self.assertEqual(unc[0]["start_timestamp"], 500)

	def test_partially_covered_event_fails_at_each_edge(self):
		"""Straddling either edge means the sampler missed part of the event."""
		leading = uncovered_events([ev(95, 150)], self.ts)[0]
		self.assertEqual(len(leading), 1)
		trailing = uncovered_events([ev(150, 250)], self.ts)[0]
		self.assertEqual(len(trailing), 1)

	def test_multiple_uncovered_events_counted_correctly(self):
		events = ([ev(10 + i, 20 + i) for i in range(7)]
		          + [ev(150, 151) for _ in range(3)])
		unc, considered = uncovered_events(events, self.ts)
		self.assertEqual(len(unc), 7)
		self.assertEqual(considered, 10)

	def test_empty_hardware_timeline_makes_everything_uncovered(self):
		"""A sampler that produced nothing cannot cover any event."""
		unc, considered = uncovered_events([ev(1, 2), ev(3, 4)], [])
		self.assertEqual(len(unc), 2)
		self.assertEqual(considered, 2)
		self.assertIsNone(coverage_window([]))

	def test_events_without_timestamps_are_not_considered(self):
		"""attribute_events skips them, so the gate must skip them too."""
		events = [{"operation_label": "kg:sparql"}, ev(150, 160)]
		unc, considered = uncovered_events(events, self.ts)
		self.assertEqual(unc, [])
		self.assertEqual(considered, 1)

	def test_missing_cpu_and_dram_is_not_a_coverage_failure(self):
		"""The WSL2 case: GPU-only timeline, no RAPL at all.

		Domain availability and timeline coverage are different things. The
		run stays a valid incomplete measurement: null CPU/DRAM, null total,
		measurement_complete false -- and NO coverage error.
		"""
		events = [ev(110, 120, gpu_energy_j=5.0)]
		unc, _ = uncovered_events(events, self.ts)
		self.assertEqual(unc, [], "missing RAPL must not trigger the gate")

		gpus = {"gpu0_w": [30.0] * len(self.ts)}
		rows, _ = attribute_events(events, self.ts, gpus, {})
		row = rows[0]
		self.assertIsNone(row["cpu_package_energy_j"])
		self.assertIsNone(row["dram_energy_j"])
		self.assertIsNone(row["measured_energy_j"])
		self.assertFalse(row["measurement_complete"])
		self.assertEqual(row["gpu_energy_j"], 5.0)

	def test_uncovered_events_are_not_zeroed_or_fabricated(self):
		"""The gate reports; it must not invent or zero an energy value."""
		e = ev(10, 20)
		unc, _ = uncovered_events([e], self.ts)
		self.assertEqual(len(unc), 1)
		self.assertNotIn("gpu_energy_j", unc[0])
		self.assertNotIn("measured_energy_j", unc[0])

	def test_failure_message_reports_count_and_percentage(self):
		# Mirrors the real contamination: 392 stale events wholly before the
		# window, 4074 good ones inside it. Every stale event must end before
		# t=100, hence the 0.01 s spacing.
		events = ([ev(10 + i * 0.01, 10.005 + i * 0.01) for i in range(392)]
		          + [ev(150, 151) for _ in range(4074)])
		unc, considered = uncovered_events(events, self.ts)
		msg = render_coverage_failure(unc, considered, self.ts)
		self.assertIn("392 events", msg)
		self.assertIn("8.8%", msg)
		self.assertIn("outside hardware measurement coverage", msg)

	def test_failure_message_names_affected_run_ids(self):
		"""A reused tag shows up as two run_ids; name the stale one."""
		events = [ev(10, 20, run_id="layout_test-1788312334"),
		          ev(150, 160, run_id="layout_test-1788313392")]
		unc, considered = uncovered_events(events, self.ts)
		msg = render_coverage_failure(unc, considered, self.ts)
		self.assertIn("layout_test-1788312334", msg)

	def test_coverage_failure_has_no_override_flag(self):
		"""The gate is unconditional in the measured-run path.

		A contaminated or incomplete legacy run does not justify an escape
		hatch here. Any future forensic tooling must live somewhere that
		cannot be mistaken for a valid measured run.
		"""
		import agent_energy_profiler.attribution as attribution
		with open(attribution.__file__) as f:
			src = f.read()
		for token in ("--allow-uncovered", "allow_uncovered", "--force",
		              "--skip-coverage"):
			self.assertNotIn(token, src,
			                 "coverage gate must have no override: %s" % token)

	def test_valid_run_still_attributes_and_reconciles(self):
		"""Existing behaviour is untouched for a covered run."""
		events = [ev(110, 120, gpu_energy_j=5.0),
		          ev(130, 140, gpu_energy_j=7.0)]
		unc, _ = uncovered_events(events, self.ts)
		self.assertEqual(unc, [])
		rows, src = attribute_events(events, self.ts, {}, {})
		self.assertEqual(len(rows), 2)
		self.assertEqual(src["counter"], 2)
		self.assertEqual(sum(r["gpu_energy_j"] for r in rows), 12.0)


if __name__ == "__main__":
	unittest.main()
