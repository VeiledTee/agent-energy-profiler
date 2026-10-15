"""Whole-machine power sampler. Run on the HOST (not in a container).

Samples at ~10 Hz:
  - GPU cumulative energy (mJ) via NVML        -> the GPU measurement instrument
  - GPU power (W) via NVML, per GPU            -> diagnostic only
  - CPU package + DRAM energy (uJ) via RAPL    -> bare-metal Linux only
    (silently absent on WSL2 / locked HPC nodes; GPU columns still logged)

Output CSV columns:
  t, gpu<i>_w ..., gpu<i>_energy_mj ..., rapl_<domain>_uj ...

Every energy column is a CUMULATIVE counter that attribution.py differences
across a window. gpu<i>_w is instantaneous power, retained for diagnostics
(mean/peak power, ramp analysis, time-series plots) and NOT used as the
primary trajectory energy reference: integrating ~10 Hz point samples resolves
fast inference power transients poorly and errs in both directions, with error
growing as windows shorten. That previously produced attribution coverage > 1
on short trajectories.

Beside the CSV it writes <out>.meta.json, recording each RAPL counter's own
max_energy_range_uj so attribution can unwrap wraparound without assuming a
range (null where the host does not declare one).

Importing this module touches no hardware. NVML is initialised and RAPL zones
are enumerated on first use (init(), called by build_header, warn_about_gaps
and main), and main shuts NVML down on exit.

Usage:
  python -m agent_energy_profiler.sampling --out power.csv   # Ctrl-C to stop
  python -m agent_energy_profiler.sampling --out power.csv --hz 10
"""

import argparse
import csv
import json
import signal
import sys
import time

from .attribution import POWER_META_FORMAT, power_meta_path
from .hardware import nvml, rapl

_RAPL = None


def init():
	"""Initialise NVML and enumerate RAPL zones, once. Returns the zone paths."""
	global _RAPL
	nvml.init()
	if _RAPL is None:
		_RAPL = rapl.discover_zones()
	return _RAPL


def power_meta(rapl_paths, hz=None, counter_hz=None):
	"""Metadata sidecar content for a power log with these RAPL counters.

	Version 2 adds `timeline` (what was read and how often); version-1
	readers ignore it.
	"""
	meta = {
		"format": POWER_META_FORMAT,
		"version": 2,
		"rapl_max_energy_range_uj": rapl.column_ranges(rapl_paths),
	}
	if hz is not None:
		meta["timeline"] = timeline_meta(hz, counter_hz)
	return meta


def build_header():
	"""CSV header for the columns this host can actually produce."""
	zones = init()
	n = nvml.device_count()
	# Two GPU column families, deliberately distinct:
	#   gpu<i>_w          instantaneous power (W)  -- DIAGNOSTIC only
	#   gpu<i>_energy_mj  cumulative energy (mJ)   -- the measurement instrument
	# The cumulative counter is the same hardware register the in-band event
	# log reads, so trajectory totals and per-event energy come from one
	# instrument. Integrated sampled power is never the primary reference.
	return (["t"]
	        + [f"gpu{i}_w" for i in range(n)]
	        + [f"gpu{i}_energy_mj" for i in range(n)]
	        + rapl.column_names(zones))


#: Counter policies (recorded in the sidecar as timeline.counter_policy):
#:   every_row     read the cumulative energy counter on every row (the
#:                 original behaviour, still the default of `aep sample`)
#:   every_n_rows  read it on every n-th row and on the final row
#:   boundaries    read it only on the first and the final row
#: Power is read on every row in all policies. Milestone 4 found, on one
#: WSL2 host, that each counter read lowers the counter's accumulation while
#: averaged-power reads do not (docs/NVML_COUNTER_VALIDATION.md).
EVERY_ROW = "every_row"
EVERY_N_ROWS = "every_n_rows"
BOUNDARIES = "boundaries"


def counter_policy(hz, counter_hz):
	"""(policy, n) for a row rate and a requested counter rate.

	counter_hz None or >= hz: every row. 0: boundaries only. Otherwise every
	n-th row with n = round(hz / counter_hz), so the effective rate is hz / n.
	"""
	if counter_hz is None or counter_hz >= hz:
		return EVERY_ROW, 1
	if counter_hz <= 0:
		return BOUNDARIES, None
	return EVERY_N_ROWS, max(1, round(hz / counter_hz))


def timeline_meta(hz, counter_hz):
	"""The sidecar's description of what was read, and how often."""
	policy, n = counter_policy(hz, counter_hz)
	return {
		"power_hz": hz,
		"power_api": "nvmlDeviceGetPowerUsage (driver-averaged board power)",
		"counter_policy": policy,
		"counter_every_n_rows": n,
		"counter_hz_effective": (hz if policy == EVERY_ROW else
		                         0.0 if policy == BOUNDARIES else hz / n),
		"counter_api": "nvmlDeviceGetTotalEnergyConsumption",
		"row_timestamp": "time.time() taken before the row's reads",
	}


def sample_row(handles, rapl_paths, read_counter=True):
	"""One CSV row. An unreadable counter writes an empty cell, never a zero.

	With read_counter False the energy cells are left empty: not read, which
	every reader already treats as missing, never as zero.
	"""
	row = [f"{time.time():.3f}"]
	for h in handles:
		watts = nvml.device_power_w(h)
		row.append(watts if watts is not None else "")
	for h in handles:
		energy = nvml.device_energy_mj(h) if read_counter else None
		row.append(energy if energy is not None else "")
	for path in rapl_paths:
		value = rapl.read_zone(path)
		row.append(value if value is not None else "")
	return row


def warn_about_gaps():
	zones = init()
	if nvml.device_count() == 0:
		print("WARNING: no NVML GPUs visible", file=sys.stderr)
	elif not nvml.energy_counter_supported():
		print("WARNING: NVML present but cumulative GPU energy unavailable -"
		      " trajectory GPU energy will be reported as unavailable, not"
		      " silently substituted with integrated power", file=sys.stderr)
	if not zones:
		print("WARNING: no RAPL domains readable (WSL2/shared node/permissions?)"
		      " - GPU-only logging", file=sys.stderr)


class Sampler:
	"""Explicit lifecycle for the sampler, run as a separate process.

	A subprocess rather than a thread: the sampler then never competes for the
	measured program's GIL, and it is the same tested code path as
	`aep-sample`. start() returns once the first sample row is on disk, so a
	span opened afterwards lies inside the timeline. stop() waits for one
	sample stamped after the stop request (so the last span is covered too),
	then sends SIGTERM, which the sampler handles by finishing cleanly.

	Its stderr (hardware warnings) goes to <power_file>.sampler.log.
	"""

	def __init__(self, power_file, hz=10.0, python=None, env=None,
	             startup_timeout_s=15.0, stop_timeout_s=10.0, counter_hz=None):
		if not hz > 0:
			raise ValueError("hz must be positive")
		if counter_hz is not None and counter_hz < 0:
			raise ValueError("counter_hz must be >= 0 (0 = boundaries only)")
		self.power_file = power_file
		self.hz = float(hz)
		#: None: counter on every row (the original behaviour); 0: first and
		#: final row only; otherwise roughly this rate (see counter_policy)
		self.counter_hz = None if counter_hz is None else float(counter_hz)
		self.python = python or sys.executable
		self.env = env
		self.startup_timeout_s = startup_timeout_s
		self.stop_timeout_s = stop_timeout_s
		self._proc = None
		self._log = None
		self.exit_code = None

	def describe(self):
		out = {"power_file": self.power_file, "hz": self.hz,
		       "meta_file": power_meta_path(self.power_file),
		       "process": "subprocess"}
		if self.counter_hz is not None:
			out["timeline"] = timeline_meta(self.hz, self.counter_hz)
		return out

	@property
	def running(self):
		return self._proc is not None and self._proc.poll() is None

	def _last_timestamp(self):
		"""Timestamp of the last complete data row, or None."""
		try:
			with open(self.power_file, "rb") as f:
				data = f.read()
		except OSError:
			return None
		lines = data.split(b"\n")
		# a complete row ends with a newline, so it is never the last element
		for line in reversed(lines[1:-1]):
			try:
				return float(line.split(b",", 1)[0])
			except ValueError:
				continue
		return None

	def _wait_for(self, predicate, timeout):
		deadline = time.monotonic() + timeout
		while time.monotonic() < deadline:
			if predicate():
				return True
			if self._proc.poll() is not None:
				return predicate()
			time.sleep(0.01)
		return predicate()

	def start(self):
		import subprocess
		if self._proc is not None:
			raise RuntimeError("sampler already started")
		self._log = open(self.power_file + ".sampler.log", "w")
		cmd = [self.python, "-m", "agent_energy_profiler.sampling",
		       "--out", self.power_file, "--hz", repr(self.hz)]
		if self.counter_hz is not None:
			cmd += ["--counter-hz", repr(self.counter_hz)]
		self._proc = subprocess.Popen(cmd, stdout=self._log, stderr=self._log, env=self.env)
		if not self._wait_for(lambda: self._last_timestamp() is not None,
		                      self.startup_timeout_s):
			self.stop()
			raise RuntimeError(f"sampler produced no samples; see "
			                   f"{self.power_file}.sampler.log")
		return self

	def stop(self):
		"""Stop sampling; returns the sampler's exit code. Safe to call twice."""
		import subprocess
		if self._proc is None:
			return self.exit_code
		if self._proc.poll() is None:
			requested = time.time()
			self._wait_for(lambda: (self._last_timestamp() or 0) > requested,
			               self.stop_timeout_s)
			self._proc.terminate()
			try:
				self._proc.wait(timeout=self.stop_timeout_s)
			except subprocess.TimeoutExpired:
				self._proc.kill()
				self._proc.wait()
		self.exit_code = self._proc.returncode
		if self._log is not None:
			self._log.close()
			self._log = None
		return self.exit_code

	def __enter__(self):
		return self.start()

	def __exit__(self, exc_type, exc, tb):
		self.stop()
		return False


def main():
	ap = argparse.ArgumentParser()
	ap.add_argument("--out", required=True)
	ap.add_argument("--hz", type=float, default=10.0,
	                help="row rate: power (and RAPL) is read on every row")
	ap.add_argument("--counter-hz", type=float, default=None,
	                help="GPU energy-counter read rate; default: every row (the "
	                     "original behaviour); 0: first and final row only")
	args = ap.parse_args()

	policy, every_n = counter_policy(args.hz, args.counter_hz)
	period = 1.0 / args.hz
	stop = {"flag": False}
	signal.signal(signal.SIGINT, lambda *_: stop.update(flag=True))
	signal.signal(signal.SIGTERM, lambda *_: stop.update(flag=True))

	try:
		zones = init()
		handles = nvml.handles()
		warn_about_gaps()

		with open(power_meta_path(args.out), "w") as f:
			json.dump(power_meta(zones, args.hz, args.counter_hz), f, indent=2, sort_keys=True)
			f.write("\n")
		with open(args.out, "w", newline="") as f:
			w = csv.writer(f)
			w.writerow(build_header())
			i = 0
			while not stop["flag"]:
				read_counter = (policy == EVERY_ROW or i == 0
				                or (policy == EVERY_N_ROWS and i % every_n == 0))
				w.writerow(sample_row(handles, zones, read_counter))
				f.flush()
				i += 1
				time.sleep(period)
			if policy != EVERY_ROW:
				# a final counter reading closes the measured run window
				w.writerow(sample_row(handles, zones, True))
				f.flush()
	finally:
		nvml.shutdown()
	print(f"power log written: {args.out}")


if __name__ == "__main__":
	main()
