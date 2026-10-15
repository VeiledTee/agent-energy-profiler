"""Reproducible attribution benchmark.

Generates a deterministic synthetic run (seeded), attributes it with the
package under src/ and, optionally, with a baseline source tree, and reports
wall-clock time and whether the four artifacts are byte-identical.

The synthetic run exercises every attribution path the frozen research runs
do not: two GPUs with cumulative counters and power columns, RAPL
package (two sockets), core and dram counters, events without an in-band GPU
reading (power-integration fallback) and sporadic empty cells. Its timeline
is monotonic and its counters never wrap or go missing as a whole column, so
the M1.1/M1.2 corrections do not change its output and a baseline comparison
is meaningful.

Usage:
  .venv/bin/python tools/benchmark_attribution.py --events 230000
  git archive 69a7e24 src | tar -x -C build/baseline
  .venv/bin/python tools/benchmark_attribution.py --events 10000 \
      --baseline-src build/baseline/src

All files are written under --work-dir (default build/bench/synthetic, which
is git-ignored).
"""

import argparse
import json
import os
import random
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
ARTIFACTS = ("events_attributed.jsonl", "energy_summary.csv",
             "trajectory_summary.csv", "trajectory_summary.json")


def generate(n_events, seed, events_path, power_path):
	"""Write a synthetic events.jsonl and power.csv. Deterministic in seed."""
	rng = random.Random(seed)
	t = 1_000_000.0
	events = []
	q = 0
	for i in range(n_events):
		if i % 65 == 0:
			q += 1
			step = 0
		duration = rng.uniform(0.005, 0.5)
		label = rng.choice(("llm:reason", "llm:answer", "kg:query", "kg:lookup"))
		events.append({
			"schema_version": 1, "event_id": f"bench-{i:07d}",
			"run_id": "bench", "question_id": f"q{q:05d}",
			"dataset": "synthetic", "paradigm": "synthetic",
			"iteration": step // 4, "traversal_depth": step % 3,
			"step_index": step, "operation_type": label.split(":")[0],
			"operation_label": label,
			"start_timestamp": round(t, 6), "end_timestamp": round(t + duration, 6),
			"duration_s": duration,
			# 80% carry an in-band counter delta; the rest use the fallback.
			"gpu_energy_j": round(duration * 150.0, 4) if rng.random() < 0.8 else None,
			"cpu_package_energy_j": None, "dram_energy_j": None,
			"input_tokens": None, "output_tokens": None, "status": "ok",
			"model_name": "bench", "model_revision": None, "git_commit": None,
			"hardware_id": None, "meta": {},
		})
		step += 1
		t += duration + rng.uniform(0.0, 0.1)
	end = t + 1.0

	with open(events_path, "w") as f:
		for e in events:
			f.write(json.dumps(e) + "\n")

	header = ["t", "gpu0_w", "gpu1_w", "gpu0_energy_mj", "gpu1_energy_mj",
	          "rapl_package-0_intel-rapl:0_uj", "rapl_package-1_intel-rapl:1_uj",
	          "rapl_core_intel-rapl:0:0_uj", "rapl_dram_intel-rapl:0:2_uj"]
	acc = [0.0] * 6
	rates = (0.15e3, 0.08e3, 30e6 / 10, 25e6 / 10, 12e6 / 10, 3e6 / 10)
	with open(power_path, "w") as f:
		f.write(",".join(header) + "\n")
		ts = 1_000_000.0 - 1.0
		while ts <= end:
			w0 = rng.uniform(60, 300)
			w1 = rng.uniform(40, 200)
			for k in range(6):
				acc[k] += rates[k] * rng.uniform(0.5, 1.5)
			cells = [f"{ts:.3f}", f"{w0:.2f}", f"{w1:.2f}"]
			cells += [str(int(a)) for a in acc]
			if rng.random() < 0.0005:   # an unreadable cell somewhere
				cells[rng.randrange(1, len(cells))] = ""
			f.write(",".join(cells) + "\n")
			ts += 0.1
	return len(events)


def attribute(src, events, power, out_dir):
	os.makedirs(out_dir, exist_ok=True)
	env = dict(os.environ, PYTHONPATH=os.path.abspath(src),
	           PYTHONDONTWRITEBYTECODE="1")
	started = time.perf_counter()
	out = subprocess.run(
		[sys.executable, "-m", "agent_energy_profiler.attribution",
		 "--events", events, "--power", power,
		 "--out", os.path.join(out_dir, "energy_summary.csv"),
		 "--out-events", os.path.join(out_dir, "events_attributed.jsonl")],
		capture_output=True, text=True, env=env)
	elapsed = time.perf_counter() - started
	if out.returncode != 0:
		raise SystemExit(f"attribution failed ({src}):\n{out.stderr}")
	return elapsed


def main():
	ap = argparse.ArgumentParser()
	ap.add_argument("--events", type=int, default=230_000)
	ap.add_argument("--seed", type=int, default=20261015)
	ap.add_argument("--work-dir", default=os.path.join(ROOT, "build", "bench", "synthetic"))
	ap.add_argument("--src", default=os.path.join(ROOT, "src"))
	ap.add_argument("--baseline-src", default="",
	                help="a source tree to compare against (e.g. an extracted "
	                     "earlier commit)")
	args = ap.parse_args()

	os.makedirs(args.work_dir, exist_ok=True)
	events = os.path.join(args.work_dir, "events.jsonl")
	power = os.path.join(args.work_dir, "power.csv")
	n = generate(args.events, args.seed, events, power)
	with open(power) as f:
		n_samples = sum(1 for _ in f) - 1
	print(f"synthetic run: {n} events, {n_samples} power samples, seed {args.seed}")

	current = attribute(args.src, events, power, os.path.join(args.work_dir, "current"))
	print(f"current  ({args.src}): {current:.2f} s")
	if args.baseline_src:
		base = attribute(args.baseline_src, events, power,
		                 os.path.join(args.work_dir, "baseline"))
		print(f"baseline ({args.baseline_src}): {base:.2f} s  "
		      f"(speed-up x{base / current:.1f})")
		for artifact in ARTIFACTS:
			with open(os.path.join(args.work_dir, "current", artifact), "rb") as a, \
			     open(os.path.join(args.work_dir, "baseline", artifact), "rb") as b:
				same = a.read() == b.read()
			print(f"  {artifact:<26} {'identical' if same else 'DIFFERS'}")


if __name__ == "__main__":
	main()
