"""M7: the OpenTelemetry integration across measurement modes.

tests/test_otel.py checks the mapping on a fixture and correlation in
`direct` mode. Here one realistic workload (nested sync spans, concurrent
asyncio tasks, explicitly parented threads, application spans in between)
is recorded in every mode, attributed against a synthetic timeline,
exported, and checked end to end: one trace tree, counter measurements and
power estimates kept apart, and missing hardware reported as missing.

Hardware-independent: the GPU is a CallableReader or disabled.
"""

import asyncio
import json
import os
import tempfile
import threading
import unittest

import pytest

pytest.importorskip("opentelemetry.sdk.trace", reason="needs the otel extra")

from opentelemetry.sdk.resources import Resource  # noqa: E402
from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (  # noqa: E402
	InMemorySpanExporter)

from agent_energy_profiler import analyze, otel, sampling  # noqa: E402
from agent_energy_profiler.measurement import (CallableReader, CounterDisturbance,  # noqa: E402
                                               Resolution)
from agent_energy_profiler.session import Session  # noqa: E402

PROFILER = {"agent:request", "retrieval:search", "llm:plan", "tool:a", "tool:b",
            "tool:thread"}
APPLICATION = {"POST /ask", "db.query", "io.a", "io.b"}

# requested mode, disturbance verdict -> (chosen mode, per-span counter reads)
MODES = {
	"direct": ("direct", None, True),
	"sampled": ("sampled", None, False),
	"hybrid": ("hybrid", None, False),
	"auto": ("sampled", None, False),
	"auto+NOT_DETECTED": ("direct", "NOT_DETECTED", True),
}


def hexid(n, width):
	return format(n, f"0{width}x")


class Counter:

	def __init__(self, fail_after=None):
		self.v, self.reads, self.fail_after = 5_000_000, 0, fail_after
		self._lock = threading.Lock()

	def read(self):
		with self._lock:
			self.reads += 1
			if self.fail_after is not None and self.reads > self.fail_after:
				raise RuntimeError("counter went away")
			self.v += 125
			return [self.v]


def write_timeline(path, t0, t1, mode, hz=10):
	"""Synthetic power.csv + sidecar: 100 W GPU, RAPL package and DRAM. The
	GPU counter is on every row for `direct`, at the boundaries for
	`sampled`, every 5th row for `hybrid` (as `agent-energy-profiler run` would configure)."""
	rows = int((t1 - t0) * hz) + 1
	lines = ["t,gpu0_w,gpu0_energy_mj,rapl_package-0_intel-rapl:0_uj,rapl_dram_intel-rapl:0:2_uj"]
	for i in range(rows):
		read = (mode == "direct" or i in (0, rows - 1) or (mode == "hybrid" and i % 5 == 0))
		e = int(100_000 * i / hz) + 5_000_000
		lines.append(f"{t0 + i / hz:.6f},100.0,{e if read else ''},{2_000_000 * i},{200_000 * i}")
	with open(path, "w") as f:
		f.write("\n".join(lines) + "\n")
	counter_hz = {"direct": None, "sampled": 0, "hybrid": hz / 5}[mode]
	with open(path + ".meta.json", "w") as f:
		json.dump({"format": "agent_energy_profiler.power_meta", "version": 2,
		           "rapl_max_energy_range_uj": {},
		           "timeline": sampling.timeline_meta(hz, counter_hz)}, f)


class Base(unittest.TestCase):

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

	def workload(self, s):
		"""Nested sync spans, concurrent tasks and a thread, inside an
		application request span, with application spans in between."""
		with self.tracer.start_as_current_span("POST /ask"):
			with s.bind(request_id="r1"), s.span("agent:request") as request:
				with s.span("retrieval:search"):
					with self.tracer.start_as_current_span("db.query"):
						pass
				with s.span("llm:plan") as plan:
					plan.set_tokens(input=10, output=2)

				async def tool(name):
					async with s.span(f"tool:{name}"):
						await asyncio.sleep(0.005)
						with self.tracer.start_as_current_span(f"io.{name}"):
							await asyncio.sleep(0)

				async def fan_out():
					await asyncio.gather(tool("a"), tool("b"))
				asyncio.run(fan_out())

				def in_thread():
					with s.span("tool:thread", parent=request):
						pass
				t = threading.Thread(target=in_thread)
				t.start()
				t.join()

	def record(self, mode_key, gpu="counter", hooks=True, timeline=True):
		mode, verdict, _ = MODES[mode_key]
		requested = mode_key.split("+")[0]
		run = os.path.join(self.d, f"run-{mode_key}-{gpu}-{hooks}-{timeline}")
		os.makedirs(run)
		counter = Counter() if gpu == "counter" else Counter(fail_after=3)
		reader = CallableReader(counter.read) if gpu in ("counter", "failing") else False
		disturbance = CounterDisturbance(verdict, "test") if verdict else None
		hook_list = [otel.Correlation(propagate=True)] if hooks else []
		with Session(os.path.join(run, "spans.jsonl"), gpu=reader, mode=requested,
		             disturbance=disturbance, hooks=hook_list,
		             resolution=Resolution.configured(0.1, "t")) as s:
			self.workload(s)
		self.assertEqual(s.mode, mode)
		power = None
		if timeline:
			with open(s.path) as f:
				spans = [r for r in map(json.loads, f) if r["record_type"] == "span"]
			t0 = min(r["start_unix_s"] for r in spans) - 1
			t1 = max(r["end_unix_s"] for r in spans) + 1
			power = os.path.join(run, "power.csv")
			write_timeline(power, t0, t1, mode)
		rep = analyze(s, power=power)
		exporter = InMemorySpanExporter()
		result = otel.export_spans(run, exporter, resource=self.provider.resource)
		return s, counter, rep, result, exporter.get_finished_spans()

	def tree(self, exported):
		"""{name: span} over application and profiler spans, checking that
		together they form one trace with every parent present."""
		spans = list(self.app_exporter.get_finished_spans()) + list(exported)
		by_name = {sp.name: sp for sp in spans}
		self.assertEqual(set(by_name), PROFILER | APPLICATION)
		self.assertEqual(len({sp.context.trace_id for sp in spans}), 1)
		ids = {sp.context.span_id for sp in spans}
		roots = [sp.name for sp in spans if sp.parent is None]
		self.assertEqual(roots, ["POST /ask"])
		for sp in spans:
			if sp.parent is not None:
				self.assertIn(sp.parent.span_id, ids, sp.name)
		return by_name


class TestEveryMode(Base):

	def test_one_trace_tree_in_every_mode(self):
		for key in MODES:
			with self.subTest(mode=key):
				self.app_exporter.clear()
				_, _, _, result, exported = self.record(key)
				self.assertEqual((result["n_spans"], result["n_skipped"],
				                  result["n_failed_batches"]), (len(PROFILER), 0, 0))
				self.assertTrue(result["attributed"])
				t = self.tree(exported)
				parent = {name: hexid(t[name].parent.span_id, 16) for name in t
				          if t[name].parent is not None}
				ids = {name: hexid(sp.context.span_id, 16) for name, sp in t.items()}
				self.assertEqual(parent["agent:request"], ids["POST /ask"])
				self.assertEqual(parent["db.query"], ids["retrieval:search"])
				self.assertEqual(parent["io.a"], ids["tool:a"])
				self.assertEqual(parent["io.b"], ids["tool:b"])
				self.assertEqual(parent["tool:thread"], ids["agent:request"])
				for name in PROFILER:
					a = t[name].attributes
					# bound attributes follow tasks; a new thread starts without them
					self.assertEqual(a.get("request_id"), None if name == "tool:thread" else "r1")
					self.assertEqual(a["aep.measurement.requested_mode"], key.split("+")[0])
					self.assertEqual(a["aep.measurement.mode"], MODES[key][0])
					self.assertEqual(a["aep.measurement.mode_source"], "caller")
					self.assertIs(a["aep.energy.external_meter_validated"], False)
				self.assertEqual(t["llm:plan"].attributes["gen_ai.usage.output_tokens"], 2)

	def test_counter_measurements_and_estimates_stay_apart(self):
		for key, (mode, _, per_span) in MODES.items():
			with self.subTest(mode=key):
				_, counter, rep, _, exported = self.record(key)
				self.assertEqual(counter.reads, 2 * len(PROFILER) if per_span else 0)
				for sp in exported:
					a = sp.attributes
					self.assertEqual(a["aep.energy.cpu_package.status"], "measured")
					self.assertEqual(a["aep.estimate.gpu.status"], "estimated")
					self.assertIn("aep.estimate.gpu.energy_j", a)
					if per_span:
						self.assertEqual(a["aep.energy.gpu.status"], "measured")
						# a whole number of counter steps; overlapping siblings
						# see each other's reads (device scope, hence "shared")
						steps = a["aep.energy.gpu.energy_j"] / 0.125
						self.assertGreaterEqual(steps, 1)
						self.assertAlmostEqual(steps, round(steps))
						self.assertEqual(a["aep.energy.gpu.method"], "in_band_counter_delta")
					else:
						# no counter value, and the estimate is not promoted
						self.assertEqual(a["aep.energy.gpu.status"], "unavailable")
						self.assertEqual(a["aep.energy.gpu.reason"], "not_read_by_mode")
						self.assertNotIn("aep.energy.gpu.energy_j", a)
						self.assertNotIn("aep.energy.total.energy_j", a)
						self.assertIn("gpu", list(a["aep.energy.total.domains_missing"]))
				codes = {w["code"] for w in rep["warnings"]}
				self.assertEqual("SPAN_ESTIMATES_ONLY" in codes, not per_span)

	def test_concurrent_siblings_are_shared_in_every_mode(self):
		for key in MODES:
			with self.subTest(mode=key):
				_, _, _, _, exported = self.record(key)
				by_name = {sp.name: sp.attributes for sp in exported}
				for name in ("tool:a", "tool:b"):
					self.assertEqual(by_name[name]["aep.accounting.attribution"], "shared")
				self.assertEqual(by_name["retrieval:search"]["aep.accounting.attribution"],
				                 "sole")

	def test_tracing_does_not_change_what_is_measured(self):
		for key in ("direct", "sampled", "hybrid"):
			with self.subTest(mode=key):
				results = []
				for hooks in (False, True):
					self.app_exporter.clear()
					s, counter, _, _, exported = self.record(key, hooks=hooks)
					energy = {}
					for sp in exported:
						a = sp.attributes
						energy[sp.name] = {
							k: v for k, v in a.items()
							# GPU only: RAPL and totals are interpolated over each
						# span's (timing-dependent) duration
						# (update_intervals: duration / update period, also timing)
						if k.startswith("aep.energy.gpu.") and not k.endswith("update_intervals")}
					results.append((counter.reads, energy))
				self.assertEqual(results[0], results[1])


class TestUnavailableHardware(Base):

	def test_no_gpu_in_every_mode(self):
		for key in MODES:
			with self.subTest(mode=key):
				self.app_exporter.clear()
				_, _, rep, result, exported = self.record(key, gpu="none")
				self.assertEqual(result["n_spans"], len(PROFILER))
				self.tree(exported)
				for sp in exported:
					a = sp.attributes
					self.assertEqual(a["aep.energy.gpu.status"], "unavailable")
					self.assertIn(a["aep.energy.gpu.reason"], ("disabled", "no_counter"))
					self.assertNotIn("aep.energy.gpu.energy_j", a)
					self.assertNotIn("aep.energy.total.energy_j", a)

	def test_no_timeline_exports_without_inventing_host_energy(self):
		_, _, rep, result, exported = self.record("sampled", gpu="none", timeline=False)
		self.assertTrue(result["attributed"])
		self.assertEqual(len(exported), len(PROFILER))
		for sp in exported:
			a = sp.attributes
			for domain in ("gpu", "cpu_package", "dram"):
				self.assertNotEqual(a.get(f"aep.energy.{domain}.status"), "measured", domain)
				self.assertNotIn(f"aep.energy.{domain}.energy_j", a)
			self.assertNotIn("aep.estimate.gpu.energy_j", a)
			self.assertNotIn("aep.energy.total.energy_j", a)

	def test_a_counter_failing_mid_run_is_reported_not_zeroed(self):
		_, counter, _, result, exported = self.record("direct", gpu="failing")
		self.assertEqual(result["n_spans"], len(PROFILER))
		statuses = {sp.attributes["aep.energy.gpu.status"] for sp in exported}
		self.assertIn("measured", statuses)
		self.assertTrue(statuses - {"measured"})
		for sp in exported:
			a = sp.attributes
			if a["aep.energy.gpu.status"] != "measured":
				self.assertIn("aep.energy.gpu.reason", a)
				self.assertNotIn("aep.energy.gpu.energy_j", a)

	def test_nvml_installed_without_a_driver(self):
		"""The CI case: nvidia-ml-py importable, nvmlInit fails."""
		import sys
		import types
		from agent_energy_profiler.hardware import nvml
		fake = types.ModuleType("pynvml")

		def nvml_init():
			raise RuntimeError("NVML Shared Library Not Found")
		fake.nvmlInit = nvml_init
		saved = sys.modules.get("pynvml")
		sys.modules["pynvml"] = fake
		nvml.reset()
		try:
			run = os.path.join(self.d, "nodriver")
			os.makedirs(run)
			with Session(os.path.join(run, "spans.jsonl"), gpu="auto", mode="direct",
			             hooks=[otel.Correlation(propagate=True)]) as s:
				self.workload(s)
		finally:
			nvml.reset()
			if saved is None:
				sys.modules.pop("pynvml", None)
			else:
				sys.modules["pynvml"] = saved
		analyze(s)
		exporter = InMemorySpanExporter()
		self.assertEqual(otel.export_spans(run, exporter)["n_spans"], len(PROFILER))
		self.tree(exporter.get_finished_spans())
		for sp in exporter.get_finished_spans():
			a = sp.attributes
			self.assertEqual((a["aep.energy.gpu.status"], a["aep.energy.gpu.reason"]),
			                 ("unavailable", "no_counter"))
			self.assertNotIn("aep.energy.gpu.energy_j", a)

	def test_raw_spans_export_when_nothing_was_attributed(self):
		run = os.path.join(self.d, "raw")
		os.makedirs(run)
		with Session(os.path.join(run, "spans.jsonl"), gpu=False, mode="sampled",
		             hooks=[otel.Correlation(propagate=True)]) as s:
			self.workload(s)
		exporter = InMemorySpanExporter()
		result = otel.export_spans(run, exporter)
		self.assertFalse(result["attributed"])
		self.assertEqual(result["n_spans"], len(PROFILER))
		self.tree(exporter.get_finished_spans())
		for sp in exporter.get_finished_spans():
			self.assertEqual(sp.attributes["aep.energy.gpu.status"], "unavailable")
			self.assertNotIn("aep.accounting.attribution", sp.attributes)


if __name__ == "__main__":
	unittest.main()
