"""M6: Session hooks, bound attributes, mode provenance and analyze().

Core only: nothing here needs OpenTelemetry (tests/test_otel.py covers the
integration built on these hooks).
"""

import asyncio
import gzip
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest

from agent_energy_profiler import analyze, report
from agent_energy_profiler import measurement as m
from agent_energy_profiler import sampling
from agent_energy_profiler.measurement import CallableReader, CounterDisturbance, Resolution
from agent_energy_profiler.session import Session

HERE = os.path.dirname(os.path.abspath(__file__))
V2_CASE = os.path.join(HERE, "golden_v2", "session_mixed")

TRACE = "0af7651916cd43dd8448eb211c80319c"
PARENT = "b7ad6b7169203331"


class Log:
	"""One ordered log shared by a fake counter and the hooks."""

	def __init__(self):
		self.events = []
		self.v = 1_000_000

	def read(self):
		self.events.append("read")
		self.v += 250
		return [self.v]


class RecordingHook:

	def __init__(self, log, context=None):
		self.log = log
		self.context = context
		self.ended = []

	def trace_context(self):
		self.log.events.append("trace_context")
		return self.context

	def on_span_start(self, span):
		self.log.events.append(f"start:{span.name}")

	def on_span_end(self, span, record):
		self.log.events.append(f"end:{span.name}")
		self.ended.append(record)


class BrokenHook:

	def trace_context(self):
		raise RuntimeError("boom")

	def on_span_start(self, span):
		raise RuntimeError("boom")

	def on_span_end(self, span, record):
		raise RuntimeError("boom")


class Tmp(unittest.TestCase):

	def setUp(self):
		self._tmp = tempfile.TemporaryDirectory()
		self.d = self._tmp.name
		self.n = 0

	def tearDown(self):
		self._tmp.cleanup()

	def path(self):
		self.n += 1
		return os.path.join(self.d, f"spans{self.n}.jsonl")

	@staticmethod
	def records(path):
		with open(path) as f:
			return [json.loads(line) for line in f if line.strip()]

	def spans(self, path):
		return [r for r in self.records(path) if r["record_type"] == "span"]


class TestHookOrdering(Tmp):

	def test_hooks_run_outside_the_counter_window_and_after_the_write(self):
		log = Log()
		hook = RecordingHook(log)
		p = self.path()
		with Session(p, gpu=CallableReader(log.read), mode="direct", hooks=[hook]) as s:
			with s.span("agent:step"):
				with s.span("llm:plan"):
					pass
		self.assertEqual(log.events, [
			"trace_context", "start:agent:step", "read",   # root: context, hook, counter
			"start:llm:plan", "read",                      # child: no context lookup
			"read", "end:llm:plan",
			"read", "end:agent:step",
		])
		# on_span_end sees the record exactly as written
		written = {r["span_id"]: r for r in self.spans(p)}
		for rec in hook.ended:
			self.assertEqual(rec, written[rec["span_id"]])

	def test_measurement_is_identical_with_and_without_hooks(self):
		results = []
		for hooks in ((), (RecordingHook(Log()), RecordingHook(Log()))):
			log = Log()
			p = self.path()
			with Session(p, gpu=CallableReader(log.read), mode="direct", hooks=hooks,
			             resolution=Resolution.configured(0.1, "t")) as s:
				for _ in range(3):
					with s.span("agent:step"):
						with s.span("tool:x"):
							pass
			gpu = []
			for r in self.spans(p):
				g = dict(r["energy"]["gpu"])
				# duration / interval: timing-dependent, not a measured value
				self.assertLess(g.pop("update_intervals"), 1.0)
				gpu.append((r["name"], g))
			results.append((gpu, log.events.count("read")))
		self.assertEqual(results[0], results[1])
		self.assertEqual(results[0][0][0][1]["energy_j"], 0.25)

	def test_end_hooks_run_in_reverse_order(self):
		order = []

		class H:
			def __init__(self, name):
				self.name = name

			def on_span_start(self, span):
				order.append("start" + self.name)

			def on_span_end(self, span, record):
				order.append("end" + self.name)

		with Session(self.path(), gpu=False, hooks=[H("A"), H("B")]) as s:
			with s.span("x:y"):
				pass
		self.assertEqual(order, ["startA", "startB", "endB", "endA"])

	def test_a_failing_hook_is_counted_and_never_breaks_the_workload(self):
		p = self.path()
		with Session(p, gpu=False, hooks=[BrokenHook()]) as s:
			with s.span("agent:step") as step:
				with s.span("llm:plan"):
					pass
			self.assertEqual(len(step.trace_id), 32)
		recs = self.records(p)
		self.assertEqual(len([r for r in recs if r["record_type"] == "span"]), 2)
		# 1 trace_context + 2 starts + 2 ends
		self.assertEqual(recs[-1]["n_hook_errors"], 5)
		self.assertEqual(recs[0]["config"]["hooks"], [f"{__name__}.BrokenHook"])
		self.assertNotIn("external_parent", recs[1])

	def test_without_hooks_the_records_are_unchanged(self):
		p = self.path()
		with Session(p, gpu=False) as s:
			with s.span("x:y"):
				pass
		recs = self.records(p)
		self.assertNotIn("n_hook_errors", recs[-1])
		self.assertNotIn("hooks", recs[0]["config"])
		self.assertNotIn("external_parent", recs[1])

	def test_workload_exceptions_still_propagate_with_hooks(self):
		log = Log()
		with Session(self.path(), gpu=False, hooks=[RecordingHook(log)]) as s:
			with self.assertRaises(KeyError):
				with s.span("x:y"):
					raise KeyError("k")
		self.assertIn("end:x:y", log.events)


class TestExternalTraceContext(Tmp):

	def test_root_spans_join_the_external_trace(self):
		ctx = {"system": "opentelemetry", "trace_id": TRACE, "span_id": PARENT,
		       "sampled": True}
		p = self.path()
		with Session(p, gpu=False, hooks=[RecordingHook(Log(), ctx)]) as s:
			with s.span("agent:step") as root:
				self.assertEqual(root.external_parent["span_id"], PARENT)
				with s.span("llm:plan"):
					pass
		child, parent = self.spans(p)
		self.assertEqual(parent["trace_id"], TRACE)
		self.assertEqual(parent["external_parent"],
		                 {"system": "opentelemetry", "trace_id": TRACE,
		                  "span_id": PARENT, "sampled": True})
		self.assertIsNone(parent["parent_span_id"])
		self.assertEqual(child["trace_id"], TRACE)
		self.assertNotIn("external_parent", child)
		self.assertEqual(child["parent_span_id"], parent["span_id"])
		# accounting treats it as an ordinary root
		from agent_energy_profiler import attribution_v2, schema_v2
		rows = {r["name"]: r["accounting"] for r in
		        attribution_v2.attribute_spans(schema_v2.read_records(p))}
		self.assertEqual((rows["agent:step"]["depth"], rows["agent:step"]["parent_missing"]),
		                 (0, False))
		self.assertEqual(rows["llm:plan"]["depth"], 1)

	def test_explicit_trace_id_and_no_context_are_left_alone(self):
		p = self.path()
		with Session(p, gpu=False, hooks=[RecordingHook(Log(), None)]) as s:
			with s.span("a:b"):
				pass
			with s.span("a:c", trace_id="1" * 32):
				pass
		first, second = self.spans(p)
		self.assertNotIn("external_parent", first)
		self.assertEqual(second["trace_id"], "1" * 32)

	def test_malformed_contexts_are_rejected_and_counted(self):
		for bad in ({"trace_id": "xyz", "span_id": PARENT},
		            {"trace_id": "0" * 32, "span_id": PARENT},
		            {"trace_id": TRACE.upper(), "span_id": PARENT},
		            {"trace_id": TRACE},
		            (TRACE, PARENT)):
			p = self.path()
			with Session(p, gpu=False, hooks=[RecordingHook(Log(), bad)]) as s:
				with s.span("a:b"):
					pass
			recs = self.records(p)
			self.assertNotIn("external_parent", recs[1], bad)
			self.assertNotEqual(recs[1]["trace_id"], TRACE)
			self.assertEqual(recs[-1]["n_hook_errors"], 1, bad)


class TestBind(Tmp):

	def test_bound_attributes_reach_every_span_in_scope(self):
		p = self.path()
		with Session(p, gpu=False) as s:
			with s.bind(request_id="r1", user="u"):
				with s.span("agent:step", attributes={"user": "explicit"}):
					with s.bind(attempt=2):
						with s.span("llm:plan"):
							pass
				with s.span("tool:x"):
					pass
			with s.span("tool:after"):
				pass
		attrs = {r["name"]: r["attributes"] for r in self.spans(p)}
		self.assertEqual(attrs["agent:step"], {"request_id": "r1", "user": "explicit"})
		self.assertEqual(attrs["llm:plan"], {"request_id": "r1", "user": "u", "attempt": 2})
		self.assertEqual(attrs["tool:x"], {"request_id": "r1", "user": "u"})
		self.assertEqual(attrs["tool:after"], {})

	def test_bind_follows_asyncio_tasks_and_stays_per_session(self):
		p1, p2 = self.path(), self.path()
		with Session(p1, gpu=False) as a, Session(p2, gpu=False) as b:
			async def request(rid):
				async with a.bind(request_id=rid):
					await asyncio.sleep(0)
					async with a.span("agent:request"):
						await asyncio.gather(work(rid), work(rid))

			async def work(rid):
				await asyncio.sleep(0)
				with a.span("tool:x") as t:
					t.set_attribute("seen", rid)
				with b.span("other:y"):
					pass

			async def main():
				await asyncio.gather(request("r1"), request("r2"))
			asyncio.run(main())
		spans = self.spans(p1)
		self.assertEqual(len(spans), 6)
		tools = [r for r in spans if r["name"] == "tool:x"]
		self.assertEqual(len(tools), 4)
		for r in tools:
			self.assertEqual(r["attributes"]["request_id"], r["attributes"]["seen"])
		self.assertEqual(sorted(r["attributes"]["request_id"] for r in spans
		                        if r["name"] == "agent:request"), ["r1", "r2"])
		self.assertTrue(all(r["attributes"] == {} for r in self.spans(p2)))

	def test_threads_start_without_bound_attributes(self):
		p = self.path()
		with Session(p, gpu=False) as s:
			with s.bind(request_id="r1"):
				def run():
					with s.span("tool:thread"):
						pass
				t = threading.Thread(target=run)
				t.start()
				t.join()
		self.assertEqual(self.spans(p)[0]["attributes"], {})


class TestModeProvenance(Tmp):

	def header(self, **kw):
		p = self.path()
		with Session(p, gpu=CallableReader(Log().read), **kw) as s:
			with s.span("x:y"):
				pass
		return self.records(p)[0]["config"]["measurement"]

	def test_default_is_auto_and_says_it_was_defaulted(self):
		meas = self.header()
		self.assertEqual((meas["mode"], meas["requested_mode"], meas["mode_source"]),
		                 ("sampled", "auto", "default"))
		self.assertTrue(meas["mode_reason"].startswith("default:"))

	def test_every_explicit_mode_is_recorded_as_the_callers(self):
		cases = {"direct": "direct", "sampled": "sampled", "hybrid": "hybrid",
		         "auto": "sampled"}
		for requested, chosen in cases.items():
			meas = self.header(mode=requested)
			self.assertEqual((meas["mode"], meas["requested_mode"], meas["mode_source"]),
			                 (chosen, requested, "caller"), requested)
		meas = self.header(mode="auto", disturbance=CounterDisturbance("NOT_DETECTED", "t"))
		self.assertEqual((meas["mode"], meas["mode_source"]), ("direct", "caller"))
		meas = self.header(mode="direct")
		self.assertEqual(meas["mode_reason"], "requested explicitly")

	def env_session(self, **kw):
		env = {"AEP_SPANS_FILE": self.path(),
		       "AEP_MEASUREMENT": json.dumps({
		           "mode": "sampled", "requested_mode": "auto",
		           "mode_reason": "auto: counter disturbance UNKNOWN",
		           "disturbance": CounterDisturbance.unknown().to_dict(),
		           "timeline": sampling.timeline_meta(10, 0)})}
		old = {k: os.environ.get(k) for k in env}
		os.environ.update(env)
		try:
			with Session.from_env(gpu=False, **kw) as s:
				with s.span("x:y"):
					pass
		finally:
			for k, v in old.items():
				if v is None:
					os.environ.pop(k, None)
				else:
					os.environ[k] = v
		return self.records(env["AEP_SPANS_FILE"])[0]["config"]["measurement"]

	def test_aep_run_configured_sessions_say_so(self):
		meas = self.env_session()
		self.assertEqual((meas["mode"], meas["requested_mode"], meas["mode_source"]),
		                 ("sampled", "auto", "aep_run"))

	def test_default_mode_applies_only_outside_aep_run(self):
		meas = self.env_session(default_mode="hybrid")
		self.assertEqual((meas["mode"], meas["mode_source"]), ("sampled", "aep_run"))
		saved = os.environ.pop("AEP_MEASUREMENT", None)
		try:
			p = self.path()
			with Session.from_env(p, gpu=False, default_mode="auto") as s:
				with s.span("x:y"):
					pass
		finally:
			if saved is not None:
				os.environ["AEP_MEASUREMENT"] = saved
		meas = self.records(p)[0]["config"]["measurement"]
		self.assertEqual((meas["mode"], meas["requested_mode"], meas["mode_source"]),
		                 ("sampled", "auto", "caller"))

	def test_an_explicit_mode_overrides_aep_run_and_is_the_callers(self):
		meas = self.env_session(mode="direct")
		self.assertEqual((meas["mode"], meas["mode_source"], meas["mode_reason"]),
		                 ("direct", "caller", "requested explicitly"))

	def test_report_warns_only_about_defaulted_direct_mode(self):
		def codes(**kw):
			p = self.path()
			with Session(p, gpu=CallableReader(Log().read), **kw) as s:
				with s.span("x:y"):
					pass
			return {w["code"] for w in report.build(p)["warnings"]}

		def legacy_codes(verdict="UNKNOWN"):
			# a 0.2.0/0.3.0 session: direct because Session's default was direct
			p = self.path()
			with Session(p, gpu=CallableReader(Log().read), mode="direct",
			             disturbance=CounterDisturbance(verdict, "t")) as s:
				with s.span("x:y"):
					pass
			with open(p) as f:
				lines = [json.loads(line) for line in f]
			lines[0]["config"]["measurement"].update(
				requested_mode="direct", mode_source="default",
				mode_reason="default: Session's default mode (direct, per-span counter reads)")
			with open(p, "w") as f:
				f.writelines(json.dumps(r) + "\n" for r in lines)
			return {w["code"] for w in report.build(p)["warnings"]}
		self.assertIn("DIRECT_MODE_BY_DEFAULT", legacy_codes())
		self.assertNotIn("DIRECT_MODE_BY_DEFAULT", legacy_codes("NOT_DETECTED"))
		self.assertNotIn("DIRECT_MODE_BY_DEFAULT", codes())          # 0.4.0 default
		self.assertNotIn("DIRECT_MODE_BY_DEFAULT", codes(mode="direct"))
		self.assertNotIn("DIRECT_MODE_BY_DEFAULT",
		                 codes(disturbance=CounterDisturbance("NOT_DETECTED", "t")))
		self.assertNotIn("DIRECT_MODE_BY_DEFAULT", codes(mode="sampled"))

	def test_report_text_shows_the_mode_source(self):
		p = self.path()
		with Session(p, gpu=False, mode="sampled") as s:
			with s.span("x:y"):
				pass
		self.assertIn("(requested sampled, caller)", report.render_text(report.build(p)))


class TestAnalyze(Tmp):

	def unpack(self, name):
		out = os.path.join(self.d, name)
		with gzip.open(os.path.join(V2_CASE, "input", name + ".gz"), "rb") as f, \
				open(out, "wb") as g:
			g.write(f.read())
		return out

	def test_analyze_equals_aep_attribute_then_report(self):
		spans, power = self.unpack("spans.jsonl"), self.unpack("power.csv")
		rep = analyze(spans, power=power, out_dir=os.path.join(self.d, "a"))
		code = subprocess.run([sys.executable, "-m", "agent_energy_profiler.cli", "attribute",
		                       spans, "--power", power, "--out-dir",
		                       os.path.join(self.d, "b")], capture_output=True).returncode
		self.assertEqual(code, 0)
		expected = report.build(os.path.join(self.d, "b"))
		for r in (rep, expected):
			r["source"]["path"] = None
		self.assertEqual(json.dumps(rep, sort_keys=True), json.dumps(expected, sort_keys=True))
		with open(os.path.join(self.d, "a", "spans_attributed.jsonl"), "rb") as f:
			a = f.read()
		with gzip.open(os.path.join(V2_CASE, "expected", "spans_attributed.jsonl.gz")) as f:
			self.assertEqual(a, f.read())

	def test_analyze_a_closed_session(self):
		p = os.path.join(self.d, "run", "spans.jsonl")
		os.makedirs(os.path.dirname(p))
		with Session(p, gpu=False, mode="sampled") as s:
			with s.span("agent:step"):
				pass
			with self.assertRaises(RuntimeError):
				analyze(s)
		rep = analyze(s)
		self.assertEqual(rep["counts"]["spans"], 1)
		self.assertEqual(rep["measurement"]["mode"], "sampled")
		self.assertTrue(os.path.exists(os.path.join(self.d, "run", "spans_attributed.jsonl")))
		with self.assertRaises(ValueError):
			analyze(os.path.join(self.d, "run", "spans_attributed.jsonl"))


class TestCoreStaysFreeOfOpenTelemetry(unittest.TestCase):

	def test_recording_with_hooks_imports_no_opentelemetry(self):
		code = ("import sys, tempfile, os\n"
		        "from agent_energy_profiler import Session, analyze\n"
		        "d = tempfile.mkdtemp()\n"
		        "class H:\n"
		        "    def on_span_start(self, span): pass\n"
		        "with Session(os.path.join(d, 's.jsonl'), gpu=False, hooks=[H()]) as s:\n"
		        "    with s.span('a:b'):\n"
		        "        pass\n"
		        "analyze(s)\n"
		        "print(sorted(m for m in sys.modules if m.startswith('opentelemetry')))\n")
		out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
		                     timeout=60)
		self.assertEqual(out.returncode, 0, out.stderr)
		self.assertEqual(out.stdout.strip(), "[]")

	def test_the_otel_module_explains_a_missing_dependency(self):
		code = ("import sys\n"
		        "sys.modules['opentelemetry'] = None\n"
		        "try:\n"
		        "    from agent_energy_profiler import otel\n"
		        "except ImportError as e:\n"
		        "    print('IMPORTERROR', e)\n")
		out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
		                     timeout=60)
		self.assertIn("IMPORTERROR", out.stdout, out.stderr)
		self.assertIn("agent-energy-profiler[otel]", out.stdout)


if __name__ == "__main__":
	unittest.main()
