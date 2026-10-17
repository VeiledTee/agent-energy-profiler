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

from ._compat import sum  # 3.12 float summation on every Python (see _compat)

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

#: Reason codes (docs/SCHEMA_V2.md section 2.5). The last two arrived with
#: measurement modes (Milestone 5).
REASONS = frozenset({
	"disabled", "no_counter", "read_failed", "counter_decrease",
	"counter_discontinuity", "insufficient_samples", "clock_anomaly",
	"not_covered", "devices_changed",
	"not_read_by_mode", "counter_not_sampled",
})


# --------------------------------------------------------------------------
# measurement modes (Milestone 5)
# --------------------------------------------------------------------------

#: Version of the measurement model recorded in session headers. Absent
#: (sessions written before Milestone 5) means version 1: per-span counter
#: reads, whatever sampler the caller ran.
MEASUREMENT_MODEL_VERSION = 2

MODE_DIRECT = "direct"
MODE_SAMPLED = "sampled"
MODE_HYBRID = "hybrid"
MODE_AUTO = "auto"

#: What each mode reads. `timeline_counter_hz`: the sampler's GPU energy
#: counter rate (0 = only its first and final row; see sampling.counter_policy).
MODES = {
	MODE_DIRECT: {
		"span_counter_reads": True,
		"timeline_counter_hz": 0.0,
		"gpu_span_energy": "measured: in-band counter difference at each span's boundaries",
		"gpu_run_energy": "measured: sampler counter at its first and final row",
	},
	MODE_SAMPLED: {
		"span_counter_reads": False,
		"timeline_counter_hz": 0.0,
		"gpu_span_energy": "estimated only: integrated averaged power (estimates.gpu)",
		"gpu_run_energy": "measured: sampler counter at its first and final row",
	},
	MODE_HYBRID: {
		"span_counter_reads": False,
		"timeline_counter_hz": 1.0,
		"gpu_span_energy": "estimated only: integrated averaged power (estimates.gpu)",
		"gpu_run_energy": "measured: sampler counter at its first and final row, plus a "
		                  "measured counter series at the timeline counter rate, checked "
		                  "against integrated power interval by interval",
	},
}

UNKNOWN_VERDICT = "UNKNOWN"


class CounterDisturbance:
	"""Whether reading the energy counter is known to lower what it reports.

	verdict: DISTURBED, NOT_DETECTED, INCONCLUSIVE (from
	`aep validate --counter-disturbance`), or UNKNOWN when no test result is
	available. UNKNOWN never means "not disturbed".

	`hostname`, `driver_version` and `gpu_models` describe the configuration
	the test ran on (0.4.0: taken from the hardware report). `auto` applies a
	NOT_DETECTED verdict only where they match the running configuration
	(see configuration_mismatch).
	"""

	def __init__(self, verdict=UNKNOWN_VERDICT, source=None, retained_median=None,
	             read_hz=None, measured_at_unix_s=None, driver_version=None,
	             hostname=None, gpu_models=None):
		self.verdict = verdict
		self.source = source
		self.retained_median = retained_median
		self.read_hz = read_hz
		self.measured_at_unix_s = measured_at_unix_s
		self.driver_version = driver_version
		self.hostname = hostname
		self.gpu_models = list(gpu_models) if gpu_models is not None else None

	@classmethod
	def unknown(cls):
		return cls()

	@classmethod
	def from_hardware_report(cls, report, source=None):
		"""From an `aep validate --counter-disturbance --json` report."""
		if isinstance(report, str):
			source = source or report
			with open(report) as f:
				report = json.load(f)
		d = (report or {}).get("gpu_counter_disturbance") or {}
		if d.get("status") != "SUPPORTED" or not d.get("verdict"):
			return cls(source=(source or "hardware report") + " (no disturbance test in it)")
		gpu = (report or {}).get("gpu") or {}
		devices = gpu.get("devices")
		return cls(d["verdict"], source or "hardware report", d.get("retained_median"),
		           d.get("read_hz"), (report or {}).get("generated_at"),
		           gpu.get("driver_version"),
		           ((report or {}).get("host") or {}).get("hostname"),
		           [dev.get("name") for dev in devices] if devices else None)

	def to_dict(self):
		out = {"verdict": self.verdict, "source": self.source,
		       "retained_median": self.retained_median, "read_hz": self.read_hz,
		       "measured_at_unix_s": self.measured_at_unix_s,
		       "driver_version": self.driver_version}
		# 0.4.0: only when known, so headers of earlier sessions keep their form
		if self.hostname is not None:
			out["hostname"] = self.hostname
		if self.gpu_models is not None:
			out["gpu_models"] = list(self.gpu_models)
		return out

	@classmethod
	def from_dict(cls, data):
		data = data or {}
		return cls(data.get("verdict", UNKNOWN_VERDICT), data.get("source"),
		           data.get("retained_median"), data.get("read_hz"),
		           data.get("measured_at_unix_s"), data.get("driver_version"),
		           data.get("hostname"), data.get("gpu_models"))


def current_configuration(reader=None):
	"""{"hostname", "driver_version", "gpu_models"} of the running host, for
	configuration_mismatch. GPU details come from NVML only when `reader` is a
	started NvmlReader; nothing here initialises NVML or reads a counter."""
	import platform
	out = {"hostname": platform.node() or None, "driver_version": None, "gpu_models": None}
	if isinstance(reader, NvmlReader) and reader.available():
		from .hardware import nvml
		out["driver_version"] = nvml.driver_version()
		try:
			out["gpu_models"] = [d.get("name") for d in reader.devices()]
		except Exception:
			pass
	return out


def configuration_mismatch(disturbance, current):
	"""Ways the configuration a disturbance test ran on differs from `current`
	(a current_configuration() dict). Empty when nothing known differs; a
	field unknown on either side is not compared."""
	if not current:
		return []
	out = []
	for key, label in (("hostname", "host"), ("driver_version", "driver"),
	                   ("gpu_models", "GPU models")):
		tested, here = getattr(disturbance, key, None), current.get(key)
		if tested is None or here is None:
			continue
		if key == "gpu_models":
			tested, here = sorted(map(str, tested)), sorted(map(str, here))
		if tested != here:
			out.append(f"{label} {tested} in the test, {here} here")
	return out


def resolve_mode(requested, disturbance=None, current=None):
	"""(mode, reason) for a requested mode. Never runs a diagnostic.

	`auto` uses per-span counter reads (direct) only when a disturbance test
	on this host found no effect. A DISTURBED, INCONCLUSIVE or UNKNOWN verdict
	selects `sampled`: the run is still measured by the counter at its
	boundaries, and per-span values are estimates, labelled as such.

	`current` (0.4.0): the running configuration (current_configuration()).
	A NOT_DETECTED verdict from a test on a different host, driver or GPU
	model then selects `sampled` too.
	"""
	disturbance = disturbance or CounterDisturbance.unknown()
	if requested in MODES:
		return requested, "requested explicitly"
	if requested != MODE_AUTO:
		raise ValueError(f"mode must be one of {sorted(MODES) + [MODE_AUTO]}")
	verdict = disturbance.verdict
	if verdict == "NOT_DETECTED":
		mismatch = configuration_mismatch(disturbance, current)
		if mismatch:
			return MODE_SAMPLED, (f"auto: counter disturbance NOT_DETECTED "
			                 f"({disturbance.source}) but for another configuration "
			                 f"({'; '.join(mismatch)}); per-span counter reads not "
			                 f"enabled without evidence they are harmless here")
		return MODE_DIRECT, (f"auto: counter disturbance NOT_DETECTED ({disturbance.source}); "
		                f"per-span counter reads enabled")
	if verdict == "DISTURBED":
		return MODE_SAMPLED, (f"auto: counter disturbance DISTURBED ({disturbance.source}); "
		                 f"no per-span counter reads")
	return MODE_SAMPLED, (f"auto: counter disturbance {verdict} "
	                 f"({disturbance.source or 'no test result available'}); per-span "
	                 f"counter reads not enabled without evidence they are harmless here")


#: Who chose the requested mode (Milestone 6): the caller passed it, the
#: entry point's default applied, or `aep run` configured the session.
MODE_SOURCE_CALLER = "caller"
MODE_SOURCE_DEFAULT = "default"
MODE_SOURCE_AEP_RUN = "aep_run"

#: Session's default mode: `auto` since 0.4.0, as in `aep run` (`direct`
#: before; docs/MEASUREMENT_MODES.md "SDK default mode migration").
SESSION_DEFAULT_MODE = MODE_AUTO
SESSION_DEFAULT_REASON = "default: Session's default mode (auto since 0.4.0)"
#: The release that changed it, and the default before it.
SESSION_DEFAULT_CHANGED_IN = "0.4.0"
SESSION_PREVIOUS_DEFAULT_MODE = MODE_DIRECT


class SessionDefaultModeWarning(FutureWarning):
	"""Deprecated: no longer emitted (0.4.0).

	0.3.0 emitted it when `Session()` was created without `mode=` and fell
	back to `direct`. Kept so that code filtering it by category keeps
	working; it will be removed in a later minor release.
	"""


def measurement_config(mode, requested, reason, disturbance, timeline=None, source=None):
	"""The `config.measurement` block of a session header.

	`mode_source` (Milestone 6) is added when `source` is given.
	"""
	out = {
		"model_version": MEASUREMENT_MODEL_VERSION,
		"mode": mode,
		"requested_mode": requested,
		"mode_reason": reason,
		"span_counter_reads": MODES[mode]["span_counter_reads"],
		"semantics": {k: v for k, v in MODES[mode].items()
		              if k in ("gpu_span_energy", "gpu_run_energy")},
		"timeline": timeline,
		"counter_disturbance": (disturbance or CounterDisturbance.unknown()).to_dict(),
	}
	if source is not None:
		out["mode_source"] = source
	return out


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
