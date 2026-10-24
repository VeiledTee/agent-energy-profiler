"""The HTML report's view model: the reports' own rules, a checked contract,
and nothing private by default."""

import gzip
import json
import math
import os
import tempfile
import threading
import time
import unittest
from unittest import mock

from agent_energy_profiler import brief, report, view_model
from agent_energy_profiler.measurement import CallableReader, Resolution
from agent_energy_profiler.session import Session

from test_report import GOLDEN, Counter_, V2Case, unpack_v1

GOLDEN_V2 = os.path.join(os.path.dirname(GOLDEN), "golden_v2", "session_mixed")


def paths(view):
	return {tuple(p["path"]): p for p in view["paths"]}


def cells(view, name, scope, domain="gpu"):
	return [c for s in view["instances"] if s["name"] == name for c in s.get("cells", ())
	        if c["scope"] == scope and c["domain"] == domain]


class ViewCase(V2Case):

	def nested(self, n_roots=2, attributes=None, **kw):
		with Session(self.spans, attributes=attributes, **kw) as s:
			for i in range(n_roots):
				with s.span("agent:step", attributes={"question": f"q{i}", "user": "alice"}):
					with s.span("llm:plan"):
						with s.span("tool:lookup"):
							pass
					with s.span("tool:search"):
						pass


class TestAccounting(ViewCase):

	def test_label_rows_are_the_brief_rows(self):
		self.nested(mode="direct", gpu=CallableReader(Counter_().read))
		self.attribute()
		view = view_model.build(self.d)
		b = brief.build(self.d)
		self.assertEqual([(r["name"], r["energy_j"], r["ranked"], r["share"], r["notes"])
		                  for r in view["operations"]],
		                 [(r["operation"], r["energy_j"], r["ranked"], r["share"], r["notes"])
		                  for r in b["rows"]])

	def test_paths_use_exclusive_energy_and_never_count_twice(self):
		self.nested(mode="direct", gpu=CallableReader(Counter_().read))
		rep = self.attribute()
		view = view_model.build(self.d)
		p = paths(view)
		self.assertEqual(set(p), {("agent:step",), ("agent:step", "llm:plan"),
		                          ("agent:step", "llm:plan", "tool:lookup"),
		                          ("agent:step", "tool:search")})
		ranked = [r for r in view["paths"] if r["ranked"]]
		self.assertEqual(len(ranked), 4)
		self.assertAlmostEqual(math.fsum(r["energy_j"] for r in ranked),
		                       rep["totals"]["domains"]["gpu"]["total_j"], places=12)
		# the same numbers as the full report's hierarchy (exclusive GPU)
		for h in rep["hierarchy"]:
			self.assertAlmostEqual(p[tuple(h["path"].split(" > "))]["energy_j"],
			                       h["exclusive_gpu_energy_j"], places=12)
		self.assertEqual(p[("agent:step", "llm:plan")]["parent"], p[("agent:step",)]["id"])
		self.assertEqual(sum(r["n"] for r in view["paths"]), view["run"]["n_spans"])

	def test_estimates_rank_leaves_and_keep_parents_inclusive(self):
		self.nested(mode="sampled", gpu=CallableReader(Counter_().read))
		self.attribute()
		view = view_model.build(self.d)
		self.assertEqual((view["basis"]["kind"], view["basis"]["scope"]), ("estimated", "window"))
		p = paths(view)
		for parent in (("agent:step",), ("agent:step", "llm:plan")):
			self.assertFalse(p[parent]["ranked"])
			self.assertIsNone(p[parent]["energy_j"])
			self.assertIn("parent", p[parent]["notes"])
		self.assertTrue(p[("agent:step", "tool:search")]["ranked"])
		# a parent's own estimate is kept, as an inclusive window value only
		[est, _] = cells(view, "agent:step", "window")
		self.assertEqual(est["kind"], "estimated")
		self.assertFalse(cells(view, "agent:step", "exclusive")[0].get("value_j"))

	def test_shared_spans_are_not_ranked(self):
		barrier = threading.Barrier(2)
		with Session(self.spans, mode="direct", gpu=CallableReader(Counter_().read)) as s:
			def worker():
				with s.span("worker:run"):
					barrier.wait()
					time.sleep(0.01)
			ts = [threading.Thread(target=worker) for _ in range(2)]
			[t.start() for t in ts]
			[t.join() for t in ts]
		self.attribute(power=False)
		view = view_model.build(self.d)
		[row] = view["paths"]
		self.assertFalse(row["ranked"])
		self.assertIn("shared", row["notes"])
		for s in view["instances"]:
			self.assertEqual(s["attribution"], "shared")
			[ex] = [c for c in s["cells"] if c["scope"] == "exclusive" and c["domain"] == "gpu"]
			self.assertEqual((ex["kind"], ex.get("value_j")), ("not_defensible", None))

	def test_missing_values_are_never_zero(self):
		self.nested(mode="direct", gpu=False)
		self.attribute(power=False)
		view = view_model.build(self.d)
		self.assertIsNone(view["basis"])
		for s in view["instances"]:
			for c in s["cells"]:
				self.assertNotIn("value_j", c)
				self.assertIn(c["kind"], ("unavailable", "invalid", "not_defensible"))
		for c in view["whole_run"]:
			self.assertNotIn("value_j", c)
			self.assertTrue(c["reason"])

	def test_below_resolution_is_qualified(self):
		self.nested(mode="direct", gpu=CallableReader(Counter_().read),
		            resolution=Resolution.configured(0.1, "test"))
		self.attribute(power=False)
		view = view_model.build(self.d)
		for c in cells(view, "tool:lookup", "inclusive"):
			self.assertIn("below_resolution", c["qualifiers"])
		self.assertFalse([r for r in view["paths"] if r["ranked"]])

	def test_v1_and_v2_golden_runs(self):
		for case in ("cor_webqsp", "synthetic_rapl_multigpu"):
			with self.subTest(case=case), tempfile.TemporaryDirectory() as d:
				view = view_model.build(unpack_v1(case, d))
				self.assertEqual(view["run"]["schema"], 1)
				self.assertEqual(len(view["paths"]), len(view["operations"]))   # no hierarchy
				self.assertEqual(view["run"]["n_spans"], report.build(d)["counts"]["spans"])
		with tempfile.TemporaryDirectory() as d:
			for name in ("spans_attributed.jsonl", "session_summary.json"):
				with gzip.open(os.path.join(GOLDEN_V2, "expected", name + ".gz"), "rb") as g, \
				     open(os.path.join(d, name), "wb") as f:
					f.write(g.read())
			view = view_model.build(d)
			kinds = {c["kind"] for s in view["instances"] for c in s.get("cells", ())}
			self.assertTrue({"measured", "unavailable", "not_defensible"} <= kinds)

	def test_zero_spans(self):
		with Session(self.spans, mode="direct", gpu=False):
			pass
		view = view_model.build(self.spans)
		self.assertEqual((view["paths"], view["instances"]), ([], []))
		self.assertIn("no spans were recorded", view["fallback_reason"])


class TestLargeRuns(ViewCase):

	def test_instances_are_limited_but_aggregates_are_complete(self):
		with Session(self.spans, mode="direct", gpu=CallableReader(Counter_().read)) as s:
			for i in range(60):
				with s.span("llm:x") as h:
					if i == 37:
						h.set_status("error")
			for _ in range(5):
				with s.span("tool:y"):
					pass
		self.attribute(power=False)
		view = view_model.build(self.d, max_instances=10)
		info = view["instances_info"]
		self.assertEqual((info["shown"], info["total"]), (10, 65))
		self.assertIn("still covers all 65 spans", info["policy"])
		p = {r["name"]: r for r in view["paths"]}
		self.assertEqual((p["llm:x"]["n"], p["tool:y"]["n"]), (60, 5))
		self.assertEqual(p["llm:x"]["n_not_ok"], 1)
		self.assertEqual(sum(r["n_instances_shown"] for r in view["paths"]), 10)
		# the span that failed is one of those listed
		self.assertIn("error", [s.get("status") for s in view["instances"]])
		full = view_model.build(self.d)
		self.assertEqual(full["paths"][0]["energy_j"], view["paths"][0]["energy_j"])


class TestPrivacy(ViewCase):

	def test_attributes_are_omitted_unless_allowed(self):
		self.nested(mode="direct", gpu=CallableReader(Counter_().read),
		            attributes={"customer": "acme-secret"})
		self.attribute(power=False)
		text = json.dumps(view_model.build(self.d))
		for secret in ("alice", "acme-secret", '"question"', '"user"', '"customer"'):
			self.assertNotIn(secret, text)
		view = view_model.build(self.d, attributes=["question"])
		self.assertEqual({k for s in view["instances"] for k in s.get("attributes", {})},
		                 {"question"})
		self.assertNotIn("alice", json.dumps(view))
		self.assertEqual(view["run"]["attributes"], {})
		view = view_model.build(self.d, all_attributes=True)
		self.assertIn("alice", json.dumps(view))
		self.assertEqual(view["run"]["attributes"], {"customer": "acme-secret"})
		self.assertEqual(view["privacy"]["attributes"], "all")

	def test_no_ids_paths_or_host_names(self):
		self.nested(mode="direct", gpu=CallableReader(Counter_().read))
		# what a recording can carry: a host name in the disturbance test and
		# the mode reason, and an absolute path to the hardware report
		with open(self.spans) as f:
			lines = [json.loads(line) for line in f]
		meas = lines[0]["config"]["measurement"]
		meas["mode_reason"] = "auto: the hardware report is from secret-host-42, not this host"
		meas["counter_disturbance"].update(hostname="secret-host-42",
		                                   source="/home/someone/private/hw.json")
		lines[0]["config"]["resolution"]["source"] = "/home/someone/private/hw.json"
		with open(self.spans, "w") as f:
			f.write("\n".join(json.dumps(r) for r in lines) + "\n")
		self.attribute(power=False)
		with mock.patch("socket.gethostname", return_value="this-machine-77"), \
		     mock.patch("platform.node", return_value="this-machine-77"):
			view = view_model.build(self.d)
		text = json.dumps(view)
		for secret in ("secret-host-42", "/home/someone", self.d, "this-machine-77"):
			self.assertNotIn(secret, text)
		self.assertIn(view_model.REDACTED, view["provenance"]["mode_reason"])
		self.assertEqual(view["provenance"]["counter_disturbance"]["source"], "hw.json")
		with open(os.path.join(self.d, "spans_attributed.jsonl")) as f:
			ids = [json.loads(line)["span_id"] for line in f]
		for sid in ids:
			self.assertNotIn(sid, text)

	def _prompt_labels(self):
		prompt = "llm:what is the account balance of jane doe"
		with Session(self.spans, mode="direct", gpu=CallableReader(Counter_().read)) as s:
			for _ in range(2):
				with s.span("agent:answer"):
					with s.span(prompt):
						pass
					with s.span("tool:lookup"):
						pass
		self.attribute(power=False)
		return prompt

	def test_labels_that_look_like_free_text_are_counted_not_changed(self):
		prompt = self._prompt_labels()
		view = view_model.build(self.d)
		privacy = view["privacy"]
		self.assertEqual((privacy["labels"], privacy["n_labels"], privacy["n_free_text_labels"],
		                  privacy["many_labels"]), ("as_written", 3, 1, False))
		self.assertIn(prompt, [r["name"] for r in view["operations"]])

	def test_redacted_labels_leave_nothing_but_numbers(self):
		prompt = self._prompt_labels()
		plain = view_model.build(self.d)
		view = view_model.build(self.d, redact_labels=True)
		text = json.dumps(view)
		for secret in ("jane doe", "agent:answer", "tool:lookup", "balance"):
			self.assertNotIn(secret, text)
		self.assertEqual(sorted(r["name"] for r in view["operations"]),
		                 ["operation 1", "operation 2", "operation 3"])
		# numbered in file order (a child is written when it closes, before
		# its parent); the hierarchy is kept
		root = [r["name"] for r in view["paths"] if r["depth"] == 0]
		self.assertEqual(len(root), 1)
		self.assertEqual(sorted(r["path"][0] for r in view["paths"]), root * 3)
		self.assertIn("operation labels", view["privacy"]["omitted"])
		# the numbers are the same; only the names changed
		self.assertEqual(sorted(r["n"] for r in view["paths"]),
		                 sorted(r["n"] for r in plain["paths"]))
		self.assertEqual(sorted(r["energy_j"] or 0 for r in view["operations"]),
		                 sorted(r["energy_j"] or 0 for r in plain["operations"]))
		self.assertNotIn(prompt, json.dumps(view))
		view["instances"][0]["name"] = prompt
		with self.assertRaises(view_model.ViewModelError):
			view_model.validate(view)

	def test_a_short_host_name_does_not_erase_words(self):
		self.nested(mode="direct", gpu=CallableReader(Counter_().read))
		self.attribute(power=False)
		normal = view_model.build(self.d)
		with mock.patch("socket.gethostname", return_value="gpu"), \
		     mock.patch("platform.node", return_value="gpu"):
			view = view_model.build(self.d)
		self.assertEqual(view["warnings"], normal["warnings"])
		self.assertEqual(view["provenance"]["totals"], normal["provenance"]["totals"])
		self.assertNotIn(view_model.REDACTED, json.dumps(view))


class TestValidate(unittest.TestCase):

	def view(self):
		with tempfile.TemporaryDirectory() as d:
			return view_model.build(unpack_v1("cor_webqsp", d))

	def test_a_missing_value_cannot_carry_a_number(self):
		view = self.view()
		view["instances"][0]["cells"][0] = {"domain": "gpu", "scope": "inclusive",
		                                    "kind": "unavailable", "value_j": 0.0}
		with self.assertRaises(view_model.ViewModelError):
			view_model.validate(view)

	def test_a_measured_cell_needs_a_value(self):
		view = self.view()
		view["instances"][0]["cells"][0] = {"domain": "gpu", "scope": "inclusive",
		                                    "kind": "measured"}
		with self.assertRaises(view_model.ViewModelError):
			view_model.validate(view)

	def test_a_shared_row_cannot_be_ranked(self):
		view = self.view()
		view["paths"][0]["notes"] = ["shared"]
		with self.assertRaises(view_model.ViewModelError):
			view_model.validate(view)

	def test_a_share_needs_a_rank(self):
		view = self.view()
		row = next(r for r in view["operations"] if r["ranked"])
		row["ranked"] = False
		with self.assertRaises(view_model.ViewModelError):
			view_model.validate(view)

	def test_attributes_need_permission(self):
		view = self.view()
		view["instances"][0]["attributes"] = {"prompt": "hello"}
		with self.assertRaises(view_model.ViewModelError):
			view_model.validate(view)

	def test_a_path_in_provenance_is_refused(self):
		view = self.view()
		view["provenance"]["mode_reason"] = "see /home/someone/hw.json"
		import re
		with self.assertRaises(view_model.ViewModelError):
			view_model.validate(view, forbidden=[re.compile(re.escape("/home/someone"))])


if __name__ == "__main__":
	unittest.main()
