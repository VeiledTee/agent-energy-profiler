# Schema v2: span records and energy provenance

Schema v2 is **additive**. Schema v1 (`events.jsonl`, written by
`agent_energy_profiler.events` / `profiler`) is frozen and still written,
read and attributed exactly as before. Schema v2 is written by the
`Session` API (`agent_energy_profiler.session`) and attributed by
`agent_energy_profiler.attribution_v2`. A v2 reader also loads v1 files
(see [Reading v1 as v2](#reading-v1-as-v2)).

This document defines every v2 field **before** it is implemented. The code
(`schema_v2.py`) refers back here; `schema_v2.validate()` enforces the shapes.

## 1. Four kinds of energy value

| Kind | Where it lives | Meaning |
|---|---|---|
| **Measured** | `energy.<domain>.energy_j` with `status = "measured"` | A difference of a cumulative hardware energy counter across the span's window. Device-level. |
| **Below resolution** | same field, `status = "measured"`, `below_resolution = true` | Still a counter difference and still reported, unchanged. The window was shorter than the counter's characterised update interval, so the reported energy is quantised (typically exactly 0 J or one update step) and may not reliably represent the span's consumption. It is a reading, not an estimate of the operation's energy. |
| **Missing** | `energy.<domain>.energy_j = null`, `status = "unavailable"` or `"invalid"`, with a `reason` | No defensible counter difference exists. Never 0. |
| **Estimated** | `estimates.<domain>` only | A model-derived value (today: trapezoidal integration of sampled board power). Never placed in `energy`. Never enters `total`. |

A measured `0.0` and a missing `null` are different values. `0.0` means the
counter did not advance during the window; for short windows that is the
expected quantisation outcome, not evidence that no energy was used.

### Resolution is a flag, not a threshold of truth

`below_resolution` compares the span's duration with the **median** counter
update interval that was characterised for the host. It is not a guaranteed
minimum measurable duration: a window longer than the interval still carries
up to one update step of quantisation error at each edge, and a window shorter
than it can by chance contain one update. `update_intervals` (duration ÷
interval) is therefore also recorded, so analysis can apply its own cut-off.

If no interval was characterised, `below_resolution` and `update_intervals`
are `null` ("unknown"), never `false`.

## 2. Records

A v2 file (`spans.jsonl`) is JSON lines. Every record carries
`schema_version: 2` and a `record_type`.

### 2.1 `session_start` (first line)

| Field | Type | Meaning |
|---|---|---|
| `session_id` | str | 32 hex chars, unique per session |
| `profiler_version` | str | `agent_energy_profiler.__version__` that wrote the file |
| `started_unix_s` | float | wall clock (`time.time()`) |
| `started_monotonic_s` | float | `time.monotonic()`; spans use the same clock |
| `process_id` | int | writer process |
| `host` | object | `platform`, `python`; no hostname unless the caller adds one |
| `config.energy_scope` | `"device"` | every energy value in this file is whole-device energy. `"process"` is reserved; nothing in this package emits it |
| `config.gpu` | object | `enabled`, `reader` (`"nvml"`, `"custom"`, `"disabled"`), `devices` (index and name of each counter read), `counter`, `counter_unit` |
| `config.resolution` | object | see 2.4 |
| `config.sampler` | object | `enabled`, `power_file`, `hz` |
| `config.measurement` | object | measurement mode and its provenance (section 6). Absent in sessions written before Milestone 5 |
| `attributes` | object | caller-supplied run context (run id, dataset, model, ...) |

### 2.2 `span`

| Field | Type | Meaning |
|---|---|---|
| `session_id` | str | owning session |
| `trace_id` | str | 32 hex; shared by a root span and all its descendants |
| `span_id` | str | 16 hex; unique within the session |
| `parent_span_id` | str or null | enclosing span in the same task/thread context, or an explicit parent |
| `name` | str | operation label supplied by the caller. Never inferred |
| `operation_type` | str | the label's `type:` prefix, `""` if none |
| `status` | `ok`/`error`/`timeout`/`cancelled` | how the span ended (same vocabulary as v1) |
| `error_type` | str or null | exception class name for `error` and `cancelled` |
| `start_unix_s`, `end_unix_s` | float | wall clock; used to join with `power.csv` |
| `start_monotonic_s`, `end_monotonic_s` | float | monotonic clock; used for durations and nesting |
| `duration_s` | float | `end_monotonic_s - start_monotonic_s`; never negative. The GPU counter is read before the start timestamps and after the end timestamps, so the duration excludes the read calls and the counter window brackets it |
| `clock_anomaly` | bool | the wall clock went backwards during the span |
| `process_id`, `thread_id` | int | where the span ran |
| `task` | str or null | asyncio task name, if the span ran inside a task |
| `other_sessions_active` | bool | another `Session` in this process had a span open at the same time. The device energy then includes that session's work |
| `input_tokens`, `output_tokens` | int or null | set by the caller |
| `attributes` | object | caller-supplied |
| `energy.gpu` | domain measurement (2.3) | in-band GPU counter difference |

### 2.3 Domain measurement

| Field | Type | Meaning |
|---|---|---|
| `energy_j` | float or null | the counter difference in joules. Non-null **only** when `status = "measured"` |
| `status` | `measured`/`unavailable`/`invalid` | `unavailable`: no reading could be taken. `invalid`: readings exist but their difference is not a defensible energy (e.g. the counter went down) |
| `reason` | str or null | null when measured; otherwise one of the reason codes in 2.5 |
| `source` | str | the instrument, e.g. `nvml_total_energy_counter`, `rapl_powercap` |
| `method` | str | `in_band_counter_delta` (read at the span boundaries in the working process) or `sampled_counter_interpolated` (out-of-band samples, linearly interpolated to the span edges) |
| `scope` | `"device"` | whole-device energy, all processes |
| `below_resolution` | bool or null | see section 1; null = resolution unknown |
| `update_intervals` | float or null | `duration_s / update_interval_s`; null when unknown |
| `raw` | list | the readings the value came from, unmodified. In-band GPU: `[{"device", "start_mj", "end_mj"}]`, with null for a failed read. Sampled: `[{"column", "samples_in_window"}]` |

Multi-device rule (unchanged from M1): a domain is measured only if every
device yields a valid difference; otherwise it is `unavailable` or `invalid`.

### 2.4 Resolution (`config.resolution`)

| Field | Meaning |
|---|---|
| `gpu_update_interval_s` | median NVML counter update interval, or null |
| `basis` | `unknown`, `configured` (the caller supplied a number), `hardware_report` (taken from an `agent-energy-profiler-validate --json` report), `probed` (measured by this session at start) |
| `source` | free text: report path, or the caller's description |
| `characterized_at_unix_s` | when it was measured, if known |
| `updates_observed` | number of counter updates behind the median, if known |
| `rapl_update_interval_s` | always null: RAPL update characteristics have not been measured on any host this project has used |

### 2.5 Reason codes

| Code | Meaning |
|---|---|
| `disabled` | the caller turned this domain off |
| `no_counter` | the host exposes no readable counter (no NVML, no device, unsupported register; no RAPL column) |
| `read_failed` | a device read failed at the start or end of the span |
| `counter_decrease` | an in-band end reading was lower than the start reading (reset, driver reload, or device change) |
| `counter_discontinuity` | the sampled counter went down inside the window and could not be explained as one wrap |
| `insufficient_samples` | fewer than two usable samples for the column |
| `clock_anomaly` | the window's wall-clock lookup is unreliable (negative difference after a backward clock step) |
| `not_covered` | the span lies outside the sampler's timeline |
| `devices_changed` | the set of devices differed between the start and end reading |
| `not_read_by_mode` | the session's measurement mode does not read the counter per span (`sampled`, `hybrid`) |
| `counter_not_sampled` | the hardware timeline declares a sparse counter policy, so this window has no counter readings close enough to measure it (the run counter is reported instead) |

### 2.6 `session_end` (last line, when the session is closed)

`ended_unix_s`, `n_spans_written`, `n_spans_dropped` (recording errors),
`n_foreign_process_writes_refused` (fork safety, see README),
`sampler_exit_code`, and `n_hook_errors` when hooks were configured
(section 7).

## 3. Attributed spans (`spans_attributed.jsonl`)

`attribution_v2` copies every raw span field and adds:

| Field | Meaning |
|---|---|
| `energy.cpu_package`, `energy.dram` | domain measurements from the sampled RAPL counters (`method = sampled_counter_interpolated`). `below_resolution` is null: RAPL resolution is uncharacterised |
| `energy.cpu_core` | the same, with `"diagnostic": true`. Never in `total` |
| `estimates.gpu` | `{energy_j, status: "estimated" or "unavailable", method: "power_integration", detail}` |
| `total.energy_j` | gpu + cpu_package + dram, only if all three are measured; else null |
| `total.status` | `measured` or `incomplete` |
| `total.domains_measured`, `total.domains_missing` | lists |
| `total.below_resolution` | true if any measured term is below resolution, null if any is unknown and none is below, else false |
| `accounting` | section 4 |

## 4. Nested-span accounting (`accounting`)

| Field | Meaning |
|---|---|
| `depth` | 0 for a root |
| `parent_missing` | the parent id is not in the file; the span is treated as a root |
| `n_children` | direct children |
| `children_overlap` | some direct children overlap each other in time |
| `concurrent_spans` | spans that overlap this one in time and are neither its ancestors nor its descendants |
| `attribution` | `sole` if `concurrent_spans == 0` and `other_sessions_active` is false; otherwise `shared`. "Sole" is among *recorded* spans only: un-instrumented work, other processes and background server activity on the same device are invisible to it |
| `inclusive_duration_s` | `duration_s` |
| `exclusive_duration_s` | `duration_s` minus the union of the children's intervals clipped to this span |
| `exclusive.<domain>` | `{energy_j, status, reason}` |

**Exclusive energy** of a domain is `inclusive − Σ children inclusive`, and is
reported (`status = "measured"`) only when all hold:

1. the span is `sole`;
2. its direct children do not overlap each other and lie inside its window;
3. the span and every direct child have that domain `measured`.

Otherwise `energy_j` is null and `status` is `not_defensible` (reasons:
`concurrent_spans`, `other_sessions_active`, `children_overlap`,
`child_outside_parent`) or `unavailable` (`child_unmeasured`,
`parent_unmeasured`). A negative result is `invalid` (`negative_exclusive`).
For the in-band GPU counter this cannot happen while reads are ordered, because
a parent's start and end reads bracket its children's reads on the same
monotonic counter.

**Inclusive energy is always the device's energy over the span's window.** For
a `shared` span it contains other spans' work; it is still reported because it
is a true statement about the device, and the `attribution` field says it is
not this span's alone.

### Summary totals (`session_summary.json`)

Totals never add a parent and its children. The session total is the sum of
**root** spans' inclusive energy, and is reported only when roots do not
overlap one another and every root is measured. With concurrent roots it is
null (`reason: "concurrent_root_spans"`), and the independent whole-window
counter reading (`window.*`, from the sampler timeline) is the only total.
Per-label rollups sum `exclusive` energy and report how many spans were left
out because their exclusive energy was not defensible.

## 5. Which `status` is which

The word `status` appears at five levels. They are separate vocabularies:

| Where | Values | Answers |
|---|---|---|
| span `status` | `ok`, `error`, `timeout`, `cancelled` | how did the work end? |
| `energy.<domain>.status` | `measured`, `unavailable`, `invalid` | is there a defensible counter difference? |
| `accounting.exclusive.<domain>.status` | the above plus `not_defensible` | can this span's own share be separated from its children's? |
| `total.status` | `measured`, `incomplete` | are all three boundary domains measured? |
| `estimates.<domain>.status` | `estimated`, `unavailable` | was an estimate computed? |

Two consequences worth knowing:

- `total.below_resolution` is `null` whenever a RAPL term is measured,
  because RAPL resolution is uncharacterised. On a full-boundary host it is
  therefore almost always `null`; read the GPU domain's flag instead.
- For `sampled_counter_interpolated` values, the sampling period (100 ms at
  10 Hz) is a second resolution limit that `below_resolution` does not
  include. Today such values exist for RAPL (flag always `null`) and for the
  session-window GPU reading, which is long.

## 6. Measurement model version 2 (Milestone 5)

Additive. Sessions and timelines written earlier have none of these fields
and are read and attributed exactly as before.

**Session header `config.measurement`:**

| Field | Meaning |
|---|---|
| `model_version` | 2. Absent block = version 1: per-span counter reads, sampler counter rate unrecorded |
| `mode` | `direct`, `sampled` or `hybrid` ([MEASUREMENT_MODES.md](MEASUREMENT_MODES.md)) |
| `requested_mode` | what the caller asked for, e.g. `auto` |
| `mode_reason` | why `mode` was chosen |
| `span_counter_reads` | whether spans read the GPU counter at their boundaries |
| `semantics` | text: what per-span and run GPU energy mean in this mode |
| `timeline` | the sampler's declared reads (as in the sidecar below), or null |
| `counter_disturbance` | `verdict` (`DISTURBED`, `NOT_DETECTED`, `INCONCLUSIVE`, `UNKNOWN`), `source`, `retained_median`, `read_hz`, `measured_at_unix_s`, `driver_version`; since 0.4.0 also `hostname` and `gpu_models` of the tested configuration, present only when the hardware report gave them |

`config.gpu.driver_version` is added when NVML is read.

**Sampler sidecar** (`<power.csv>.meta.json`, version 2) adds `timeline`:
`power_hz`, `power_api`, `counter_policy` (`every_row`, `every_n_rows`,
`boundaries`), `counter_every_n_rows`, `counter_hz_effective`,
`counter_api`, `row_timestamp`. Rows on which the counter was not read have
an empty counter cell. A sidecar without `timeline` means `every_row`.

**Session summary**, only when the timeline declares a `timeline` block:

| Field | Meaning |
|---|---|
| `timeline.declared` | the sidecar's `timeline` block |
| `run_counter` | `{energy_j, status, reason, start_unix_s, end_unix_s, duration_s, n_readings, brackets_all_spans, source, method: "sampled_counter_readings", scope}`: the counter difference between the first and last readings on which every GPU was read. Its window is the readings' own |
| `counter_intervals` | diagnostic comparison of counter energy and integrated averaged power between consecutive readings: `n_intervals`, `median_interval_s`, `counter_j`, `estimate_j_same_intervals`, `counter_over_estimate_median`, `_p10`, `_p90` |

With a sparse policy (`every_n_rows`, `boundaries`), `window.domains.gpu` is
`unavailable` with reason `counter_not_sampled`; with `every_row` it is
computed as before.

## 7. Milestone 6 additions (hooks and trace correlation)

Additive. Each field appears only when its feature is used. Sessions
without hooks write exactly the records of Milestone 5, plus `mode_source`.

| Where | Field | Meaning |
|---|---|---|
| `config.measurement` | `mode_source` | who chose `requested_mode`: `caller`, `default` (the entry point's default) or `aep_run`. Absent in sessions written before M6 |
| `config` | `hooks` | `module.Class` of each hook passed to `Session(hooks=...)`; absent without hooks |
| `span` | `external_parent` | root spans only, when a hook supplied an external trace context at the span's start: `{system, trace_id, span_id, sampled?}`. The span's `trace_id` is then the external trace's. Absent otherwise |
| `session_end` | `n_hook_errors` | hook calls that raised (never propagated to the workload); present when hooks were configured |

`run.json` (`agent-energy-profiler run`) gains `measurement.mode_source`: `default` when
`--mode` was not given, else `caller`.

`external_parent` refers to a span the profiler did not record. Accounting
ignores it: the span is a root (`depth` 0) and `parent_missing` stays false.
Exporters use it to attach the span to the external trace
([OPENTELEMETRY.md](OPENTELEMETRY.md)).

Attributes added with `Session.bind(**attributes)` are ordinary span
`attributes`; the span's own attributes win on a key clash.

## Reading v1 as v2

`schema_v2.read_records(path)` yields v2-shaped spans for v1 lines
(`upgraded_from_schema: 1`):

- `event_id` becomes `span_id`; `parent_span_id` and `trace_id` are null
  (v1 has no hierarchy); `question_id`, `iteration`, `traversal_depth`,
  `step_index`, `dataset`, `paradigm`, `run_id`, `model_*`, `git_commit`,
  `hardware_id` and `meta` move into `attributes`.
- A v1 `gpu_energy_j` from the in-band counter becomes `energy.gpu` measured,
  with `raw` empty (v1 kept no raw reads). A v1 value with
  `gpu_energy_source = "power_integration"` becomes `estimates.gpu`, and
  `energy.gpu` is `unavailable`. This is the v1 → v2 correction of known
  limitation 8; the v1 file itself is not changed.
- v1 has no monotonic clock: `duration_s` is the wall-clock duration and
  `clock_anomaly` is set when it is negative (duration then null).
- Resolution is unknown unless the reader is given one.

Files with `schema_version` above 2 are rejected, not guessed at.

### Moving code from the v1 API

| v1 | v2 |
|---|---|
| `profiler.configure(events_file=..., run_id=...)` | `Session(path, attributes={"run_id": ...})` |
| `profiler.span("llm:plan") as s; s["input_tokens"] = n` | `session.span("llm:plan") as s; s.set_tokens(input=n)` (`s["input_tokens"] = n` also works) |
| `profiler.set_question(q)` | `attributes={"question_id": q}` or a parent span per question |
| `agent-energy-profiler-attribute` | `agent-energy-profiler attribute` (detects the schema) |

| | Schema v1 (`events.jsonl`) | Schema v2 (`spans.jsonl`) |
|---|---|---|
| Written by | `profiler.span`, `profiler.configure` (legacy functional API) | `Session` |
| Attributed by | `agent-energy-profiler attribute` (v1 pipeline, unchanged, byte-identical to the research results) | `agent-energy-profiler attribute` / `agent-energy-profiler-attribute-v2` |
| Hierarchy | none | trace, span and parent ids; exclusive energy |
| Estimates | mixed into `gpu_energy_j` (marked by `gpu_energy_source`) | separate `estimates` |

The v1-only tools (`agent-energy-profiler-aggregate`,
`agent-energy-profiler-visualize`, `trajectory`) do not read v2.
