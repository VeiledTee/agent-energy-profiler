"""Step test: does integrated GetPowerUsage lag the counter at a load change?

Each trial: GPU idle, then the steady workload starts and a window of
--window seconds begins at the moment it reports ready. Over that window it
compares the counter's endpoint difference with GetPowerUsage read at 10 Hz
(GetPowerUsage did not disturb the counter in experiment.py) and integrated.
A trailing-average power reading under-integrates right after a step up and
over-integrates after a step down; the counter is unaffected by that lag.

  .venv/bin/python tools/nvml_diagnostics/step_response.py \
      --workload-python /path/to/torch-venv/bin/python --out results/x/step.json
"""

import argparse
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from experiment import Gpu, host_metadata  # noqa: E402


def integrate(points, t0, t1):
	pts = [(t, w) for t, w in points if t0 <= t <= t1]
	e = sum(0.5 * (pts[i][1] + pts[i + 1][1]) * (pts[i + 1][0] - pts[i][0])
	        for i in range(len(pts) - 1))
	e += pts[0][1] * (pts[0][0] - t0) + pts[-1][1] * (t1 - pts[-1][0])
	return e


def main():
	ap = argparse.ArgumentParser()
	ap.add_argument("--workload-python", required=True)
	ap.add_argument("--out", required=True)
	ap.add_argument("--trials", type=int, default=5)
	ap.add_argument("--window", type=float, default=3.0)
	ap.add_argument("--idle", type=float, default=8.0)
	args = ap.parse_args()
	gpu = Gpu(0)
	trials = []
	for i in range(args.trials):
		time.sleep(args.idle)                      # idle, no reads
		worker = subprocess.Popen([args.workload_python, os.path.join(HERE, "workload.py"),
		                           "steady"], stdout=subprocess.PIPE, text=True)
		worker.stdout.readline()                   # "ready": the GPU is now busy
		t0 = time.perf_counter()
		e0 = gpu.energy()
		power = []
		while time.perf_counter() - t0 < args.window:
			power.append((time.perf_counter(), gpu.power_avg() / 1000.0))
			time.sleep(0.1)
		e1 = gpu.energy()
		t1 = time.perf_counter()
		worker.terminate()
		worker.wait(timeout=30)
		counter_j = (e1 - e0) / 1000.0
		power_j = integrate(power, t0, t1)
		trials.append({"trial": i, "window_s": t1 - t0, "counter_j": counter_j,
		               "integrated_getpowerusage_j": power_j,
		               "counter_over_power": counter_j / power_j,
		               "power_first_last_w": [power[0][1], power[-1][1]],
		               "power_reads": [[round(t - t0, 4), w] for t, w in power]})
		print(f"trial {i}: counter {counter_j:.1f} J, integrated GetPowerUsage {power_j:.1f} J, "
		      f"ratio {counter_j / power_j:.3f}, power first/last read "
		      f"{power[0][1]:.0f}/{power[-1][1]:.0f} W", flush=True)
	with open(args.out, "w") as f:
		json.dump({"experiment": "step_response", "args": vars(args), "gpu": gpu.metadata(),
		           "host": host_metadata(), "trials": trials}, f, indent=2)


if __name__ == "__main__":
	main()
