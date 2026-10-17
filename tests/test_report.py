"""M3: the version-aware report (schema v1 and v2)."""

import asyncio
import csv
import gzip
import io
import json
import math
import os
import tempfile
import unittest

from agent_energy_profiler import attribution_v2, report
from agent_energy_profiler.measurement import CallableReader, Resolution
from agent_energy_profiler.session import Session

HERE = os.path.dirname(os.path.abspath(__file__))
GOLDEN = os.path.join(HERE, "golden")


def unpack_v1(case, d):
	for f in ("events_attributed.jsonl", "energy_summary.csv", "trajectory_summary.json"):
		with gzip.open(os.path.join(GOLDEN, case, "expected", f + ".gz"), "rb") as g, \
		     open(os.path.join(d, f), "wb") as out:
			out.write(g.read())
	return d


def codes(rep):
	return {w["code"] for w in rep["warnings"]}


class Counter_:
	def __init__(self, step=100, fail_at=()):
		self.v, self.n, self.step, self.fail_at = 1_000_000, 0, step, set(fail_at)

	def read(self):
		self.n += 1
		if self.n in self.fail_at:
			return [None]
		self.v += self.step
		return [self.v]


class V2Case(unittest.TestCase):

	def setUp(self):
		self._tmp = tempfile.TemporaryDirectory()
		self.d = self._tmp.name
		self.spans = os.path.join(self.d, "spans.jsonl")

	def tearDown(self):
		self._tmp.cleanup()

	def attribute(self, power=True):
		args = ["--spans", self.spans, "--out-dir", self.d]
		if power:
			with open(self.spans) as f:
				recs = [json.loads(l) for l in f if '"record_type": "span"' in l]
			t0 = min(r["start_unix_s"] for r in recs) - 1
			t1 = max(r["end_unix_s"] for r in recs) + 1
			path = os.path.join(self.d, "power.csv")
			with open(path, "w") as f:
				f.write("t,gpu0_w,gpu0_energy_mj,rapl_package-0_intel-rapl:0_uj,"
				        "rapl_dram_intel-rapl:0:2_uj\n")
				for i in range(21):
					t = t0 + (t1 - t0) * i / 20
					f.write(f"{t:.6f},100.0,{1000 * i},{2_000_000 * i},{200_000 * i}\n")
			args += ["--power", path]
		self.assertEqual(attribution_v2.main(args), 0)
		return report.build(self.d)


class TestNestedNoDoubleCounting(V2Case):

	def test_totals_use_roots_and_operations_use_exclusive(self):
		with Session(self.spans, mode="direct", gpu=CallableReader(Counter_().read),
		             resolution=Resolution.configured(0.1, "test")) as s:
			for _ in range(2):
				with s.span("agent:step"):
					with s.span("llm:plan"):
						with s.span("tool:lookup"):
							pass
					with s.span("tool:search"):
						pass
		rep = self.attribute()
		gpu = rep["totals"]["domains"]["gpu"]
		self.assertEqual(gpu["n_spans"], 2)              # two roots, not eight spans
		self.assertIsNotNone(gpu["total_j"])
		ops = {r["operation"]: r for r in rep["operations"]}
		exclusive_sum = sum(r["energy"]["gpu"]["energy_j"] for r in ops.values())
		self.assertAlmostEqual(exclusive_sum, gpu["total_j"], places=12)
		# inclusive sums would exceed the total: the parent contains its children
		with open(os.path.join(self.d, "spans_attributed.jsonl")) as f:
			inclusive = sum(json.loads(l)["energy"]["gpu"]["energy_j"] for l in f)
		self.assertGreater(inclusive, gpu["total_j"])
		self.assertEqual(ops["agent:step"]["energy_basis"], "exclusive")
		paths = {h["path"]: h for h in rep["hierarchy"]}
		self.assertIn("agent:step > llm:plan > tool:lookup", paths)
		self.assertEqual(paths["agent:step > llm:plan"]["depth"], 1)
		# per-span boundary totals summed vs per-domain totals summed: equal up
		# to floating-point summation order
		self.assertAlmostEqual(rep["totals"]["domains"]["total"]["total_j"],
		                       math.fsum(rep["totals"]["domains"][d]["total_j"]
		                                 for d in ("gpu", "cpu_package", "dram")), places=12)
		self.assertIn("BELOW_RESOLUTION", codes(rep))
		# every span is measured: estimates exist (timeline present) but are
		# only a diagnostic comparison, never "estimated energy" of the run
		self.assertEqual(rep["totals"]["estimated_gpu"]["n_spans"], 0)
		self.assertIsNone(rep["totals"]["estimated_gpu"]["sum_j"])
		self.assertEqual(rep["totals"]["gpu_estimate_vs_counter"]["n_spans"], 2)
		self.assertTrue(all(r["estimated_gpu_j"] is None for r in rep["operations"]))
		self.assertNotIn("ESTIMATES_PRESENT", codes(rep))

	def test_report_round_trips_through_json_and_csv(self):
		with Session(self.spans, mode="direct", gpu=False) as s:
			with s.span("llm:x"):
				pass
		rep = self.attribute(power=False)
		json.loads(json.dumps(rep))
		rows = list(csv.DictReader(io.StringIO(report.operations_csv(rep))))
		self.assertEqual(rows[0]["operation"], "llm:x")
		self.assertIn("MEASURED ENERGY", report.render_text(rep))


class TestMissingValues(V2Case):

	def test_missing_root_withholds_the_total_but_shows_the_partial_sum(self):
		with Session(self.spans, mode="direct", gpu=CallableReader(Counter_(fail_at=(3,)).read)) as s:
			for _ in range(3):
				with s.span("llm:x"):
					pass
		rep = self.attribute(power=False)
		gpu = rep["totals"]["domains"]["gpu"]
		self.assertIsNone(gpu["total_j"])
		self.assertEqual((gpu["n_measured"], gpu["n_spans"]), (2, 3))
		self.assertAlmostEqual(gpu["sum_measured_j"], 0.2)
		self.assertIn("1 of 3", gpu["reason"])
		self.assertEqual(rep["coverage"]["gpu"]["reasons"], {"read_failed": 1})
		self.assertIn("DOMAIN_UNAVAILABLE", codes(rep))   # no timeline: no CPU/DRAM
		self.assertFalse(rep["window"]["available"])

	def test_gpu_absent(self):
		with Session(self.spans, mode="direct", gpu=False) as s:
			with s.span("llm:x"):
				pass
		rep = self.attribute(power=False)
		gpu = rep["totals"]["domains"]["gpu"]
		self.assertIsNone(gpu["total_j"])
		self.assertIsNone(gpu["sum_measured_j"])
		self.assertEqual(rep["coverage"]["gpu"]["reasons"], {"disabled": 1})
		self.assertEqual(rep["run"]["gpu"]["reader"], "disabled")

	def test_unknown_resolution_is_warned(self):
		with Session(self.spans, mode="direct", gpu=CallableReader(Counter_().read)) as s:
			with s.span("llm:x"):
				pass
		rep = self.attribute(power=False)
		self.assertIn("RESOLUTION_UNKNOWN", codes(rep))
		self.assertNotIn("BELOW_RESOLUTION", codes(rep))

	def test_zero_readings_are_flagged(self):
		with Session(self.spans, mode="direct", gpu=CallableReader(Counter_(step=0).read)) as s:
			with s.span("llm:x"):
				pass
		rep = self.attribute(power=False)
		self.assertIn("ZERO_READINGS", codes(rep))
		self.assertEqual(rep["totals"]["domains"]["gpu"]["total_j"], 0.0)


class TestShared(V2Case):

	def test_concurrent_roots_refuse_a_total(self):
		counter = Counter_()
		with Session(self.spans, mode="direct", gpu=CallableReader(counter.read)) as s:
			async def worker(name):
				async with s.span(name):
					await asyncio.sleep(0.01)

			async def main():
				await asyncio.gather(worker("tool:a"), worker("tool:b"))
			asyncio.run(main())
		rep = self.attribute()
		self.assertTrue(rep["totals"]["top_level_overlap"])
		for d in ("gpu", "cpu_package", "dram", "total"):
			self.assertIsNone(rep["totals"]["domains"][d]["total_j"])
			self.assertIsNone(rep["totals"]["domains"][d]["sum_measured_j"])
		self.assertTrue({"SHARED_ATTRIBUTION", "OVERLAPPING_TOP_LEVEL"} <= codes(rep))
		self.assertTrue(rep["window"]["available"])
		self.assertEqual(rep["window"]["domains"]["gpu"]["status"], "measured")

	def test_raw_input_is_attributed_without_a_timeline(self):
		with Session(self.spans, mode="direct", gpu=CallableReader(Counter_().read)) as s:
			with s.span("llm:x"):
				pass
		rep = report.build(self.spans)
		self.assertEqual(rep["source"]["kind"], "raw")
		self.assertIn("NO_TIMELINE", codes(rep))
		self.assertIsNotNone(rep["totals"]["domains"]["gpu"]["total_j"])


class TestV1(unittest.TestCase):

	def setUp(self):
		self._tmp = tempfile.TemporaryDirectory()

	def tearDown(self):
		self._tmp.cleanup()

	def v1_summary(self, d):
		with open(os.path.join(d, "energy_summary.csv")) as f:
			return {(r["scope"], r["key"]): r for r in csv.DictReader(f)}

	def test_estimates_are_separate_and_v1_numbers_are_preserved(self):
		d = unpack_v1("synthetic_rapl_multigpu", self._tmp.name)
		rep = report.build(d)
		self.assertEqual(rep["source"]["schema"], 1)
		self.assertEqual(rep["totals"]["estimated_gpu"]["n_spans"], 3)
		self.assertIn("ESTIMATES_PRESENT", codes(rep))
		v1 = self.v1_summary(d)
		for op in rep["operations"]:
			row = v1[("operation_label", op["operation"])]
			measured = (op["energy"].get("gpu") or {}).get("energy_j") or 0.0
			estimated = op["estimated_gpu_j"] or 0.0
			# v1's gpu column mixed the two; the report separates them, and the
			# parts add back to v1's figure exactly as v1 printed it.
			self.assertEqual(f"{measured + estimated:.3f}", row["gpu_energy_j"], op["operation"])
			self.assertEqual(f"{op['energy']['cpu_package']['energy_j']:.3f}",
			                 row["cpu_package_energy_j"])
			self.assertEqual(op["n"], int(row["n_events"]))
		# the synthetic case nests one event in another: v1 totals are refused
		self.assertTrue(rep["totals"]["top_level_overlap"])
		self.assertEqual(rep["window"]["domains"]["gpu"]["energy_j"],
		                 math.fsum(t["trajectory_gpu_energy_j"] for t in json.load(
		                     open(os.path.join(d, "trajectory_summary.json")))["trajectories"]))

	def test_real_run_slice_matches_v1_per_label(self):
		d = unpack_v1("cor_webqsp", self._tmp.name)
		rep = report.build(d)
		v1 = self.v1_summary(d)
		self.assertEqual(rep["totals"]["estimated_gpu"]["n_spans"], 0)
		for op in rep["operations"]:
			gpu = op["energy"]["gpu"]
			self.assertEqual(f"{gpu['energy_j']:.3f}",
			                 v1[("operation_label", op["operation"])]["gpu_energy_j"])
		gpu = rep["totals"]["domains"]["gpu"]
		self.assertFalse(rep["totals"]["top_level_overlap"])
		self.assertEqual(gpu["n_spans"], 1192)
		self.assertIsNotNone(gpu["total_j"])
		# GPU-only host: the boundary total is unavailable, never a partial sum
		self.assertIsNone(rep["totals"]["domains"]["total"]["total_j"])
		self.assertIn("DOMAIN_UNAVAILABLE", codes(rep))

	def test_resolution_can_be_supplied_for_v1(self):
		d = unpack_v1("cor_webqsp", self._tmp.name)
		rep = report.build(d, Resolution.configured(0.106, "spec"))
		self.assertIn("BELOW_RESOLUTION", codes(rep))
		self.assertGreater(rep["coverage"]["gpu"]["states"]["measured_below_resolution"], 0)


if __name__ == "__main__":
	unittest.main()
