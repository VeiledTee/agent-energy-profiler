"""Does the M5 sampler configuration reduce counter interference? (M4 method)

The same design as experiment.py, but what runs inside each 8 s window is a
real `aep sample` process in one of these configurations:

    no_sampler          nothing reads NVML inside the window
    legacy_10hz         --hz 10 (counter read on every row: the original default)
    boundaries_10hz     --hz 10 --counter-hz 0   (direct / sampled modes)
    boundaries_50hz     --hz 50 --counter-hz 0   (faster averaged power)
    hybrid_1hz          --hz 10 --counter-hz 1   (hybrid mode)

This process reads the counter only at the window's two endpoints. The
sampler starts before the window and stops after it, so its own boundary
counter reads fall outside the window. Counter rate divided by the
no_sampler trial of the same block is the retained fraction.

  .venv/bin/python tools/nvml_diagnostics/sampler_interference.py \
      --out results/nvml_m5/sampler_interference.json
"""

import argparse
import json
import os
import random
import statistics
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(HERE)), "src"))

from experiment import Gpu, host_metadata  # noqa: E402

from agent_energy_profiler.sampling import Sampler  # noqa: E402

CONDITIONS = {
	"no_sampler": None,
	"legacy_10hz": {"hz": 10.0, "counter_hz": None},
	"boundaries_10hz": {"hz": 10.0, "counter_hz": 0.0},
	"boundaries_50hz": {"hz": 50.0, "counter_hz": 0.0},
	"hybrid_1hz": {"hz": 10.0, "counter_hz": 1.0},
}


def main():
	ap = argparse.ArgumentParser()
	ap.add_argument("--out", required=True)
	ap.add_argument("--reps", type=int, default=5)
	ap.add_argument("--window", type=float, default=8.0)
	ap.add_argument("--rest", type=float, default=3.0)
	ap.add_argument("--seed", type=int, default=20261016)
	args = ap.parse_args()
	gpu = Gpu(0)
	rng = random.Random(args.seed)
	trials = []
	work = tempfile.mkdtemp(prefix="aep-interference-")
	for block in range(args.reps):
		order = list(CONDITIONS)
		rng.shuffle(order)
		for position, cond in enumerate(order):
			time.sleep(args.rest)
			cfg = CONDITIONS[cond]
			sampler = None
			if cfg is not None:
				sampler = Sampler(os.path.join(work, f"{block}_{cond}.csv"), hz=cfg["hz"],
				                  counter_hz=cfg["counter_hz"])
				sampler.start()
				time.sleep(0.5)              # past the sampler's first-row reads
			a0 = time.perf_counter()
			e0 = gpu.energy()
			time.sleep(args.window)
			e1 = gpu.energy()
			a1 = time.perf_counter()
			if sampler is not None:
				sampler.stop()
			state = gpu.state()
			watts = (e1 - e0) / 1000.0 / (a1 - a0)
			trials.append({"block": block, "position": position, "condition": cond,
			               "window_s": a1 - a0, "counter_w": watts, "state_after": state})
			print(f"block {block} {cond:<16} {watts:7.2f} W  pstate {state.get('pstate')}",
			      flush=True)

	summary = {}
	base = {t["block"]: t["counter_w"] for t in trials if t["condition"] == "no_sampler"}
	pstate = {t["block"]: t["state_after"].get("pstate") for t in trials
	          if t["condition"] == "no_sampler"}
	for cond in CONDITIONS:
		rel = [t["counter_w"] / base[t["block"]] for t in trials
		       if t["condition"] == cond and t["state_after"].get("pstate") == pstate[t["block"]]]
		mean = statistics.fmean(rel) if rel else None
		half = (2.776 * statistics.stdev(rel) / len(rel) ** 0.5) if len(rel) == 5 else (
			statistics.stdev(rel) * 2 / len(rel) ** 0.5 if len(rel) > 1 else None)
		summary[cond] = {"n": len(rel), "retained_mean": mean, "ci95_approx": half,
		                 "retained_per_block": [round(r, 4) for r in rel]}
		print(f"{cond:<16} retained {mean:.3f} ± {half if half is not None else float('nan'):.3f} "
		      f"(n={len(rel)})")
	os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
	with open(args.out, "w") as f:
		json.dump({"experiment": "sampler_interference", "args": vars(args),
		           "conditions": CONDITIONS, "gpu": gpu.metadata(), "host": host_metadata(),
		           "summary": summary, "trials": trials}, f, indent=2)


if __name__ == "__main__":
	main()
