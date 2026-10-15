"""Schema-v1 event recording, context propagation and energy accounting.

Ported from Chain-of-Relations-Emissions tests/test_energy_measurement.py
(source commit recorded in docs/PROVENANCE.md). The source exercised the
recorder through the research host's adapter and its frozen label enum. Here
the same assertions run against agent_energy_profiler.events directly, with the
labels spelled as the plain strings the adapter's enum serialized to.
Host-only tests (the research taxonomy, its LLM client wrapper, its agent,
its SPARQL classification) stay in the research repository.

Pure unit tests: no LLM endpoint, no SPARQL endpoint, no GPU.
"""

import json
import os
import tempfile
import unittest

from agent_energy_profiler import attribution
from agent_energy_profiler import events as aep_events
from agent_energy_profiler import labels, schema


class OperationLabel:
	"""The label strings the source tests used (values of the host enum)."""

	LLM_RELATION_RANK = "llm:relation_rank"
	LLM_REASON = "llm:reason"
	LLM_ANSWER_FILTER = "llm:answer_filter"
	LLM_DIRECT_ANSWER = "llm:direct_answer"
	KG_ID2NAME = "kg:id2name"
	KG_RELATION_SEARCH = "kg:relation_search"
	KG_ENTITY_SEARCH = "kg:entity_search"
	KG_SPARQL = "kg:sparql"


#: A host vocabulary registered for the tests, as an adapter would register one.
VOCABULARY = [v for k, v in vars(OperationLabel).items() if not k.startswith("_")]
TYPES = ["llm", "kg", "embedding", "system"]

REQUIRED_KEYS = schema.EVENT_FIELDS


class EventsHarness(unittest.TestCase):
	"""Gives each test a fresh recorder writing to a temp file.

	Mirrors the source harness, which set ENERGY_EVENTS_FILE and reloaded the
	host adapter; the adapter's import-time work was registering its
	vocabulary and calling events.reset_from_env().
	"""

	def setUp(self):
		self._tmp = tempfile.TemporaryDirectory()
		self.events_path = os.path.join(self._tmp.name, "events.jsonl")
		os.environ["ENERGY_EVENTS_FILE"] = self.events_path
		self._saved_labels = labels.REGISTRY.known_labels()
		labels.register(labels=VOCABULARY, types=TYPES)
		aep_events.reset_from_env()
		self.ee = aep_events

	def tearDown(self):
		self.ee.close()
		os.environ.pop("ENERGY_EVENTS_FILE", None)
		aep_events.reset_from_env()
		labels.register(self._saved_labels)
		self._tmp.cleanup()

	def read_events(self):
		if not os.path.exists(self.events_path):
			return []
		with open(self.events_path) as f:
			return [json.loads(line) for line in f if line.strip()]


# --------------------------------------------------------------------------
# 1. schema
# --------------------------------------------------------------------------

class TestSchemaV1(EventsHarness):

	def test_event_has_all_required_keys(self):
		self.ee.record(OperationLabel.KG_ENTITY_SEARCH, 100.0, 100.5)
		event, = self.read_events()
		for key in REQUIRED_KEYS:
			self.assertIn(key, event, f"schema-v1 event missing {key!r}")

	def test_schema_version_is_1(self):
		self.ee.record(OperationLabel.KG_SPARQL, 1.0, 2.0)
		event, = self.read_events()
		self.assertEqual(event["schema_version"], 1)
		self.assertEqual(schema.SCHEMA_VERSION, 1)

	def test_event_ids_are_unique(self):
		for _ in range(50):
			self.ee.record(OperationLabel.KG_SPARQL, 1.0, 2.0)
		ids = [e["event_id"] for e in self.read_events()]
		self.assertEqual(len(ids), len(set(ids)))

	def test_timing_is_valid_and_ordered(self):
		self.ee.record(OperationLabel.KG_ID2NAME, 10.0, 12.5)
		event, = self.read_events()
		self.assertLessEqual(event["start_timestamp"], event["end_timestamp"])
		self.assertAlmostEqual(
			event["duration_s"],
			event["end_timestamp"] - event["start_timestamp"])

	def test_unavailable_measurements_are_null_not_zero(self):
		# No NVML marks and no in-band RAPL: these must be null, never 0.0.
		self.ee.record(OperationLabel.KG_SPARQL, 1.0, 2.0)
		event, = self.read_events()
		for field in ("gpu_energy_j", "cpu_package_energy_j", "dram_energy_j"):
			self.assertIsNone(event[field], f"{field} fabricated a value")

	def test_gpu_counter_delta_is_converted_from_millijoules(self):
		self.ee.record(OperationLabel.LLM_REASON, (1.0, 1000), (2.0, 3500))
		event, = self.read_events()
		self.assertAlmostEqual(event["gpu_energy_j"], 2.5)

	def test_legacy_keys_are_preserved_for_unmigrated_readers(self):
		self.ee.record(OperationLabel.LLM_DIRECT_ANSWER, 1.0, 2.0)
		event, = self.read_events()
		self.assertEqual(event["category"], "inference")
		self.assertEqual(event["label"], "llm:direct_answer")
		self.assertEqual(event["t_start"], event["start_timestamp"])
		self.assertEqual(event["t_end"], event["end_timestamp"])


# --------------------------------------------------------------------------
# 2 + 3. taxonomy validity and type/label consistency
# --------------------------------------------------------------------------


class TestEventTypeLabelAgreement(EventsHarness):

	def test_operation_type_always_matches_label_prefix(self):
		for label in VOCABULARY:
			self.ee.record(label, 1.0, 2.0)
		for event in self.read_events():
			self.assertEqual(event["operation_type"],
			                 event["operation_label"].split(":")[0])
			self.assertTrue(labels.is_known(event["operation_label"]))

	def test_unknown_label_is_flagged_not_dropped(self):
		self.ee.record("llm:bogus_stage", 1.0, 2.0)
		event, = self.read_events()
		self.assertTrue(event["meta"].get("unknown_label"))

	def test_unknown_status_is_flagged_not_silently_accepted(self):
		self.ee.record(OperationLabel.KG_SPARQL, 1.0, 2.0, status="exploded")
		event, = self.read_events()
		self.assertEqual(event["status"], "error")
		self.assertEqual(event["meta"].get("unknown_status"), "exploded")


# --------------------------------------------------------------------------
# 4. CoR LLM calls no longer collapse to a generic label
# --------------------------------------------------------------------------



class TestScopedEventMeta(EventsHarness):

	def test_scoped_meta_is_merged_into_events(self):
		with self.ee.event_meta(entity_count=64, batched=True):
			self.ee.record(OperationLabel.KG_ID2NAME, 1.0, 2.0, attempts=1)
		event, = self.read_events()
		self.assertEqual(event["meta"]["entity_count"], 64)
		self.assertTrue(event["meta"]["batched"])
		self.assertEqual(event["meta"]["attempts"], 1)

	def test_explicit_kwargs_outrank_scoped_meta(self):
		with self.ee.event_meta(entity_count=64):
			self.ee.record(OperationLabel.KG_ID2NAME, 1.0, 2.0, entity_count=1)
		event, = self.read_events()
		self.assertEqual(event["meta"]["entity_count"], 1)

	def test_scope_is_restored_and_does_not_leak(self):
		with self.ee.event_meta(batched=True):
			self.ee.record(OperationLabel.KG_ID2NAME, 1.0, 2.0)
		self.ee.record(OperationLabel.KG_ID2NAME, 2.0, 3.0)
		first, second = self.read_events()
		self.assertTrue(first["meta"]["batched"])
		self.assertNotIn("batched", second["meta"])

	def test_nested_scopes_merge_with_inner_winning(self):
		with self.ee.event_meta(entity_count=64, batched=True):
			with self.ee.event_meta(entity_count=8):
				self.ee.record(OperationLabel.KG_ID2NAME, 1.0, 2.0)
		event, = self.read_events()
		self.assertEqual(event["meta"]["entity_count"], 8)
		self.assertTrue(event["meta"]["batched"])




class TestContextPropagation(EventsHarness):

	def test_question_id_is_attached(self):
		self.ee.set_question("WebQTest-42")
		self.ee.record(OperationLabel.KG_SPARQL, 1.0, 2.0)
		event, = self.read_events()
		self.assertEqual(event["question_id"], "WebQTest-42")

	def test_step_index_is_ordered_and_resets_per_question(self):
		self.ee.set_question("q1")
		for _ in range(3):
			self.ee.record(OperationLabel.KG_SPARQL, 1.0, 2.0)
		self.ee.set_question("q2")
		for _ in range(2):
			self.ee.record(OperationLabel.KG_SPARQL, 1.0, 2.0)

		events = self.read_events()
		q1 = [e["step_index"] for e in events if e["question_id"] == "q1"]
		q2 = [e["step_index"] for e in events if e["question_id"] == "q2"]
		self.assertEqual(q1, [0, 1, 2])
		self.assertEqual(q2, [0, 1])

	def test_iteration_and_step_index_are_distinct(self):
		self.ee.set_question("q1")
		with self.ee.iteration(2):
			self.ee.record(OperationLabel.KG_SPARQL, 1.0, 2.0)
			self.ee.record(OperationLabel.KG_SPARQL, 2.0, 3.0)
		events = self.read_events()
		self.assertEqual([e["iteration"] for e in events], [2, 2])
		self.assertEqual([e["step_index"] for e in events], [0, 1])

	def test_iteration_scope_is_restored(self):
		self.ee.set_question("q1")
		with self.ee.iteration(1):
			with self.ee.iteration(2):
				self.ee.record(OperationLabel.KG_SPARQL, 1.0, 2.0)
			self.ee.record(OperationLabel.KG_SPARQL, 2.0, 3.0)
		self.assertEqual([e["iteration"] for e in self.read_events()], [2, 1])

	def test_begin_iteration_counts_up_from_zero_and_resets_per_question(self):
		self.ee.set_question("q1")
		self.assertEqual([self.ee.begin_iteration() for _ in range(3)], [0, 1, 2])
		self.ee.set_question("q2")
		self.assertEqual(self.ee.begin_iteration(), 0)

	def test_traversal_depth_scope_is_restored(self):
		self.ee.set_question("q1")
		with self.ee.traversal_depth(1):
			with self.ee.traversal_depth(2):
				self.ee.record(OperationLabel.KG_SPARQL, 1.0, 2.0)
			self.ee.record(OperationLabel.KG_SPARQL, 2.0, 3.0)
		self.assertEqual([e["traversal_depth"] for e in self.read_events()], [2, 1])

	def test_traversal_depth_is_null_for_work_outside_the_dfs(self):
		self.ee.set_question("q1")
		self.ee.begin_iteration()
		self.ee.set_traversal_depth(None)
		self.ee.record(OperationLabel.LLM_DIRECT_ANSWER, 1.0, 2.0)
		event, = self.read_events()
		self.assertIsNone(event["traversal_depth"])
		self.assertEqual(event["iteration"], 0,
		                 "iteration must keep advancing outside the DFS")

	def test_backtracking_depth_falls_while_iteration_keeps_rising(self):
		"""The critical distinction, on the required sequence.

		A DFS that descends to depth 2, backtracks to 1, then descends again
		produces depths [0, 1, 2, 1, 2] while iterations run [0, 1, 2, 3, 4].
		Each control-loop pass emits two events, so step_index runs 0..9
		independently of both.
		"""
		self.ee.set_question("q1")
		for depth in (0, 1, 2, 1, 2):
			self.ee.begin_iteration()
			self.ee.set_traversal_depth(depth)
			self.ee.record(OperationLabel.KG_RELATION_SEARCH, 1.0, 2.0)
			self.ee.record(OperationLabel.LLM_RELATION_RANK, 2.0, 3.0)

		events = self.read_events()
		# one iteration/depth pair per control-loop pass, two events each
		self.assertEqual([e["traversal_depth"] for e in events],
		                 [0, 0, 1, 1, 2, 2, 1, 1, 2, 2])
		self.assertEqual([e["iteration"] for e in events],
		                 [0, 0, 1, 1, 2, 2, 3, 3, 4, 4])
		self.assertEqual([e["step_index"] for e in events], list(range(10)))

		# the defining properties
		iterations = [e["iteration"] for e in events]
		depths = [e["traversal_depth"] for e in events]
		self.assertEqual(iterations, sorted(iterations),
		                 "iteration must never decrease within a question")
		self.assertTrue(any(b < a for a, b in zip(depths, depths[1:])),
		                "traversal_depth must be able to decrease")
		self.assertNotEqual(iterations, depths,
		                    "iteration and traversal_depth must not be the same field")

	def test_iteration_and_depth_survive_into_the_attributed_artifact(self):
		self.ee.set_question("q1")
		self.ee.begin_iteration()
		self.ee.set_traversal_depth(2)
		self.ee.record(OperationLabel.LLM_REASON, 1.0, 2.0)
		raw, = self.read_events()

		rows, _ = attribution.attribute_events([raw], [1.0, 2.0], {}, {})
		row, = rows
		self.assertEqual(row["iteration"], 0)
		self.assertEqual(row["traversal_depth"], 2)
		self.assertEqual(row["step_index"], raw["step_index"])

	def test_run_provenance_is_configured_once_and_attached_to_every_event(self):
		self.ee.configure(
			run_id="run-abc", dataset="webqsp", paradigm="cor",
			model_name="qwen2.5-7b", model_revision="rev1",
			git_commit="deadbeef", hardware_id="host|RTX 4090")
		self.ee.set_question("q1")
		self.ee.record(OperationLabel.KG_SPARQL, 1.0, 2.0)
		self.ee.record(OperationLabel.LLM_REASON, 2.0, 3.0)
		for event in self.read_events():
			self.assertEqual(event["run_id"], "run-abc")
			self.assertEqual(event["dataset"], "webqsp")
			self.assertEqual(event["paradigm"], "cor")
			self.assertEqual(event["model_name"], "qwen2.5-7b")
			self.assertEqual(event["model_revision"], "rev1")
			self.assertEqual(event["git_commit"], "deadbeef")
			self.assertEqual(event["hardware_id"], "host|RTX 4090")

	def test_missing_optional_provenance_is_null_and_does_not_break(self):
		self.ee.configure(run_id="run-abc", dataset="webqsp", paradigm="cor")
		self.ee.record(OperationLabel.KG_SPARQL, 1.0, 2.0)
		event, = self.read_events()
		self.assertEqual(event["run_id"], "run-abc")
		self.assertIsNone(event["model_revision"])
		self.assertIsNone(event["git_commit"])
		self.assertIsNone(event["hardware_id"])


# --------------------------------------------------------------------------
# KG labelling
# --------------------------------------------------------------------------


class TestEnergyAccounting(unittest.TestCase):

	def setUp(self):
		self.attribute = attribution

	def test_core_is_classified_separately_from_package(self):
		cols = {
			"rapl_package-0_intel-rapl:0_uj": [],
			"rapl_core_intel-rapl:0:0_uj": [],
			"rapl_dram_intel-rapl:0:1_uj": [],
		}
		package, core, dram = self.attribute.classify_rapl(cols)
		self.assertEqual(list(package), ["rapl_package-0_intel-rapl:0_uj"])
		self.assertEqual(list(core), ["rapl_core_intel-rapl:0:0_uj"])
		self.assertEqual(list(dram), ["rapl_dram_intel-rapl:0:1_uj"])

	def test_psys_is_excluded_because_it_contains_package(self):
		package, core, dram = self.attribute.classify_rapl(
			{"rapl_psys_intel-rapl:1_uj": [], "rapl_package-0_intel-rapl:0_uj": []})
		self.assertEqual(list(package), ["rapl_package-0_intel-rapl:0_uj"])

	def test_measured_total_is_gpu_plus_package_plus_dram(self):
		self.assertAlmostEqual(self.attribute.measured_total(10.0, 5.0, 2.0), 17.0)

	def test_core_is_never_added_to_the_total(self):
		"""End-to-end: a run with package=20J, core=15J, dram=4J, gpu=1J."""
		ts = [0.0, 1.0, 2.0]
		rapls = {
			# uJ cumulative counters
			"rapl_package-0_intel-rapl:0_uj": [0.0, 10e6, 20e6],
			"rapl_core_intel-rapl:0:0_uj": [0.0, 7.5e6, 15e6],
			"rapl_dram_intel-rapl:0:1_uj": [0.0, 2e6, 4e6],
		}
		events = [{
			"start_timestamp": 0.0, "end_timestamp": 2.0, "duration_s": 2.0,
			"gpu_energy_j": 1.0, "operation_label": "llm:reason",
			"operation_type": "llm",
		}]
		rows, _ = self.attribute.attribute_events(events, ts, {}, rapls)
		row, = rows
		self.assertAlmostEqual(row["cpu_package_energy_j"], 20.0)
		self.assertAlmostEqual(row["cpu_core_energy_j"], 15.0)
		self.assertAlmostEqual(row["dram_energy_j"], 4.0)
		# 1 + 20 + 4 == 25. If core were summed in, it would be 40.
		self.assertAlmostEqual(row["measured_energy_j"], 25.0)
		self.assertNotAlmostEqual(row["measured_energy_j"], 40.0)

	def test_missing_rapl_yields_null_not_zero(self):
		events = [{
			"start_timestamp": 0.0, "end_timestamp": 2.0, "duration_s": 2.0,
			"gpu_energy_j": 3.0, "operation_label": "llm:reason",
			"operation_type": "llm",
		}]
		rows, _ = self.attribute.attribute_events(events, [0.0, 2.0], {}, {})
		row, = rows
		self.assertIsNone(row["cpu_package_energy_j"])
		self.assertIsNone(row["dram_energy_j"])
		self.assertIsNone(row["measured_energy_j"],
		                  "an unmeasured component must not produce a partial total")

	def test_available_domains_and_completeness_are_reported(self):
		ts = [0.0, 1.0, 2.0]
		rapls = {
			"rapl_package-0_intel-rapl:0_uj": [0.0, 10e6, 20e6],
			"rapl_dram_intel-rapl:0:1_uj": [0.0, 2e6, 4e6],
		}
		event = {"start_timestamp": 0.0, "end_timestamp": 2.0, "duration_s": 2.0,
		         "gpu_energy_j": 1.0, "operation_label": "llm:reason",
		         "operation_type": "llm"}
		row, = self.attribute.attribute_events([event], ts, {}, rapls)[0]
		self.assertEqual(row["available_energy_domains"],
		                 ["gpu", "cpu_package", "dram"])
		self.assertTrue(row["measurement_complete"])

	def test_incomplete_measurement_is_reported_as_incomplete(self):
		event = {"start_timestamp": 0.0, "end_timestamp": 2.0, "duration_s": 2.0,
		         "gpu_energy_j": 3.0, "operation_label": "llm:reason",
		         "operation_type": "llm"}
		rows, _ = self.attribute.attribute_events([event], [0.0, 2.0], {}, {})
		row, = rows
		self.assertEqual(row["available_energy_domains"], ["gpu"])
		self.assertFalse(row["measurement_complete"])
		self.assertIsNone(row["measured_energy_j"])

	def test_core_is_not_listed_as_a_boundary_domain(self):
		ts = [0.0, 1.0, 2.0]
		rapls = {"rapl_core_intel-rapl:0:0_uj": [0.0, 7.5e6, 15e6]}
		event = {"start_timestamp": 0.0, "end_timestamp": 2.0, "duration_s": 2.0,
		         "gpu_energy_j": 1.0, "operation_label": "llm:reason",
		         "operation_type": "llm"}
		rows, _ = self.attribute.attribute_events([event], ts, {}, rapls)
		row, = rows
		self.assertAlmostEqual(row["cpu_core_energy_j"], 15.0)
		self.assertNotIn("cpu_core", row["available_energy_domains"])
		self.assertFalse(row["measurement_complete"])

	def test_attribution_preserves_every_semantic_field(self):
		event = {
			"schema_version": 1, "event_id": "abc123",
			"run_id": "run-1", "question_id": "WebQTest-7",
			"dataset": "webqsp", "paradigm": "cor",
			"iteration": 7, "traversal_depth": 2, "step_index": 11,
			"operation_type": "llm", "operation_label": "llm:relation_rank",
			"start_timestamp": 0.0, "end_timestamp": 1.0, "duration_s": 1.0,
			"gpu_energy_j": 5.0,
			"input_tokens": 4172, "output_tokens": 73,
			"status": "ok",
			"model_name": "qwen2.5-7b", "model_revision": "rev1",
			"git_commit": "deadbeef", "hardware_id": "host|RTX 4090",
			"meta": {"attempts": 2},
		}
		rows, _ = self.attribute.attribute_events([event], [0.0, 1.0], {}, {})
		row, = rows
		for key in ("run_id", "question_id", "dataset", "paradigm", "iteration",
		            "traversal_depth", "step_index", "operation_type", "operation_label",
		            "input_tokens", "output_tokens", "status", "model_name",
		            "model_revision", "git_commit", "hardware_id", "meta",
		            "event_id", "schema_version"):
			self.assertEqual(row[key], event[key], f"attribution lost {key!r}")
		self.assertIn("measured_energy_j", row)
		self.assertIn("cpu_core_energy_j", row)


# --------------------------------------------------------------------------
# 10. instrumentation disabled
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# instrumentation disabled
# --------------------------------------------------------------------------

class TestInstrumentationDisabled(unittest.TestCase):

	def setUp(self):
		os.environ.pop("ENERGY_EVENTS_FILE", None)
		aep_events.reset_from_env()
		self.ee = aep_events

	def test_disabled_reports_disabled(self):
		self.assertFalse(self.ee.enabled())

	def test_record_is_a_no_op_and_never_raises(self):
		self.ee.set_question("q1")
		with self.ee.iteration(3):
			self.ee.record(OperationLabel.LLM_REASON, 1.0, 2.0, input_tokens=5)
		self.ee.record(OperationLabel.KG_SPARQL, 1.0, 2.0)

	def test_record_never_raises_on_a_bad_sink(self):
		self.ee.configure(events_file="/nonexistent-dir/nope/events.jsonl")
		try:
			self.ee.record(OperationLabel.LLM_REASON, 1.0, 2.0)
		finally:
			self.ee.configure(events_file="")


if __name__ == "__main__":
	unittest.main(verbosity=2)
