"""Controlled experiment: does reading NVML change the energy it reports?

Every trial measures the cumulative energy counter
(nvmlDeviceGetTotalEnergyConsumption) at its two endpoints, the same way in
every condition, over a window of the same nominal length. What differs
between conditions is only what is read IN BETWEEN:

    endpoints            nothing
    temperature_10hz     nvmlDeviceGetTemperature (control: an unrelated query)
    power_avg_10hz       nvmlDeviceGetPowerUsage (averaged power) every 100 ms
    power_avg_50hz       the same every 20 ms
    power_instant_10hz   field NVML_FI_DEV_POWER_INSTANT every 100 ms
    energy_1hz ... energy_1000hz   the energy counter itself, 1 to 1000 Hz
    energy_field_10hz    the energy counter via nvmlDeviceGetFieldValues
                         (NVML_FI_DEV_TOTAL_ENERGY_CONSUMPTION) every 100 ms
    combined_10hz        power then energy every 100 ms (what the sampler does)

Before every trial there is a rest with no NVML reads at all. Conditions are
shuffled within each repetition block (seeded), so slow drift (temperature,
background activity) spreads across conditions instead of biasing one.

The workload (idle, or tools/nvml_diagnostics/workload.py run by a separate
interpreter with PyTorch) runs in its own process, warmed up before the
first trial, and is identical across conditions within a run.

Everything is recorded raw: each in-window read with the perf_counter time
before and after the call, the endpoint values, post-trial GPU state, and the
host/driver metadata. analyze.py turns that into statistics.

Usage (from the repository root):
  .venv/bin/python tools/nvml_diagnostics/experiment.py --workload idle \
      --out results/nvml_m4/idle
  .venv/bin/python tools/nvml_diagnostics/experiment.py --workload steady \
      --workload-python build/m4-venv/bin/python --out results/nvml_m4/steady
"""

import argparse
import json
import os
import platform
import random
import signal
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))

#: condition -> list of (stream kind, period in seconds)
CONDITIONS = {
	"endpoints": [],
	"temperature_10hz": [("temperature", 0.1)],
	"power_avg_10hz": [("power_avg", 0.1)],
	"power_avg_50hz": [("power_avg", 0.02)],
	"power_instant_10hz": [("power_instant", 0.1)],
	"energy_1hz": [("energy", 1.0)],
	"energy_10hz": [("energy", 0.1)],
	"energy_50hz": [("energy", 0.02)],
	"energy_200hz": [("energy", 0.005)],
	"energy_1000hz": [("energy", 0.001)],
	"energy_field_10hz": [("energy_field", 0.1)],
	"combined_10hz": [("power_avg", 0.1), ("energy", 0.1)],
}


class Gpu:
	def __init__(self, index):
		import pynvml
		self.nv = pynvml
		pynvml.nvmlInit()
		self.h = pynvml.nvmlDeviceGetHandleByIndex(index)
		self.f_energy = getattr(pynvml, "NVML_FI_DEV_TOTAL_ENERGY_CONSUMPTION", 83)
		self.f_instant = getattr(pynvml, "NVML_FI_DEV_POWER_INSTANT", 186)

	def energy(self):
		return self.nv.nvmlDeviceGetTotalEnergyConsumption(self.h)

	def power_avg(self):
		return self.nv.nvmlDeviceGetPowerUsage(self.h)

	def _field(self, fid):
		r = self.nv.nvmlDeviceGetFieldValues(self.h, [fid])[0]
		if r.nvmlReturn != 0:
			return None
		return r.value.ullVal if fid == self.f_energy else r.value.uiVal

	def energy_field(self):
		return self._field(self.f_energy)

	def power_instant(self):
		return self._field(self.f_instant)

	def temperature(self):
		return self.nv.nvmlDeviceGetTemperature(self.h, 0)

	def state(self):
		nv, h = self.nv, self.h
		out = {}
		for key, fn in (("temperature_c", lambda: nv.nvmlDeviceGetTemperature(h, 0)),
		                ("sm_clock_mhz", lambda: nv.nvmlDeviceGetClockInfo(h, nv.NVML_CLOCK_SM)),
		                ("mem_clock_mhz", lambda: nv.nvmlDeviceGetClockInfo(h, nv.NVML_CLOCK_MEM)),
		                ("pstate", lambda: nv.nvmlDeviceGetPerformanceState(h)),
		                ("power_avg_mw", lambda: nv.nvmlDeviceGetPowerUsage(h))):
			try:
				out[key] = fn()
			except Exception as e:  # pragma: no cover - hardware dependent
				out[key] = repr(e)
		return out

	def metadata(self):
		nv, h = self.nv, self.h
		meta = {}
		for key, fn in (("name", lambda: nv.nvmlDeviceGetName(h)),
		                ("driver_version", nv.nvmlSystemGetDriverVersion),
		                ("nvml_version", nv.nvmlSystemGetNVMLVersion),
		                ("cuda_driver_version", nv.nvmlSystemGetCudaDriverVersion),
		                ("power_limit_mw", lambda: nv.nvmlDeviceGetEnforcedPowerLimit(h)),
		                ("default_power_limit_mw", lambda: nv.nvmlDeviceGetPowerManagementDefaultLimit(h))):
			try:
				v = fn()
				meta[key] = v.decode() if isinstance(v, bytes) else v
			except Exception as e:  # pragma: no cover
				meta[key] = repr(e)
		try:
			meta["compute_processes_visible"] = len(nv.nvmlDeviceGetComputeRunningProcesses(h))
		except Exception as e:  # pragma: no cover
			meta["compute_processes_visible"] = repr(e)
		return meta


def host_metadata():
	meta = {"platform": platform.platform(), "python": platform.python_version(),
	        "wsl": "microsoft" in platform.release().lower()}
	try:
		import pynvml
		meta["pynvml_module"] = getattr(pynvml, "__version__", None) or pynvml.__file__
	except Exception:
		pass
	try:
		out = subprocess.run(["nvidia-smi", "--query-gpu=name,driver_version,power.draw,"
		                      "power.draw.average,power.draw.instant,power.limit,pstate",
		                      "--format=csv"], capture_output=True, text=True, timeout=20)
		meta["nvidia_smi"] = out.stdout.strip()
	except Exception as e:
		meta["nvidia_smi"] = repr(e)
	return meta


def run_trial(gpu, condition, duration):
	"""One window: endpoint read, condition reads, endpoint read."""
	streams = [(kind, period, getattr(gpu, kind)) for kind, period in CONDITIONS[condition]]
	reads = {kind: [] for kind, _, _ in streams}
	a0 = time.perf_counter()
	e0 = gpu.energy()
	a1 = time.perf_counter()
	start = (a0 + a1) / 2
	end_target = start + duration
	due = [start + period for _, period, _ in streams]
	while True:
		if streams:
			k = min(range(len(streams)), key=due.__getitem__)
			t_due = due[k]
			if t_due >= end_target or time.perf_counter() >= end_target:
				break
			wait = t_due - time.perf_counter()
			if wait > 0:
				time.sleep(wait)
			# streams due at the same instant are read back to back, in order
			for j, (kind, period, fn) in enumerate(streams):
				if due[j] <= t_due + 1e-9:
					t_before = time.perf_counter()
					try:
						value = fn()
					except Exception:
						value = None
					t_after = time.perf_counter()
					reads[kind].append((round(t_before - start, 7), round(t_after - start, 7), value))
					# a read slower than its period skips missed slots instead
					# of building a backlog that would overrun the window
					due[j] = max(due[j] + period, t_after)
		else:
			wait = end_target - time.perf_counter()
			if wait <= 0:
				break
			time.sleep(min(wait, 0.5))
	wait = end_target - time.perf_counter()
	if wait > 0:
		time.sleep(wait)
	b0 = time.perf_counter()
	e1 = gpu.energy()
	b1 = time.perf_counter()
	return {
		"condition": condition,
		"window_s": (b0 + b1) / 2 - start,
		"endpoint_read_s": [a1 - a0, b1 - b0],
		"energy_start_mj": e0,
		"energy_end_mj": e1,
		"reads": reads,
	}


def main():
	ap = argparse.ArgumentParser(description=__doc__,
	                             formatter_class=argparse.RawDescriptionHelpFormatter)
	ap.add_argument("--workload", choices=("idle", "steady", "pulsed"), required=True)
	ap.add_argument("--workload-python", default="",
	                help="interpreter with PyTorch for the steady/pulsed workload")
	ap.add_argument("--out", required=True)
	ap.add_argument("--device", type=int, default=0)
	ap.add_argument("--duration", type=float, default=8.0)
	ap.add_argument("--rest", type=float, default=3.0)
	ap.add_argument("--reps", type=int, default=4)
	ap.add_argument("--warmup", type=float, default=30.0)
	ap.add_argument("--seed", type=int, default=20261015)
	ap.add_argument("--max-temp", type=int, default=83,
	                help="abort (and stop the workload) at this GPU temperature, deg C")
	ap.add_argument("--conditions", default="", help="comma-separated subset")
	args = ap.parse_args()

	conditions = [c for c in (args.conditions.split(",") if args.conditions else CONDITIONS)]
	for c in conditions:
		if c not in CONDITIONS:
			sys.exit(f"unknown condition {c}")
	os.makedirs(args.out, exist_ok=True)
	trials_path = os.path.join(args.out, "trials.jsonl")
	if os.path.exists(trials_path):
		sys.exit(f"{trials_path} exists; use a new --out")

	gpu = Gpu(args.device)
	worker = None
	if args.workload != "idle":
		if not args.workload_python:
			sys.exit("--workload-python (with PyTorch) is required for GPU workloads")
		worker = subprocess.Popen([args.workload_python, os.path.join(HERE, "workload.py"),
		                           args.workload], stdout=subprocess.PIPE, text=True)
		if worker.stdout.readline().strip() != "ready":
			worker.kill()
			sys.exit("workload failed to start")

	def stop_worker(*_):
		if worker is not None and worker.poll() is None:
			worker.send_signal(signal.SIGTERM)
			worker.wait(timeout=30)

	meta = {
		"experiment": "nvml_counter_read_disturbance",
		"version": 1,
		"started_unix_s": time.time(),
		"args": vars(args),
		"conditions": {c: CONDITIONS[c] for c in conditions},
		"gpu": gpu.metadata(),
		"host": host_metadata(),
	}
	aborted = None
	try:
		time.sleep(args.warmup)  # no NVML reads during warm-up
		meta["state_after_warmup"] = gpu.state()
		rng = random.Random(args.seed)
		with open(trials_path, "w") as f:
			for block in range(args.reps):
				order = list(conditions)
				rng.shuffle(order)
				for position, condition in enumerate(order):
					time.sleep(args.rest)  # no reads before any trial
					trial = run_trial(gpu, condition, args.duration)
					trial.update(workload=args.workload, block=block, position=position,
					             unix_s=time.time(), state_after=gpu.state())
					f.write(json.dumps(trial) + "\n")
					f.flush()
					temp = trial["state_after"].get("temperature_c")
					print(f"block {block} {position:>2} {condition:<20} "
					      f"{(trial['energy_end_mj'] - trial['energy_start_mj']) / 1000 / trial['window_s']:8.2f} W"
					      f"  {temp} C", flush=True)
					if isinstance(temp, int) and temp >= args.max_temp:
						aborted = f"temperature {temp} C >= {args.max_temp} C"
						raise RuntimeError(aborted)
	except RuntimeError:
		print(f"ABORTED: {aborted}", file=sys.stderr)
	finally:
		stop_worker()
		meta["ended_unix_s"] = time.time()
		meta["aborted"] = aborted
		with open(os.path.join(args.out, "meta.json"), "w") as f:
			json.dump(meta, f, indent=2)
	return 1 if aborted else 0


if __name__ == "__main__":
	sys.exit(main())
