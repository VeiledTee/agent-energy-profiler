# Provenance

This project started as a copy of the `agent_energy_profiler` package from a
PhD research repository. From the extraction onward, the two are maintained
independently.

## Source

| | |
|---|---|
| Source repository | `Chain-of-Relations-Emissions` (local, WSL path `/home/penguins/Chain-of-Relations-Emissions`) |
| Source branch | `c2-preliminary-interventions` |
| Source commit | `08bbc96dad39b9d059e6e0797d87ec9955830934` (working tree clean at extraction) |
| Source package | `agent_energy_profiler/` (itself extracted in-repo at commit `27253fa`, 2026-08-29) |
| Extraction date | 2026-10-15 |
| Frozen research analysis tag | `c2-analysis-complete-2026-09-29` |

The research repository still contains, and keeps using, its own copy of
`agent_energy_profiler/`. Nothing in it was modified by this extraction.
Research results continue to depend on that in-repo copy, not on this project.

## What was copied

| Here | From the source | Change |
|---|---|---|
| `src/agent_energy_profiler/**` | `agent_energy_profiler/**` (git-tracked files) | none. `diff -r` is empty at extraction |
| `pyproject.toml` | `pyproject.toml` | `src/` package-dir; pytest config; `dev` extra comment |
| `tests/test_aggregation.py` | same name | module paths via `__file__`; recorded-run tests read `AEP_FROZEN_RUNS_DIR` |
| `tests/test_visualize.py` | same name | module path via `__file__` |
| `tests/test_energy_units.py` | same name | dropped the check on research figure CLIs |
| `tests/test_hardware_validation.py` | same name | imports the package instead of the `measurement/` shims; CoR taxonomy check reduced to the schema-version check |
| `tests/test_package_boundary.py` | same name | `src/` paths; host-adapter checks removed (they test the research host) |
| `tests/test_events_schema_v1.py` | `test_energy_measurement.py` (generic classes) | runs against `agent_energy_profiler.events` directly with plain-string labels |
| `tests/test_coverage_gate.py` | `test_run_integrity.py::TestHardwareCoverageGate` | verbatim class |
| `tests/test_clock_and_provenance.py` | `test_profiler_hardening.py` (GitProvenance, ClockSafety, profiler half of CpuDramPreserved) | verbatim classes |
| `docs/source/MEASUREMENT_SPEC.md` | `MEASUREMENT_SPEC.md` | verbatim, historical reference |
| `scripts/git-hooks/commit-msg` | same path | verbatim |

New in this repository: `tests/test_golden_equivalence.py`, `tests/golden/`,
`tools/build_golden_fixtures.py`, `constraints/baseline.txt`, `README.md`,
this file.

## What was not copied

- The research host: `chain_of_relations/` (CoR, ToG and PoG agents, the LLM
  client wrapper, the Freebase backend, the operation taxonomy and its adapter
  `energy_events.py`).
- Research tooling: `measurement/` (the run wrapper `measure_run.py`, the vLLM
  preflight `run_preflight.py`, the SPARQL-based `validate_measurement.py`,
  thesis figure and table scripts, and the compatibility shims).
- All experiment outputs (`measurement/runs/`, `results/`), datasets, model
  weights, Freebase, CodeCarbon output and the research figures.
- Tests of research code: the agents, taxonomy, LLM wrapper, preflight,
  run-integrity tag guard, evaluation and figure scripts.

## Golden fixtures

`tests/golden/` holds four cases. Inputs and expected outputs are both
gzip-compressed, and each `manifest.json` records its provenance.

| Case | Source | Contents |
|---|---|---|
| `cor_webqsp` | `measurement/runs/full_cor-rank-compact_webqsp` | first 5 questions, 1,192 events, 285 power rows |
| `tog_cwq` | `measurement/runs/gemma4b_full_tog_cwq_0918_2049` | first 5 questions, 1,196 events, 625 power rows |
| `pog_cwq` | `measurement/runs/full_pog-rank-cache_cwq` | first 5 questions, 1,093 events, 307 power rows |
| `synthetic_rapl_multigpu` | generated | RAPL package/core/uncore/dram/psys, two GPUs, power-integration fallback, a failed event, nested events, an empty power cell, a backward clock step |

How the fixtures were made:

- The expected artifacts were produced by running the **source**
  implementation (the research repository's own code and virtual environment)
  on exactly these inputs.
- The only redaction is the measurement host's hostname inside `hardware_id`.
- The synthetic case exists because every frozen run came from a GPU-only
  host. The RAPL code paths have never seen real data (see limitations).

Equivalence evidence at extraction:

- All four cases reproduce byte for byte (`tests/test_golden_equivalence.py`).
- The opt-in full-run check (`pytest -m frozen_runs` with
  `AEP_FROZEN_RUNS_DIR` pointing at the research runs) re-attributes three
  complete frozen runs:
  - `full_cor-rank-compact_webqsp` and `full_pog-rank-cache_cwq` match the
    stored artifacts exactly;
  - `gemma4b_full_tog_cwq_0918_2049` matches on every stored field. The
    re-attribution additionally emits the clock-anomaly fields, which were
    added to the source after that run was attributed.

## Known measurement limitations

These were inherited at extraction. Items 6, 7, 11, 13 and 14 were fixed in
Milestone 1 (see [Milestone 1](#milestone-1-correctness-and-performance)).
Items 8, 9, 10 and 12 are addressed in schema v2 only (see
[Milestone 2](#milestone-2-schema-v2-and-structured-tracing)); the v1 path
keeps them, by design, so research artifacts do not change. Items are kept
here, marked, so the numbering stays stable.

**Scope of what is measured**

1. **Device-level, not process-level.** NVML reports whole-GPU energy and
   RAPL whole-package energy. Per-span energy is time-window attribution and
   is valid only on an otherwise idle, single-tenant machine. Concurrent
   requests on a shared server (batching) make per-span attribution invalid.
   `trajectory.py` measures overlap and concurrency but does not prevent this.
2. **GPU counter resolution floor.** The NVML cumulative counter updates at a
   host-specific interval (median ~106 ms on the RTX 4090 validation host).
   Shorter spans read exactly 0 J or one quantum, and are biased low. In the
   CoR frozen run, 84% of events read exactly 0.0 J GPU.
3. **CPU-package/DRAM untested on real hardware.** Every frozen run came from
   a WSL2 host with no RAPL. Wrap handling, multi-socket summation and RAPL
   update characteristics are covered only by synthetic tests.
4. **Asynchronous GPU work.** For a model served by another process, an event
   bounds the request, not the kernels.
5. **No idle subtraction.** Idle draw is characterised, never subtracted.

**Defects deliberately preserved at extraction**

6. **Partial sums.** *(Fixed in M1.1.)* `attribution.sum_counter_domain`
   (multi-GPU) and `attribution.sum_domain` (multi-socket RAPL) returned the
   sum of the available columns when only some were readable, instead of
   null.
7. **RAPL wrap.** *(Fixed in M1.2.)* Wraparound used a hardcoded
   `RAPL_MAX = 2**32` µJ rather than each zone's `max_energy_range_uj`.
8. **Estimates mixed into measured fields.** *(v2: M2.1; v1 unchanged.)* When
   an event has no in-band GPU counter reading, v1 attribution integrates
   sampled power into `gpu_energy_j`, and that value enters
   `measured_energy_j`. It is distinguished only by
   `gpu_energy_source = "power_integration"`, while the trajectory layer
   refuses the same estimate.

**Recorder limitations**

9. **Process-global recorder state.** *(v2: M2.4; v1 unchanged.)* The step
   and iteration counters and the run context are module globals, so
   concurrent tasks (threads or asyncio) interleave them. Forked children
   inherit the open sink.
10. **No span hierarchy.** *(v2: M2.3; v1 unchanged.)* There are no span or
    parent ids. Nested spans each record the full counter delta, so summing
    them double-counts. Overlap is detected, not resolved.
11. **Cancellation status.** *(Fixed in M1.3.)* `span()` recorded
    `KeyboardInterrupt` and `asyncio.CancelledError` as `error`, not
    `cancelled`.
12. **Wall clock only.** *(v2 spans also record the monotonic clock.)* Events
    and samples use `time.time()`. Backward steps are flagged, never
    repaired. The sampler still writes wall-clock time only, so joining spans
    to `power.csv` still depends on it.
13. **Import side effect.** *(Fixed in M1.4.)* Importing
    `agent_energy_profiler.sampling` initialised NVML and scanned RAPL zones.
14. **Scaling.** *(Fixed in M1.5.)* Attribution was O(windows × samples).
    Re-attributing the 230k-event ToG run took 393 s; it now takes ~8.5 s.

## Milestone 1: correctness and performance

Scope: fix measurement defects 6, 7, 11, 13 and 14 and make attribution
scale, without changing validated research measurements and without schema
v2. Event schema v1 is unchanged: no field was added, renamed or re-typed.

### M1.1 Partial multi-device aggregates

*Defect.* `sum_counter_domain` returned the sum of the readable GPU counter
columns when another GPU's column could not be differenced; `sum_domain` did
the same for RAPL sockets. A subset of the hardware was reported as the whole
domain.

*Fix.* Both go through `_sum_complete`: a group sums only when every column
yields a value, otherwise the domain is `null` (and so is
`measured_energy_j`, through the existing `measured_total` rule). A measured
`0.0` is a value and is kept. Single-device groups and complete groups are
unchanged. The per-event power-integration fallback already required every
GPU; that is now pinned by a test.

*Policy, stated once.* A multi-device domain is complete or it is `null`. No
estimate replaces a missing device, and no partial sum is emitted under a
total's name.

### M1.2 RAPL wraparound

*Defect.* Every RAPL counter was unwrapped with a hardcoded `2**32` µJ. The
real range comes from each zone's `max_energy_range_uj` (commonly
~262 kJ, and not equal across domains). The value never reached attribution:
`power.csv` carries only counter readings. The same branch also turned any
negative difference, which arises from a wall-clock backward step rather than
a wrap, into +4,294.967 J. Separately, two samples with the same timestamp
raised `ZeroDivisionError` in `rapl_delta` (the GPU `counter_delta` already
guarded this).

*Fix.*

- `hardware.rapl.max_energy_range_uj` reads each zone's range (None when the
  zone declares none, e.g. amd_energy hwmon, or the file is unreadable or not
  a positive integer).
- The sampler writes a sidecar beside the log, `<power.csv>.meta.json`:
  `{"format": "agent_energy_profiler.power_meta", "version": 1,
  "rapl_max_energy_range_uj": {<column>: <int or null>}}`. `power.csv`
  itself is unchanged, so existing readers are unaffected.
- `attribution` loads the sidecar (`--power-meta`, default
  `<power>.meta.json` if present; absent or malformed means every range is
  unknown) and unwraps each counter with its own range
  (`unwrap_counter`), in sample-read order.
- A decrease counts as exactly one wrap only if the range is known and valid,
  both readings lie in `[0, range]`, and the wrap-corrected increment is below
  half the range. Anything else (reset, possible multiple wraps, unknown or
  contradicted metadata) starts a new segment: windows that span it are
  `null`; windows on either side stay measured.
- A negative difference is `null`, not +2³² µJ. Duplicate timestamps no
  longer raise.

*Behaviour change.* Without a sidecar, a RAPL counter that decreases inside a
window now yields `null` there instead of a guessed wrap; a counter that
never decreases gives exactly the old result. Every frozen run is GPU-only and
the synthetic golden fixture never reaches the wrap branch (checked), so no
reference output changes. `attribution.RAPL_MAX` was removed.

*Known limits of the policy* (pinned by tests so they cannot drift):

- A reset from above half the range is indistinguishable from a wrap.
- A sampler stall longer than one full range hides a wrap inside an increase;
  it cannot be detected from the samples.
- These are synthetic tests of arithmetic and policy. They do not show the
  behaviour of real RAPL hardware, which this project has still not measured.

### M1.3 Cancellation status

*Defect.* `span()` recorded `KeyboardInterrupt` and `asyncio.CancelledError`
(including `wait_for` timeouts) as `error`.

*Fix.* They are recorded as `cancelled`. `meta.exception_type` is kept, the
end mark is taken at the interruption, the operation scope is restored, and
the same exception object propagates. Other `BaseException`s (e.g.
`SystemExit`) keep `error`. asyncio is looked up in `sys.modules`, not
imported.

*Compatibility.* `cancelled` was already in the schema-v1 status vocabulary
(`schema.Status.CANCELLED`), so `schema.is_known_status` and existing readers
accept it. Readers that test `status == "error"` will no longer count
interrupted spans; `trajectory.n_failed_events` and `aggregate` count every
status other than `ok`, so their counts are unchanged.

### M1.4 Import-time hardware initialisation

*Defect.* `import agent_energy_profiler.sampling` called `nvml.init()` and
globbed `/sys/class/powercap` and `/sys/class/hwmon`.

*Fix.* `sampling.init()` does both, once, on first use (`build_header`,
`warn_about_gaps`, `main`). `main` releases NVML through the new
`nvml.shutdown()` in a `finally`. Public names and the CLI are unchanged. No
module in the package touches hardware, imports `pynvml` or starts a thread
at import; a test checks every module with a recording fake NVML and a
recording `glob`.

### M1.5 Attribution performance

*Profile (original code, ToG run, 230,042 events, 699,557 samples, under
cProfile): 398 s.* 385 s (97%) was trajectory `window_energy`:
`integrate_gpu` (169 s) and `counter_delta` (136 s) rebuilt each column's
usable-sample list from all ~700k rows for each of 3,520 trajectories.
Per-event attribution took only 3 s on this run because it has no RAPL
columns and most events carry an in-band counter; with RAPL the same rebuild
ran for every event and every RAPL column.

*Fix.* Each column is filtered once per attribution call
(`usable_samples`, `prepare_rapl`); window lookups then bisect the prepared
lists. The lists are the ones the per-call code built, the integration loop
and interpolation are unchanged, and no prefix sums or reordering were
introduced, so floating-point results are bit-identical. The public per-call
functions keep their signatures.

*Benchmarks* (this machine, WSL2, Python 3.12.3; wall clock; not profiled):

| Input | Events / samples | Original (`69a7e24`) | M1 | Artifacts |
|---|---|---|---|---|
| ToG frozen run (GPU only) | 230,042 / 699,557 | 392.7 s | 8.4–8.6 s (3 runs) | identical |
| Synthetic, seed 20261015 (2 GPUs + RAPL) | 5,000 / 15,268 | 26.3 s | 0.19 s | identical |
| Synthetic, seed 20261015 | 10,000 / 30,400 | 108.7 s | 0.37 s | identical |
| Synthetic, seed 20261015 | 230,000 / 695,425 | not run (quadratic fit: ~16 h) | 12.1 s | — |

The original's synthetic time grows 4.1× when events double: the quadratic
behaviour. The ~16 h figure is an extrapolation from the 10k point, not a
measurement. Some of the M1 ToG timings overlapped another test process on
this 16-core machine.

`tools/benchmark_attribution.py` regenerates the synthetic runs from a seed
and compares against any source tree.

### Regression coverage

| File | Covers |
|---|---|
| `tests/test_partial_aggregation.py` | complete, incomplete, measured-zero and unavailable groups for multi-GPU and multi-socket, through to `measured_energy_j` |
| `tests/test_rapl_wraparound.py` | normal deltas, one and two wraps, per-counter ranges, missing/invalid metadata, readings above range, reset, ambiguous wrap, clock step, duplicate timestamps, sidecar loading, sysfs range discovery, CLI end to end with and without sidecar |
| `tests/test_span_cancellation.py` | ok, error, `KeyboardInterrupt`, sync and async `CancelledError`, `wait_for` timeout, `SystemExit`, scope restoration, sibling tasks |
| `tests/test_import_side_effects.py` | no NVML/sysfs/thread at import of any module, import without `pynvml`, lazy and idempotent init, sidecar written and NVML shut down by `main` |
| `tests/test_attribution_equivalence.py` | exact (`==`) equality with verbatim copies of the original functions on randomised timelines; each column filtered once per call |

Each of these fails against the extraction commit (43 failures), except the
import tests, which target `src/`; the original import was checked separately
and calls `nvmlInit` plus two sysfs globs.

### Unresolved after M1

- **Estimates in measured fields (item 8)** are unchanged: the
  power-integration fallback still fills `gpu_energy_j` and enters
  `measured_energy_j`, marked only by `gpu_energy_source`. Separating it is a
  schema change and belongs to a later milestone.
- **GPU counter decreases.** `counter_delta` still differences the NVML
  counter as-is. The register is 64-bit and does not wrap in practice, but a
  driver reload or GPU reset would give a negative delta. Left unchanged to
  keep GPU-only results identical; worth the same segment treatment as RAPL.
- **Sidecar coverage.** Only this package's sampler writes
  `<power.csv>.meta.json`. Logs from the research repository's sampler, or
  hand-made ones, carry no ranges, so any RAPL decrease in them is `null`.
- **Cancellation** recognises `KeyboardInterrupt` and asyncio cancellation
  only. trio cancellation and `GeneratorExit` from an abandoned generator are
  still recorded as `error`.
- **More nulls on mixed hosts.** After M1.1, a host where one GPU's counter is
  unsupported reports GPU energy `null` instead of the other GPUs' sum. That is
  the intended policy, but it is visible.
- The textual references to the research repository listed below were left
  in place: changing them was not needed for any M1 fix.

### Validation

Run on 2026-10-15, before the M1 commit:

- Baseline before changes: commit `69a7e24`, 268 passed, 8 skipped.
- Full suite after M1: 333 passed, 8 skipped (the same eight tests, which
  need frozen research data). With `AEP_FROZEN_RUNS_DIR` set: 334 passed, 7
  skipped (those seven need runs `gemma4b_v2_*`, which are not in the research
  directory).
- Golden fixtures: all four cases (`cor_webqsp`, `tog_cwq`, `pog_cwq`,
  `synthetic_rapl_multigpu`) reproduce byte for byte. No fixture was
  regenerated.
- Frozen runs (`pytest -m frozen_runs`, data read only): all three
  re-attribute with the same results recorded at extraction (CoR and PoG
  exact; ToG exact on every stored field plus the additive clock-anomaly
  fields).
- Wheel: built with `pip wheel`, installed into a fresh virtual environment;
  the golden and all M1 regression tests pass against the installed copy. The
  only failure there is `test_package_under_test_is_this_repository`, which by
  design asserts the package is imported from `src/`. The wheel carries
  `LICENSE` (`License-File: LICENSE`, `License: MIT`).
- The research repository was not modified: HEAD `08bbc96`, clean working
  tree, index unchanged.

## Milestone 2: schema v2 and structured tracing

Scope: an additive schema v2 with explicit energy provenance, resolution
awareness, nested spans, an explicit `Session` API, and NVML counter validity.
Schema v1 is untouched: its writer (`events.py`, `profiler.py`), its
attribution (`attribution.py`), `trajectory`, `aggregate` and `visualize`
produce byte-identical output, which the golden fixtures and the frozen runs
confirm. Field definitions were written first, in
[SCHEMA_V2.md](SCHEMA_V2.md), and the code follows them.

New modules: `measurement.py` (measurement model, `Resolution`, GPU readers),
`schema_v2.py` (validator, v1 → v2 reader), `session.py` (`Session`),
`accounting.py` (inclusive/exclusive), `attribution_v2.py`
(`aep-attribute-v2`), and `sampling.Sampler`. Version 0.2.0.

### M2.1 Energy provenance

Every domain value is an object with `energy_j`, `status`
(`measured`/`unavailable`/`invalid`), `reason`, `source`, `method`
(`in_band_counter_delta` or `sampled_counter_interpolated`), `scope`
(always `device`), `below_resolution`, `update_intervals` and the unmodified
`raw` readings. `energy_j` is non-null only when `measured`. Integrated sampled
power appears only in `estimates.gpu` and never enters `total`. Each
`session_start` record carries the profiler version and the measurement
configuration (GPU reader and devices, resolution and how it was established,
sampler settings, energy scope).

### M2.2 Resolution awareness

`Resolution` records the GPU counter's median update interval and its basis:
`unknown`, `configured` (a source description is mandatory),
`hardware_report` (from `aep-validate --json`), or `probed` (measured at
session start with `validation.probe_nvml_resolution`). A measured value
shorter than the interval keeps its value and gets `below_resolution: true`;
with no characterisation the flag is `null`, never `false`. The ratio
`update_intervals` is recorded because the median interval is not a hard
floor. RAPL resolution is recorded as unknown: it has never been measured.

*Note on the interval.* The M2 brief cites ~100.6 ms for this machine. The
spec and validation runs recorded at extraction give a median of 106.1–107.1
ms (RTX 4090, six runs). The code hardcodes neither; the value comes in with
its provenance, and reconciling the two figures is an open measurement task.

### M2.3 Nested spans and accounting

Spans carry `trace_id`, `span_id` and `parent_span_id`. Parents come from
contextvars, so sync and async nesting both work and concurrent asyncio tasks
become overlapping siblings. `accounting.annotate` adds depth, children,
`concurrent_spans` (overlapping spans that are neither ancestors nor
descendants, counted with sorted-endpoint bisection), `attribution`
(`sole`/`shared`), inclusive and exclusive duration, and exclusive energy per
domain.

Exclusive energy = inclusive − Σ direct children, only for a `sole` span with
sequential, contained, measured children. For the in-band GPU counter it is
computed from the raw integer mJ readings, so it is exact and cannot be
negative while reads are ordered (a parent's reads bracket its children's on
the same monotonic register). For interpolated float domains, a negative
result within the floating-point round-off bound of the subtraction is 0;
beyond it the value is `invalid`. Summary totals add root spans only, and
only when roots do not overlap; otherwise the total is null and the
independent whole-window counter reading stands alone. Per-label rollups add
exclusive energy and count the spans left out.

Concurrent work is never split: overlapping spans are `shared` and keep their
inclusive (device-window) energy with that label.

### M2.4 Session and concurrency

`Session` is an explicit object: no global default session, one file per
session (exclusive create unless `overwrite=True`), a lock per session for
writes, contextvars for the current span, `with`/`async with` and a
`trace(label)` decorator for sync and async functions (the label is
mandatory; function names are never used). `Session` starts and stops an
optional `Sampler` subprocess so the timeline brackets every span, and closes
cleanly when the body raises.

Supported: threads (separate stacks; explicit `parent=` to link across a
thread), asyncio (task-local context), several sessions in one process (their
spans are marked `other_sessions_active` when they overlap, because device
energy cannot be separated). Not supported: attribution across processes. A
forked child refuses to write through an inherited session, and nothing
separates device energy between processes.

The v1 functional API is preserved unchanged as the compatibility layer, and
`SpanHandle` accepts v1-style `s["input_tokens"] = n`, so moving a call site
from `profiler.span` to `session.span` needs no body changes. A v1 file can be
read and attributed as v2 (`schema_v2.read_records`, `aep-attribute-v2`).

### M2.5 NVML counter validity

*Finding.* NVML's total-energy register is 64-bit mJ and does not wrap in
practice, so a decrease means a reset, driver reload or device change. v1
handles this inconsistently: `events.record` silently nulls an in-band
decrease, while `attribution.counter_delta` differences the sampled timeline
blindly and can return a negative or understated window energy. That v1
behaviour is unchanged and pinned by a test.

*v2.* In-band: any device whose end reading is below its start makes the
domain `invalid` (`counter_decrease`); a failed read is `unavailable`
(`read_failed`); a changed device count is `devices_changed`; raw readings
are kept in every case. Timeline: the sampled GPU counter goes through the
same segment logic as RAPL (M1.2) with no wrap range, so a window spanning a
decrease is `invalid` (`counter_discontinuity`). `power.csv` is never
modified.

### What changes for an existing v1 artifact read as v2

Demonstrated on the synthetic golden case (`tests/test_attribution_v2.py`):
every CPU-package, DRAM, core and in-band GPU value equals v1's exactly. The
3 events v1 attributed by power integration keep that value, now in
`estimates.gpu`; their `energy.gpu` and `total` become null, and `total`
lists `gpu` as missing. That is correct under the v2 definitions: the value
is an estimate, so it may not appear as measured energy. v1 output for the
same input is unchanged.

### Regression coverage (M2)

| File | Tests | Covers |
|---|---|---|
| `test_measurement_model.py` | 18 | short/long spans, measured zero, missing reads, unknown resolution, resolution provenance and reports, in-band decrease, multi-device, device changes |
| `test_schema_v2.py` | 13 | validator, every golden v1 event upgrades to a valid v2 span, estimates moved out, context preserved, mixed files, torn lines, future versions rejected |
| `test_session.py` | 29 | lifecycle, header/footer, refusal to share a file, labels required, sync and async nesting, siblings, exceptions, `KeyboardInterrupt`, task cancellation, explicit status, threads, two sessions, fork guard, GPU disabled/absent/failing/reset, sampler lifecycle with a fake NVML, real NVML (opt-in) |
| `test_accounting.py` | 15 | exclusive energy, exact partition of a root, measured zero, overlapping children, unmeasured or outside children, negative exclusive, other sessions, orphans, float round-off, root-only totals, concurrent roots, resolution counts |
| `test_attribution_v2.py` | 10 | parity with the v1 golden output, estimate separation, end to end from a Session, timeline GPU counter reset, coverage refusal and opt-in, no timeline, reason codes |

`test_import_side_effects.py` now also imports the five new modules and checks
they touch no hardware.

### Performance (this machine, WSL2, Python 3.12.3)

| Measurement | Result |
|---|---|
| Empty-span overhead, v1 recorder disabled | 2.1 µs |
| Empty-span overhead, v2 Session, no GPU / in-memory counter | 16.1 / 16.4 µs |
| Empty-span overhead, v1 with NVML / v2 with NVML | 2,475 / 2,535 µs |
| Recording 230,000 nested v2 spans (in-memory counter, incl. file writes) | 3.8 s (16.7 µs/span) |
| v2 attribution: 230,000 nested spans + RAPL timeline | 21.6 s |
| v2 attribution: synthetic 230,000 v1 events (2 GPUs + RAPL) | 26.3 s (v1 path: 12.6 s) |
| v2 attribution: ToG frozen run, 230,042 events, read as v1 | 22.2 s, peak RSS 2.2 GB |

On this WSL2 host one NVML energy read takes ~1.2 ms, so with a GPU the
per-span overhead (two reads) is dominated by NVML for v1 and v2 alike. That
is a property of the host's NVML path, not of the recorder; it should be
re-measured on bare metal. `Session` reads the counter before taking the
start timestamps and after taking the end timestamps, so `duration_s` covers
the work only, while the counter window also includes part of each read call.
`below_resolution` is therefore judged on the shorter interval, which errs
towards flagging. (v1's `mark()` takes the time before the read at both ends.)
`tools/benchmark_overhead.py` and `tools/benchmark_attribution.py --v2`
reproduce these figures. v2 attribution has no quadratic term; its time is
spread over JSON I/O (~7 s), accounting (~11 s) and window lookups (~4.5 s).

### Validation (M2)

- Baseline before changes: `fd5e30f`, 333 passed, 8 skipped.
- Full suite after M2: 417 passed, 9 skipped (8 need frozen data, 1 is the
  opt-in real-NVML test, which passes when enabled on this machine).
- With `AEP_FROZEN_RUNS_DIR`: 418 passed, 8 skipped; the three frozen runs
  re-attribute through v1 exactly as before.
- Golden fixtures: all four byte-identical through v1; none regenerated.
- Wheel 0.2.0 in a fresh venv with no `pynvml` (the GPU-absent path): 331
  passed. Two failures, both unrelated to the package: the repository-path
  check, which by design fails outside `src/`, and a pre-existing unit test
  that needs pandas, which the clean venv does not have. `aep-attribute-v2` is
  installed as a console script.
- The research repository was only read (frozen runs), never written.

### Known limitations and open research questions after M2

- **Device scope only.** Every value is whole-device energy. `scope:
  "process"` is reserved and never emitted; nothing here measures
  per-process energy.
- **Shared energy is reported, not split.** Concurrent spans keep inclusive
  device-window energy labelled `shared`. Whether any allocation rule (time
  share, utilisation-weighted) is defensible for agent workloads is an open
  question.
- **Resolution is a median.** `below_resolution` uses the median update
  interval; the real interval jitters, and edge quantisation affects longer
  spans too. The ~100.6 ms vs ~106 ms discrepancy is unreconciled.
- **RAPL** is still unmeasured on real hardware: no resolution, no wrap
  behaviour, no update cadence. All CPU/DRAM handling is synthetic-tested only.
- **Asynchronous GPU work.** In-band reads bound the request, not the kernels
  (spec §6, §14).
- **NVML overhead** on WSL2 is ~1.2 ms per read and perturbs short spans.
- **Cross-session detection is in-process only.** Another process using the
  GPU at the same time is invisible to `other_sessions_active`.
- **v1 tools do not read v2.** `trajectory`, `aggregate` and `visualize` are
  v1-only.
- **Accounting assumes one clock per file.** Spans are compared on the
  monotonic clock of one process; upgraded v1 events use the wall clock.

## Remaining references to the research repository

The research repository is not a dependency: no code imports it, no test
needs it (except the opt-in frozen-run check, which reads data from a path
you supply), and the virtual environments are separate.

Textual references remain, in docstrings, comments and one error message:

- `events.py` (CoR control loop, `chain_of_relations.run`, `energy_taxonomy`);
- `attribution.py` and `events.py` (legacy readers in `measurement/`);
- `validation.py` usage text (`measurement/validate_hardware.py`);
- `visualize.py` error message (`measurement/attribute.py`).

They were left untouched so that the extracted source stays identical to the
reference. They should be generalised in the first milestone that edits
these modules.

The schema-v1 field names `question_id`, `dataset`, `paradigm`, `iteration`
and `traversal_depth` come from the original KGQA host. They are frozen in
schema v1 and generic in role. Renaming them is a schema-v2 decision.

## Change log relative to the source

| Version | Change | Golden status |
|---|---|---|
| 0.1.0 | Initial extraction; no behaviour change | identical |
| 0.1.0 + M1 (version not bumped) | Partial multi-device sums become `null` (M1.1). RAPL unwrapped per counter from `max_energy_range_uj` via the new `<power.csv>.meta.json` sidecar; unresolvable decreases and negative differences become `null`; `RAPL_MAX` removed (M1.2). Interrupted spans recorded `cancelled` (M1.3). No hardware access at import; `sampling.init()`, `nvml.shutdown()` (M1.4). Columns filtered once per attribution call (M1.5). | identical: all four fixtures byte for byte, and the three frozen runs |
| 0.2.0 (M2) | Additive schema v2: `Session`, `measurement`, `schema_v2`, `accounting`, `attribution_v2` (`aep-attribute-v2`), `sampling.Sampler`, v1 → v2 reader. `attribution.counter_window` factored out of `rapl_delta_prepared` (same arithmetic). No v1 behaviour change. | identical: all four fixtures byte for byte, and the three frozen runs |
