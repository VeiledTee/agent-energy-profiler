"""Schema-v2 measurement model: what a counter difference does and does not mean.

Field meanings are defined in docs/SCHEMA_V2.md; this module implements them.

Three things are kept apart here, and must stay apart downstream:

  * the raw readings (preserved verbatim in `raw`);
  * the energy value, which is a counter difference or null -- never an
    estimate and never a substitute for a missing reading;
  * what we know about the counter's resolution, which only qualifies the
    value (`below_resolution`) and never changes it.
"""

import json
import time

MEASURED = "measured"
UNAVAILABLE = "unavailable"
INVALID = "invalid"
ESTIMATED = "estimated"
NOT_DEFENSIBLE = "not_defensible"

#: Every energy value this package reports is whole-device energy.
SCOPE_DEVICE = "device"

GPU_SOURCE = "nvml_total_energy_counter"
IN_BAND = "in_band_counter_delta"
SAMPLED = "sampled_counter_interpolated"

#: Reason codes (docs/SCHEMA_V2.md section 2.5).
REASONS = frozenset({
	"disabled", "no_counter", "read_failed", "counter_decrease",
	"counter_discontinuity", "insufficient_samples", "clock_anomaly",
	"not_covered", "devices_changed",
})


# --------------------------------------------------------------------------
# resolution
# --------------------------------------------------------------------------

class Resolution:
	"""What is known about the GPU counter's update interval, and how.

	`classify(duration)` qualifies a measured value; it never alters it. An
	unknown resolution classifies every duration as unknown (None), never as
	"above resolution".
	"""

	BASES = ("unknown", "configured", "hardware_report", "probed")

	def __init__(self, gpu_update_interval_s=None, basis="unknown", source=None,
	             characterized_at_unix_s=None, updates_observed=None):
		if basis not in self.BASES:
			raise ValueError(f"basis must be one of {self.BASES}")
		interval = gpu_update_interval_s
		if interval is not None:
			if isinstance(interval, bool) or not isinstance(interval, (int, float)) \
			        or not interval > 0 or interval != interval:
				raise ValueError("gpu_update_interval_s must be a positive number")
			interval = float(interval)
		if (interval is None) != (basis == "unknown"):
			raise ValueError("an interval requires a basis other than 'unknown', "
			                 "and basis 'unknown' requires no interval")
		self.gpu_update_interval_s = interval
		self.basis = basis
		self.source = source
		self.characterized_at_unix_s = characterized_at_unix_s
		self.updates_observed = updates_observed

	@classmethod
	def unknown(cls):
		return cls()

	@classmethod
	def configured(cls, gpu_update_interval_s, source):
		"""A caller-supplied interval. `source` must say where it came from."""
		if not source:
			raise ValueError("a configured resolution needs a source description")
		return cls(gpu_update_interval_s, "configured", source)

	@classmethod
	def from_hardware_report(cls, report, source=None):
		"""From an `aep-validate --json` report (a path or the loaded dict).

		Unknown when the report has no SUPPORTED gpu_counter_resolution.
		"""
		if isinstance(report, str):
			source = source or report
			with open(report) as f:
				report = json.load(f)
		res = (report or {}).get("gpu_counter_resolution") or {}
		interval = res.get("median_update_interval_s")
		if res.get("status") != "SUPPORTED" or not interval:
			return cls.unknown()
		return cls(interval, "hardware_report", source or "hardware report",
		           report.get("generated_at"), res.get("updates_observed"))

	@classmethod
	def probe(cls, samples=3000):
		"""Measure the interval now (polls NVML ~`samples` ms). Unknown on failure."""
		from . import validation
		res = validation.probe_nvml_resolution(samples=samples)
		if res.get("status") != validation.SUPPORTED:
			return cls.unknown()
		return cls(res["median_update_interval_s"], "probed",
		           "validation.probe_nvml_resolution", time.time(),
		           res.get("updates_observed"))

	def classify(self, duration_s):
		"""(below_resolution, update_intervals); (None, None) when unknown."""
		if self.gpu_update_interval_s is None or duration_s is None:
			return None, None
		ratio = duration_s / self.gpu_update_interval_s
		return duration_s < self.gpu_update_interval_s, ratio

	def to_dict(self):
		return {
			"gpu_update_interval_s": self.gpu_update_interval_s,
			"basis": self.basis,
			"source": self.source,
			"characterized_at_unix_s": self.characterized_at_unix_s,
			"updates_observed": self.updates_observed,
			"rapl_update_interval_s": None,
		}

	@classmethod
	def from_dict(cls, data):
		data = data or {}
		if data.get("gpu_update_interval_s") is None:
			return cls.unknown()
		return cls(data["gpu_update_interval_s"], data.get("basis", "configured"),
		           data.get("source"), data.get("characterized_at_unix_s"),
		           data.get("updates_observed"))


# --------------------------------------------------------------------------
# GPU counter readers
# --------------------------------------------------------------------------

class NvmlReader:
	"""Per-device cumulative energy (mJ) through hardware.nvml.

	NVML is initialised by `start()`, not at construction or import.
	"""

	name = "nvml"

	def __init__(self):
		self._handles = []

	def start(self):
		from .hardware import nvml
		nvml.init()
		self._handles = nvml.handles() if nvml.energy_counter_supported() else []
		return self.available()

	def available(self):
		return bool(self._handles)

	def devices(self):
		from .hardware import nvml
		names = nvml.device_names()
		return [{"index": i, "name": names[i] if i < len(names) else None}
		        for i in range(len(self._handles))]

	def read(self):
		"""One reading per device, None for a failed read."""
		from .hardware import nvml
		return [nvml.device_energy_mj(h) for h in self._handles]

	def stop(self):
		self._handles = []


class CallableReader:
	"""Adapter for a caller- or test-supplied `read() -> list of mJ or None`."""

	name = "custom"

	def __init__(self, read, devices=None):
		self._read = read
		self._devices = devices

	def start(self):
		return self.available()

	def available(self):
		return True

	def devices(self):
		if self._devices is not None:
			return list(self._devices)
		return None

	def read(self):
		return self._read()

	def stop(self):
		pass


# --------------------------------------------------------------------------
# domain measurements
# --------------------------------------------------------------------------

def domain(energy_j, status, reason, source, method, below=None, intervals=None,
           raw=None, **extra):
	"""A domain-measurement dict (docs/SCHEMA_V2.md 2.3)."""
	if status != MEASURED:
		energy_j = None
	record = {
		"energy_j": energy_j,
		"status": status,
		"reason": None if status == MEASURED else reason,
		"source": source,
		"method": method,
		"scope": SCOPE_DEVICE,
		"below_resolution": below if status == MEASURED else None,
		"update_intervals": intervals if status == MEASURED else None,
		"raw": list(raw or []),
	}
	record.update(extra)
	return record


def unavailable_gpu(reason):
	return domain(None, UNAVAILABLE, reason, GPU_SOURCE, IN_BAND)


def gpu_in_band(start, end, duration_s, resolution):
	"""GPU domain from per-device in-band reads taken at a span's boundaries.

	`start`/`end` are lists of per-device mJ readings (None for a failed
	read), or None when no reading could be taken at all.

	  * any failed read            -> unavailable / read_failed
	  * different device counts    -> invalid / devices_changed
	  * any device's end < start   -> invalid / counter_decrease. NVML's
	    register is 64-bit and does not wrap in practice, so a decrease is a
	    reset, driver reload or device change; treating it as energy would
	    invent a value. (v1's record() silently nulls the same case.)
	  * otherwise                  -> measured, sum over devices, mJ -> J

	The raw readings are kept in every case.
	"""
	if start is None or end is None:
		return unavailable_gpu("no_counter")
	raw = [{"device": i,
	        "start_mj": start[i] if i < len(start) else None,
	        "end_mj": end[i] if i < len(end) else None}
	       for i in range(max(len(start), len(end)))]
	if not raw:
		return domain(None, UNAVAILABLE, "no_counter", GPU_SOURCE, IN_BAND, raw=raw)
	if len(start) != len(end):
		return domain(None, INVALID, "devices_changed", GPU_SOURCE, IN_BAND, raw=raw)
	if any(r["start_mj"] is None or r["end_mj"] is None for r in raw):
		return domain(None, UNAVAILABLE, "read_failed", GPU_SOURCE, IN_BAND, raw=raw)
	if any(r["end_mj"] < r["start_mj"] for r in raw):
		return domain(None, INVALID, "counter_decrease", GPU_SOURCE, IN_BAND, raw=raw)
	energy_j = sum(r["end_mj"] - r["start_mj"] for r in raw) / 1000.0
	below, intervals = resolution.classify(duration_s)
	return domain(energy_j, MEASURED, None, GPU_SOURCE, IN_BAND, below, intervals, raw)
