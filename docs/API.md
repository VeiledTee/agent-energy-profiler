# Public API (0.4.0)

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
| `Session(path, *, attributes=None, gpu="auto", resolution=None, sampler=None, overwrite=False, mode=None, disturbance=None, timeline=None, hooks=())` | context manager (`with`), or `start()` / `close()`. `mode=None` → `auto` since 0.4.0 (`direct` before; [MEASUREMENT_MODES.md](MEASUREMENT_MODES.md#sdk-default-mode-migration)). `mode` may change from `direct` to `sampled` at `start()` when an `auto` session's disturbance test came from another configuration |
| `Session.from_env(default_path=None, *, attributes=None, default_mode=None, **kwargs)` | configured by `aep run` when run under it; otherwise `default_path` and `default_mode` (default: `Session`'s, `auto`) |
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

`agent_energy_profiler.sampling.Sampler(power_file, hz=10.0, ..., counter_hz=None)`:
the out-of-process hardware sampler, passed as `Session(sampler=...)`.

## Attribution and reports

| API | Notes |
|---|---|
| `analyze(spans, power=None, out_dir=None, resolution_report=None, allow_uncovered=False)` | a closed `Session` or a spans file → attributed run directory and the report dict |
| `report.build(path, resolution=None)` | report dict of an attributed (or raw) v1/v2 run |
| `report.render_text(rep, top=20)`, `report.operations_csv(rep)` | text report, per-operation CSV |
| `attribution_v2.attribute_run(spans, out_dir, power="", power_meta="", resolution_report="", allow_uncovered=False)` | the `aep attribute` step for v2 |
| `carbon.CarbonIntensity`, `carbon.convert(joules, intensity)` | explicit conversion with caller-supplied grid intensity |

## OpenTelemetry (`[otel]` extra)

`agent_energy_profiler.otel` — never imported by the core:
`Correlation(propagate=False)`, `export_spans(path, exporter, resource=None, batch_size=512)`,
`to_readable_spans(path, resource=None, stats=None)`, `span_attributes(record, header=None)`,
`session_attributes(header)`, `JsonLinesSpanExporter(path)`, `MAPPING_VERSION`.
Attribute names and meanings: [OPENTELEMETRY.md](OPENTELEMETRY.md).

## Command line

`aep validate | run | attribute | report | export | otel-export | sample`
(`aep --help`; equivalently `python -m agent_energy_profiler`). The older
single-purpose scripts (`aep-sample`, `aep-attribute`, `aep-attribute-v2`,
`aep-validate`, `aep-aggregate`, `aep-visualize`) remain.

## Legacy (schema v1)

`profiler.span`, `profiler.configure` and the v1 pipeline (`attribution`,
`trajectory`, `aggregate`, `visualize`) are kept unchanged for the research
artifacts they produced. New code should use `Session`.
