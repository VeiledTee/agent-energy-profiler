"""M2: schema-v2 attribution, NVML timeline validity, and parity with v1."""

import contextlib
import gzip
import io
import json
import os
import tempfile
import unittest

from agent_energy_profiler import attribution as v1
from agent_energy_profiler import attribution_v2 as v2
from agent_energy_profiler import measurement as m
from agent_energy_profiler import schema_v2
from agent_energy_profiler.measurement import CallableReader, Resolution
from agent_energy_profiler.session import Session

HERE = os.path.dirname(os.path.abspath(__file__))
SYNTH = os.path.join(HERE, "golden", "synthetic_rapl_multigpu")


def gunzip_to(src, dst):
	with gzip.open(src, "rb") as f, open(dst, "wb") as g:
		g.write(f.read())


def run_cli(*argv):
	out, err = io.StringIO(), io.StringIO()
	with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
		code = v2.main(list(argv))
	return code, out.getvalue(), err.getvalue()


class TestParityWithV1Golden(unittest.TestCase):
	"""The synthetic golden case through v2. Every value v1 produced from a
	counter is reproduced exactly; the only difference is the documented one:
	v1's power-integration GPU estimate moves to estimates.gpu, so energy.gpu
	and total become null for those spans."""

	@classmethod
	def setUpClass(cls):
		cls.tmp = tempfile.TemporaryDirectory()
		d = cls.tmp.name
		gunzip_to(os.path.join(SYNTH, "input", "events.jsonl.gz"), os.path.join(d, "events.jsonl"))
		gunzip_to(os.path.join(SYNTH, "input", "power.csv.gz"), os.path.join(d, "power.csv"))
		code, _, err = run_cli("--spans", os.path.join(d, "events.jsonl"),
		                       "--power", os.path.join(d, "power.csv"),
		                       "--out-dir", os.path.join(d, "out"))
		assert code == 0, err
		with open(os.path.join(d, "out", "spans_attributed.jsonl")) as f:
			cls.v2_rows = [json.loads(line) for line in f]
		with gzip.open(os.path.join(SYNTH, "expected", "events_attributed.jsonl.gz"), "rt") as f:
			cls.v1_rows = [json.loads(line) for line in f]
		with open(os.path.join(d, "out", "session_summary.json")) as f:
			cls.summary = json.load(f)

	@classmethod
	def tearDownClass(cls):
		cls.tmp.cleanup()

	def test_same_spans_in_the_same_order(self):
		self.assertEqual([r["span_id"] for r in self.v2_rows],
		                 [r["event_id"] for r in self.v1_rows])

	def test_rapl_values_are_identical(self):
		for a, b in zip(self.v2_rows, self.v1_rows):
			for domain, field in (("cpu_package", "cpu_package_energy_j"),
			                      ("dram", "dram_energy_j"),
			                      ("cpu_core", "cpu_core_energy_j")):
				self.assertEqual(a["energy"][domain]["energy_j"], b[field],
				                 (a["name"], domain))

	def test_counter_gpu_identical_and_estimates_moved(self):
		moved = 0
		for a, b in zip(self.v2_rows, self.v1_rows):
			if b["gpu_energy_source"] == "nvml_counter":
				self.assertEqual(a["energy"]["gpu"]["energy_j"], b["gpu_energy_j"])
				self.assertEqual(a["total"]["energy_j"], b["measured_energy_j"])
			elif b["gpu_energy_source"] == "power_integration":
				moved += 1
				self.assertIsNone(a["energy"]["gpu"]["energy_j"])
				self.assertEqual(a["estimates"]["gpu"]["energy_j"], b["gpu_energy_j"])
				self.assertIsNotNone(b["measured_energy_j"])  # v1 mixed it in
				self.assertIsNone(a["total"]["energy_j"])     # v2 does not
				self.assertEqual(a["total"]["domains_missing"], ["gpu"])
		self.assertEqual(moved, 3)

	def test_every_row_validates(self):
		for row in self.v2_rows:
			self.assertEqual(schema_v2.validate(row), [])

	def test_v1_input_has_no_hierarchy_so_overlap_is_shared(self):
		rows = {r["span_id"]: r for r in self.v2_rows}
		nested = rows["synthetic-005"]["accounting"]   # inside synthetic-004
		self.assertEqual(nested["attribution"], "shared")
		self.assertEqual(self.summary["input_schema"], 1)


class TestSessionEndToEnd(unittest.TestCase):

	def setUp(self):
		self._tmp = tempfile.TemporaryDirectory()
		self.d = self._tmp.name

	def tearDown(self):
		self._tmp.cleanup()

	def record(self):
		counter = {"v": 1_000_000}

		def read():
			counter["v"] += 250
			return [counter["v"]]
		path = os.path.join(self.d, "spans.jsonl")
		with Session(path, gpu=CallableReader(read),
		             resolution=Resolution.configured(0.1, "test")) as s:
			with s.span("agent:step"):
				with s.span("llm:plan"):
					pass
				with s.span("tool:search"):
					pass
		return path, schema_v2.read_records(path)

	def write_power(self, t0, t1, gpu_drop_at=None):
		path = os.path.join(self.d, "power.csv")
		rows = ["t,gpu0_w,gpu0_energy_mj,rapl_package-0_intel-rapl:0_uj,"
		        "rapl_dram_intel-rapl:0:2_uj"]
		n = 50
		for i in range(n + 1):
			t = t0 - 1.0 + (t1 - t0 + 2.0) * i / n
			g = 5_000 * i
			if gpu_drop_at is not None and i >= gpu_drop_at:
				g = 10 * (i - gpu_drop_at)  # counter reset
			rows.append(f"{t:.6f},100.0,{g},{1_000_000 * i},{100_000 * i}")
		with open(path, "w") as f:
			f.write("\n".join(rows) + "\n")
		return path

	def test_attribution_adds_rapl_estimate_total_and_accounting(self):
		spans_path, spans = self.record()
		t0 = min(s["start_unix_s"] for s in spans)
		t1 = max(s["end_unix_s"] for s in spans)
		power = self.write_power(t0, t1)
		out = os.path.join(self.d, "out")
		code, _, err = run_cli("--spans", spans_path, "--power", power, "--out-dir", out)
		self.assertEqual(code, 0, err)
		with open(os.path.join(out, "spans_attributed.jsonl")) as f:
			rows = {r["name"]: r for r in map(json.loads, f)}
		with open(os.path.join(out, "session_summary.json")) as f:
			summ = json.load(f)
		root = rows["agent:step"]
		self.assertEqual(root["energy"]["cpu_package"]["status"], "measured")
		self.assertEqual(root["energy"]["cpu_package"]["method"], "sampled_counter_interpolated")
		self.assertIsNone(root["energy"]["cpu_package"]["below_resolution"])
		self.assertEqual(root["estimates"]["gpu"]["status"], "estimated")
		self.assertEqual(root["total"]["status"], "measured")
		self.assertEqual(root["accounting"]["n_children"], 2)
		self.assertEqual(root["accounting"]["exclusive"]["gpu"]["status"], "measured")
		# the v2 RAPL value equals v1's for the same window
		ts, _, rapls, _ = v1.load_power(power)
		pkg, _, _ = v1.classify_rapl(rapls)
		self.assertEqual(root["energy"]["cpu_package"]["energy_j"],
		                 v1.sum_domain(ts, pkg, root["start_unix_s"], root["end_unix_s"]))
		self.assertEqual(summ["input_schema"], 2)
		self.assertEqual(summ["resolution"]["basis"], "configured")
		self.assertEqual(summ["sum_root_inclusive"]["gpu"]["energy_j"],
		                 root["energy"]["gpu"]["energy_j"])
		self.assertEqual(summ["window"]["domains"]["gpu"]["status"], "measured")
		self.assertEqual(summ["session"]["record_type"], "session_start")

	def test_gpu_counter_reset_in_the_timeline_invalidates_the_window(self):
		spans_path, spans = self.record()
		t0 = min(s["start_unix_s"] for s in spans)
		t1 = max(s["end_unix_s"] for s in spans)
		power = self.write_power(t0, t1, gpu_drop_at=25)
		out = os.path.join(self.d, "out")
		code, _, err = run_cli("--spans", spans_path, "--power", power, "--out-dir", out)
		self.assertEqual(code, 0, err)
		with open(os.path.join(out, "session_summary.json")) as f:
			gpu = json.load(f)["window"]["domains"]["gpu"]
		self.assertEqual((gpu["status"], gpu["reason"], gpu["energy_j"]),
		                 ("invalid", "counter_discontinuity", None))
		# The v1 path is unchanged by design: it differences blindly and here
		# returns a negative "energy". Pinned so the legacy policy cannot drift.
		ts, _, _, gpu_energy = v1.load_power(power)
		legacy = v1.sum_counter_domain(ts, gpu_energy, t0, t1, 1e3)
		self.assertLess(legacy, 0)

	def test_uncovered_spans_refuse_unless_allowed(self):
		spans_path, spans = self.record()
		t1 = max(s["end_unix_s"] for s in spans)
		power = self.write_power(t1 + 10, t1 + 20)
		out = os.path.join(self.d, "out")
		code, _, err = run_cli("--spans", spans_path, "--power", power, "--out-dir", out)
		self.assertEqual(code, 1)
		self.assertIn("outside", err)
		self.assertFalse(os.path.exists(os.path.join(out, "spans_attributed.jsonl")))
		code, _, err = run_cli("--spans", spans_path, "--power", power, "--out-dir", out,
		                       "--allow-uncovered")
		self.assertEqual(code, 0, err)
		with open(os.path.join(out, "spans_attributed.jsonl")) as f:
			row = json.loads(f.readline())
		self.assertEqual(row["energy"]["cpu_package"]["reason"], "not_covered")
		self.assertEqual(row["energy"]["gpu"]["status"], "measured")  # in-band, unaffected

	def test_without_a_timeline_only_in_band_gpu_is_measured(self):
		spans_path, _ = self.record()
		out = os.path.join(self.d, "out")
		code, _, err = run_cli("--spans", spans_path, "--out-dir", out)
		self.assertEqual(code, 0, err)
		with open(os.path.join(out, "spans_attributed.jsonl")) as f:
			row = json.loads(f.readline())
		self.assertEqual(row["energy"]["cpu_package"]["status"], "unavailable")
		self.assertEqual(row["total"]["status"], "incomplete")
		self.assertEqual(row["total"]["domains_measured"], ["gpu"])


class TestCounterWindowReasons(unittest.TestCase):

	def test_reasons(self):
		ts = [0.0, 1.0, 2.0, 3.0]
		s = v1.prepare_rapl(ts, [0, 10, 5, 20], None)
		self.assertEqual(v1.counter_window(s, 0.0, 1.0), (10, None))
		self.assertEqual(v1.counter_window(s, 0.0, 3.0), (None, "counter_discontinuity"))
		self.assertEqual(v1.counter_window(s, 2.0, 1.0), (None, "clock_anomaly"))
		self.assertEqual(v1.counter_window(v1.prepare_rapl([0.0], [1], None), 0, 1),
		                 (None, "insufficient_samples"))


if __name__ == "__main__":
	unittest.main()
