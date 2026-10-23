"""M6: the OpenTelemetry integration (agent_energy_profiler.otel).

Skipped when the `otel` extra is not installed. Every test builds its own
TracerProvider and exporter; nothing here (or in the module under test) sets
OpenTelemetry's global configuration, and one test checks that.
"""

import asyncio
import gzip
import json
import os
import subprocess
import sys
import tempfile
import unittest

import pytest

pytest.importorskip("opentelemetry.sdk.trace", reason="needs the otel extra")

from opentelemetry import trace as otel_trace  # noqa: E402
from opentelemetry.sdk.resources import Resource  # noqa: E402
from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (  # noqa: E402
	InMemorySpanExporter)
from opentelemetry.trace import StatusCode  # noqa: E402

from agent_energy_profiler import analyze, attribution_v2, otel  # noqa: E402
from agent_energy_profiler.measurement import CallableReader, Resolution  # noqa: E402
from agent_energy_profiler.session import Session  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
V2_CASE = os.path.join(HERE, "golden_v2", "session_mixed")
V1_CASE = os.path.join(HERE, "golden", "synthetic_rapl_multigpu")


def hexid(n, width):
	return format(n, f"0{width}x")


class Counter_:
	def __init__(self):
		self.v, self.reads = 1_000_000, 0

	def read(self):
		self.reads += 1
		self.v += 125
		return [self.v]


class Tmp(unittest.TestCase):

	def setUp(self):
		self._tmp = tempfile.TemporaryDirectory()
		self.d = self._tmp.name
		self.app_exporter = InMemorySpanExporter()
		self.provider = TracerProvider(resource=Resource.create({"service.name": "app"}))
		self.provider.add_span_processor(SimpleSpanProcessor(self.app_exporter))
		self.tracer = self.provider.get_tracer("test-app")

	def tearDown(self):
		self.provider.shutdown()
		self._tmp.cleanup()

	@staticmethod
	def records(path):
		with open(path) as f:
			return [json.loads(line) for line in f if line.strip()]

	def spans(self, path):
		return [r for r in self.records(path) if r["record_type"] == "span"]


class TestCorrelation(Tmp):

	def test_root_spans_join_the_current_application_span(self):
		p = os.path.join(self.d, "s.jsonl")
		with Session(p, gpu=False, hooks=[otel.Correlation()]) as s:
			with self.tracer.start_as_current_span("http:request") as app:
				ctx = app.get_span_context()
				with s.span("agent:step"):
					with s.span("llm:plan"):
						pass
			with s.span("agent:outside"):
				pass
		child, root, outside = self.spans(p)
		self.assertEqual(root["trace_id"], hexid(ctx.trace_id, 32))
		self.assertEqual(root["external_parent"],
		                 {"system": "opentelemetry", "trace_id": hexid(ctx.trace_id, 32),
		                  "span_id": hexid(ctx.span_id, 16), "sampled": True})
		self.assertEqual(child["trace_id"], root["trace_id"])
		self.assertNotIn("external_parent", child)
		self.assertNotIn("external_parent", outside)
		self.assertNotEqual(outside["trace_id"], root["trace_id"])

	def test_without_propagation_the_application_context_is_untouched(self):
		p = os.path.join(self.d, "s.jsonl")
		with Session(p, gpu=False, hooks=[otel.Correlation()]) as s:
			with self.tracer.start_as_current_span("http:request") as app:
				with s.span("agent:step"):
					inner = otel_trace.get_current_span()
		self.assertIs(inner, app)

	def test_propagation_nests_application_spans_under_profiler_spans(self):
		p = os.path.join(self.d, "s.jsonl")
		with Session(p, gpu=False, hooks=[otel.Correlation(propagate=True)]) as s:
			with self.tracer.start_as_current_span("http:request") as app:
				with s.span("agent:step") as step:
					with self.tracer.start_as_current_span("db.query"):
						pass
					with s.span("tool:x") as tool:
						with self.tracer.start_as_current_span("http.client"):
							pass
				self.assertIs(otel_trace.get_current_span(), app)   # restored
			self.assertFalse(otel_trace.get_current_span().get_span_context().is_valid)
		by_name = {sp.name: sp for sp in self.app_exporter.get_finished_spans()}
		self.assertEqual(hexid(by_name["db.query"].parent.span_id, 16), step.span_id)
		self.assertEqual(hexid(by_name["http.client"].parent.span_id, 16), tool.span_id)
		self.assertEqual(hexid(by_name["db.query"].context.trace_id, 32), step.trace_id)

	def test_propagation_keeps_concurrent_tasks_apart(self):
		p = os.path.join(self.d, "s.jsonl")
		with Session(p, gpu=False, hooks=[otel.Correlation(propagate=True)]) as s:
			async def worker(i):
				async with s.span("tool:x", attributes={"i": i}):
					await asyncio.sleep(0.001 * (3 - i))
					with self.tracer.start_as_current_span(f"io.{i}"):
						await asyncio.sleep(0)

			async def main():
				async with s.span("agent:batch"):
					await asyncio.gather(*(worker(i) for i in range(3)))
			asyncio.run(main())
		tool_ids = {r["attributes"]["i"]: r["span_id"] for r in self.spans(p)
		            if r["name"] == "tool:x"}
		for sp in self.app_exporter.get_finished_spans():
			i = int(sp.name.split(".")[1])
			self.assertEqual(hexid(sp.parent.span_id, 16), tool_ids[i])

	def test_unsampled_application_traces_stay_unsampled(self):
		from opentelemetry.sdk.trace.sampling import ALWAYS_OFF
		provider = TracerProvider(sampler=ALWAYS_OFF)
		tracer = provider.get_tracer("t")
		p = os.path.join(self.d, "s.jsonl")
		seen = {}
		with Session(p, gpu=False, hooks=[otel.Correlation(propagate=True)]) as s:
			with tracer.start_as_current_span("app"):
				with s.span("agent:step"):
					seen["flags"] = otel_trace.get_current_span().get_span_context().trace_flags
		self.assertFalse(seen["flags"].sampled)
		# an unsampled parent is still a valid parent for correlation
		self.assertEqual(self.spans(p)[0]["external_parent"]["sampled"], False)

	def test_correlation_does_not_change_measurement(self):
		results = []
		for hooks in ((), (otel.Correlation(propagate=True),)):
			c = Counter_()
			p = os.path.join(self.d, f"s{len(results)}.jsonl")
			with Session(p, gpu=CallableReader(c.read), mode="direct", hooks=hooks,
			             resolution=Resolution.configured(0.1, "t")) as s:
				with self.tracer.start_as_current_span("app"):
					with s.span("agent:step"):
						with s.span("llm:plan"):
							pass
			energy = []
			for r in self.spans(p):
				g = dict(r["energy"]["gpu"])
				g.pop("update_intervals")
				energy.append(g)
			results.append((energy, c.reads))
		self.assertEqual(results[0], results[1])

	def test_no_global_configuration_is_changed(self):
		before = otel_trace.get_tracer_provider()
		p = os.path.join(self.d, "s.jsonl")
		with Session(p, gpu=False, hooks=[otel.Correlation(propagate=True)]) as s:
			with s.span("agent:step"):
				pass
		otel.export_spans(p, InMemorySpanExporter())
		self.assertIs(otel_trace.get_tracer_provider(), before)
		self.assertIsInstance(before, otel_trace.ProxyTracerProvider)


class TestExport(Tmp):

	@classmethod
	def setUpClass(cls):
		cls._case = tempfile.TemporaryDirectory()
		d = cls._case.name
		for name in ("spans.jsonl", "power.csv"):
			with gzip.open(os.path.join(V2_CASE, "input", name + ".gz"), "rb") as f, \
					open(os.path.join(d, name), "wb") as g:
				g.write(f.read())
		cls.raw = os.path.join(d, "spans.jsonl")
		attribution_v2.attribute_run(cls.raw, os.path.join(d, "out"),
		                             power=os.path.join(d, "power.csv"))
		cls.attributed = os.path.join(d, "out", "spans_attributed.jsonl")
		with open(cls.attributed) as f:
			cls.rows = [json.loads(line) for line in f]

	@classmethod
	def tearDownClass(cls):
		cls._case.cleanup()

	def exported(self, path=None):
		exporter = InMemorySpanExporter()
		result = otel.export_spans(path or self.attributed, exporter, batch_size=7)
		return result, exporter.get_finished_spans()

	def test_every_span_is_exported_with_its_ids_times_and_hierarchy(self):
		result, spans = self.exported()
		self.assertEqual(result["n_spans"], len(self.rows))
		self.assertEqual(result["n_batches"], -(-len(self.rows) // 7))
		self.assertTrue(result["attributed"])
		for row, sp in zip(self.rows, spans):
			self.assertEqual(sp.name, row["name"])
			self.assertEqual(hexid(sp.context.span_id, 16), row["span_id"])
			self.assertEqual(hexid(sp.context.trace_id, 32), row["trace_id"])
			if row["parent_span_id"]:
				self.assertEqual(hexid(sp.parent.span_id, 16), row["parent_span_id"])
				self.assertFalse(sp.parent.is_remote)
			else:
				self.assertIsNone(sp.parent)
			self.assertEqual(sp.start_time, int(round(row["start_unix_s"] * 1e9)))
			self.assertEqual(sp.end_time - sp.start_time, int(round(row["duration_s"] * 1e9)))
			self.assertEqual(sp.instrumentation_scope.name, "agent_energy_profiler")

	def test_status_mapping(self):
		_, spans = self.exported()
		for row, sp in zip(self.rows, spans):
			self.assertEqual(sp.attributes["aep.status"], row["status"])
			if row["status"] in ("error", "timeout"):
				self.assertEqual(sp.status.status_code, StatusCode.ERROR)
				self.assertEqual(sp.attributes.get("error.type"), row["error_type"])
			else:
				self.assertEqual(sp.status.status_code, StatusCode.UNSET)
		self.assertIn("error", {r["status"] for r in self.rows})

	def test_energy_attributes_carry_the_record_unchanged(self):
		_, spans = self.exported()
		seen = set()
		for row, sp in zip(self.rows, spans):
			a = sp.attributes
			for domain, d in row["energy"].items():
				prefix = f"aep.energy.{domain}"
				self.assertEqual(a[prefix + ".status"], d["status"])
				self.assertEqual(a[prefix + ".scope"], "device")
				self.assertEqual(a[prefix + ".source"], d["source"])
				self.assertEqual(a[prefix + ".method"], d["method"])
				seen.add((domain, d["status"]))
				if d["status"] == "measured":
					self.assertEqual(a[prefix + ".energy_j"], d["energy_j"])
					self.assertNotIn(prefix + ".reason", a)
					self.assertEqual(a[prefix + ".resolution_known"],
					                 d["below_resolution"] is not None)
					if d["below_resolution"] is not None:
						self.assertEqual(a[prefix + ".below_resolution"], d["below_resolution"])
				else:
					# missing stays missing: no number, a reason instead
					self.assertNotIn(prefix + ".energy_j", a)
					self.assertEqual(a[prefix + ".reason"], d["reason"])
			self.assertTrue(a["aep.energy.cpu_core.diagnostic"])
			est = row["estimates"]["gpu"]
			self.assertEqual(a["aep.estimate.gpu.status"], est["status"])
			if est["status"] == "estimated":
				self.assertEqual(a["aep.estimate.gpu.energy_j"], est["energy_j"])
			# estimates never appear under measured keys
			if row["energy"]["gpu"]["status"] != "measured":
				self.assertNotIn("aep.energy.gpu.energy_j", a)
			total = row["total"]
			self.assertEqual(a["aep.energy.total.status"], total["status"])
			self.assertEqual(list(a["aep.energy.total.domains_missing"]),
			                 total["domains_missing"])
			if total["energy_j"] is None:
				self.assertNotIn("aep.energy.total.energy_j", a)
			else:
				self.assertEqual(a["aep.energy.total.energy_j"], total["energy_j"])
			acc = row["accounting"]
			self.assertEqual(a["aep.accounting.attribution"], acc["attribution"])
			self.assertEqual(a["aep.accounting.depth"], acc["depth"])
			for domain, ex in acc["exclusive"].items():
				self.assertEqual(a[f"aep.accounting.exclusive.{domain}.status"], ex["status"])
				if ex["energy_j"] is None:
					self.assertNotIn(f"aep.accounting.exclusive.{domain}.energy_j", a)
			self.assertEqual(a["aep.energy.scope"], "device")
			self.assertIs(a["aep.energy.external_meter_validated"], False)
			self.assertEqual(a["aep.otel.mapping_version"], otel.MAPPING_VERSION)
		self.assertIn(("gpu", "unavailable"), seen)
		self.assertIn(("gpu", "measured"), seen)
		self.assertIn("shared", {r["accounting"]["attribution"] for r in self.rows})

	def test_attribute_values_are_valid_opentelemetry_types(self):
		_, spans = self.exported()
		for sp in spans:
			for key, value in sp.attributes.items():
				self.assertIsNotNone(value, key)
				if isinstance(value, (list, tuple)):
					self.assertLessEqual(len({type(v) for v in value}), 1, key)
				else:
					self.assertIsInstance(value, (bool, str, int, float), key)

	def test_caller_attributes_and_tokens(self):
		row = dict(self.rows[0], attributes={"question": "q?", "k": 3, "obj": {"a": 1},
		                                     "mixed": [1, "x"], "none": None,
		                                     "aep.status": "spoofed"},
		           input_tokens=12, output_tokens=None)
		a = otel.span_attributes(row)
		self.assertEqual((a["question"], a["k"]), ("q?", 3))
		self.assertEqual(a["obj"], '{"a": 1}')
		self.assertEqual(a["mixed"], '[1, "x"]')
		self.assertNotIn("none", a)
		self.assertEqual(a["aep.status"], row["status"])   # the profiler's key wins
		self.assertEqual(a["gen_ai.usage.input_tokens"], 12)
		self.assertNotIn("gen_ai.usage.output_tokens", a)

	def test_session_provenance_on_every_span(self):
		_, spans = self.exported()
		header = otel._header_for(self.attributed)
		self.assertIsNotNone(header)
		for sp in spans:
			self.assertEqual(sp.attributes["aep.session.id"], header["session_id"])
			# the fixture predates measurement modes: say so, do not guess
			self.assertIs(sp.attributes["aep.measurement.recorded"], False)

	def test_raw_spans_export_only_what_was_recorded(self):
		result, spans = self.exported(self.raw)
		self.assertFalse(result["attributed"])
		self.assertEqual(len(spans), len(self.rows))
		self.assertNotIn("aep.energy.cpu_package.status", spans[0].attributes)
		self.assertNotIn("aep.accounting.attribution", spans[0].attributes)

	def test_v1_events_get_deterministic_synthetic_ids(self):
		d = os.path.join(self.d, "v1")
		os.makedirs(d)
		with gzip.open(os.path.join(V1_CASE, "expected", "events_attributed.jsonl.gz")) as f, \
				open(os.path.join(d, "events_attributed.jsonl"), "wb") as g:
			g.write(f.read())
		first = self.exported(d)[1]
		second = self.exported(d)[1]
		self.assertGreater(len(first), 0)
		self.assertEqual([s.context.span_id for s in first], [s.context.span_id for s in second])
		self.assertEqual(len({s.context.trace_id for s in first}), len(first))
		for sp in first:
			self.assertIsNone(sp.parent)
			self.assertTrue(sp.attributes["aep.ids_synthesized"])
			self.assertEqual(sp.attributes["aep.upgraded_from_schema"], 1)

	def test_json_lines_exporter_and_cli(self):
		out = os.path.join(self.d, "otel.jsonl")
		res = subprocess.run([sys.executable, "-m", "agent_energy_profiler.cli", "otel-export",
		                      os.path.dirname(self.attributed), "--out", out,
		                      "--service-name", "svc"], capture_output=True, text=True)
		self.assertEqual(res.returncode, 0, res.stderr)
		with open(out) as f:
			lines = [json.loads(line) for line in f]
		self.assertEqual(len(lines), len(self.rows))
		self.assertEqual(lines[0]["resource"]["attributes"]["service.name"], "svc")
		self.assertEqual(lines[0]["context"]["span_id"], "0x" + self.rows[0]["span_id"])
		self.assertIn(f"{len(self.rows)} spans", res.stderr)

		res = subprocess.run([sys.executable, "-m", "agent_energy_profiler.cli", "otel-export",
		                      self.raw, "--out", out], capture_output=True, text=True)
		self.assertEqual(res.returncode, 0, res.stderr)
		self.assertIn("not attributed", res.stderr)


FAKE_PYNVML = """
import time
_t0 = time.time()
def nvmlInit(): pass
def nvmlShutdown(): pass
def nvmlDeviceGetCount(): return 1
def nvmlDeviceGetHandleByIndex(i): return i
def nvmlDeviceGetTotalEnergyConsumption(h): return int((time.time() - _t0) * 30_000)
def nvmlDeviceGetPowerUsage(h): return 30_000
def nvmlDeviceGetName(h): return "FakeGPU"
def nvmlSystemGetDriverVersion(): return "fake-1.0"
"""


class TestExample(Tmp):

	def test_otel_example_runs_standalone(self):
		example = os.path.join(os.path.dirname(HERE), "examples", "otel_tracing", "app.py")
		if not os.path.exists(example):
			self.skipTest("examples/ not present (installed-package test run)")
		fake = os.path.join(self.d, "fake")
		os.makedirs(fake)
		with open(os.path.join(fake, "pynvml.py"), "w") as f:
			f.write(FAKE_PYNVML)
		env = {k: v for k, v in os.environ.items() if not k.startswith("AEP_")}
		env["PYTHONPATH"] = fake
		out_dir = os.path.join(self.d, "run")
		res = subprocess.run([sys.executable, example, "--out-dir", out_dir, "--work", "20000"],
		                     capture_output=True, text=True, env=env, timeout=120)
		self.assertEqual(res.returncode, 0, res.stderr)
		# Session.profile with no mode: auto, recorded as the entry point's default
		self.assertIn("mode sampled (requested auto, default)", res.stdout)
		self.assertIn("exported 12 profiler spans", res.stdout)
		self.assertIn("vector_db.query", res.stdout)
		with open(os.path.join(out_dir, "otel_spans.jsonl")) as f:
			spans = [json.loads(line) for line in f]
		self.assertEqual(len(spans), 18)                 # 6 application + 12 profiler
		self.assertEqual(len({s["context"]["trace_id"] for s in spans}), 3)
		ids = {s["context"]["span_id"] for s in spans}
		roots = [s for s in spans if s["parent_id"] is None]
		self.assertEqual({s["name"] for s in roots}, {"POST /ask"})
		self.assertTrue(all(s["parent_id"] in ids for s in spans if s["parent_id"]))


class TestEndToEnd(Tmp):

	def test_application_and_energy_spans_form_one_tree(self):
		run = os.path.join(self.d, "run")
		os.makedirs(run)
		with Session(os.path.join(run, "spans.jsonl"), gpu=CallableReader(Counter_().read),
		             mode="direct", hooks=[otel.Correlation(propagate=True)]) as s:
			with self.tracer.start_as_current_span("http:request"):
				with s.span("agent:step"):
					with self.tracer.start_as_current_span("db.query"):
						pass
					with s.span("llm:plan") as plan:
						plan.set_tokens(input=10, output=2)
		rep = analyze(s)
		self.assertEqual(rep["counts"]["spans"], 2)
		otel.export_spans(run, self.app_exporter, resource=self.provider.resource)
		spans = self.app_exporter.get_finished_spans()
		self.assertEqual(sorted(sp.name for sp in spans),
		                 ["agent:step", "db.query", "http:request", "llm:plan"])
		ids = {sp.context.span_id for sp in spans}
		self.assertEqual(len({sp.context.trace_id for sp in spans}), 1)
		roots = [sp for sp in spans if sp.parent is None]
		self.assertEqual([sp.name for sp in roots], ["http:request"])
		for sp in spans:
			if sp.parent is not None:
				self.assertIn(sp.parent.span_id, ids, sp.name)
		plan = next(sp for sp in spans if sp.name == "llm:plan")
		self.assertEqual(plan.attributes["aep.energy.gpu.status"], "measured")
		self.assertEqual(plan.attributes["aep.energy.gpu.energy_j"], 0.125)
		self.assertEqual(plan.attributes["gen_ai.usage.output_tokens"], 2)
		self.assertEqual(plan.resource.attributes["service.name"], "app")


if __name__ == "__main__":
	unittest.main()
