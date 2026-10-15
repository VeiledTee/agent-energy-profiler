"""Baseline equivalence with the source research implementation.

Every case under tests/golden/ holds raw inputs (events + hardware timeline)
and the four attribution artifacts the SOURCE implementation produced from
exactly those inputs (see tools/build_golden_fixtures.py and each case's
manifest.json). This package must reproduce them byte for byte.

A difference here means measurement semantics changed. That is never an
incidental refactoring outcome: a deliberate change must regenerate nothing
silently, and must be documented in docs/PROVENANCE.md with its reason.

A second, opt-in check re-attributes complete frozen research runs. Set
AEP_FROZEN_RUNS_DIR to a directory of run folders (each holding events.jsonl,
power.csv and the attributed artifacts) and run `pytest -m frozen_runs`.
"""

import gzip
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest

import pytest

import agent_energy_profiler

HERE = os.path.dirname(os.path.abspath(__file__))
GOLDEN = os.path.join(HERE, "golden")
ARTIFACTS = ("events_attributed.jsonl", "energy_summary.csv",
             "trajectory_summary.csv", "trajectory_summary.json")


def cases():
	return sorted(name for name in os.listdir(GOLDEN)
	              if os.path.isfile(os.path.join(GOLDEN, name, "manifest.json")))


def gunzip(path):
	with gzip.open(path, "rb") as f:
		return f.read()


def run_attribution(events, power, out_dir):
	"""Attribute with THIS package through its command-line entry point."""
	out = subprocess.run(
		[sys.executable, "-m", "agent_energy_profiler.attribution",
		 "--events", events, "--power", power,
		 "--out", os.path.join(out_dir, "energy_summary.csv"),
		 "--out-events", os.path.join(out_dir, "events_attributed.jsonl")],
		capture_output=True, text=True, timeout=1800)
	return out


class TestGoldenFixtures(unittest.TestCase):

	def test_cases_exist(self):
		self.assertGreaterEqual(len(cases()), 4)

	def test_package_under_test_is_this_repository(self):
		src = os.path.join(os.path.dirname(HERE), "src", "agent_energy_profiler")
		self.assertEqual(os.path.realpath(os.path.dirname(agent_energy_profiler.__file__)),
		                 os.path.realpath(src))

	def test_expected_artifacts_match_their_manifest(self):
		for case in cases():
			with open(os.path.join(GOLDEN, case, "manifest.json")) as f:
				manifest = json.load(f)
			for artifact in ARTIFACTS:
				with self.subTest(case=case, artifact=artifact):
					data = gunzip(os.path.join(GOLDEN, case, "expected", artifact + ".gz"))
					self.assertEqual(hashlib.sha256(data).hexdigest(),
					                 manifest["expected_sha256"][artifact])

	def test_attribution_reproduces_the_reference_byte_for_byte(self):
		for case in cases():
			with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
				events = os.path.join(tmp, "events.jsonl")
				power = os.path.join(tmp, "power.csv")
				for name, path in (("events.jsonl", events), ("power.csv", power)):
					with open(path, "wb") as f:
						f.write(gunzip(os.path.join(GOLDEN, case, "input", name + ".gz")))
				result = run_attribution(events, power, tmp)
				self.assertEqual(result.returncode, 0, result.stderr)
				for artifact in ARTIFACTS:
					expected = gunzip(os.path.join(GOLDEN, case, "expected",
					                               artifact + ".gz"))
					with open(os.path.join(tmp, artifact), "rb") as f:
						actual = f.read()
					self.assertTrue(actual == expected,
					                f"{case}/{artifact} differs from the reference")


# --------------------------------------------------------------------------
# opt-in: complete frozen research runs
# --------------------------------------------------------------------------

#: Frozen runs checked when AEP_FROZEN_RUNS_DIR is set, with the fields that
#: may legitimately be ABSENT from the frozen artifact because the run was
#: attributed before the source implementation began emitting them. Every
#: field the frozen artifact does carry must match exactly.
FROZEN_RUNS = {
	"full_cor-rank-compact_webqsp": {},
	"full_pog-rank-cache_cwq": {},
	"gemma4b_full_tog_cwq_0918_2049": {
		"event": {"clock_anomaly"},
		"trajectory": {"clock_anomaly", "n_reversed_events",
		               "power_backsteps_in_window"},
		"summary": {"clock_anomaly", "n_clock_anomaly_trajectories",
		            "n_reversed_events", "power_backward_steps"},
	},
}


def compare_records(frozen, fresh, additive):
	"""Problems found comparing one frozen record with its re-attribution."""
	problems = []
	for key, value in frozen.items():
		if key not in fresh:
			problems.append(f"missing {key!r}")
		elif fresh[key] != value:
			problems.append(f"{key!r}: {value!r} != {fresh[key]!r}")
	for key in fresh:
		if key not in frozen and key not in additive:
			problems.append(f"unexpected new field {key!r}")
	return problems


@pytest.mark.frozen_runs
class TestFrozenRuns(unittest.TestCase):

	def setUp(self):
		self.root = os.environ.get("AEP_FROZEN_RUNS_DIR", "")
		if not self.root:
			self.skipTest("AEP_FROZEN_RUNS_DIR not set")

	def test_frozen_runs_reattribute_identically(self):
		for run, additive in FROZEN_RUNS.items():
			run_dir = os.path.join(self.root, run)
			if not os.path.isdir(run_dir):
				self.skipTest(f"{run} not present under {self.root}")
			with self.subTest(run=run), tempfile.TemporaryDirectory() as tmp:
				result = run_attribution(os.path.join(run_dir, "events.jsonl"),
				                         os.path.join(run_dir, "power.csv"), tmp)
				self.assertEqual(result.returncode, 0, result.stderr)

				with open(os.path.join(run_dir, "energy_summary.csv"), "rb") as f:
					frozen_summary = f.read()
				with open(os.path.join(tmp, "energy_summary.csv"), "rb") as f:
					self.assertTrue(f.read() == frozen_summary, "energy_summary.csv")

				with open(os.path.join(run_dir, "events_attributed.jsonl")) as a, \
				     open(os.path.join(tmp, "events_attributed.jsonl")) as b:
					frozen_rows, fresh_rows = a.readlines(), b.readlines()
				self.assertEqual(len(frozen_rows), len(fresh_rows))
				for i, (x, y) in enumerate(zip(frozen_rows, fresh_rows)):
					problems = compare_records(json.loads(x), json.loads(y),
					                           additive.get("event", set()))
					self.assertEqual(problems, [], f"event line {i}")

				with open(os.path.join(run_dir, "trajectory_summary.json")) as f:
					frozen_traj = json.load(f)
				with open(os.path.join(tmp, "trajectory_summary.json")) as f:
					fresh_traj = json.load(f)
				self.assertEqual(compare_records(frozen_traj["summary"],
				                                 fresh_traj["summary"],
				                                 additive.get("summary", set())), [])
				self.assertEqual(len(frozen_traj["trajectories"]),
				                 len(fresh_traj["trajectories"]))
				for x, y in zip(frozen_traj["trajectories"], fresh_traj["trajectories"]):
					self.assertEqual(compare_records(x, y, additive.get("trajectory", set())),
					                 [], x.get("question_id"))


if __name__ == "__main__":
	unittest.main()
