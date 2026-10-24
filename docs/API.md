# Public API (0.5.0)

What is covered by the compatibility policy in [CHANGELOG.md](../CHANGELOG.md):
a change to anything listed here is announced one minor release ahead with a
warning. Names not listed (anything starting with `_`, helpers inside
modules, the internal layout of `report.build()` output beyond the documented
keys) may change without notice. File formats are specified separately:
[SCHEMA_V2.md](SCHEMA_V2.md) (v2 spans), the v1 event schema in
`agent_energy_profiler.schema`.

## Recording (schema v2)

```python
from agent_energy_profiler import Session
```

| API | Notes |
|---|---|
| `Session.profile(name=None, *, out_dir=None, mode=None, hardware_report=None, attributes=None, report=True, strict=False, quiet=False, **kwargs)` | **the recommended entry point** (0.5.0). A `Session` that creates its run directory (`agent-energy-profiler-runs/<name>-<timestamp>/` unless `out_dir`, which is refused if it already holds any file a profile writes), runs a sampler at 10 Hz with the mode's GPU counter policy, and on close attributes the run, saves `report.json` and `report.txt` (the full report), and prints the brief summary (`brief.render`) to stderr. `report`: `True` or `"brief"` (default), `"full"` (print the full report instead), `False` (record only); anything else raises `ValueError`. `hardware_report` sets both resolution and disturbance. `kwargs`: other `Session` arguments except `path`, `sampler`, `overwrite`, `resolution`, `disturbance` and `timeline` (`TypeError`). Under `agent-energy-profiler run` it is `Session.from_env`: no sampler of its own, no report |
| `session.out_dir`, `.report`, `.profile_status`, `.profile_error` | set by `Session.profile` (`None` on other sessions). `out_dir` is known once the session has started. `profile_status` after close: `"complete"`; `"incomplete"` (stopping the recording, the sampler or attribution failed; `profile_error` holds the first exception; raw files kept); `"recorded"` (`report=False`); `"no_spans"` (no operation-level data: nothing is attributed, and the sampler's whole-session readings are printed, device-level); `"recorded_for_run"`; `"failed_to_start"` |
| `Session(path, *, attributes=None, gpu="auto", resolution=None, sampler=None, overwrite=False, mode=None, disturbance=None, timeline=None, hooks=())` | context manager (`with`), or `start()` / `close()`. `mode=None` → `auto` since 0.4.0 (`direct` before; [MEASUREMENT_MODES.md](MEASUREMENT_MODES.md#sdk-default-mode-migration)). `mode` may change from `direct` to `sampled` at `start()` when an `auto` session's disturbance test came from another configuration |
| `Session.from_env(default_path=None, *, attributes=None, default_mode=None, **kwargs)` | configured by `agent-energy-profiler run` when run under it; otherwise `default_path` and `default_mode` (default: `Session`'s, `auto`) |
| `session.span(name, *, attributes=None, parent=None, trace_id=None)` | sync or async context manager yielding a `SpanHandle`; `name` is the operation label and is required |
| `session.trace(name, *, attributes=None)` | decorator for sync and async functions; the label is required |
| `session.bind(**attributes)` | sync or async context manager: attributes added to every span opened inside |
| `session.current_span()` | innermost open `SpanHandle` in this context, or `None` |
| `session.mode`, `.requested_mode`, `.mode_reason`, `.mode_source`, `.span_counter_reads`, `.session_id`, `.path`, `.closed` | read-only; final once the session has started |
| `SpanHandle`: `.span_id`, `.trace_id`, `.parent_span_id`, `.name`, `.session_id`, `.external_parent`; `set_attribute(k, v)`, `set_attributes(**kv)`, `set_tokens(input=, output=)`, `set_status(status)`; `handle[key] = value` | |

`agent_energy_profiler.measurement`:

| API | Notes |
|---|---|
| `Resolution.unknown()`, `.configured(gpu_update_interval_s, source)`, `.from_hardware_report(path_or_dict)` | GPU counter resolution recorded with the session |
| `CounterDisturbance(verdict, source, ...)`, `.unknown()`, `.from_hardware_report(path_or_dict)` | input to `mode="auto"`; records the tested `driver_version`, `hostname` and `gpu_models` |
| `resolve_mode(requested, disturbance=None, current=None)`, `current_configuration(reader=None)`, `configuration_mismatch(disturbance, current)` | how `auto` decides (0.4.0: `current` is the running configuration); never runs a diagnostic |
| `CallableReader(read, devices=None)` | custom or synthetic GPU counter (`read()` → cumulative mJ per device) |
| `MODE_DIRECT`, `MODE_SAMPLED`, `MODE_HYBRID`, `MODE_AUTO`, `MODES`, `SESSION_DEFAULT_MODE` | mode names, their properties, and `Session`'s default |
| `SessionDefaultModeWarning` | **deprecated**: no longer emitted since 0.4.0; kept for code that filters it, removed in a later minor release |

`agent_energy_profiler.sampling.Sampler(power_file, hz=10.0, ..., counter_hz=None, watch_parent=False)`:
the out-of-process hardware sampler, passed as `Session(sampler=...)`.
`counter_hz=None` reads the GPU energy counter on every row; pass the
mode's `MODES[mode]["timeline_counter_hz"]` (as `Session.profile` and
`run` do) to avoid frequent counter reads. `watch_parent=True` (0.5.0) makes
the sampler stop by itself once the process that started it has exited.

### Lifecycle and failures of `Session.profile`

| Phase | Behaviour |
|---|---|
| `Session.profile(...)` | nothing is created or started; a missing `hardware_report` file raises here |
| `start()` / `with` | creates the directory, starts NVML and the sampler. A taken `out_dir` (`FileExistsError`) or a sampler that produces no samples raises **before your code runs**; `profile_status` is then `"created"` or `"failed_to_start"` |
| your code raises | the exception propagates unchanged; the run is still attributed and reported |
| close: sampler or attribution fails | recorded in `profile_status` / `profile_error`, a message names the run directory and the commands to retry; never raised over your exception. With `strict=True` and no exception from your code, close raises `RuntimeError` from the failure |
| never closed | not attributed; the sampler stops itself when the process exits (`watch_parent`). No exit handler is installed |
| forked child calls `close()` | nothing happens: the sampler, file and report belong to the parent |

## Attribution and reports

| API | Notes |
|---|---|
| `analyze(spans, power=None, out_dir=None, resolution_report=None, allow_uncovered=False)` | a closed `Session` or a spans file → attributed run directory and the report dict |
| `report.build(path, resolution=None)` | report dict of an attributed (or raw) v1/v2 run |
| `report.render_text(rep, top=20)`, `report.operations_csv(rep)` | text report, per-operation CSV |
| `view_model.build(path, *, attributes=(), all_attributes=False, max_instances=10000, resolution=None, name=None, redact_labels=False)`, `view_model.validate(view)`, `html_report.write(path, out, **build_kwargs)`, `html_report.render(view)` | the HTML report (0.5.0): a checked, versioned view model (`VIEW_VERSION`) and the self-contained page built from it ([HTML_REPORT.md](HTML_REPORT.md)). Attributes are left out unless allowed; host names, paths and ids always; operation labels are kept unless `redact_labels=True` |
| `brief.build(path, resolution=None)`, `brief.render(summary, top=10)` | the brief summary (0.5.0): operations ranked by energy where comparable, coverage, caveats. `build` returns a dict (`basis`, `fallback_reason`, `rows`, `whole_run`, `coverage`, `warnings`); its layout may change, the report dict's does not. Not written to any file |
| `attribution_v2.attribute_run(spans, out_dir, power="", power_meta="", resolution_report="", allow_uncovered=False)` | the `agent-energy-profiler attribute` step for v2 |
| `carbon.CarbonIntensity`, `carbon.convert(joules, intensity)` | explicit conversion with caller-supplied grid intensity |

## OpenTelemetry (`[otel]` extra)

`agent_energy_profiler.otel` — never imported by the core:
`Correlation(propagate=False)`, `export_spans(path, exporter, resource=None, batch_size=512)`,
`to_readable_spans(path, resource=None, stats=None)`, `span_attributes(record, header=None)`,
`session_attributes(header)`, `JsonLinesSpanExporter(path)`, `MAPPING_VERSION`.
Attribute names and meanings: [OPENTELEMETRY.md](OPENTELEMETRY.md).

## Command line

`agent-energy-profiler validate | run | attribute | report | export | otel-export | sample`
(`agent-energy-profiler --help`; equivalently `python -m agent_energy_profiler`).
`report --brief` prints the brief summary (text only: with `--format json`
or `csv` it exits 2). `report --html FILE [--attributes NAMES | --all-attributes] [--redact-labels]
[--max-instances N]` writes the HTML report.
The older single-purpose scripts remain under the same prefix:
`agent-energy-profiler-sample`, `-attribute`, `-attribute-v2`, `-validate`,
`-aggregate`, `-visualize`. Before 0.4.0 (unpublished) these were `aep` and
`aep-*`.

Interfaces that keep the short prefix `aep` and are covered by this policy:
the environment variables `AEP_SPANS_FILE`, `AEP_RESOLUTION`, `AEP_GPU`,
`AEP_RUN_ATTRIBUTES` and `AEP_MEASUREMENT` (set by `agent-energy-profiler
run`, read by `Session.from_env`), the OpenTelemetry attribute namespace
`aep.*`, and the recorded `mode_source` value `"aep_run"`.

## Legacy (schema v1)

`profiler.span`, `profiler.configure` and the v1 pipeline (`attribution`,
`trajectory`, `aggregate`, `visualize`) are kept unchanged for the research
artifacts they produced. New code should use `Session`.
