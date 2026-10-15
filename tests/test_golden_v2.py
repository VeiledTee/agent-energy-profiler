"""Schema-v2 golden: a recorded session attributed exactly as Milestone 2 did.

tests/golden_v2/<case>/expected was produced by the M2 reference code
(commit baf6108) from the inputs beside it (tools/build_v2_fixture.py). The
current attribution -- streaming since M3 -- must reproduce it byte for byte.
"""

import gzip
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest

from agent_energy_profiler import accounting, attribution_v2, schema_v2

HERE = os.path.dirname(os.path.abspath(__file__))
CASE = os.path.join(HERE, "golden_v2", "session_mixed")


def gunzip(path):
	with gzip.open(path, "rb") as f:
		return f.read()


def normalized_summary(data):
	summary = json.loads(data)
	summary["input"] = None
	summary["timeline"]["power_file"] = None
	return (json.dumps(summary, indent=2, sort_keys=True) + "\n").encode()


class TestGoldenV2(unittest.TestCase):

	@classmethod
	def setUpClass(cls):
		cls.tmp = tempfile.TemporaryDirectory()
		d = cls.tmp.name
		cls.spans = os.path.join(d, "spans.jsonl")
		cls.power = os.path.join(d, "power.csv")
		for name, path in (("spans.jsonl", cls.spans), ("power.csv", cls.power)):
			with open(path, "wb") as f:
				f.write(gunzip(os.path.join(CASE, "input", name + ".gz")))
		cls.out = os.path.join(d, "out")
		res = subprocess.run([sys.executable, "-m", "agent_energy_profiler.attribution_v2",
		                      "--spans", cls.spans, "--power", cls.power,
		                      "--out-dir", cls.out], capture_output=True, text=True)
		assert res.returncode == 0, res.stderr

	@classmethod
	def tearDownClass(cls):
		cls.tmp.cleanup()

	def test_manifest_matches_fixture(self):
		with open(os.path.join(CASE, "manifest.json")) as f:
			manifest = json.load(f)
		for name, digest in manifest["sha256"].items():
			data = gunzip(os.path.join(CASE, name + ".gz"))
			self.assertEqual(hashlib.sha256(data).hexdigest(), digest, name)

	def test_spans_attributed_byte_identical(self):
		with open(os.path.join(self.out, "spans_attributed.jsonl"), "rb") as f:
			actual = f.read()
		expected = gunzip(os.path.join(CASE, "expected", "spans_attributed.jsonl.gz"))
		self.assertTrue(actual == expected, "spans_attributed.jsonl differs from M2")

	def test_session_summary_identical(self):
		with open(os.path.join(self.out, "session_summary.json"), "rb") as f:
			actual = normalized_summary(f.read())
		expected = gunzip(os.path.join(CASE, "expected", "session_summary.json.gz"))
		self.assertTrue(actual == expected, "session_summary.json differs from M2")

	def test_streaming_equals_in_memory_path(self):
		timeline = attribution_v2.Timeline(self.power)
		rows = attribution_v2.attribute_spans(schema_v2.read_records(self.spans), timeline)
		with open(os.path.join(self.out, "spans_attributed.jsonl")) as f:
			self.assertEqual([json.dumps(r) for r in rows], [l.rstrip("\n") for l in f])
		in_memory = accounting.summarize(rows, attribution_v2.session_window(rows, timeline, None))
		with tempfile.TemporaryDirectory() as d:
			streamed = attribution_v2.attribute_file(self.spans, os.path.join(d, "x.jsonl"),
			                                         timeline)
		self.assertEqual(json.dumps(in_memory), json.dumps(streamed))

	def test_fixture_exercises_the_hard_cases(self):
		with open(os.path.join(self.out, "spans_attributed.jsonl")) as f:
			rows = [json.loads(l) for l in f]
		statuses = {r["status"] for r in rows}
		self.assertTrue({"ok", "error"} <= statuses)
		self.assertIn("read_failed", {r["energy"]["gpu"]["reason"] for r in rows})
		self.assertIn("shared", {r["accounting"]["attribution"] for r in rows})
		self.assertTrue(any(r["accounting"]["depth"] == 2 for r in rows))
		self.assertTrue(any(r["thread_id"] != rows[0]["thread_id"] for r in rows))


if __name__ == "__main__":
	unittest.main()
