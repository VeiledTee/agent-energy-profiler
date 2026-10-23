"""Session: the schema-v2 instrumentation API.

    from agent_energy_profiler import Session
    from agent_energy_profiler.measurement import Resolution

    with Session("spans.jsonl", mode="auto",
                 resolution=Resolution.from_hardware_report("hw.json"),
                 attributes={"run_id": "run-1"}) as session:
        with session.span("agent:step") as step:
            with session.span("llm:plan") as s:
                s.set_tokens(input=812, output=64)

        @session.trace("tool:search")
        async def search(q): ...

For most programs, `Session.profile(name)` is the shorter form: it creates a
run directory, starts the hardware sampler with the mode's counter policy,
and attributes and reports the run when the session closes. Under
`agent-energy-profiler run` it records into that run instead, without a
second sampler (see Session.profile).

A Session is an explicit object. There is no global default session: every
span belongs to the session that created it, writes to that session's file,
and finds its parent only among that session's open spans in the current
task or thread context (contextvars). Field meanings: docs/SCHEMA_V2.md.

Concurrency model
-----------------
* asyncio: a task created inside a span inherits it as parent (contextvars
  are copied into new tasks). Concurrent sibling tasks are therefore siblings
  that overlap in time; accounting reports their energy as shared.
* threads: a new thread starts with an empty context, so its spans are roots
  unless `parent=` is passed explicitly.
* several sessions in one process are isolated in what they record, but not
  in what the hardware sees: device energy cannot be split between them. A
  span that overlapped another session's open span is marked
  `other_sessions_active`.
* multiprocessing: not supported across processes. Each process must create
  its own Session and file. A forked child refuses to write through an
  inherited session (counted, never interleaved into the parent's file), and
  nothing links spans across processes or separates their device energy.

Recording is best-effort like v1: an instrumentation failure is counted in
the session_end record and never raised into the workload. Exceptions raised
BY the workload always propagate unchanged.

Hooks (Milestone 6)
-------------------
`Session(hooks=[...])` lets an integration observe spans without the core
knowing about it (agent_energy_profiler.otel is one). A hook may define any of:

    trace_context()             -> {"trace_id", "span_id", ...} or None
        consulted for a root span (no parent, no explicit trace_id): the span
        joins that external trace and records it as `external_parent`
    on_span_start(span)         span is a SpanHandle; called before the
                                span's GPU counter read and clocks
    on_span_end(span, record)   called after the record is written (record
                                is None if it could not be built)

Hooks run outside both the span's duration and its counter window. A hook
that raises is counted (`n_hook_errors`) and never breaks the workload.
"""

import contextvars
import functools
import inspect
import json
import os
import platform
import secrets
import sys
import threading
import time

from . import __version__
from . import labels as _labels
from . import measurement as m
from . import schema_v2
from .events import _is_cancellation
from .schema import Status, is_known_status

#: {session_id: _Span} of the innermost open span per session, per context.
_ACTIVE = contextvars.ContextVar("aep_v2_active_spans", default=None)

#: {session_id: {attribute: value}} bound by Session.bind(), per context.
_BOUND = contextvars.ContextVar("aep_v2_bound_attributes", default=None)

_HEX = frozenset("0123456789abcdef")


def _hex_id(value, length):
	"""value if it is a non-zero lowercase hex id of `length` chars, else None."""
	if (isinstance(value, str) and len(value) == length and set(value) <= _HEX
	        and value.strip("0")):
		return value
	return None

#: Every open span in this process, for cross-session overlap DETECTION only.
_open_lock = threading.Lock()
_open_spans = set()


class SpanHandle:
	"""What the body of a span can see and set."""

	def __init__(self, span):
		self._span = span

	@property
	def span_id(self):
		return self._span.span_id

	@property
	def trace_id(self):
		return self._span.trace_id

	@property
	def parent_span_id(self):
		return self._span.parent_span_id

	@property
	def name(self):
		"""The operation label the caller gave this span."""
		return self._span.name

	@property
	def session_id(self):
		return self._span.session.session_id

	@property
	def external_parent(self):
		"""The external trace parent of a root span (see hooks), or None."""
		return self._span.external_parent

	def set_attribute(self, key, value):
		self._span.attributes[str(key)] = value

	def set_attributes(self, **values):
		for key, value in values.items():
			self.set_attribute(key, value)

	def set_tokens(self, input=None, output=None):
		if input is not None:
			self._span.input_tokens = input
		if output is not None:
			self._span.output_tokens = output

	def set_status(self, status):
		"""Record how the span ended (e.g. "timeout"). Wins over the default."""
		if not is_known_status(status):
			raise ValueError(f"unknown status {status!r}")
		self._span.status = str(status)
		self._span.status_explicit = True

	# v1-style access, so `s["input_tokens"] = n` keeps working after a move
	# from profiler.span to session.span.
	def __setitem__(self, key, value):
		if key in ("input_tokens", "output_tokens"):
			setattr(self._span, key, value)
		elif key == "meta" and isinstance(value, dict):
			self._span.attributes.update(value)
		else:
			self.set_attribute(key, value)

	def __getitem__(self, key):
		if key in ("input_tokens", "output_tokens"):
			return getattr(self._span, key)
		if key == "meta":
			return self._span.attributes
		return self._span.attributes[key]


class _Span:
	__slots__ = ("session", "span_id", "trace_id", "parent_span_id", "name",
	             "operation_type", "attributes", "input_tokens", "output_tokens",
	             "status", "status_explicit", "start_unix", "start_mono",
	             "gpu_start", "other_sessions", "token", "thread_id", "task",
	             "external_parent", "__weakref__")


class _SpanScope:
	"""Context manager for one span; usable with `with` and `async with`."""

	def __init__(self, session, name, attributes, parent, trace_id):
		self._session = session
		self._name = name
		self._attributes = attributes
		self._parent = parent
		self._trace_id = trace_id
		self._span = None

	def __enter__(self):
		self._span = self._session._begin(self._name, self._attributes,
		                                  self._parent, self._trace_id)
		return SpanHandle(self._span)

	def __exit__(self, exc_type, exc, tb):
		self._session._end(self._span, exc)
		return False  # never swallow the workload's exception

	async def __aenter__(self):
		return self.__enter__()

	async def __aexit__(self, exc_type, exc, tb):
		return self.__exit__(exc_type, exc, tb)


class _BindScope:
	"""Context manager adding attributes to every span opened inside it."""

	def __init__(self, session_id, attributes):
		self._session_id = session_id
		self._attributes = attributes
		self._token = None

	def __enter__(self):
		current = _BOUND.get() or {}
		merged = dict(current.get(self._session_id) or {})
		merged.update(self._attributes)
		new = dict(current)
		new[self._session_id] = merged
		self._token = _BOUND.set(new)
		return self

	def __exit__(self, exc_type, exc, tb):
		try:
			_BOUND.reset(self._token)
		except ValueError:
			pass  # exited in another context; that context's copy is discarded
		return False

	async def __aenter__(self):
		return self.__enter__()

	async def __aexit__(self, exc_type, exc, tb):
		return self.__exit__(exc_type, exc, tb)


def _task_name():
	asyncio = sys.modules.get("asyncio")
	if asyncio is None:
		return None
	try:
		task = asyncio.current_task()
	except RuntimeError:
		return None
	return task.get_name() if task is not None else None


class Session:
	"""An independent profiling session writing schema-v2 records to `path`.

	gpu         "auto" (NVML if available), False (disabled), or a reader with
	            start()/read()/devices()/stop() (see measurement.CallableReader)
	resolution  measurement.Resolution; unknown if omitted
	sampler     an optional sampling.Sampler, started and stopped with the
	            session so the hardware timeline brackets every span
	overwrite   by default an existing file is an error: two sessions must
	            never share one file
	mode        measurement mode (measurement.MODES): "direct" reads the GPU
	            counter at each span's boundaries; "sampled" and "hybrid"
	            never read it per span, leaving GPU span energy to estimates;
	            "auto" picks from `disturbance` (resolve_mode). Omitted: "auto"
	            since 0.4.0 (before: "direct"), recorded with mode_source
	            "default" (docs/MEASUREMENT_MODES.md). `auto` selects direct
	            only for a NOT_DETECTED verdict whose host, driver and GPU
	            models match this one (checked when the session starts), and
	            sampled otherwise; it never runs the disturbance test
	disturbance measurement.CounterDisturbance: what is known about counter
	            disturbance on this host. Recorded in the header; UNKNOWN if
	            omitted, and never assumed to mean "not disturbed"
	timeline    description of a hardware timeline run outside this session
	            (as `agent-energy-profiler run` does), recorded in the header
	hooks       integrations observing spans (see "Hooks" above), e.g.
	            agent_energy_profiler.otel.Correlation()
	"""

	def __init__(self, path, *, attributes=None, gpu="auto", resolution=None,
	             sampler=None, overwrite=False, mode=None, disturbance=None,
	             timeline=None, hooks=()):
		self.path = path
		self.session_id = secrets.token_hex(16)
		self.attributes = dict(attributes or {})
		self.resolution = resolution or m.Resolution.unknown()
		if not isinstance(self.resolution, m.Resolution):
			raise TypeError("resolution must be a measurement.Resolution")
		self.disturbance = disturbance or m.CounterDisturbance.unknown()
		if mode is None:
			self.requested_mode = m.SESSION_DEFAULT_MODE
			self.mode_source = m.MODE_SOURCE_DEFAULT
		else:
			self.requested_mode = mode
			self.mode_source = m.MODE_SOURCE_CALLER
		self._resolve_mode(None)
		self.hooks = tuple(hooks or ())
		self._hook_context = [h.trace_context for h in self.hooks
		                      if callable(getattr(h, "trace_context", None))]
		self._hook_start = [h.on_span_start for h in self.hooks
		                    if callable(getattr(h, "on_span_start", None))]
		self._hook_end = [h.on_span_end for h in self.hooks
		                  if callable(getattr(h, "on_span_end", None))]
		self.n_hook_errors = 0
		self._timeline = timeline
		self.sampler = sampler
		self._overwrite = overwrite
		self._gpu_mode = gpu
		self._reader = None
		self._gpu_reason = None
		self._fh = None
		self._lock = threading.Lock()
		self._pid = None
		self._started = False
		self._closed = False
		self.n_spans_written = 0
		self.n_spans_dropped = 0
		self.n_foreign_process_writes_refused = 0
		self.sampler_exit_code = None
		#: Set by Session.profile only (None otherwise); see there.
		self.out_dir = None
		self.report = None
		self.profile_status = None
		self.profile_error = None
		self._profile = None

	#: Environment variables set by `agent-energy-profiler run` for the profiled program.
	ENV_SPANS_FILE = "AEP_SPANS_FILE"
	ENV_RESOLUTION = "AEP_RESOLUTION"
	ENV_GPU = "AEP_GPU"
	ENV_ATTRIBUTES = "AEP_RUN_ATTRIBUTES"
	#: JSON {"mode", "disturbance", "timeline"} set by `agent-energy-profiler run` (Milestone 5)
	ENV_MEASUREMENT = "AEP_MEASUREMENT"

	@classmethod
	def from_env(cls, default_path=None, *, attributes=None, default_mode=None, **kwargs):
		"""A Session configured by `agent-energy-profiler run`, or by `default_path` when the
		program runs on its own.

		Reads AEP_SPANS_FILE (output path), AEP_RESOLUTION (a Resolution as
		JSON), AEP_GPU ("auto" or "off") and AEP_RUN_ATTRIBUTES (JSON object,
		merged under `attributes`). Keyword arguments override the environment.
		The sampler is not started here: under `agent-energy-profiler run` it belongs to the
		parent process, which brackets the whole program with it.

		`default_mode` (Milestone 6) is the mode to use when neither `mode=`
		nor `agent-energy-profiler run` gives one, i.e. when the program runs on its own; it is
		recorded as the caller's choice. Without it, Session's own default
		(auto since 0.4.0) applies.
		"""
		path = os.environ.get(cls.ENV_SPANS_FILE) or default_path
		if not path:
			raise RuntimeError(f"no output path: set {cls.ENV_SPANS_FILE} (as `agent-energy-profiler run` "
			                   f"does) or pass default_path")
		if "resolution" not in kwargs and os.environ.get(cls.ENV_RESOLUTION):
			kwargs["resolution"] = m.Resolution.from_dict(
				json.loads(os.environ[cls.ENV_RESOLUTION]))
		meas = None
		mode_from_env = False
		if os.environ.get(cls.ENV_MEASUREMENT):
			meas = json.loads(os.environ[cls.ENV_MEASUREMENT])
			mode_from_env = kwargs.get("mode") is None
			if mode_from_env:
				kwargs["mode"] = meas.get("mode", m.MODE_DIRECT)
			kwargs.setdefault("disturbance", m.CounterDisturbance.from_dict(meas.get("disturbance")))
			kwargs.setdefault("timeline", meas.get("timeline"))
		elif kwargs.get("mode") is None and default_mode is not None:
			kwargs["mode"] = default_mode
		if "gpu" not in kwargs and os.environ.get(cls.ENV_GPU):
			kwargs["gpu"] = False if os.environ[cls.ENV_GPU] == "off" else "auto"
		merged = {}
		if os.environ.get(cls.ENV_ATTRIBUTES):
			merged.update(json.loads(os.environ[cls.ENV_ATTRIBUTES]))
		merged.update(attributes or {})
		session = cls(path, attributes=merged, **kwargs)
		if meas and mode_from_env and session.mode == meas.get("mode"):
			# keep how `agent-energy-profiler run` chose the mode (e.g. "auto" and why)
			session.requested_mode = meas.get("requested_mode", session.requested_mode)
			session.mode_reason = meas.get("mode_reason", session.mode_reason)
			session.mode_source = m.MODE_SOURCE_AEP_RUN
		return session

	#: Where Session.profile creates run directories (as `agent-energy-profiler run`).
	PROFILE_RUNS_DIR = "agent-energy-profiler-runs"
	#: Session.profile's sampler row rate (power and RAPL), as `run`'s default.
	PROFILE_POWER_HZ = 10.0

	@classmethod
	def profile(cls, name=None, *, out_dir=None, mode=None, hardware_report=None,
	            attributes=None, report=True, strict=False, quiet=False, **kwargs):
		"""A Session that manages its own run directory, sampler and report.

		    with Session.profile("my-run") as session:
		        with session.span("llm:generate"):
		            ...
		    session.report          # the report dict; session.out_dir has the files

		On its own, the session writes to `out_dir`, or to a new directory
		agent-energy-profiler-runs/<name>-<YYYYmmdd-HHMMSS> (suffixed -2, -3,
		... if taken), created when the session starts. An explicit `out_dir`
		is never overwritten: one already holding any file a profile writes is
		an error.
		The hardware sampler runs from start to close at 10 Hz and reads the
		GPU energy counter as `mode` prescribes (measurement.MODES
		timeline_counter_hz: first and final row only, except hybrid), never
		the sampler's every-row default. On close the run is attributed
		(analyze) and the report is saved as report.json and report.txt,
		stored in `session.report` and printed to stderr. Nothing is printed
		to stdout.

		Under `agent-energy-profiler run` (AEP_SPANS_FILE set) this is
		Session.from_env: the run's directory, mode and sampler are used, no
		sampler is started here, and `run` attributes and reports. `name`,
		`out_dir`, `hardware_report` and `report` then have no effect.

		mode             as Session; omitted: auto (sampled unless a hardware
		                 report shows counter reads are harmless here)
		hardware_report  an `agent-energy-profiler validate --json` file; sets
		                 both the counter resolution and the disturbance verdict
		report           False: stop after recording; attribute later with
		                 analyze() or `agent-energy-profiler attribute`
		strict           raise from close when stopping the sampler, attribution
		                 or the report failed although the workload succeeded
		quiet            print nothing to stderr
		kwargs           other Session arguments (gpu, hooks); path, sampler,
		                 overwrite, resolution, disturbance and timeline are
		                 managed here

		After close, `session.profile_status` is "complete", "incomplete"
		(see `session.profile_error`; the raw files are kept), "recorded"
		(report=False), "no_spans" (nothing to attribute; the whole-session
		readings are printed, never assigned to an operation),
		"recorded_for_run" (under `run`) or "failed_to_start".
		A failure after the workload never hides the workload's own exception.
		"""
		managed = sorted(set(kwargs) & {"path", "sampler", "overwrite", "resolution",
		                                "disturbance", "timeline"})
		if managed:
			raise TypeError(f"Session.profile() manages {', '.join(managed)}; "
			                f"use Session(...) to set them yourself")
		if os.environ.get(cls.ENV_SPANS_FILE):
			session = cls.from_env(attributes=attributes, mode=mode, **kwargs)
			session.out_dir = os.path.dirname(os.path.abspath(session.path))
			session._profile = _Profile(None, explicit=False, report=False, strict=strict,
			                            quiet=quiet, under_run=True)
			session.profile_status = "created"
			if hardware_report and not quiet:
				print(f"agent-energy-profiler: hardware_report={hardware_report!r} is not "
				      f"used under `agent-energy-profiler run`; pass --hardware-report "
				      f"to run instead", file=sys.stderr)
			return session
		resolution = disturbance = None
		if hardware_report:
			resolution = m.Resolution.from_hardware_report(hardware_report)
			disturbance = m.CounterDisturbance.from_hardware_report(hardware_report)
		if out_dir:
			directory, explicit = out_dir, True
		else:
			slug = "".join(c if c.isalnum() or c in "._-" else "-"
			               for c in str(name or "run")).strip(".-") or "run"
			directory = os.path.join(cls.PROFILE_RUNS_DIR,
			                         f"{slug}-{time.strftime('%Y%m%d-%H%M%S')}")
			explicit = False
		session = cls(os.path.join(directory, "spans.jsonl"), attributes=attributes,
		              mode=mode, resolution=resolution, disturbance=disturbance, **kwargs)
		session._profile = _Profile(directory, explicit=explicit, report=report,
		                            strict=strict, quiet=quiet)
		session.profile_status = "created"
		return session

	def bind(self, **attributes):
		"""Add `attributes` to every span of this session opened inside the
		returned context manager (`with` or `async with`), in this task or
		thread context and in tasks created from it.

		For tagging work with a request or task id without an extra span:

		    with session.bind(request_id=rid):
		        handle(request)

		A span's own attributes win over bound ones.
		"""
		return _BindScope(self.session_id, attributes)

	# -- lifecycle --------------------------------------------------------

	def start(self):
		"""Open the file, start GPU reading and the sampler. Idempotent."""
		if self._started:
			return self
		if self._closed:
			raise RuntimeError("a closed Session cannot be restarted")
		if self._profile is not None:
			self._profile.prepare(self)
		self._fh = open(self.path, "w" if self._overwrite else "x", buffering=1)
		self._pid = os.getpid()
		self._started = True
		try:
			self._start_gpu()
			if (self.requested_mode == m.MODE_AUTO and self.mode == m.MODE_DIRECT
			        and self.mode_source != m.MODE_SOURCE_AEP_RUN):
				# auto's direct choice holds only for the configuration the
				# disturbance test ran on; `agent-energy-profiler run` checked it in the parent
				self._resolve_mode(m.current_configuration(self._reader))
			if self._profile is not None:
				self._profile.started(self)
			if self.sampler is not None:
				self.sampler.start()
			self._write(self._session_start_record())
		except BaseException:
			if self._profile is not None:
				self._profile.start_failed = True
			self.close()
			raise
		return self

	def close(self):
		"""Stop the sampler, write session_end, close the file. Idempotent.

		Spans still open are not written (their end is unknown). A session
		from Session.profile then attributes and reports the run.
		"""
		self._close(None)

	def _close(self, exc):
		if self._profile is not None:
			self._profile.finish(self, exc)
		else:
			self._close_recording()

	def _close_recording(self):
		if self._closed or not self._started:
			self._closed = True
			return
		self._closed = True
		try:
			if self.sampler is not None:
				self.sampler_exit_code = self.sampler.stop()
		finally:
			try:
				if self._reader is not None:
					self._reader.stop()
			except Exception:
				pass
			if os.getpid() == self._pid:
				end = {
					"schema_version": schema_v2.SCHEMA_VERSION,
					"record_type": schema_v2.SESSION_END,
					"session_id": self.session_id,
					"ended_unix_s": time.time(),
					"n_spans_written": self.n_spans_written,
					"n_spans_dropped": self.n_spans_dropped,
					"n_foreign_process_writes_refused":
						self.n_foreign_process_writes_refused,
					"sampler_exit_code": self.sampler_exit_code,
				}
				if self.hooks:
					end["n_hook_errors"] = self.n_hook_errors
				self._write(end, force=True)
				try:
					self._fh.close()
				except Exception:
					pass

	@property
	def closed(self):
		return self._closed

	def __enter__(self):
		return self.start()

	def __exit__(self, exc_type, exc, tb):
		self._close(exc)
		return False

	async def __aenter__(self):
		return self.start()

	async def __aexit__(self, exc_type, exc, tb):
		self._close(exc)
		return False

	# -- public span API ---------------------------------------------------

	def span(self, name, *, attributes=None, parent=None, trace_id=None):
		"""A span labelled `name`, as a (sync or async) context manager.

		The label is the caller's: nothing is inferred from function names,
		prompts or the call stack. `parent` (a SpanHandle) overrides the
		contextual parent, e.g. to link work handed to a thread.
		"""
		if not isinstance(name, str) or not name:
			raise TypeError("span name (the operation label) must be a non-empty string")
		if parent is not None and not isinstance(parent, SpanHandle):
			raise TypeError("parent must be a SpanHandle from this session")
		return _SpanScope(self, name, attributes, parent, trace_id)

	def trace(self, name, *, attributes=None):
		"""Decorator recording each call of a sync or async function as a span.

		The label is required: the function's own name is never used.
		"""
		if not isinstance(name, str) or not name:
			raise TypeError("trace() needs an explicit operation label, "
			                "e.g. @session.trace('tool:search')")

		def decorate(fn):
			if inspect.iscoroutinefunction(fn):
				@functools.wraps(fn)
				async def async_wrapper(*args, **kwargs):
					async with self.span(name, attributes=attributes):
						return await fn(*args, **kwargs)
				return async_wrapper

			@functools.wraps(fn)
			def wrapper(*args, **kwargs):
				with self.span(name, attributes=attributes):
					return fn(*args, **kwargs)
			return wrapper
		return decorate

	def current_span(self):
		"""Handle of this session's innermost open span in this context, or None."""
		span = (_ACTIVE.get() or {}).get(self.session_id)
		return SpanHandle(span) if span is not None else None

	# -- internals ---------------------------------------------------------

	def _start_gpu(self):
		mode = self._gpu_mode
		if mode is False or mode is None:
			self._gpu_reason = "disabled"
			return
		reader = m.NvmlReader() if mode in ("auto", True) else mode
		try:
			ok = reader.start()
		except Exception:
			ok = False
		if ok:
			self._reader = reader
		else:
			self._gpu_reason = "no_counter"
			try:
				reader.stop()
			except Exception:
				pass
		self._gpu_reader_name = getattr(reader, "name", "custom")

	def _resolve_mode(self, current):
		self.mode, self.mode_reason = m.resolve_mode(self.requested_mode, self.disturbance,
		                                             current)
		if self.mode_source == m.MODE_SOURCE_DEFAULT:
			self.mode_reason = f"{m.SESSION_DEFAULT_REASON}; {self.mode_reason}"

	@property
	def span_counter_reads(self):
		"""Whether this session reads the GPU counter at span boundaries."""
		return m.MODES[self.mode]["span_counter_reads"]

	def _read_gpu(self):
		if self._reader is None or not self.span_counter_reads:
			return None
		try:
			return self._reader.read()
		except Exception:
			return None

	def _session_start_record(self):
		devices = None
		if self._reader is not None:
			try:
				devices = self._reader.devices()
			except Exception:
				devices = None
		gpu_cfg = {
			"enabled": self._reader is not None,
			"reader": ("disabled" if self._gpu_reason == "disabled"
			           else getattr(self, "_gpu_reader_name", None)),
			"unavailable_reason": self._gpu_reason,
			"devices": devices,
			"counter": m.GPU_SOURCE,
			"counter_unit": "mJ",
		}
		if isinstance(self._reader, m.NvmlReader):
			from .hardware import nvml
			gpu_cfg["driver_version"] = nvml.driver_version()
		sampler_cfg = {"enabled": self.sampler is not None}
		if self.sampler is not None:
			sampler_cfg.update(self.sampler.describe())
		timeline = ((self.sampler.describe().get("timeline") if self.sampler is not None
		             else None) or self._timeline)
		record = {
			"schema_version": schema_v2.SCHEMA_VERSION,
			"record_type": schema_v2.SESSION_START,
			"session_id": self.session_id,
			"profiler_version": __version__,
			"started_unix_s": time.time(),
			"started_monotonic_s": time.monotonic(),
			"process_id": os.getpid(),
			"host": {"platform": sys.platform,
			         "python": platform.python_version()},
			"config": {
				"energy_scope": m.SCOPE_DEVICE,
				"gpu": gpu_cfg,
				"resolution": self.resolution.to_dict(),
				"sampler": sampler_cfg,
				"measurement": m.measurement_config(
					self.mode, self.requested_mode, self.mode_reason, self.disturbance,
					timeline, source=self.mode_source),
			},
			"attributes": self.attributes,
		}
		if self.hooks:
			record["config"]["hooks"] = [f"{type(h).__module__}.{type(h).__qualname__}"
			                             for h in self.hooks]
		return record

	def _hook_failed(self):
		with self._lock:
			self.n_hook_errors += 1

	def _external_context(self):
		"""The first valid external trace context a hook offers, or None."""
		for get in self._hook_context:
			try:
				ctx = get()
				if not ctx:
					continue
				trace_id = _hex_id(ctx.get("trace_id"), 32)
				span_id = _hex_id(ctx.get("span_id"), 16)
				if trace_id is None or span_id is None:
					raise ValueError("malformed trace context")
				out = {"system": str(ctx.get("system") or "external"),
				       "trace_id": trace_id, "span_id": span_id}
				if "sampled" in ctx:
					out["sampled"] = bool(ctx["sampled"])
				return out
			except Exception:
				self._hook_failed()
		return None

	def _begin(self, name, attributes, parent, trace_id):
		if not self._started or self._closed:
			raise RuntimeError("Session is not started (use `with Session(...)` "
			                   "or session.start())")
		span = _Span()
		span.session = self
		span.span_id = secrets.token_hex(8)
		active = _ACTIVE.get() or {}
		parent_span = parent._span if parent is not None else active.get(self.session_id)
		if parent_span is not None and parent_span.session is not self:
			raise ValueError("parent belongs to a different session")
		span.parent_span_id = parent_span.span_id if parent_span is not None else None
		span.external_parent = None
		if trace_id:
			span.trace_id = trace_id
		elif parent_span is not None:
			span.trace_id = parent_span.trace_id
		else:
			external = self._external_context() if self._hook_context else None
			if external is not None:
				span.trace_id = external["trace_id"]
				span.external_parent = external
			else:
				span.trace_id = secrets.token_hex(16)
		span.name = name
		span.operation_type, unknown = _labels.REGISTRY.resolve(name)
		bound = (_BOUND.get() or {}).get(self.session_id)
		if bound:
			span.attributes = dict(bound)
			span.attributes.update(attributes or {})
		else:
			span.attributes = dict(attributes or {})
		if unknown:
			span.attributes["unknown_label"] = True
		span.input_tokens = span.output_tokens = None
		span.status = Status.OK.value
		span.status_explicit = False
		span.thread_id = threading.get_ident()
		span.task = _task_name()
		span.other_sessions = False
		with _open_lock:
			for other in _open_spans:
				if other.session is not self:
					other.other_sessions = True
					span.other_sessions = True
			_open_spans.add(span)
		new_active = dict(active)
		new_active[self.session_id] = span
		span.token = _ACTIVE.set(new_active)
		if self._hook_start:
			# before the counter read and the clocks: outside the measured window
			handle = SpanHandle(span)
			for hook in self._hook_start:
				try:
					hook(handle)
				except Exception:
					self._hook_failed()
		# Order: counter, then clocks at the start; clocks, then counter at the
		# end. duration_s then covers the work only, never the read calls
		# (~1.2 ms each through NVML on WSL2), while the counter window
		# brackets the duration. below_resolution is judged on the shorter of
		# the two, which errs towards flagging.
		span.gpu_start = self._read_gpu()
		span.start_unix = time.time()
		span.start_mono = time.monotonic()
		return span

	def _end(self, span, exc):
		end_mono = time.monotonic()
		end_unix = time.time()
		gpu_end = self._read_gpu()
		try:
			_ACTIVE.reset(span.token)
		except ValueError:
			# Exited in a different context than it was entered (a scope
			# handed across tasks). Drop this session's entry instead.
			active = dict(_ACTIVE.get() or {})
			if active.get(self.session_id) is span:
				active.pop(self.session_id)
				_ACTIVE.set(active)
		with _open_lock:
			_open_spans.discard(span)
		try:
			status, error_type = span.status, None
			if exc is not None:
				error_type = type(exc).__name__
				if not span.status_explicit:
					status = (Status.CANCELLED.value if _is_cancellation(exc)
					          else Status.ERROR.value)
			duration = max(0.0, end_mono - span.start_mono)
			if self._reader is None:
				gpu = m.unavailable_gpu(self._gpu_reason or "no_counter")
			elif not self.span_counter_reads:
				gpu = m.unavailable_gpu("not_read_by_mode")
			elif span.gpu_start is None or gpu_end is None:
				gpu = m.unavailable_gpu("read_failed")
			else:
				gpu = m.gpu_in_band(span.gpu_start, gpu_end, duration, self.resolution)
			record = {
				"schema_version": schema_v2.SCHEMA_VERSION,
				"record_type": schema_v2.SPAN,
				"session_id": self.session_id,
				"trace_id": span.trace_id,
				"span_id": span.span_id,
				"parent_span_id": span.parent_span_id,
				"name": span.name,
				"operation_type": span.operation_type,
				"status": status,
				"error_type": error_type,
				"start_unix_s": span.start_unix,
				"end_unix_s": end_unix,
				"start_monotonic_s": span.start_mono,
				"end_monotonic_s": end_mono,
				"duration_s": duration,
				"clock_anomaly": end_unix < span.start_unix,
				"process_id": os.getpid(),
				"thread_id": span.thread_id,
				"task": span.task,
				"other_sessions_active": span.other_sessions,
				"input_tokens": span.input_tokens,
				"output_tokens": span.output_tokens,
				"attributes": span.attributes,
				"energy": {"gpu": gpu},
			}
			if span.external_parent is not None:
				record["external_parent"] = span.external_parent
		except Exception:
			record = None
			self.n_spans_dropped += 1
		if record is not None and self._write(record):
			self.n_spans_written += 1
		if self._hook_end:
			# after the counter read, the clocks and the write; in reverse
			# order, so context a hook attached at start is detached LIFO
			handle = SpanHandle(span)
			for hook in reversed(self._hook_end):
				try:
					hook(handle, record)
				except Exception:
					self._hook_failed()

	def _write(self, record, force=False):
		"""Append one record. Never raises; False when the record was not written."""
		if os.getpid() != self._pid:
			# Forked child holding the parent's handle: interleaving into the
			# parent's file would corrupt it. Refuse and count.
			self.n_foreign_process_writes_refused += 1
			return False
		if self._closed and not force:
			self.n_spans_dropped += 1
			return False
		try:
			line = json.dumps(record) + "\n"
			with self._lock:
				self._fh.write(line)
			return True
		except Exception:
			self.n_spans_dropped += 1
			return False


def _say(text):
	"""One message to stderr; a closed or broken stderr is not the workload's problem."""
	try:
		print(text, file=sys.stderr, flush=True)
	except Exception:
		pass


class _Profile:
	"""What Session.profile adds to a Session: the run directory, the sampler,
	and attribution and reporting at close. Private; see Session.profile."""

	#: Every file a profile writes into its directory. An explicit out_dir
	#: holding any of them is refused, so no earlier result is overwritten.
	FILES = ("spans.jsonl", "power.csv", "power.csv.meta.json", "power.csv.sampler.log",
	         "spans_attributed.jsonl", "session_summary.json", "report.json", "report.txt")

	def __init__(self, directory, *, explicit, report, strict, quiet, under_run=False):
		self.directory = directory
		self.explicit = explicit
		self.report = report
		self.strict = strict
		self.quiet = quiet
		self.under_run = under_run
		self.start_failed = False

	def prepare(self, session):
		"""Create the run directory and the sampler, before the spans file opens."""
		if self.under_run:
			return
		from .sampling import Sampler
		directory = self.directory
		if self.explicit:
			os.makedirs(directory, exist_ok=True)
			existing = [name for name in self.FILES
			            if os.path.exists(os.path.join(directory, name))]
			if existing:
				raise FileExistsError(f"{directory} already holds {', '.join(existing)}; "
				                      f"Session.profile never overwrites results: use a new "
				                      f"out_dir, or leave it out for a generated one")
		else:
			base, n = directory, 2
			while True:
				try:
					os.makedirs(directory)
					break
				except FileExistsError:
					directory, n = f"{base}-{n}", n + 1
		session.out_dir = directory
		session.path = os.path.join(directory, "spans.jsonl")
		# the mode's counter policy, never the sampler's every-row default; auto
		# can still move from direct to sampled at start, and both read the
		# counter at the first and final row only
		session.sampler = Sampler(os.path.join(directory, "power.csv"),
		                          hz=session.PROFILE_POWER_HZ,
		                          counter_hz=m.MODES[session.mode]["timeline_counter_hz"],
		                          watch_parent=True)
		session.profile_status = "recording"

	def started(self, session):
		"""Say where the run goes, and why GPU energy will be missing if it will."""
		if self.under_run:
			session.profile_status = "recording"
			return    # `run` has said both already
		if self.quiet:
			return
		_say(f"agent-energy-profiler: profiling into {session.out_dir} "
		     f"(measurement mode {session.mode})")
		if session._reader is None and session._gpu_reason == "no_counter":
			from .cli import _no_gpu_hint
			_say(f"agent-energy-profiler: no GPU energy counter, so GPU values will be "
			     f"n/a. {_no_gpu_hint()}")

	def finish(self, session, exc):
		"""Close the recording, then attribute and report. `exc` is the
		workload's exception, if any: it always wins, so nothing here raises
		while it propagates."""
		if session._closed:
			return
		if session._started and os.getpid() != session._pid:
			# a forked child: the sampler, the file and the report are the parent's
			session._closed = True
			return
		errors = []
		try:
			session._close_recording()
		except Exception as e:
			errors.append(("stopping the recording", e))
		if self.start_failed:
			session.profile_status = "failed_to_start"
			return
		if not session._started:
			return
		if self.under_run:
			session.profile_status = "recorded_for_run"
			return
		code = session.sampler_exit_code
		if code not in (0, None):
			errors.append(("the sampler", RuntimeError(
				f"it exited with code {code}; see {session.sampler.power_file}.sampler.log")))
		has_spans = session.n_spans_written > 0
		if not has_spans and not self.quiet:
			self._explain_no_spans(session)
		if has_spans and self.report:
			try:
				self._report(session)
			except Exception as e:
				errors.append(("attribution and reporting", e))
		if not errors:
			session.profile_status = (("complete" if self.report else "recorded") if has_spans
			                          else "no_spans")
			return
		session.profile_status = "incomplete"
		session.profile_error = errors[0][1]
		failed = "; ".join(f"{stage} failed ({type(e).__name__}: {e})" for stage, e in errors)
		message = (f"agent-energy-profiler: profile incomplete: {failed}. The raw files are "
		           f"kept in {session.out_dir}. To attribute and report them: "
		           f"agent-energy-profiler attribute {session.path} --power "
		           f"{session.sampler.power_file} && agent-energy-profiler report "
		           f"{session.out_dir}")
		if self.strict and exc is None:
			raise RuntimeError(message) from errors[0][1]
		if not self.quiet:
			_say(message)

	def _explain_no_spans(self, session):
		"""No span means no operation-level data: say so, and give the
		whole-session measurement the sampler made, as `run` does for a program
		without spans. Nothing is attributed to an operation."""
		dropped = session.n_spans_dropped
		lines = [f"agent-energy-profiler: no spans were recorded"
		         + (f" ({dropped} could not be written)" if dropped else "")
		         + ", so there is no operation-level data. Wrap work in "
		         "`session.span(label)` to measure it per operation."]
		try:
			lines += self._whole_session(session)
		except Exception:
			pass            # the explanation must not fail; power.csv is kept either way
		lines.append(f"  hardware timeline: {session.sampler.power_file}")
		_say("\n".join(lines))

	@staticmethod
	def _whole_session(session):
		"""Lines with the sampler's whole-session readings (device-level)."""
		from . import attribution_v2
		from .cli import _fmt_domain
		with open(session.path) as f:
			records = [json.loads(line) for line in f if line.strip()]
		start, end = records[0]["started_unix_s"], records[-1]["ended_unix_s"]
		timeline = attribution_v2.Timeline(session.sampler.power_file)
		lines = [f"  whole session, {end - start:.2f} s (device-level: everything on the "
		         f"devices, not split by operation):"]
		if timeline.timeline_meta is not None:
			counter = timeline.run_counter(start, end)
			if counter:
				span = (f", {counter['duration_s']:.2f} s" if counter.get("duration_s")
				        is not None else "")
				lines.append(f"    GPU, sampler run counter ({counter['n_readings']} "
				             f"readings{span}): {_fmt_domain(counter)}")
		window = attribution_v2._window_from_bounds(start, end, timeline, session.resolution)
		for domain, value in ((window or {}).get("domains") or {}).items():
			lines.append(f"    {domain}, sampled counter: {_fmt_domain(value)}")
		return lines

	def _report(self, session):
		from . import report as _report
		rep = _report.analyze(session)
		session.report = rep
		text = _report.render_text(rep)
		with open(os.path.join(session.out_dir, "report.json"), "w") as f:
			json.dump(rep, f, indent=2)
			f.write("\n")
		with open(os.path.join(session.out_dir, "report.txt"), "w") as f:
			f.write(text + "\n")
		if not self.quiet:
			_say("\n" + text)
