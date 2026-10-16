"""M7 (0.3.0): the announced change of Session's default mode.

Session() without mode= still runs `direct` exactly as before, and warns once
per process that the default becomes `auto` in 0.4.0. Explicit modes never
warn. Nothing measured changes.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
import warnings

from agent_energy_profiler import measurement as m
from agent_energy_profiler import sampling
from agent_energy_profiler import session as session_module
from agent_energy_profiler.measurement import (CallableReader, CounterDisturbance,
                                               SessionDefaultModeWarning)
from agent_energy_profiler.session import Session

HERE = os.path.abspath(__file__)
SRC = os.path.join(os.path.dirname(os.path.dirname(HERE)), "src")


class Counter:

	def __init__(self):
		self.v = 1_000_000
		self.reads = 0

	def read(self):
		self.reads += 1
		self.v += 250
		return [self.v]


class Base(unittest.TestCase):

	def setUp(self):
		self._tmp = tempfile.TemporaryDirectory()
		self.d = self._tmp.name
		self.n = 0
		self._saved_flag = session_module._default_mode_warned
		session_module._default_mode_warned = False
		self._saved_env = {k: os.environ.pop(k, None)
		                   for k in (Session.ENV_SPANS_FILE, Session.ENV_MEASUREMENT)}

	def tearDown(self):
		session_module._default_mode_warned = self._saved_flag
		for k, v in self._saved_env.items():
			if v is not None:
				os.environ[k] = v
			else:
				os.environ.pop(k, None)
		self._tmp.cleanup()

	def path(self):
		self.n += 1
		return os.path.join(self.d, f"spans{self.n}.jsonl")

	def caught(self, make):
		"""(result of make(), [SessionDefaultModeWarning])"""
		with warnings.catch_warnings(record=True) as w:
			warnings.simplefilter("always")
			result = make()
		return result, [x for x in w if issubclass(x.category, SessionDefaultModeWarning)]

	@staticmethod
	def records(path):
		with open(path) as f:
			return [json.loads(line) for line in f if line.strip()]


class TestImplicitDefault(Base):

	def test_implicit_default_warns_once_with_the_migration_path(self):
		s, w = self.caught(lambda: Session(self.path(), gpu=False))
		self.assertEqual(len(w), 1)
		self.assertTrue(issubclass(w[0].category, FutureWarning))
		msg = str(w[0].message)
		for needed in ("without mode=", "'direct'", "0.4.0", "'auto'", "'sampled'",
		               "estimate", "mode='direct'", "mode='auto'", "MEASUREMENT_MODES.md"):
			self.assertIn(needed, msg)
		# attributed to the caller, not to the library
		self.assertEqual(os.path.abspath(w[0].filename), HERE)
		# the default itself is unchanged in 0.3.0
		self.assertEqual((s.mode, s.requested_mode, s.mode_source),
		                 ("direct", "direct", "default"))
		self.assertTrue(m.MODES[s.mode]["span_counter_reads"])

	def test_the_warning_is_not_repeated_in_the_same_process(self):
		_, first = self.caught(lambda: Session(self.path(), gpu=False))
		_, again = self.caught(lambda: [Session(self.path(), gpu=False) for _ in range(5)])
		self.assertEqual((len(first), len(again)), (1, 0))

	def test_explicit_modes_never_warn(self):
		for mode in ("direct", "sampled", "hybrid", "auto"):
			_, w = self.caught(lambda: Session(self.path(), gpu=False, mode=mode))
			self.assertEqual(w, [], mode)
		_, w = self.caught(lambda: Session(
			self.path(), gpu=False, mode="auto",
			disturbance=CounterDisturbance("NOT_DETECTED", "t")))
		self.assertEqual(w, [])
		# and an explicit mode does not use up the once-per-process warning
		self.assertFalse(session_module._default_mode_warned)

	def test_from_env_warns_only_when_nothing_chose_a_mode(self):
		s, w = self.caught(lambda: Session.from_env(self.path(), gpu=False))
		self.assertEqual(len(w), 1)
		self.assertEqual(os.path.abspath(w[0].filename), HERE)
		self.assertEqual(s.mode_source, "default")

		session_module._default_mode_warned = False
		_, w = self.caught(lambda: Session.from_env(self.path(), gpu=False,
		                                            default_mode="direct"))
		self.assertEqual(w, [])

		os.environ[Session.ENV_MEASUREMENT] = json.dumps({
			"mode": "sampled", "requested_mode": "auto", "mode_reason": "auto: t",
			"disturbance": CounterDisturbance.unknown().to_dict(),
			"timeline": sampling.timeline_meta(10, 0)})
		s, w = self.caught(lambda: Session.from_env(self.path(), gpu=False))
		self.assertEqual((w, s.mode_source), ([], "aep_run"))

	def test_measurement_is_identical_to_explicit_direct(self):
		def run(**kw):
			c = Counter()
			p = self.path()
			with warnings.catch_warnings():
				warnings.simplefilter("ignore", SessionDefaultModeWarning)
				with Session(p, gpu=CallableReader(c.read), **kw) as s:
					for _ in range(3):
						with s.span("agent:step"):
							with s.span("tool:x"):
								pass
			spans = [r for r in self.records(p) if r["record_type"] == "span"]
			gpu = []
			for r in spans:
				g = dict(r["energy"]["gpu"])
				g.pop("update_intervals")      # timing-dependent, not a measured value
				gpu.append((r["name"], r["status"], g))
			return c.reads, gpu, self.records(p)[0]["config"]["measurement"]
		reads_d, gpu_d, meas_d = run()
		reads_e, gpu_e, meas_e = run(mode="direct")
		self.assertEqual((reads_d, gpu_d), (reads_e, gpu_e))
		self.assertEqual(reads_d, 12)      # still one counter read per span boundary
		self.assertEqual({k: v for k, v in meas_d.items() if k not in ("mode_source", "mode_reason")},
		                 {k: v for k, v in meas_e.items() if k not in ("mode_source", "mode_reason")})
		self.assertEqual((meas_d["mode_source"], meas_e["mode_source"]), ("default", "caller"))


def _run(code, *args):
	env = dict(os.environ, PYTHONPATH=SRC + os.pathsep + os.environ.get("PYTHONPATH", ""))
	env.pop("PYTHONWARNINGS", None)
	env.pop(Session.ENV_MEASUREMENT, None)
	return subprocess.run([sys.executable, *args, "-c", code], capture_output=True,
	                      text=True, env=env, timeout=60)


class TestOutsidePytest(unittest.TestCase):
	"""Python's default warning filters, not the test runner's."""

	CODE = (
		"import os, tempfile, threading\n"
		"from agent_energy_profiler import Session\n"
		"d = tempfile.mkdtemp()\n"
		"def make(i):\n"
		"    Session(os.path.join(d, f'{i}.jsonl'), gpu=False)\n"
		"ts = [threading.Thread(target=make, args=(i,)) for i in range(8)]\n"
		"[t.start() for t in ts]; [t.join() for t in ts]\n"
		"Session(os.path.join(d, 'x.jsonl'), gpu=False, mode='direct')\n"
		"print('ok')\n")

	def test_shown_by_default_once_across_threads(self):
		r = _run(self.CODE)
		self.assertEqual(r.returncode, 0, r.stderr)
		self.assertEqual(r.stderr.count("SessionDefaultModeWarning"), 1, r.stderr)

	def test_silenced_by_a_standard_filter(self):
		r = _run(self.CODE, "-W", "ignore::FutureWarning")
		self.assertEqual((r.returncode, r.stderr), (0, ""))

	def test_as_an_error_every_implicit_session_fails_before_writing(self):
		code = ("import os, tempfile\n"
		        "from agent_energy_profiler import Session\n"
		        "d = tempfile.mkdtemp(); n = 0\n"
		        "for i in range(2):\n"
		        "    try:\n"
		        "        Session(os.path.join(d, f'{i}.jsonl'), gpu=False).__enter__()\n"
		        "    except FutureWarning:\n"
		        "        n += 1\n"
		        "print(n, len(os.listdir(d)))\n")
		r = _run(code, "-W", "error::FutureWarning")
		self.assertEqual(r.returncode, 0, r.stderr)
		self.assertEqual(r.stdout.split(), ["2", "0"])


if __name__ == "__main__":
	unittest.main()
