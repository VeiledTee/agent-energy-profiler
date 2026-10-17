# OpenTelemetry integration (Milestone 6)

`agent_energy_profiler.otel` connects profiler spans to OpenTelemetry
traces. It is optional:

```bash
pip install 'agent-energy-profiler[otel]'    # opentelemetry-api + -sdk
pip install 'agent-energy-profiler[otlp]'    # + OTLP/HTTP exporter for `agent-energy-profiler otel-export --endpoint`
```

The core package never imports OpenTelemetry. Only an explicit
`from agent_energy_profiler import otel` (or `agent-energy-profiler otel-export`) does.
Without the extra, that import fails with an `ImportError` that names it.

Tested with opentelemetry-api/-sdk 1.45.1 (current at the time of writing)
and 1.20.0 (the declared minimum).

## What it is for, and what it is not

It puts the profiler's numbers next to the application's traces, with the
same meaning and provenance they have in `spans_attributed.jsonl`.

The attributes are **not evidence of accuracy**. They carry the profiler's
values unchanged, including what is unknown. Every exported span says
`aep.energy.external_meter_validated = false`: no value has been checked
against an external power meter
([HARDWARE_VALIDATION_PROTOCOL.md](HARDWARE_VALIDATION_PROTOCOL.md)). The
known NVML counter disturbance, the lag of averaged power, and RAPL never
having been checked on physical hardware all apply exactly as in
[MEASUREMENT_MODES.md](MEASUREMENT_MODES.md) and the README's limitations.

## Design: correlate while recording, export after attribution

| Step | When | Needs | What happens |
|---|---|---|---|
| 1. Correlation | while the program runs | `opentelemetry-api` | a root profiler span opened inside an application span joins that trace |
| 2. Propagation (opt-in) | while the program runs | `opentelemetry-api` | while a profiler span is open, it is the current OpenTelemetry span, so application spans nest under it |
| 3. Export | after attribution | `opentelemetry-sdk` | each recorded span becomes an OpenTelemetry span with energy attributes, sent to an exporter you supply |

Why export after the run instead of emitting live spans:

- **Most energy only exists after attribution.** Per-span CPU/DRAM energy,
  GPU estimates (`sampled`/`hybrid` modes), totals and exclusive energy are
  computed from the hardware timeline after the program ends. A live span
  could carry only the in-band GPU value of `direct` mode.
- **One set of ids.** Exported spans keep the profiler's own trace, span and
  parent ids, which are already W3C-sized (128-bit trace, 64-bit span).
  `spans.jsonl`, the attributed file and the trace backend therefore refer to
  the same spans. Live OpenTelemetry spans would get ids from the SDK, a
  second set to reconcile.
- **Recording stays cheap and unchanged.** Correlation reads the current
  context once per root span; propagation attaches and detaches one context
  per span. Both run outside the span's measured window (below).

## 1–2. Correlation and propagation

```python
from agent_energy_profiler import Session
from agent_energy_profiler.otel import Correlation

with Session("spans.jsonl", mode="auto", hooks=[Correlation(propagate=True)]) as session:
    with tracer.start_as_current_span("POST /ask"):          # the application's span
        with session.span("agent:request"):                  # joins the trace above
            with tracer.start_as_current_span("db.query"):   # child of agent:request
                ...
```

- **Correlation** (always on): a root span opened while an OpenTelemetry span
  is current takes that span's trace id and records it:
  `"external_parent": {"system": "opentelemetry", "trace_id", "span_id", "sampled"}`.
  Child spans inherit the trace id through the profiler's own hierarchy. With
  no current OpenTelemetry span, nothing changes.
- **Propagation** (`propagate=True`): while a profiler span is open, a
  non-recording OpenTelemetry span with the profiler span's ids is the
  current span. Application spans started inside therefore become its
  children, and the previous context is restored when it closes. It follows
  `contextvars` exactly as the profiler does: asyncio tasks inherit it, new
  threads do not.
  - The sampling decision of the application's trace is kept. Spans outside
    any application trace are marked sampled.
  - **Caveat:** those application spans point at a parent that a backend
    sees only after you export the profiler's spans (step 3). Without the
    export they appear with a missing parent. That is why it is opt-in.

Both are implemented as a `Session` hook (`Session(hooks=[...])`, see the
`session` module docstring). They run before the span's GPU counter read and
start clocks, and after its end clocks, counter read and record write, so
neither the duration nor the counter window includes them. A hook that raises
is counted in `session_end.n_hook_errors` and never reaches the workload. A
test checks that sessions with and without these hooks record identical
energy and make the same number of counter reads.

**Global configuration is never modified.** The module does not call
`set_tracer_provider`, does not install propagators, and creates no tracer.
It reads the current context through the API (a no-op API returns "no span"),
and in propagation it attaches and detaches context in the caller's own
context, as any instrumentation does.

## 3. Export

```python
from agent_energy_profiler import analyze
from agent_energy_profiler.otel import export_spans

rep = analyze(session)                           # attribute: spans_attributed.jsonl
export_spans("run/", exporter, resource=provider.resource)
```

Or from the command line:

```bash
agent-energy-profiler otel-export runs/x --endpoint http://localhost:4318/v1/traces   # OTLP/HTTP (otlp extra)
agent-energy-profiler otel-export runs/x --out runs/x/otel_spans.jsonl                # JSON lines, offline
agent-energy-profiler otel-export runs/x                                              # console
```

- **Input:** a run directory or spans file: attributed, raw, schema v2 or v1.
  Raw input has only the in-band GPU value; the CLI says so.
- **Exporter:** any `opentelemetry.sdk.trace.export.SpanExporter`. Pass the
  one your application uses so both kinds of span reach the same backend. It
  is not shut down; it stays yours.
- **Resource:** pass your application's (`provider.resource`) so the spans
  appear under its service. Default: `service.name = agent-energy-profiler`.
- **Batches** of 512. Returns `{n_spans, n_skipped, n_batches,
  n_failed_batches, attributed}`. A span without a start time (a v1 event
  with a broken clock) cannot be placed on a timeline and is skipped and
  counted.

### Span fields

| OpenTelemetry | From |
|---|---|
| name | the span's label (`name`), as the caller gave it |
| trace id, span id | `trace_id`, `span_id` unchanged |
| parent | `parent_span_id`; for a root with `external_parent`, that span (remote) |
| start / end time | `start_unix_s`; end = start + `duration_s` (monotonic), so a wall-clock step cannot make a span negative |
| status | `error` and `timeout` → ERROR (description: `error_type` or the status); `ok` and `cancelled` → UNSET. `aep.status` always has the original |
| kind | INTERNAL |
| instrumentation scope | `agent_energy_profiler`, package version |

Schema-v1 events have no ids or hierarchy. Each gets deterministic synthetic
ids derived from its run id and event id, becomes its own root, and is marked
`aep.ids_synthesized = true` and `aep.upgraded_from_schema = 1`.

### Attributes

OpenTelemetry attributes cannot be null, so "missing" is expressed by the
presence of a status and a reason and the **absence** of `energy_j`.

**Profiler fields** (namespace `aep.*`; a caller attribute with the same key
is overwritten):

| Attribute | Meaning |
|---|---|
| `aep.operation.label`, `aep.operation.type` | label and its `type:` prefix |
| `aep.status` | `ok` / `error` / `timeout` / `cancelled` |
| `error.type` | exception class (OpenTelemetry semantic convention) |
| `aep.span.duration_s` | monotonic duration |
| `aep.span.other_sessions_active` | another Session's span overlapped this one |
| `aep.span.clock_anomaly` | only when true |
| `aep.span.task`, `thread.id` | where it ran |
| `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens` | caller-reported token counts (GenAI semantic convention names) |
| `aep.external_parent.system` | the root joined an external trace |

**Energy, per domain** `gpu`, `cpu_package`, `dram`, `cpu_core`
(`aep.energy.<domain>.*`):

| Attribute | Present | Meaning |
|---|---|---|
| `.status` | always | `measured`, `unavailable` or `invalid` |
| `.energy_j` | only when `measured` | counter difference, joules, whole device |
| `.reason` | only when not measured | reason code ([SCHEMA_V2.md](SCHEMA_V2.md) 2.5), e.g. `not_read_by_mode`, `no_counter` |
| `.resolution_known` | when measured | whether the counter's update interval was characterised |
| `.below_resolution` | when measured and resolution known | the window was shorter than one counter update: quantised, biased low |
| `.update_intervals` | when known | duration ÷ update interval |
| `.source`, `.method`, `.scope` | always | instrument, how the value was derived, `device` |
| `.diagnostic` | `cpu_core` | never part of a total |

**Estimates** (`aep.estimate.<domain>.*`; never under `aep.energy`):
`.status` (`estimated` / `unavailable`), `.energy_j` (only when estimated),
`.method` (`power_integration`).

**Total** (`aep.energy.total.*`): `.status` (`measured` / `incomplete`),
`.energy_j` (only when every boundary domain was measured),
`.boundary` (`gpu+cpu_package+dram`), `.domains_measured`,
`.domains_missing`, `.below_resolution` (when known).

**Accounting** (`aep.accounting.*`): `.attribution` (`sole` / `shared`),
`.depth`, `.parent_missing`, `.n_children`, `.children_overlap`,
`.concurrent_spans`, `.exclusive_duration_s`, and per domain
`.exclusive.<domain>.status` (incl. `not_defensible`), `.energy_j` (only
when measured), `.reason`.

**Session provenance, on every span:**

| Attribute | Meaning |
|---|---|
| `aep.otel.mapping_version` | version of this table (1); bumped on any rename or change of meaning |
| `aep.energy.scope` | `device`: every value is whole-device energy |
| `aep.energy.external_meter_validated` | `false` (see above) |
| `aep.session.id`, `aep.profiler.version` | which session and profiler wrote the spans |
| `aep.measurement.recorded` | whether the session recorded its measurement mode (false for pre-M5 sessions and v1) |
| `aep.measurement.mode`, `.requested_mode`, `.mode_source`, `.model_version`, `.span_counter_reads` | the measurement mode and who chose it |
| `aep.measurement.counter_disturbance` | `DISTURBED`, `NOT_DETECTED`, `INCONCLUSIVE`, `UNKNOWN` |
| `aep.resolution.basis`, `aep.resolution.gpu_update_interval_s` | counter resolution provenance |
| `aep.gpu.driver_version` | when NVML was read |
| `aep.session.attributes.<key>` | the session's caller attributes |

**Caller attributes** of the span keep their own keys. Strings, numbers,
booleans and homogeneous lists pass through. Objects and mixed lists become
JSON text, and `null` values are left out. Raw counter readings (`raw`) are not
exported; they stay in `spans.jsonl`.

### Conventions used and not used

- Used: the span model itself (ids, parent, status, resource, scope),
  `error.type`, `thread.id`, and the GenAI `gen_ai.usage.*` token names.
- Not used: there is no semantic convention for per-span energy. The
  hardware conventions' `hw.energy` / `hw.power` are **metrics** of a device
  over time, not attributes of an operation, so the profiler's own `aep.*`
  namespace is used rather than inventing a pseudo-standard. GenAI
  conventions are still marked "development". Only the two token attributes,
  stable in name since 2024, are emitted.

## Performance

Development host (WSL2, Python 3.12, opentelemetry 1.45.1), empty spans,
median of 5 × 20 000; `tools/benchmark_overhead.py`:

| Configuration | No GPU reads | With NVML (`direct`) |
|---|---|---|
| no hooks | 14.4 µs/span | 2486 µs/span |
| `session.bind(...)` active | 15.1 µs (+0.7) | — |
| `Correlation()` under an application span | 17.5 µs (+3.1) | 2495 µs (within noise) |
| `Correlation(propagate=True)` | 21.4 µs (+7.0) | 2487 µs (within noise) |

The hooks' cost is outside the span's measured duration and counter window.
It lengthens the workload by a few microseconds per span, which is negligible
next to the two NVML reads of `direct` mode.

`export_spans` on an attributed 50 000-span nested session: 1.99 s
(~25 000 spans/s), peak RSS 92 MB for the whole benchmark process. Export
streams the file in batches and does not hold all spans.

## Behaviour across measurement modes (0.3.0)

`tests/test_otel_modes.py` records one workload in every mode (`direct`,
`sampled`, `hybrid`, `auto` without evidence → `sampled`, `auto` with
`NOT_DETECTED` → `direct`). Since 0.4.0, `auto` without evidence is also
what a `Session` created without `mode=` records. The workload has nested sync spans, two
concurrent asyncio tasks, an explicitly parented thread and application
spans in between. Each run is attributed against a synthetic power timeline
and exported. In every mode:

- application and profiler spans form one trace with one root, and every
  parent is present;
- `aep.measurement.mode` / `requested_mode` / `mode_source` match the
  session;
- only `direct` carries `aep.energy.gpu.energy_j`, with the counter read
  twice per span. In `sampled` and `hybrid` the GPU counter is not read per
  span: the span says `unavailable` / `not_read_by_mode`, the estimate sits
  under `aep.estimate.gpu.*`, and no total that needs the GPU is emitted;
- overlapping siblings are `shared`, and a sequential child is `sole`;
- the GPU attributes and the number of counter reads are identical with and
  without `Correlation(propagate=True)`.

Missing hardware stays missing in the export:

| Case | Exported GPU fields |
|---|---|
| no GPU (`gpu=False`) | `unavailable`, `disabled` |
| `nvidia-ml-py` installed, no driver | `unavailable`, `no_counter` |
| counter fails mid-run | `measured` spans before the failure, a reason and no number after |
| no power timeline | nothing measured or estimated for any domain, and no total |
| not attributed at all | the recorded fields only, with no accounting |

`bind()` attributes follow asyncio tasks but not new threads, as documented
for the profiler's own spans.

## Limitations

- **Per-span energy is device energy.** Exporting it to a trace backend does
  not make it per-request energy. Concurrent spans are `shared` and the
  attribute says so.
- **No live energy.** Spans reach the backend only when exported after the
  run. Long-running services need periodic sessions (one Session per
  interval, each attributed and exported) — not provided as a helper yet.
- **Propagation across threads** needs the same care as the profiler's own
  spans: new threads start with an empty context
  (`contextvars.copy_context().run(...)` or `asyncio.to_thread` carry it).
- **Duplicate export** of the same file sends the same span ids again;
  backends typically de-duplicate or show duplicates. Export each run once.
- **Clock alignment** between the application's spans (SDK clock) and the
  profiler's (the same `time.time()` wall clock, same process) is exact to the
  float precision of the recorded seconds (~0.2 µs), but spans recorded in
  another process or host are only as aligned as the clocks.
