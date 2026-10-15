"""Build the golden equivalence fixtures under tests/golden/.

Run once, at extraction time, against the source research repository. Nothing
in the source repository is written: its run artifacts are only read, and its
attribution code is executed with bytecode writing disabled, writing only into
a temporary directory whose results are copied here.

For each case the builder writes, gzip-compressed and deterministic:

  input/events.jsonl.gz      raw events (a slice of a frozen run, or synthetic)
  input/power.csv.gz         the hardware timeline covering those events
  expected/*.gz              the four attribution artifacts as produced by the
                             SOURCE implementation from exactly these inputs
  manifest.json              provenance: source run, source file hashes,
                             questions kept, redactions, reference commit

tests/test_golden_equivalence.py then re-runs attribution with THIS package and
requires byte-identical artifacts.

Usage:
  python tools/build_golden_fixtures.py \
      --source-root /path/to/Chain-of-Relations-Emissions \
      --reference-python /path/to/Chain-of-Relations-Emissions/.venv/bin/python
"""

import argparse
import gzip
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
GOLDEN = os.path.join(os.path.dirname(HERE), "tests", "golden")

#: Frozen runs sliced into fixtures, one per paradigm. The ToG run predates
#: the clock-anomaly fields; its slice is re-attributed by the reference
#: implementation, so the expected artifacts still match current behaviour.
REAL_CASES = {
	"cor_webqsp": "full_cor-rank-compact_webqsp",
	"tog_cwq": "gemma4b_full_tog_cwq_0918_2049",
	"pog_cwq": "full_pog-rank-cache_cwq",
}
N_QUESTIONS = 5
#: Seconds of hardware timeline kept either side of the sliced events.
POWER_MARGIN_S = 1.0

ARTIFACTS = ("events_attributed.jsonl", "energy_summary.csv",
             "trajectory_summary.csv", "trajectory_summary.json")

#: The only identifying value in the events: the measurement host's hostname,
#: the first half of hardware_id ("<hostname>|<gpu models>").
REDACTED_HOST = "redacted-host"


def sha256(path):
	h = hashlib.sha256()
	with open(path, "rb") as f:
		for block in iter(lambda: f.read(1 << 20), b""):
			h.update(block)
	return h.hexdigest()


def write_gz(path, data):
	os.makedirs(os.path.dirname(path), exist_ok=True)
	with open(path, "wb") as raw:
		with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz:
			gz.write(data)


def redact(event):
	hw = event.get("hardware_id")
	if isinstance(hw, str) and "|" in hw:
		event["hardware_id"] = REDACTED_HOST + "|" + hw.split("|", 1)[1]
	return event


def slice_run(run_dir):
	"""First N_QUESTIONS questions' events (file order) plus covering power rows."""
	kept, questions = [], []
	with open(os.path.join(run_dir, "events.jsonl")) as f:
		for line in f:
			if not line.strip():
				continue
			event = json.loads(line)
			q = event.get("question_id")
			if q not in questions:
				if len(questions) == N_QUESTIONS:
					break
				questions.append(q)
			kept.append(event)
	t0 = min(e["start_timestamp"] for e in kept) - POWER_MARGIN_S
	t1 = max(e["end_timestamp"] for e in kept) + POWER_MARGIN_S
	with open(os.path.join(run_dir, "power.csv")) as f:
		header = f.readline()
		rows = [line for line in f
		        if line.strip() and t0 <= float(line.split(",", 1)[0]) <= t1]
	events_text = "".join(json.dumps(redact(e)) + "\n" for e in kept)
	return events_text, header + "".join(rows), questions, len(kept), len(rows)


def synthetic_case():
	"""A small timeline exercising the paths the real runs never reached.

	The frozen runs come from a GPU-only host (no RAPL). This case adds
	package/core/dram/psys/uncore RAPL columns, a second GPU, events with no
	in-band GPU counter (power-integration fallback), a failed event, a nested
	(overlapping) pair, an empty power cell and a backward wall-clock step.
	"""
	header = ["t", "gpu0_w", "gpu1_w", "gpu0_energy_mj", "gpu1_energy_mj",
	          "rapl_package-0_intel-rapl:0_uj", "rapl_core_intel-rapl:0:0_uj",
	          "rapl_uncore_intel-rapl:0:1_uj", "rapl_dram_intel-rapl:0:2_uj",
	          "rapl_psys_intel-rapl:1_uj"]
	rows = []
	for i in range(61):
		t = 1000.0 + 0.1 * i
		if i == 40:
			t = 1003.85  # wall clock stepped back 0.05 s
		g0 = 50.0 + (i % 7) * 10.0
		g1 = 30.0 + (i % 5) * 5.0
		rows.append(",".join([
			f"{t:.3f}", f"{g0:.1f}", "" if i == 13 else f"{g1:.1f}",
			str(10_000_000 + 6_000 * i), str(20_000_000 + 3_500 * i),
			str(5_000_000 + 2_500_000 * i), str(3_000_000 + 1_500_000 * i),
			str(1_000_000 + 400_000 * i), str(2_000_000 + 700_000 * i),
			str(9_000_000 + 4_000_000 * i)]))
	power = ",".join(header) + "\n" + "\n".join(rows) + "\n"

	def ev(i, q, label, t0, t1, gpu=None, status="ok", depth=0, it=0, **meta):
		llm = label.startswith("llm")
		return {
			"schema_version": 1, "event_id": f"synthetic-{i:03d}",
			"run_id": "synthetic-run", "question_id": q, "dataset": "synthetic",
			"paradigm": "synthetic", "iteration": it, "traversal_depth": depth,
			"step_index": i, "operation_type": label.split(":")[0],
			"operation_label": label, "start_timestamp": t0, "end_timestamp": t1,
			"duration_s": t1 - t0, "gpu_energy_j": gpu,
			"cpu_package_energy_j": None, "dram_energy_j": None,
			"input_tokens": 100 if llm else None,
			"output_tokens": 10 if llm else None,
			"status": status, "model_name": "synthetic-model",
			"model_revision": None, "git_commit": None, "hardware_id": None,
			"meta": meta,
		}

	events = [
		ev(0, "q1", "tool:search", 1000.20, 1000.25, gpu=0.0, attempts=1),
		ev(1, "q1", "llm:plan", 1000.30, 1001.40, gpu=61.25, attempts=1),
		ev(2, "q1", "llm:plan", 1001.50, 1002.05, gpu=None, attempts=2),
		ev(3, "q1", "tool:search", 1002.10, 1002.12, gpu=0.0, status="error",
		   attempts=3, it=1, depth=1),
		ev(4, "q2", "llm:answer", 1002.50, 1003.70, gpu=80.5),
		ev(5, "q2", "tool:lookup", 1003.00, 1003.20, gpu=12.0),  # nested in 4
		ev(6, "q2", "llm:answer", 1003.75, 1003.95, gpu=None, it=1, depth=None),
		ev(7, "q2", "tool:lookup", 1004.10, 1005.80, gpu=None, it=2, depth=1),
	]
	return "".join(json.dumps(e) + "\n" for e in events), power


def attribute_with_reference(python, root, events_text, power_text):
	"""Run the SOURCE implementation's attribution on the given inputs."""
	with tempfile.TemporaryDirectory() as tmp:
		events = os.path.join(tmp, "events.jsonl")
		power = os.path.join(tmp, "power.csv")
		with open(events, "w") as f:
			f.write(events_text)
		with open(power, "w") as f:
			f.write(power_text)
		env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=root)
		which = subprocess.run(
			[python, "-c",
			 "import agent_energy_profiler.attribution as a; print(a.__file__)"],
			cwd=root, env=env, capture_output=True, text=True, check=True,
		).stdout.strip()
		expected_dir = os.path.join(root, "agent_energy_profiler")
		if not os.path.realpath(which).startswith(expected_dir + os.sep):
			raise SystemExit(f"reference attribution resolved to {which}, "
			                 f"not inside {expected_dir}")
		out = subprocess.run(
			[python, "-m", "agent_energy_profiler.attribution",
			 "--events", events, "--power", power,
			 "--out", os.path.join(tmp, "energy_summary.csv"),
			 "--out-events", os.path.join(tmp, "events_attributed.jsonl")],
			cwd=root, env=env, capture_output=True, text=True)
		if out.returncode != 0:
			raise SystemExit(f"reference attribution failed:\n{out.stderr}")
		result = {}
		for name in ARTIFACTS:
			with open(os.path.join(tmp, name), "rb") as f:
				result[name] = f.read()
		return result


def source_state(root):
	commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root,
	                        capture_output=True, text=True).stdout.strip()
	dirty = subprocess.run(["git", "status", "--porcelain"], cwd=root,
	                       capture_output=True, text=True).stdout.strip()
	return commit, bool(dirty)


def write_case(name, events_text, power_text, expected, manifest):
	case = os.path.join(GOLDEN, name)
	if os.path.isdir(case):
		shutil.rmtree(case)
	write_gz(os.path.join(case, "input", "events.jsonl.gz"), events_text.encode())
	write_gz(os.path.join(case, "input", "power.csv.gz"), power_text.encode())
	for artifact, data in expected.items():
		write_gz(os.path.join(case, "expected", artifact + ".gz"), data)
	manifest["expected_sha256"] = {a: hashlib.sha256(d).hexdigest()
	                               for a, d in sorted(expected.items())}
	with open(os.path.join(case, "manifest.json"), "w") as f:
		json.dump(manifest, f, indent=2, sort_keys=True)
		f.write("\n")


def main():
	ap = argparse.ArgumentParser()
	ap.add_argument("--source-root", required=True)
	ap.add_argument("--reference-python", required=True)
	args = ap.parse_args()
	root = os.path.realpath(args.source_root)
	commit, dirty = source_state(root)
	if dirty:
		raise SystemExit("source repository has uncommitted changes; refusing")
	common = {"reference_implementation":
	          "Chain-of-Relations-Emissions agent_energy_profiler.attribution",
	          "reference_commit": commit}

	for name, run in REAL_CASES.items():
		run_dir = os.path.join(root, "measurement", "runs", run)
		events_text, power_text, questions, n_events, n_power = slice_run(run_dir)
		expected = attribute_with_reference(args.reference_python, root,
		                                    events_text, power_text)
		write_case(name, events_text, power_text, expected, dict(common, **{
			"kind": "frozen-run slice",
			"source_run": f"measurement/runs/{run}",
			"source_sha256": {f: sha256(os.path.join(run_dir, f))
			                  for f in ("events.jsonl", "power.csv")},
			"question_ids": questions,
			"n_events": n_events,
			"n_power_rows": n_power,
			"power_margin_s": POWER_MARGIN_S,
			"redactions": ["hardware_id hostname -> " + REDACTED_HOST],
		}))
		print(f"{name}: {len(questions)} questions, {n_events} events, "
		      f"{n_power} power rows")

	events_text, power_text = synthetic_case()
	expected = attribute_with_reference(args.reference_python, root,
	                                    events_text, power_text)
	write_case("synthetic_rapl_multigpu", events_text, power_text, expected,
	           dict(common, kind="synthetic", source_run=None, redactions=[],
	                note="exercises RAPL, multi-GPU, power-integration fallback, "
	                     "overlap and clock back-step paths absent from the "
	                     "GPU-only frozen runs"))
	print("synthetic_rapl_multigpu: written")


if __name__ == "__main__":
	sys.exit(main())
