"""The brief report: operations ranked by energy only where values are
comparable, measured and estimated never mixed, missing never shown as 0."""

import json
import math
import os
import subprocess
import sys
import threading
import time
import unittest

from agent_energy_profiler import brief, report
from agent_energy_profiler.measurement import CallableReader, Resolution
from agent_energy_profiler.session import Session

from test_report import GOLDEN, Counter_, V2Case, unpack_v1
from test_report_wording import Reversing


def rows(b):
	return {r["operation"]: r for r in b["rows"]}


class BriefCase(V2Case):

	def nested(self, n_roots=2, **kw):
		with Session(self.spans, **kw) as s:
			for _ in range(n_roots):
				with s.span("agent:step"):
					with s.span("llm:plan"):
						with s.span("tool:lookup"):
							pass
					with s.span("tool:search"):
						pass

	def attribute_without_gpu(self):
		"""A timeline with RAPL counters only: a host without a GPU."""
		from agent_energy_profiler import attribution_v2
		with open(self.spans) as f:
			recs = [json.loads(l) for l in f if '"record_type": "span"' in l]
		t0 = min(r["start_unix_s"] for r in recs) - 1
		t1 = max(r["end_unix_s"] for r in recs) + 1
		path = os.path.join(self.d, "power.csv")
		with open(path, "w") as f:
			f.write("t,rapl_package-0_intel-rapl:0_uj,rapl_dram_intel-rapl:0:2_uj\n")
			for i in range(21):
				f.write(f"{t0 + (t1 - t0) * i / 20:.6f},{2_000_000 * i},{200_000 * i}\n")
		self.assertEqual(attribution_v2.main(["--spans", self.spans, "--out-dir", self.d,
		                                      "--power", path]), 0)

	def brief(self, power=True):
		if power == "no_gpu":
			self.attribute_without_gpu()
		elif power is not None:
			self.attribute(power=power)
		b = brief.build(self.d)
		return b, brief.render(b)


def header_line(text):
	return [l for l in text.splitlines() if l.lstrip().startswith("#  operation")][0]


class TestMeasured(BriefCase):

	def test_nested_spans_rank_by_exclusive_energy_without_double_counting(self):
		self.nested(mode="direct", gpu=CallableReader(Counter_().read))
		b, text = self.brief()
		rep = report.build(self.d)
		self.assertEqual(b["basis"], ("gpu", "measured"))
		ranked = [r for r in b["rows"] if r["ranked"]]
		self.assertEqual(len(ranked), 4)                # the parent is ranked by its own share
		self.assertAlmostEqual(math.fsum(r["energy_j"] for r in ranked),
		                       rep["totals"]["domains"]["gpu"]["total_j"], places=12)
		self.assertAlmostEqual(math.fsum(r["share"] for r in ranked), 1.0, places=12)
		values = [r["energy_j"] for r in ranked]
		self.assertEqual(values, sorted(values, reverse=True))
		self.assertIn("Operations ranked by measured GPU energy (counter), exclusive", text)
		self.assertNotIn("~", text)
		self.assertNotIn("ESTIMATED", text)
		# the report's numbers, not new ones
		ops = {r["operation"]: r for r in rep["operations"]}
		for r in ranked:
			self.assertEqual(r["energy_j"], ops[r["operation"]]["energy"]["gpu"]["energy_j"])

	def test_rapl_is_shown_beside_the_gpu_and_never_added_to_it(self):
		self.nested(mode="direct", gpu=CallableReader(Counter_().read))
		b, text = self.brief()
		self.assertTrue(b["has_cpu_column"])
		self.assertIn("CPU pkg J", text)
		self.assertIn("shown, not ranked", text)
		whole = {d: (v, how) for d, v, how in b["whole_run"]}
		self.assertIsNotNone(whole["cpu_package"][0])
		self.assertNotIn("gpu+cpu", text)

	def test_without_a_gpu_the_cpu_package_is_the_basis(self):
		self.nested(mode="direct", gpu=False)
		b, text = self.brief(power="no_gpu")
		self.assertEqual(b["basis"], ("cpu_package", "measured"))
		self.assertIn("ranked by measured CPU package energy (RAPL), exclusive", text)
		self.assertIn("GPU          n/a, not measured (disabled)", text)
		self.assertIn("GPU:         0 of 8 measured (not measured: disabled x8)", text)

	def test_partial_coverage_is_listed_not_ranked(self):
		# reads 1-2 llm:x, 3-4 llm:x (read 3 fails), 5-6 llm:x, then tool:y
		with Session(self.spans, mode="direct", gpu=CallableReader(Counter_(fail_at=(3,)).read)) as s:
			for _ in range(3):
				with s.span("llm:x"):
					pass
			for _ in range(2):
				with s.span("tool:y"):
					pass
		b, text = self.brief(power=False)
		r = rows(b)
		self.assertTrue(r["tool:y"]["ranked"])
		self.assertFalse(r["llm:x"]["ranked"])
		self.assertIsNone(r["llm:x"]["energy_j"])        # not the sum of the 2 measured
		self.assertIn("partial 1/3 (read_failed)", r["llm:x"]["notes"])
		self.assertEqual(r["tool:y"]["share"], 1.0)
		line = [l for l in text.splitlines() if " llm:x " in l][0]
		self.assertIn("n/a", line)
		self.assertTrue(line.strip().startswith("-"))

	def test_invalid_readings_are_not_ranked(self):
		with Session(self.spans, mode="direct", gpu=CallableReader(Reversing().read)) as s:
			with s.span("agent:step"):
				with s.span("llm:x"):
					pass
		b, text = self.brief(power=False)
		r = rows(b)
		self.assertTrue(r["llm:x"]["ranked"])
		self.assertIsNone(r["agent:step"]["energy_j"])
		self.assertIn("none (counter_decrease)", r["agent:step"]["notes"])
		self.assertIn("INVALID_MEASUREMENTS", text)

	def test_values_below_the_counter_resolution_are_not_ranked(self):
		self.nested(mode="direct", gpu=CallableReader(Counter_().read),
		            resolution=Resolution.configured(0.1, "test"))
		b, text = self.brief()
		self.assertEqual(b["basis"], ("gpu", "measured"))
		self.assertFalse([r for r in b["rows"] if r["ranked"]])
		self.assertIn("4 of 4 have values only below the counter's update interval (quantised)",
		              " ".join(text.split()))
		self.assertIn("quantised", rows(b)["llm:plan"]["notes"])
		self.assertIsNotNone(rows(b)["llm:plan"]["energy_j"])    # shown, not ranked
		self.assertNotIn("share", header_line(text))

	def test_not_ranked_names_each_operations_own_reason(self):
		# regression: one operation quantised and one shared was reported as
		# "every operation's GPU values are below the counter's update interval"
		with Session(self.spans, mode="direct", gpu=CallableReader(Counter_().read),
		             resolution=Resolution.configured(0.1, "test")) as s:
			with s.span("tool:short"):
				pass
			barrier = threading.Barrier(2)
			def worker():
				with s.span("worker:long"):
					barrier.wait()
					time.sleep(0.15)
			ts = [threading.Thread(target=worker) for _ in range(2)]
			[t.start() for t in ts]
			[t.join() for t in ts]
		b, text = self.brief(power=False)
		r = rows(b)
		self.assertEqual(r["worker:long"]["notes"], ["shared"])     # long: not quantised
		self.assertEqual(r["tool:short"]["notes"], ["quantised"])
		self.assertNotIn("every operation", b["fallback_reason"])
		flat = " ".join(text.split())
		self.assertIn("1 of 2 overlapped other recorded work (shared)", flat)
		self.assertIn("1 of 2 have values only below the counter's update interval "
		              "(quantised)", flat)

	def test_some_values_below_resolution_are_ranked_and_flagged(self):
		reader = CallableReader(Counter_().read)
		with Session(self.spans, mode="direct", gpu=reader,
		             resolution=Resolution.configured(0.05, "test")) as s:
			with s.span("llm:x"):
				time.sleep(0.06)
			with s.span("llm:x"):
				pass
		b, _ = self.brief(power=False)
		r = rows(b)["llm:x"]
		self.assertTrue(r["ranked"])
		self.assertIn("quantised 1/2", r["notes"])


class TestEstimated(BriefCase):

	def test_sampled_ranks_leaf_estimates_and_lists_parents(self):
		self.nested(mode="sampled", gpu=CallableReader(Counter_().read))
		b, text = self.brief()
		self.assertEqual(b["basis"], ("gpu", "estimated"))
		r = rows(b)
		for leaf in ("tool:lookup", "tool:search"):
			self.assertTrue(r[leaf]["ranked"], leaf)
		for parent in ("agent:step", "llm:plan"):
			self.assertFalse(r[parent]["ranked"], parent)
			self.assertIsNone(r[parent]["energy_j"])
			self.assertIn("parent", r[parent]["notes"])
		self.assertIn("ESTIMATED GPU energy (~)", text)
		self.assertIn("GPU J est.", text)
		self.assertIn("~", [l for l in text.splitlines() if " tool:lookup " in l][0])
		# the whole-run GPU line is the measured run counter, never the estimates
		# (this synthetic timeline has no run counter: the sampled counter's window)
		whole = {d: (v, how) for d, v, how in b["whole_run"]}
		rep = report.build(self.d)
		self.assertEqual(whole["gpu"], (rep["window"]["domains"]["gpu"]["energy_j"],
		                                "sampler, session window"))
		self.assertEqual(rep["window"]["domains"]["gpu"]["status"], "measured")

	def test_hybrid_and_auto_rank_estimates_too(self):
		for mode, label in (("hybrid", "hybrid (requested hybrid, caller)"),
		                    ("auto", "sampled (requested auto, caller)")):
			with self.subTest(mode=mode):
				for name in os.listdir(self.d):
					os.remove(os.path.join(self.d, name))
				self.nested(mode=mode, gpu=CallableReader(Counter_().read))
				b, text = self.brief()
				self.assertEqual(b["basis"], ("gpu", "estimated"))
				self.assertIn(f"mode       {label}", text)

	def test_sampled_without_a_timeline_falls_back_to_time(self):
		self.nested(mode="sampled", gpu=CallableReader(Counter_().read))
		b, text = self.brief(power=False)
		self.assertIsNone(b["basis"])
		self.assertIn("Operations by total time. Not ranked by energy", text)
		self.assertIn("no hardware timeline", text)
		self.assertIn("not_read_by_mode", text)
		self.assertTrue(all(r["energy_j"] is None for r in b["rows"]))
		self.assertNotIn("GPU J", header_line(text))
		totals = [r["duration_total_s"] for r in b["rows"]]
		self.assertEqual(totals, sorted(totals, reverse=True))


class TestConcurrency(BriefCase):

	def concurrent(self, mode):
		barrier = threading.Barrier(2)
		with Session(self.spans, mode=mode, gpu=CallableReader(Counter_().read)) as s:
			def root(i):
				with s.span(f"worker:{i}"):
					barrier.wait()
					time.sleep(0.01)
			ts = [threading.Thread(target=root, args=(i,)) for i in range(2)]
			[t.start() for t in ts]
			[t.join() for t in ts]

	def test_overlapping_spans_are_not_ranked(self):
		for mode in ("direct", "sampled"):
			with self.subTest(mode=mode):
				for name in os.listdir(self.d):
					os.remove(os.path.join(self.d, name))
				self.concurrent(mode)
				b, text = self.brief()
				self.assertFalse([r for r in b["rows"] if r["ranked"]])
				self.assertIn("every operation had spans that overlapped other recorded work",
				              " ".join(text.split()))
				for r in b["rows"]:
					self.assertIn("shared", r["notes"])
				self.assertIn("shared: ran while other recorded spans ran", text)

	def test_concurrent_children_of_one_parent_with_estimates(self):
		# the agent --parallel shape: a sole parent whose children overlap
		barrier = threading.Barrier(2)
		with Session(self.spans, mode="sampled", gpu=CallableReader(Counter_().read)) as s:
			with s.span("agent:batch") as parent:
				def child():
					with s.span("llm:reason", parent=parent):
						barrier.wait()
						time.sleep(0.01)
				ts = [threading.Thread(target=child) for _ in range(2)]
				[t.start() for t in ts]
				[t.join() for t in ts]
		b, text = self.brief()
		r = rows(b)
		self.assertFalse([x for x in b["rows"] if x["ranked"]])
		self.assertIn("parent", r["agent:batch"]["notes"])
		self.assertIn("shared", r["llm:reason"]["notes"])
		flat = " ".join(text.split())
		self.assertIn("1 of 2 overlapped other recorded work (shared)", flat)
		self.assertIn("1 of 2 have child spans", flat)

	def test_shared_and_sole_operations_in_one_run(self):
		with Session(self.spans, mode="direct", gpu=CallableReader(Counter_().read)) as s:
			with s.span("setup:load"):
				pass
			barrier = threading.Barrier(2)
			def root():
				with s.span("worker:run"):
					barrier.wait()
					time.sleep(0.01)
			ts = [threading.Thread(target=root) for _ in range(2)]
			[t.start() for t in ts]
			[t.join() for t in ts]
		b, _ = self.brief(power=False)
		r = rows(b)
		self.assertTrue(r["setup:load"]["ranked"])
		self.assertFalse(r["worker:run"]["ranked"])
		self.assertIn("shared", r["worker:run"]["notes"])


class TestEdgeCases(BriefCase):

	def test_zero_spans(self):
		with Session(self.spans, mode="direct", gpu=False):
			pass
		b = brief.build(self.spans)
		text = brief.render(b)
		self.assertEqual(b["rows"], [])
		self.assertIn("no spans were recorded, so there is no operation-level data", text)

	def test_no_energy_anywhere_falls_back_to_time(self):
		self.nested(mode="direct", gpu=False)
		b, text = self.brief(power=False)
		self.assertIsNone(b["basis"])
		self.assertIn("no operation has a value for every span", text)
		self.assertTrue(all(r["energy_j"] is None for r in b["rows"]))
		self.assertIn("Total time is inclusive", text)

	def test_lone_attributed_spans_without_summary(self):
		self.nested(mode="direct", gpu=CallableReader(Counter_().read))
		self.attribute(power=False)
		os.remove(os.path.join(self.d, "session_summary.json"))
		b = brief.build(self.d)
		self.assertIn("SUMMARY_MISSING", b["warnings"])
		self.assertEqual(b["basis"], ("gpu", "measured"))
		self.assertIn("mode       not recorded", brief.render(b))

	def test_top_limits_rows_and_says_so(self):
		with Session(self.spans, mode="direct", gpu=CallableReader(Counter_().read)) as s:
			for i in range(5):
				with s.span(f"op:{i}"):
					pass
		self.attribute(power=False)
		text = brief.render(brief.build(self.d), top=2)
		self.assertIn("... 3 more operations in the full report", text)

	def test_report_dict_is_unchanged_by_the_brief_pass(self):
		self.nested(mode="sampled", gpu=CallableReader(Counter_().read))
		self.attribute(power=True)
		before = json.dumps(report.build(self.d), sort_keys=True)
		brief.build(self.d)
		self.assertEqual(json.dumps(report.build(self.d), sort_keys=True), before)


class TestLegacyV1(unittest.TestCase):

	def setUp(self):
		import tempfile
		self._tmp = tempfile.TemporaryDirectory()
		self.d = self._tmp.name

	def tearDown(self):
		self._tmp.cleanup()

	def test_sequential_events_rank_per_event(self):
		b = brief.build(unpack_v1("cor_webqsp", self.d))
		text = brief.render(b)
		self.assertEqual(b["basis"], ("gpu", "measured"))
		self.assertIn("per event (schema v1 has no span hierarchy", text)
		self.assertEqual(rows(b)["llm:relation_rank"]["energy_j"],
		                 {r["operation"]: r for r in report.build(self.d)["operations"]}
		                 ["llm:relation_rank"]["energy"]["gpu"]["energy_j"])

	def test_overlapping_events_are_not_ranked(self):
		b = brief.build(unpack_v1("synthetic_rapl_multigpu", self.d))
		self.assertIn("schema-v1 events overlap in time", b["fallback_reason"])
		self.assertTrue(all(not r["ranked"] for r in b["rows"]))


class TestFullReportNote(BriefCase):

	def test_note_on_parent_estimates(self):
		self.nested(mode="sampled", gpu=CallableReader(Counter_().read))
		text = report.render_text(self.attribute(power=True))
		self.assertIn("est. gpu J is not exclusive", text)

	def test_no_note_without_estimates(self):
		self.nested(mode="direct", gpu=CallableReader(Counter_().read))
		self.assertNotIn("est. gpu J is not exclusive",
		                 report.render_text(self.attribute(power=True)))


class TestCli(BriefCase):

	def cli(self, *args):
		return subprocess.run([sys.executable, "-m", "agent_energy_profiler", *args],
		                      capture_output=True, text=True, timeout=120)

	def test_report_brief(self):
		self.nested(mode="direct", gpu=CallableReader(Counter_().read))
		self.attribute(power=True)
		res = self.cli("report", self.d, "--brief")
		self.assertEqual(res.returncode, 0, res.stderr)
		self.assertIn("ENERGY SUMMARY (brief)", res.stdout)
		self.assertIn(f"Full report: agent-energy-profiler report {self.d}", res.stdout)
		self.assertNotIn("ENERGY REPORT", res.stdout)
		full = self.cli("report", self.d)
		self.assertIn("ENERGY REPORT", full.stdout)
		self.assertEqual(full.stdout.strip(), report.render_text(report.build(self.d)).strip())
		res = self.cli("report", self.d, "--brief", "--top", "1")
		self.assertIn("... 3 more operations", res.stdout)

	def test_brief_is_text_only(self):
		res = self.cli("report", GOLDEN, "--brief", "--format", "json")
		self.assertEqual(res.returncode, 2)
		self.assertIn("--brief is a text report", res.stderr)


if __name__ == "__main__":
	unittest.main()
