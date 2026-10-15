"""M2: schema-v2 validation, and reading v1 artifacts as v2."""

import gzip
import json
import os
import tempfile
import unittest

from agent_energy_profiler import measurement as m
from agent_energy_profiler import schema_v2

HERE = os.path.dirname(os.path.abspath(__file__))
GOLDEN = os.path.join(HERE, "golden")


def golden_lines(case, kind, name):
	with gzip.open(os.path.join(GOLDEN, case, kind, name + ".gz"), "rt") as f:
		return [json.loads(line) for line in f if line.strip()]


class TestValidate(unittest.TestCase):

	def test_measured_domain_needs_a_number_and_no_reason(self):
		d = m.domain(1.0, m.MEASURED, None, "x", m.IN_BAND)
		self.assertEqual(schema_v2.validate_domain("gpu", d), [])
		d["energy_j"] = None
		self.assertTrue(schema_v2.validate_domain("gpu", d))

	def test_missing_value_cannot_carry_energy_or_resolution(self):
		d = m.domain(None, m.UNAVAILABLE, "no_counter", "x", m.IN_BAND)
		self.assertEqual(schema_v2.validate_domain("gpu", d), [])
		d["energy_j"] = 0.0
		self.assertTrue(schema_v2.validate_domain("gpu", d))
		d = m.domain(None, m.UNAVAILABLE, "made_up_reason", "x", m.IN_BAND)
		self.assertTrue(schema_v2.validate_domain("gpu", d))

	def test_domain_helper_refuses_to_put_energy_on_a_missing_value(self):
		d = m.domain(5.0, m.INVALID, "counter_decrease", "x", m.IN_BAND, below=True)
		self.assertIsNone(d["energy_j"])
		self.assertIsNone(d["below_resolution"])

	def test_process_scope_is_rejected(self):
		d = m.domain(1.0, m.MEASURED, None, "x", m.IN_BAND)
		d["scope"] = "process"
		self.assertTrue(schema_v2.validate_domain("gpu", d))


class TestReadV1(unittest.TestCase):

	def test_every_golden_v1_event_upgrades_to_a_valid_v2_span(self):
		for case in ("cor_webqsp", "tog_cwq", "pog_cwq", "synthetic_rapl_multigpu"):
			for kind, name in (("input", "events.jsonl"),
			                   ("expected", "events_attributed.jsonl")):
				for event in golden_lines(case, kind, name):
					span = schema_v2.upgrade_v1(event)
					self.assertEqual(schema_v2.validate(span), [], (case, name))
					self.assertEqual(span["upgraded_from_schema"], 1)

	def test_upgrade_preserves_context_tokens_and_status(self):
		event = golden_lines("synthetic_rapl_multigpu", "input", "events.jsonl")[3]
		span = schema_v2.upgrade_v1(event)
		self.assertEqual(span["span_id"], event["event_id"])
		self.assertEqual(span["name"], event["operation_label"])
		self.assertEqual(span["status"], "error")
		for key in ("question_id", "iteration", "traversal_depth", "step_index",
		            "run_id", "dataset", "paradigm", "model_name"):
			self.assertEqual(span["attributes"][key], event[key], key)
		self.assertEqual(span["attributes"]["meta"], event["meta"])
		self.assertIsNone(span["parent_span_id"])

	def test_in_band_counter_becomes_measured(self):
		event = golden_lines("synthetic_rapl_multigpu", "expected",
		                     "events_attributed.jsonl")[1]
		self.assertEqual(event["gpu_energy_source"], "nvml_counter")
		span = schema_v2.upgrade_v1(event)
		self.assertEqual(span["energy"]["gpu"]["status"], "measured")
		self.assertEqual(span["energy"]["gpu"]["energy_j"], event["gpu_energy_j"])
		self.assertEqual(span["estimates"], {})
		self.assertEqual(span["energy"]["cpu_package"]["energy_j"],
		                 event["cpu_package_energy_j"])
		self.assertTrue(span["energy"]["cpu_core"]["diagnostic"])

	def test_power_integration_moves_out_of_the_measured_field(self):
		"""Known limitation 8, corrected on read: the v1 estimate becomes
		estimates.gpu and energy.gpu is unavailable."""
		rows = golden_lines("synthetic_rapl_multigpu", "expected", "events_attributed.jsonl")
		integrated = [e for e in rows if e["gpu_energy_source"] == "power_integration"]
		self.assertTrue(integrated)
		for event in integrated:
			span = schema_v2.upgrade_v1(event)
			self.assertIsNone(span["energy"]["gpu"]["energy_j"])
			self.assertEqual(span["energy"]["gpu"]["status"], "unavailable")
			self.assertEqual(span["estimates"]["gpu"]["energy_j"], event["gpu_energy_j"])
			self.assertEqual(span["estimates"]["gpu"]["status"], "estimated")

	def test_resolution_is_unknown_unless_supplied(self):
		event = golden_lines("synthetic_rapl_multigpu", "input", "events.jsonl")[0]
		self.assertIsNone(schema_v2.upgrade_v1(event)["energy"]["gpu"]["below_resolution"])
		res = m.Resolution.configured(0.1006, "test")
		gpu = schema_v2.upgrade_v1(event, res)["energy"]["gpu"]
		self.assertTrue(gpu["below_resolution"])  # 0.05 s span
		self.assertEqual(gpu["energy_j"], 0.0)     # the recorded zero is kept

	def test_reversed_v1_event_is_a_clock_anomaly(self):
		span = schema_v2.upgrade_v1({"operation_label": "llm:x", "start_timestamp": 2.0,
		                             "end_timestamp": 1.0, "gpu_energy_j": 1.0})
		self.assertTrue(span["clock_anomaly"])
		self.assertIsNone(span["duration_s"])
		self.assertEqual(span["energy"]["gpu"]["energy_j"], 1.0)

	def test_the_v1_input_is_not_modified(self):
		event = golden_lines("synthetic_rapl_multigpu", "expected", "events_attributed.jsonl")[2]
		before = json.dumps(event, sort_keys=True)
		schema_v2.upgrade_v1(event)
		self.assertEqual(json.dumps(event, sort_keys=True), before)


class TestReadRecords(unittest.TestCase):

	def write(self, d, lines):
		path = os.path.join(d, "x.jsonl")
		with open(path, "w") as f:
			f.write("\n".join(lines) + "\n")
		return path

	def test_mixed_v1_and_v2_with_a_torn_line(self):
		v2 = {"schema_version": 2, "record_type": "session_start", "session_id": "s"}
		v1 = {"schema_version": 1, "operation_label": "llm:x", "start_timestamp": 0.0,
		      "end_timestamp": 1.0, "gpu_energy_j": None, "event_id": "e"}
		with tempfile.TemporaryDirectory() as d:
			path = self.write(d, [json.dumps(v2), json.dumps(v1), '{"torn": '])
			spans = schema_v2.read_records(path)
			self.assertEqual(len(spans), 1)
			self.assertEqual(spans[0]["span_id"], "e")
			self.assertEqual(len(schema_v2.read_records(path, spans_only=False)), 2)
			self.assertEqual(schema_v2.session_header(path)["session_id"], "s")

	def test_unknown_future_version_is_rejected(self):
		with tempfile.TemporaryDirectory() as d:
			path = self.write(d, [json.dumps({"schema_version": 3})])
			with self.assertRaises(ValueError):
				schema_v2.read_records(path)


if __name__ == "__main__":
	unittest.main()
