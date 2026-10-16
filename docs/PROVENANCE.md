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

## Milestone 3: developer usability, reporting and performance

### M3.1 Review of M2 (findings recorded before any M3 change)

Baseline: `baf6108`, 417 passed, 9 skipped. The uncommitted `CLAUDE.md`
change is the owner's "Project Isolation" section (12 added lines, nothing
removed); it is preserved verbatim and committed with M3.

**GPU counter update interval (100.6 vs 106 ms).** The figure is not a
constant of the hardware. Evidence, all from the same RTX 4090:

| Source | Driver | Median interval | Notes |
|---|---|---|---|
| research spec / ROADMAP | 591.86 | 106.1–107.1 ms | six `validate_hardware.py` runs, ~80 updates each |
| research `measurement/hardware_report.json` | 591.86 | 99.97 ms | one report, 66 updates, median step 331 mJ (a loaded GPU) |
| this repository, 2026-10-15, tight loop (no sleep, 15 s) | 610.88 | 102.2 ms | 146 intervals, p05–p95 101.5–102.8 ms |
| this repository, 2026-10-15, `probe_nvml_resolution` ×3 | 610.88 | 102.3, 103.2, 102.7 ms | ~83 updates each |
| this repository, 2026-10-15, under sustained GPU load (8192² matmul loop) | 610.88 | 102.4 ms tight loop; 102.4–102.6 ms stock ×3 | interval unchanged under load |

The value 100.6 ms does not appear in the research repository (searched
read-only); it is within the 100–107 ms range observed. Load does not move
the interval on driver 610.88 (idle 102.2 ms, loaded 102.4 ms), so load is
not the explanation for the 99.97 ms report either; the observed spread
tracks time and driver version, which could not be separated with the data
available. Within one run the
interval is tight (±0.7 ms), but between runs it moved by ~7 ms, the driver
has changed since extraction (591.86 → 610.88), and the one loaded-GPU
report is the shortest. Conclusion: the interval must be characterised per
host, per driver and ideally under the workload's own load, then recorded with
its provenance. That is how `Resolution` already works; no default value is
added. The stock probe polls with `sleep(0.001)` plus a ~0.7–1.2 ms read, so
each detection lags an update by up to ~2 ms; the lag cancels in the median
(stock 102.3–103.2 ms vs tight loop 102.2 ms) but widens the spread.

**CRITICAL: reading the NVML energy counter makes it under-report (this
host, WSL2, driver 610.88).** *(Investigated in Milestone 4 with a
controlled experiment; see [NVML_COUNTER_VALIDATION.md](NVML_COUNTER_VALIDATION.md).
M4 refines this preliminary account: power reads via `GetPowerUsage` do not
disturb the counter, the instantaneous-power field does, and the "no polling"
row below still read power at 4 Hz.)* Measured 2026-10-15 with
`nvmlDeviceGetTotalEnergyConsumption` against `nvmlDeviceGetPowerUsage`:

| Energy reads | counter ÷ board power, idle (~28 W) | under 8192² matmul load (~426 W) |
|---|---|---|
| none (start/end only) | 0.97 | 0.97 |
| every 1 s | 0.96 | 0.97 |
| every 100 ms | 0.87 | 0.87 |
| every 20 ms | 0.40 | 0.42 |
| every 1 ms | 0.06 | 0.06 |
| back to back | 0.02 | 0.02 |

- The effect is global: while one process polls, a second process reading
  only at start and end sees the same deficit (0.77 W vs 30 W).
- The real `aep sample` at 10 Hz depresses every reader to 0.85 (idle) and
  0.86 (load); without it the same reader sees 0.97.
- The frozen research runs carry the same signature: whole-log counter power
  ÷ mean sampled power is 0.866 (CoR), 0.864 (PoG), 0.876 (ToG), and their
  recorded `sampled_vs_counter_ratio` medians are 1.150, 1.149, 1.141, i.e.
  ≈ 1/0.87. Those runs used driver 591.86 and a 10 Hz sampler, plus in-band
  reads at every event boundary. This is consistent with the counter having
  been under-read by roughly 13% or more in those runs, but it is not proof:
  the board-power reading has not been validated independently.
- Not yet explained: in the M3 GPU example the in-band counter (516 J) was
  *above* integrated sampled power (499 J) with the sampler running.
- Consequences if confirmed: every GPU figure taken while the sampler runs or
  while spans read the counter frequently is biased low, increasingly so for
  short, frequent spans; the per-span "below resolution" zeros are partly this
  effect. This needs an external reference (a wall-power or PCIe-rail meter)
  before any correction is designed. No code was changed for it.

**NVML instrumentation overhead.** One `nvmlDeviceGetTotalEnergyConsumption`
call costs 0.69 ms median when isolated (p90 0.82 ms, max 15 ms) and
1.15 ms when calls run back to back on this WSL2 host; a power read costs
0.14 ms. M2's 2.5 ms/span came from empty spans, i.e. back-to-back reads.
With work inside the span, a Session adds a median 1.7–2.2 ms per span
(p90 ~8.4 ms) with NVML, versus 0.013–0.044 ms without. The reads lie outside
`duration_s`, so durations are not inflated, but the workload is: a 10 ms
operation runs ~20% longer end to end, and the read calls' own energy falls
partly inside the counter window. Below ~20 ms the overhead is a material
fraction of the operation.

**Peak memory of v2 attribution (2.2 GB at 230k events).** Stage by stage on
the ToG run: timeline 116 MB; reading and upgrading 230k records 998 MB (~3.8
KB of Python objects per span); attribution plus accounting 2,135 MB. Every
span is held twice (shallow copies in `attribute_spans` and `annotate`), with
~25 small nested dicts each, and deleting the input frees nothing because the
copies share nested objects. Before: nested 230k 20.7 s / 2.43 GB; ToG 21.5 s /
2.24 GB (v1 ToG: 7.3 s / 1.29 GB).

**Wheel-environment failures.**
`test_package_under_test_is_this_repository` asserts the package is imported
from `src/`; it is meaningless for an installed wheel and should skip there
explicitly instead of failing. `test_numpy_and_pandas_keep_nan` imports numpy
and pandas, which no extra declares; it should skip when they are absent, and
the full test environment should be declared as an extra.

**Schema-v2 review.** No correctness defect found in the recorded values.
Ambiguities and gaps:

1. `status` means different things at five levels: span (`ok`/`error`/...),
   domain (`measured`/`unavailable`/`invalid`), exclusive (adds
   `not_defensible`), total (`measured`/`incomplete`), estimate
   (`estimated`/`unavailable`). Valid but easy to misread; documented in one
   table rather than renamed (renaming would break v2 files).
2. `attribution: "sole"` means sole among *recorded* spans. Un-instrumented
   work, other processes and a model server's background activity are
   invisible to it. The wording in SCHEMA_V2.md is tightened.
3. With in-band GPU reading off but a sampler running, a span's `energy.gpu`
   is unavailable even though the sampled GPU counter could be differenced
   over its window (as RAPL is). A design gap, not an error; deferred.
4. `below_resolution` on a `sampled_counter_interpolated` value uses the NVML
   interval only; the sampling period (100 ms at 10 Hz) is a second
   resolution limit. Today this affects only the long session window.
5. `total.below_resolution` is `null` whenever RAPL is measured (RAPL
   resolution unknown), so on full-boundary hosts it is almost always null.
   Correct, but it should be explained to readers.
6. `duration_s` excludes the read calls while the counter window includes part
   of them, so `update_intervals` slightly understates the window.
7. Upgraded v1 records use v1 `run_id` as `session_id`, have empty `raw`, and
   wall-clock durations. Documented in SCHEMA_V2.md, unchanged.

### M3.2 Implementation decisions

- **Streaming v2 attribution (memory).** `accounting.Index` keeps only a
  compact per-span node (ids, window, measured values, raw mJ deltas);
  `SummaryBuilder` aggregates one span at a time; `attribution_v2.attribute_file`
  makes two passes over the input (pass 1 builds the index and checks
  coverage before anything is written; pass 2 recomputes each span, adds its
  accounting and writes it). Same rules, key order and arithmetic as M2:
  output is byte-identical on both 230k runs and on the new
  `tests/golden_v2/session_mixed` fixture, whose expected files were produced
  by the M2 code (`baf6108`) via `tools/build_v2_fixture.py`. The in-memory
  `attribute_spans` / `annotate` / `summarize` API is kept.
- **Reporting (`report.py`).** One accumulator for both schemas: v1 events
  pass through `schema_v2.upgrade_v1`, so a v1 power-integration value is an
  estimate exactly as in v2. Totals add top-level spans only (v2 roots; every
  v1 event) and are refused when those overlap. A total appears only when
  every contributing span is measured; the partial sum is shown separately
  with its coverage. Per-operation energy is v2 exclusive energy, v1 per-event
  energy. Estimates are reported only for spans with no counter measurement;
  where both exist the report prints an estimate-vs-counter line marked
  diagnostic. Reads files line by line. v1 numerical semantics are preserved:
  for every operation, measured + estimated GPU equals v1's
  `energy_summary.csv` value as printed (tested on the synthetic and CoR
  fixtures).
- **CLI (`cli.py`, `aep`).** `validate`, `sample`, `run`, `attribute`,
  `report`, `export`. `aep run` measures the whole program (in-band GPU reads
  before and after, sampler window) and attributes spans only if the program
  wrote them through `Session.from_env()`; it says so when it did not. v1
  input to `aep attribute` uses the unchanged v1 pipeline unless
  `--schema 2`.
- **Examples.** CPU-only, GPU inference (PyTorch; synthetic decoder by
  default, optional Hugging Face model) and an agent loop, all without paid
  APIs. Run on the development host; outputs in their READMEs carry the
  counter-disturbance caveat.
- **Test dependencies.** New `test` extra (`pytest`, `matplotlib`, `numpy`,
  `pandas`); numpy/pandas tests skip without them; the repository-path test
  checks the opposite (installed from `site-packages`) under
  `AEP_TEST_INSTALLED=1`.

### M3.3 Performance (development host, WSL2, Python 3.12.3)

| | Before (M2) | After (M3) |
|---|---|---|
| v2 attribution, nested 230k spans | 20.7 s, 2.43 GB peak RSS | 21.0 s, 0.27 GB |
| v2 attribution, ToG frozen run (230,042 events) | 21.5 s, 2.24 GB | 19.4 s, 0.46 GB |
| v1 attribution, ToG (unchanged code) | 7.3 s, 1.29 GB | unchanged |
| Span overhead, no GPU reading | 13–44 µs | unchanged |
| Span overhead, NVML (median / p90) | 1.7–2.2 ms / ~8.4 ms | unchanged |

Attribution does not touch the GPU, so its cost is the same with or without
one. Without NVML (`gpu=False`) a span costs tens of microseconds; with it,
two NVML reads dominate (0.69 ms each in isolation, 1.15 ms back to back on
this host). The reads are outside `duration_s`, so durations are not
inflated, but the workload is, and on this host the reads also depress the
counter itself (M3.1). For operations shorter than ~20 ms both effects are
material.

### M3.4 Validation

- Full suite: 443 passed, 9 skipped (8 need frozen research data, 1 is the
  opt-in real-NVML test).
- Frozen runs (`pytest -m frozen_runs`, data read only): all three
  re-attribute through v1 exactly as before.
- Golden fixtures: the four v1 cases byte-identical; the new v2 case
  byte-identical to M2. Nothing regenerated.
- Wheel 0.2.0 in two fresh venvs: minimal (no `pynvml`, no optional
  packages) 358 passed, 58 skipped; with `[test]` 407 passed, 9 skipped. The
  installed `aep` ran `validate`, `run` (agent and CPU examples, GPU absent)
  and `export` successfully.
- Finding while validating: with `TMPDIR` inside a git working tree,
  `test_not_a_repository_is_unknown_not_clean` fails, correctly, because the
  temporary directory then *is* inside a repository. Documented in the README.
- The research repository was only read (frozen runs, the hardware report,
  a text search), never written.
- PyTorch for the GPU example was installed into a throwaway venv under the
  git-ignored `build/`, with the pip cache disabled and `TMPDIR` inside the
  repository; it was deleted afterwards.

### M3.5 Unresolved after M3

- **NVML counter disturbance** (M3.1): needs an external power reference, a
  check on bare metal and other drivers, and a decision on how the sampler
  and in-band reads should behave (e.g. a lower read rate, a disturbance
  probe in `aep validate`). The research runs' GPU energies may be ~13% low.
- The GPU example's counter (516 J) above integrated power (499 J) with the
  sampler running does not fit the disturbance pattern and is unexplained.
- Schema-v2 items 3–6 of M3.1 (sampled per-span GPU when in-band reading is
  off; sampling period as a second resolution limit) remain open.

## Milestone 4: GPU energy measurement validation

Full write-up: [NVML_COUNTER_VALIDATION.md](NVML_COUNTER_VALIDATION.md).
Tools: `tools/nvml_diagnostics/`. Raw data and summaries: `results/nvml_m4/`.

- **Finding (WSL2, RTX 4090, driver 610.88).** Each read of the energy counter
  (either API) or of the instantaneous-power field lowers the counter's
  accumulation by ~9–11 ms of energy, for all readers, at 27, 254 and 431 W.
  `GetPowerUsage` and temperature reads do not. The 10 Hz sampler retains
  0.886–0.903 of read-free counter energy. Undisturbed counter and integrated
  `GetPowerUsage` differ by 2–4%; which is right is unknown.
- **The M3 contradiction** is consistent with `GetPowerUsage` being a
  trailing average: right after an idle → load step, counter ÷ integrated
  power is 1.03–1.06.
- **Frozen runs** (read-only): their 0.864–0.876 matches the sampler-level
  effect; the in-band per-read effect in them is ~7× weaker than the current
  driver predicts. Historical figures are unchanged; the implication is
  qualified in the write-up (section 5).
- **`validation.probe_nvml_resolution`'s median step** is itself disturbed
  (the probe polls every ~2 ms); a note in the hardware report says so. The
  interval is unaffected.
- **Added.** `aep validate --counter-disturbance` (opt-in, ~30 s): paired
  read-free vs 10 Hz windows, P-state-checked; verdict DISTURBED /
  NOT_DETECTED / INCONCLUSIVE. On this host: DISTURBED, 0.893.
- **Not changed.** Sampler, `Session`, attribution and report behaviour,
  schema, golden fixtures. Recommendations for the measurement architecture
  are in section 8 of the write-up, pending a decision.
- **Tests.** `tests/test_nvml_diagnostics.py`: trial scheduling with a fake GPU
  (equal windows, no backlog), statistics, verdict logic, and the probe
  against fake counters that do and do not lose energy per read.
- **Safety.** Workloads are ordinary matmul within the board's power limit;
  the experiment aborts at 83 °C (maximum observed 79 °C). PyTorch was
  installed into a throwaway venv under `build/` with no pip cache, and
  removed afterwards.

## Milestone 5: non-intrusive measurement architecture

Design and semantics: [MEASUREMENT_MODES.md](MEASUREMENT_MODES.md). Field
definitions: [SCHEMA_V2.md](SCHEMA_V2.md) section 6. Validation protocol for
hardware not available here: [HARDWARE_VALIDATION_PROTOCOL.md](HARDWARE_VALIDATION_PROTOCOL.md).

**Architecture (smallest change that separates counter reads from the
timeline).**

- `sampling`: the GPU energy counter has its own policy, `every_row`
  (default, unchanged), `every_n_rows` or `boundaries` (`--counter-hz`,
  `Sampler(counter_hz=)`). Averaged power and RAPL stay on every row. Rows
  without a counter read leave the cell empty, which every reader (v1 and v2)
  already treats as missing. Sparse policies add a final counter row on stop.
  The sidecar (version 2) declares the policy in a `timeline` block.
- `measurement`: modes `direct`, `sampled`, `hybrid`, `auto`;
  `CounterDisturbance` (verdict + evidence source, UNKNOWN by default);
  `resolve_mode` (conservative, never runs a diagnostic);
  `measurement_config` (header block, model version 2). Two reason codes:
  `not_read_by_mode`, `counter_not_sampled`.
- `Session(mode=, disturbance=, timeline=)`: `sampled`/`hybrid` never read the
  counter per span; the header records mode, reason, timeline, verdict and
  driver version. Default `direct`, as before.
- `attribution_v2.Timeline`: for timelines that declare a sparse policy, the
  session-window GPU value is `counter_not_sampled` instead of interpolated;
  new summary fields `run_counter` (first-to-last-reading counter energy,
  with its own window) and `counter_intervals` (counter vs integrated power
  per reading interval, diagnostic). Timelines without the declaration take
  the previous code path.
- `aep run`: `--mode` (default `auto`), `--hardware-report`, `--power-hz`
  (`--hz` kept), `--counter-hz`; the mode is resolved before the program
  starts, passed to it (`AEP_MEASUREMENT`) and written to `run.json`.
- `report`: measurement section, run counter and interval diagnostics;
  warnings `MEASUREMENT_NOT_RECORDED`, `COUNTER_DISTURBANCE_UNKNOWN`,
  `COUNTER_DISTURBED`, `SPAN_ESTIMATES_ONLY`.

**Behaviour changes.** Only `aep run`'s default (M3: per-span reads and a
10 Hz counter; now `auto`, i.e. `sampled` unless a hardware report shows no
disturbance). Everything else is opt-in. Existing artifacts attribute and
report as before, plus warnings that their counter-read rate was not known
to be harmless.

**A defect caught by the v2 golden fixture during development.** A new mode
constant first shadowed the existing method constant `SAMPLED`, relabelling
RAPL values' `method`; `tests/test_golden_v2.py` failed and the constants
were renamed (`MODE_*`) before anything was committed.

**Validation on this machine (RTX 4090, WSL2, driver 610.88).**

| Check | Result |
|---|---|
| Full suite | 474 passed, 9 skipped |
| v1 golden fixtures, v2 golden fixture | byte-identical |
| Frozen research runs (v1 attribution) | identical |
| Clean wheel, minimal / with `[test,gpu]` | 380 passed, 59 skipped / 429 passed, 10 skipped; no failures. The installed `aep` ran `validate --counter-disturbance`, `run` in `auto` (→ `sampled`) and `--mode direct`, GPU present and absent |
| `aep validate --counter-disturbance` on this GPU | DISTURBED, retained median 0.862 (pairs 0.687, 0.862, 0.865; `results/nvml_m5/`). A first attempt was INCONCLUSIVE because two of three pairs saw the idle GPU change performance state; the probe now collects pairs until it has enough valid ones |
| Sampler interference (M4 method, idle, 5 blocks): counter retained under a running sampler | original default 0.866 ± 0.007; `boundaries` 10 Hz power 0.996 ± 0.018; `boundaries` 50 Hz power 0.991 ± 0.016; `hybrid` 1 Hz 0.984 ± 0.019 |
| Per-span overhead (median, real NVML) | `direct` 1.9–4.5 ms (p90 ~9.5 ms); `sampled`/`hybrid` 0.014–0.042 ms |
| v2 attribution, 230k nested spans | legacy timeline 18.6 s / 270 MB; sparse timeline 17.3 s / 270 MB (M3: 21.0 s / 270 MB) |
| `aep report` on 230k spans | 5.1 s, 161 MB |

Raw data: `results/nvml_m5/sampler_interference.json`. These show the
modes avoid the *detectable* interference measured in M4 on this host. They
do not show accuracy: no external reference was available.

**Tests.** `tests/test_measurement_modes.py` (22): mode resolution for every
verdict; counter policies; per-span reads per mode (counted); GPU disabled;
`from_env` provenance; sampler policies through a counting fake NVML
(first/final-row reads, every n-th row, power on every row, clean exit);
sparse attribution with nested, overlapping and short spans, missing power
samples, a counter reset, different counter/power rates; report warnings for
unknown, detected and no disturbance and for legacy artifacts; `aep run` in
`auto` without evidence, `auto` with NOT_DETECTED, and `hybrid` at an
explicit rate.

**Remaining uncertainty.** Host-specific disturbance (one GPU, one driver,
WSL2); the 2–4% gap between undisturbed counter and averaged power; the lag
of averaged power (estimates of short spans and steps are biased); hybrid's
1 Hz rate retains 0.984 ± 0.019 here, consistent with the ~1% the M4 model
predicts but not resolved from zero.

## Milestone 6: OpenTelemetry integration and SDK hardening

Design and attribute mapping: [OPENTELEMETRY.md](OPENTELEMETRY.md). LangGraph
evaluation: [LANGGRAPH.md](LANGGRAPH.md). Default-mode migration:
[MEASUREMENT_MODES.md](MEASUREMENT_MODES.md#migrating-towards-conservative-defaults-proposal-m6).
New fields: [SCHEMA_V2.md](SCHEMA_V2.md) section 7.

**Changes.**

- `Session(hooks=[...])`: a generic, dependency-free extension point
  (`trace_context()`, `on_span_start(span)`, `on_span_end(span, record)`).
  Hooks run before the span's counter read and clocks and after its end
  clocks, counter read and write. Failures are counted
  (`session_end.n_hook_errors`), never raised. A root span can join an
  external trace (`external_parent`, its `trace_id`).
- `Session.bind(**attributes)`: attributes for every span opened in a
  context, following asyncio tasks, isolated per session.
- `SpanHandle.name`, `.session_id`, `.external_parent`.
- `agent_energy_profiler.otel` (`[otel]` extra, never imported by the core):
  `Correlation(propagate=False)` hook; `span_attributes`,
  `to_readable_spans`, `export_spans`, `JsonLinesSpanExporter`. `aep
  otel-export` (`--endpoint` needs the `[otlp]` extra).
- `analyze(spans_or_session, power=...)` (re-exported at the package top
  level): attribute and build the report in one call.
  `attribution_v2.attribute_run` is the former body of `main` (same output;
  the golden test checks it).
- Mode provenance: `Session()` without `mode=` still runs `direct`, but its
  header now says `mode_source: "default"` and its `mode_reason` says it was
  defaulted. Before M6 it claimed "requested explicitly", which was wrong for
  a defaulted session. `Session.from_env(default_mode=...)`. `aep run`
  records `mode_source` (`default` / `caller`). Report warning
  `DIRECT_MODE_BY_DEFAULT`; the text report shows the source.
- Examples pass `default_mode="direct"` explicitly (standalone runs have no
  sampler; under `aep run` its mode applies). New example
  `examples/otel_tracing/`.

**Behaviour changes.** None to measured values or to any existing field's
meaning. New sessions carry `mode_source`, and a defaulted session's
`mode_reason` text changed (see above). Reports of defaulted-direct sessions
gain one warning. Sessions with hooks carry the new fields.

**LangGraph.** No integration module: explicit `session.trace` /
`session.span` in node functions, pinned by `tests/test_langgraph.py`
against LangGraph 1.2.15 (sync and async graphs, parallel branches, config
injection). Callbacks were rejected for now (labels would be inferred from
node names; sync handlers run on another thread in async graphs).

**Validation on this machine (RTX 4090, WSL2, driver 610.88, Python 3.12).**

| Check | Result |
|---|---|
| Full suite (`.venv`, OpenTelemetry 1.45.1, no LangGraph) | 515 passed, 10 skipped |
| LangGraph 1.2.15 venv: `test_langgraph`, `test_otel`, `test_session_hooks` | 43 passed |
| v1 golden fixtures, v2 golden fixture | byte-identical (`attribute_run` refactor included) |
| Frozen research runs (v1 attribution) | identical (3 runs) |
| Clean wheel: minimal / `[test,gpu]` / `[otlp]` / OpenTelemetry 1.20.0 | 403 passed, 61 skipped / 470 passed, 11 skipped / 421 passed, 60 skipped / 421 passed, 60 skipped; no failures. Minimal install: `import agent_energy_profiler.otel` and `aep otel-export` fail with the extra's name |
| `aep otel-export --endpoint` against a local OTLP/HTTP receiver | 1 request to `/v1/traces`, `application/x-protobuf`, 20 spans decoded; measured GPU spans carry `energy_j`, the unmeasured one only status and reason |
| Example `otel_tracing` on the GPU | standalone: 3 traces, application span nested under a profiler span; under `aep run` (`auto` → `sampled`), then `aep otel-export`: 12 spans, `mode_source` `aep_run` |

**Overhead** (`tools/benchmark_overhead.py`, empty spans, median of 5 × 20 000):

| Configuration | No GPU reads | NVML, `direct` |
|---|---|---|
| no hooks | 14.4 µs | 2486 µs |
| `bind` active | 15.1 µs | — |
| `otel.Correlation()` | 17.5 µs | 2495 µs |
| `otel.Correlation(propagate=True)` | 21.4 µs | 2487 µs |

`otel.export_spans`: 50 000 attributed spans in 1.99 s (~25 000/s), 92 MB
peak RSS.

**Tests added.** `tests/test_session_hooks.py` (24): hook order relative to
counter reads and the write; identical energy and read counts with and
without hooks; LIFO end hooks; failing and malformed hooks counted, never
raised; external trace context (root only, explicit trace id wins,
malformed contexts rejected, accounting unaffected); `bind` (nesting,
precedence, asyncio tasks, per-session isolation, threads); mode provenance
for the default, every explicit mode, `from_env` with and without `aep run`,
`default_mode`; the `DIRECT_MODE_BY_DEFAULT` warning; `analyze` equals `aep
attribute` + `report.build` and reproduces the v2 golden output; the core
imports no OpenTelemetry. `tests/test_otel.py` (18): correlation,
propagation (nesting, restoration, concurrent tasks, unsampled traces),
unchanged measurement, no global configuration, export of the v2 golden run
(ids, hierarchy, times, status, every energy/estimate/total/accounting
attribute against the record, valid attribute types), raw and v1 input, the
CLI, the example, and an end-to-end application + profiler trace tree.
`tests/test_langgraph.py` (2). `tests/test_measurement_modes.py`:
`mode_source` in `run.json` and the child session.

**Not established.** Accuracy of any value: no external meter. RAPL has not
been checked on physical hardware. The counter disturbance and the lag of
averaged power stay as documented in M4/M5. OpenTelemetry attributes change
none of this.

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
| 0.2.0 + M3 (version not bumped) | Streaming v2 attribution (same output, ~5–9× less memory); `report.py`; `aep` CLI and `Session.from_env`; examples; `test` extra; v2 golden fixture from M2. No v1 or v2 output change. NVML counter-disturbance finding documented, not corrected. | identical: v1 fixtures, frozen runs, and the v2 fixture |
| 0.2.0 + M4 (version not bumped) | NVML counter validation: diagnostics in `tools/nvml_diagnostics/`, raw results in `results/nvml_m4/`, `docs/NVML_COUNTER_VALIDATION.md`, opt-in `aep validate --counter-disturbance`, a note on the resolution probe's step. No measurement behaviour changed. | identical: v1 fixtures, frozen runs, and the v2 fixture |
| 0.2.0 + M5 (version not bumped) | Measurement modes (`direct`/`sampled`/`hybrid`/`auto`), sampler counter policies, measurement provenance (model version 2), run counter and interval diagnostics, disturbance-aware report warnings; `aep run` defaults to `auto`. Existing fields keep their meaning. | identical: v1 fixtures, frozen runs, and the v2 fixture |
| 0.2.0 + M6 (version not bumped) | Session hooks, `bind`, external trace parent; optional `otel` module and `aep otel-export`; `analyze()`; `mode_source` provenance and the `DIRECT_MODE_BY_DEFAULT` warning (Session's default mode unchanged); LangGraph guidance. No measured value or existing field meaning changed. | identical: v1 fixtures, frozen runs, and the v2 fixture |
