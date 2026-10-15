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

These are inherited unchanged. They are documented, not fixed: fixes come
after baseline validation, behind the golden tests.

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

6. **Partial sums.** `attribution.sum_counter_domain` (multi-GPU) and
   `attribution.sum_domain` (multi-socket RAPL) return the sum of the
   available columns when only some are readable, instead of null.
7. **RAPL wrap.** Wraparound uses a hardcoded `RAPL_MAX = 2**32` µJ rather
   than each zone's `max_energy_range_uj`.
8. **Estimates mixed into measured fields.** When an event has no in-band GPU
   counter reading, attribution integrates sampled power into
   `gpu_energy_j`, and that value enters `measured_energy_j`. It is
   distinguished only by `gpu_energy_source = "power_integration"`, while the
   trajectory layer refuses the same estimate.

**Recorder limitations**

9. **Process-global recorder state.** The step and iteration counters and the
   run context are module globals, so concurrent tasks (threads or asyncio)
   interleave them. Forked children inherit the open sink.
10. **No span hierarchy.** There are no span or parent ids. Nested spans each
    record the full counter delta, so summing them double-counts. Overlap is
    detected, not resolved.
11. **Cancellation status.** `span()` records `KeyboardInterrupt` and
    `asyncio.CancelledError` as `error`, not `cancelled`.
12. **Wall clock only.** Events and samples use `time.time()`. Backward steps
    are flagged, never repaired, and no monotonic timestamp is recorded.
13. **Import side effect.** Importing `agent_energy_profiler.sampling`
    initialises NVML and scans RAPL zones.
14. **Scaling.** Attribution is O(events × samples). Re-attributing a 230k-event
    run takes several minutes.

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
