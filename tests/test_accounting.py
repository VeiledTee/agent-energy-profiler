"""M2: inclusive/exclusive accounting and double-count-free totals."""

import unittest

from agent_energy_profiler import accounting
from agent_energy_profiler import measurement as m

RES = m.Resolution.configured(0.1, "synthetic")


def span(name, sid, parent, t0, t1, mj=None, gpu=None, other=False):
	"""A v2 span; `mj` = (start_mj, end_mj) of one device."""
	if gpu is None:
		gpu = (m.gpu_in_band([mj[0]], [mj[1]], t1 - t0, RES) if mj
		       else m.unavailable_gpu("no_counter"))
	return {"name": name, "span_id": sid, "parent_span_id": parent,
	        "start_monotonic_s": t0, "end_monotonic_s": t1, "duration_s": t1 - t0,
	        "other_sessions_active": other, "status": "ok", "energy": {"gpu": gpu}}


def by_name(spans):
	return {s["name"]: s for s in spans}


class TestExclusive(unittest.TestCase):

	def test_nested_parent_minus_sequential_children(self):
		spans = by_name(accounting.annotate([
			span("root", "r", None, 0.0, 10.0, (0, 10_000)),
			span("a", "a", "r", 1.0, 3.0, (1_000, 3_000)),
			span("b", "b", "r", 4.0, 8.0, (4_000, 8_000)),
			span("a1", "a1", "a", 1.5, 2.0, (1_500, 2_000)),
		]))
		r, a, b, a1 = (spans[k]["accounting"] for k in ("root", "a", "b", "a1"))
		self.assertEqual((r["depth"], a["depth"], a1["depth"]), (0, 1, 2))
		self.assertEqual(r["n_children"], 2)
		self.assertEqual(r["exclusive"]["gpu"], {"energy_j": 4.0, "status": "measured",
		                                        "reason": None})
		self.assertEqual(a["exclusive"]["gpu"]["energy_j"], 1.5)
		self.assertEqual(b["exclusive"]["gpu"]["energy_j"], 4.0)
		self.assertEqual(a1["exclusive"]["gpu"]["energy_j"], 0.5)
		self.assertAlmostEqual(r["exclusive_duration_s"], 4.0)
		self.assertAlmostEqual(a["exclusive_duration_s"], 1.5)
		for acc in (r, a, b, a1):
			self.assertEqual(acc["attribution"], "sole")
			self.assertEqual(acc["concurrent_spans"], 0)
		# exclusive values partition the root's inclusive energy exactly
		total = sum(s["accounting"]["exclusive"]["gpu"]["energy_j"] for s in spans.values())
		self.assertEqual(total, spans["root"]["energy"]["gpu"]["energy_j"])

	def test_zero_exclusive_is_measured_zero(self):
		spans = by_name(accounting.annotate([
			span("root", "r", None, 0.0, 1.0, (0, 500)),
			span("a", "a", "r", 0.1, 0.9, (0, 500)),
		]))
		self.assertEqual(spans["root"]["accounting"]["exclusive"]["gpu"]["energy_j"], 0.0)
		self.assertEqual(spans["root"]["accounting"]["exclusive"]["gpu"]["status"], "measured")

	def test_overlapping_children_make_parent_exclusive_not_defensible(self):
		spans = by_name(accounting.annotate([
			span("root", "r", None, 0.0, 10.0, (0, 10_000)),
			span("a", "a", "r", 1.0, 5.0, (1_000, 5_000)),
			span("b", "b", "r", 3.0, 7.0, (3_000, 7_000)),
		]))
		r = spans["root"]["accounting"]
		self.assertTrue(r["children_overlap"])
		self.assertEqual(r["exclusive"]["gpu"]["status"], "not_defensible")
		self.assertEqual(r["exclusive"]["gpu"]["reason"], "children_overlap")
		self.assertIsNone(r["exclusive"]["gpu"]["energy_j"])
		for k in ("a", "b"):
			acc = spans[k]["accounting"]
			self.assertEqual(acc["attribution"], "shared")
			self.assertEqual(acc["concurrent_spans"], 1)
			self.assertEqual(acc["exclusive"]["gpu"]["reason"], "concurrent_spans")
		# exclusive duration uses the union of the children
		self.assertAlmostEqual(r["exclusive_duration_s"], 4.0)
		# inclusive energy is still reported: it is the device's energy
		self.assertEqual(spans["a"]["energy"]["gpu"]["energy_j"], 4.0)

	def test_unmeasured_child_makes_parent_exclusive_unavailable(self):
		spans = by_name(accounting.annotate([
			span("root", "r", None, 0.0, 10.0, (0, 10_000)),
			span("a", "a", "r", 1.0, 3.0, None),
		]))
		ex = spans["root"]["accounting"]["exclusive"]["gpu"]
		self.assertEqual((ex["status"], ex["reason"]), ("unavailable", "child_unmeasured"))
		ex = spans["a"]["accounting"]["exclusive"]["gpu"]
		self.assertEqual((ex["status"], ex["reason"]), ("unavailable", "parent_unmeasured"))

	def test_negative_exclusive_is_invalid(self):
		spans = by_name(accounting.annotate([
			span("root", "r", None, 0.0, 10.0, (0, 1_000)),
			span("a", "a", "r", 1.0, 3.0, (0, 5_000)),
		]))
		ex = spans["root"]["accounting"]["exclusive"]["gpu"]
		self.assertEqual((ex["status"], ex["reason"], ex["energy_j"]),
		                 ("invalid", "negative_exclusive", None))

	def test_child_outside_parent_is_not_defensible(self):
		spans = by_name(accounting.annotate([
			span("root", "r", None, 0.0, 2.0, (0, 2_000)),
			span("a", "a", "r", 1.0, 3.0, (1_000, 1_500)),
		]))
		self.assertEqual(spans["root"]["accounting"]["exclusive"]["gpu"]["reason"],
		                 "child_outside_parent")

	def test_other_session_makes_everything_shared(self):
		spans = by_name(accounting.annotate([
			span("root", "r", None, 0.0, 2.0, (0, 2_000), other=True)]))
		acc = spans["root"]["accounting"]
		self.assertEqual(acc["attribution"], "shared")
		self.assertEqual(acc["exclusive"]["gpu"]["reason"], "other_sessions_active")

	def test_orphan_is_a_root_flagged_parent_missing(self):
		spans = by_name(accounting.annotate([
			span("x", "x", "ghost", 0.0, 1.0, (0, 1_000))]))
		self.assertEqual(spans["x"]["accounting"]["depth"], 0)
		self.assertTrue(spans["x"]["accounting"]["parent_missing"])

	def test_float_domains_without_raw(self):
		def d(v):
			return m.domain(v, m.MEASURED, None, "rapl", m.SAMPLED)
		spans = by_name(accounting.annotate([
			span("root", "r", None, 0.0, 1.0, gpu=d(0.3)),
			span("a", "a", "r", 0.1, 0.2, gpu=d(0.1)),
			span("b", "b", "r", 0.3, 0.4, gpu=d(0.2)),
		]))
		ex = spans["root"]["accounting"]["exclusive"]["gpu"]
		# 0.3 - 0.1 - 0.2 is -2.8e-17 in floating point: within round-off, so 0
		self.assertEqual((ex["status"], ex["energy_j"]), ("measured", 0.0))
		spans = by_name(accounting.annotate([
			span("root", "r", None, 0.0, 1.0, gpu=d(0.3)),
			span("a", "a", "r", 0.1, 0.2, gpu=d(0.31)),
		]))
		self.assertEqual(spans["root"]["accounting"]["exclusive"]["gpu"]["status"], "invalid")

	def test_touching_spans_do_not_overlap(self):
		spans = by_name(accounting.annotate([
			span("a", "a", None, 0.0, 1.0, (0, 1)),
			span("b", "b", None, 1.0, 2.0, (1, 2)),
		]))
		self.assertEqual(spans["a"]["accounting"]["concurrent_spans"], 0)
		self.assertEqual(spans["b"]["accounting"]["concurrent_spans"], 0)


class TestSummary(unittest.TestCase):

	def test_totals_add_roots_only_never_parent_and_child(self):
		spans = accounting.annotate([
			span("root", "r", None, 0.0, 10.0, (0, 10_000)),
			span("a", "a", "r", 1.0, 3.0, (1_000, 3_000)),
			span("root2", "r2", None, 11.0, 12.0, (11_000, 12_000)),
		])
		summ = accounting.summarize(spans)
		self.assertEqual(summ["sum_root_inclusive"]["gpu"]["energy_j"], 11.0)
		self.assertFalse(summ["root_spans_overlap"])
		self.assertEqual(summ["by_label"]["root"]["exclusive"]["gpu"]["energy_j"], 8.0)
		self.assertEqual(summ["by_label"]["a"]["exclusive"]["gpu"]["energy_j"], 2.0)
		labelled = sum(row["exclusive"]["gpu"]["energy_j"] for row in summ["by_label"].values())
		self.assertEqual(labelled, 11.0)

	def test_concurrent_roots_give_no_total(self):
		spans = accounting.annotate([
			span("t1", "t1", None, 0.0, 5.0, (0, 5_000)),
			span("t2", "t2", None, 2.0, 6.0, (2_000, 6_000)),
		])
		summ = accounting.summarize(spans)
		self.assertTrue(summ["root_spans_overlap"])
		self.assertEqual(summ["max_root_concurrency"], 2)
		self.assertEqual(summ["sum_root_inclusive"]["gpu"],
		                 {"energy_j": None, "reason": "concurrent_root_spans"})
		self.assertEqual(summ["by_label"]["t1"]["exclusive"]["gpu"]["n_excluded"], 1)
		self.assertIsNone(summ["by_label"]["t1"]["exclusive"]["gpu"]["energy_j"])

	def test_unmeasured_root_gives_no_total(self):
		spans = accounting.annotate([
			span("t1", "t1", None, 0.0, 5.0, (0, 5_000)),
			span("t2", "t2", None, 6.0, 7.0, None),
		])
		t = accounting.summarize(spans)["sum_root_inclusive"]["gpu"]
		self.assertEqual((t["energy_j"], t["reason"]), (None, "root_unmeasured"))

	def test_window_minus_roots_is_not_clamped(self):
		spans = accounting.annotate([span("t1", "t1", None, 0.0, 5.0, (0, 5_000))])
		window = {"domains": {"gpu": m.domain(4.5, m.MEASURED, None, "x", m.SAMPLED)}}
		self.assertAlmostEqual(accounting.summarize(spans, window)["window_minus_roots"]["gpu"], -0.5)

	def test_resolution_counts(self):
		spans = accounting.annotate([
			span("short", "s", None, 0.0, 0.05, (0, 0)),
			span("long", "l", None, 1.0, 2.0, (0, 1_000)),
			span("none", "n", None, 3.0, 4.0, None),
		])
		counts = accounting.summarize(spans)["energy_status_counts"]["gpu"]
		self.assertEqual(counts, {"measured": 1, "measured_below_resolution": 1,
		                          "unavailable": 1})


if __name__ == "__main__":
	unittest.main()
