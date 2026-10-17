# Changelog

Notable changes per release. Measurement-semantics changes and their effect
on the reference artifacts are recorded in more detail in
[docs/PROVENANCE.md](docs/PROVENANCE.md#change-log-relative-to-the-source).
Versions follow [Semantic Versioning](https://semver.org/); before 1.0, a
minor release may change behaviour, and every such change is announced one
minor release ahead with a warning.

## 0.4.0 — unreleased

The first public release. Versions 0.1.0–0.3.0 below were development
milestones and were never published.

### Changed: the command is `agent-energy-profiler`

- The command-line entry point is `agent-energy-profiler` (it was `aep`), the
  same name as the distribution and the GitHub repository. The single-purpose
  scripts are `agent-energy-profiler-sample`, `-attribute`, `-attribute-v2`,
  `-validate`, `-aggregate` and `-visualize` (they were `aep-*`).
  `python -m agent_energy_profiler` is unchanged, as is the import name
  `agent_energy_profiler`. No `aep` alias is installed: no release ever
  shipped it, and `aep` is already the name of an unrelated PyPI project.
- `agent-energy-profiler run` without `--out-dir` writes to
  `agent-energy-profiler-runs/<timestamp>` (it was `aep-runs/`).
- Unchanged, because recorded data and other code depend on them: the
  `AEP_*` environment variables, the OpenTelemetry attribute namespace
  `aep.*` and its span-id derivation, and the recorded value
  `mode_source: "aep_run"` (and the `run.json` key `aep_run_version`).

### Changed: `Session`'s default mode is `auto`

- **`Session()` without `mode=` now uses `auto`** (before: `direct`), as
  `agent-energy-profiler run` already did. Without a hardware report showing that counter
  reads are harmless on this configuration, `auto` resolves to `sampled`:
  **per-span GPU energy becomes an estimate** (`estimates.gpu`) instead of
  a counter measurement (`energy.gpu`), and measured GPU energy covers the
  whole run. `Session.from_env()` follows the same default when neither
  `agent-energy-profiler run`, `mode=` nor `default_mode=` chooses one.
- **Comparing with earlier results:** check each artifact's `mode`,
  `requested_mode` and `mode_source` (in the session header and on every
  report's `measurement` line) before comparing per-span GPU numbers.
  Whole-run measured GPU energy is comparable across modes. Pass
  `mode="direct"` to reproduce 0.3.0 behaviour exactly.
- `auto` now applies a `NOT_DETECTED` disturbance verdict only to the
  configuration it was measured on: host name, NVIDIA driver version and
  GPU models are compared where the hardware report and the running host
  both know them. A report from another machine or driver selects
  `sampled`, and `mode_reason` says why. `CounterDisturbance` records
  `hostname` and `gpu_models` from the report (new optional header keys).
- Explicit `mode="direct"`, `"sampled"` and `"hybrid"` are unchanged.

### Deprecated

- `SessionDefaultModeWarning` is no longer emitted. The class remains so
  that code filtering it keeps working; it will be removed in a later minor
  release.

### Fixed: report wording

- With no GPU measured and no estimate, the report said "every top-level
  span with GPU energy was measured". It now says how many top-level spans
  have neither a measurement nor an estimate, and why (`disabled`,
  `no_counter`, `not_read_by_mode`, `read_failed`, ...).
- Measured totals that are withheld name the reasons of the missing spans;
  the estimate line says how many of the top-level spans it covers, and why
  no sum is given when top-level spans overlap; the window line names why a
  domain is `n/a`.
- `COUNTER_DISTURBANCE_UNKNOWN`, `COUNTER_DISTURBED` and
  `DIRECT_MODE_BY_DEFAULT` are no longer raised for a counter that was never
  read (no GPU). `DIRECT_MODE_BY_DEFAULT` applies only to sessions recorded
  before 0.4.0.
- `SPAN_ESTIMATES_ONLY` no longer refers to a run counter or to estimates
  that do not exist; `DOMAIN_UNAVAILABLE` now also covers a GPU that was
  never measured for a hardware reason.
- The text report states what per-span and whole-run GPU energy mean in the
  recorded mode.
- New report keys: `totals.domains.*.missing_reasons`,
  `totals.estimated_gpu.{n_top_level, n_without_gpu_value,
  without_gpu_value_reasons, reason}`.

### Project

- Project URLs (GitHub `VeiledTee/agent-energy-profiler`), private
  vulnerability reporting in `SECURITY.md`, an agent walkthrough
  (`examples/agent_workflow/README.md`), and a README that states the
  measurement's limits up front.
- `results/` (raw counter-disturbance data) documented, with the
  developer's host name and local paths redacted.

### Unchanged

- No stored artifact and no existing field changed meaning; v1 and v2
  golden fixtures and the frozen research runs are reproduced exactly.
  Float summation (`_compat.sum`) is unchanged.

## 0.3.0 — unreleased

### Deprecation announced

- `Session()` created without `mode=` still runs `direct` (per-span GPU
  counter reads), but now emits `SessionDefaultModeWarning` (a
  `FutureWarning`) once per process. **In 0.4.0 the default becomes
  `auto`**, as in `aep run`: `direct` only where a hardware report shows
  counter reads are harmless, `sampled` otherwise, where per-span GPU energy
  is an estimate from averaged power rather than a counter measurement. Pass
  `mode="direct"` to keep today's behaviour, or `mode="auto"` to adopt the
  new default now. `Session.from_env()` warns only when neither `aep run`,
  `mode=` nor `default_mode=` chose a mode. See
  [docs/MEASUREMENT_MODES.md](docs/MEASUREMENT_MODES.md#sdk-default-mode-migration).

### Added

- OpenTelemetry integration (`[otel]` extra, `agent_energy_profiler.otel`):
  `Correlation` hook (profiler spans join the application's trace;
  optionally application spans nest under profiler spans), post-run export
  of attributed spans with energy as `aep.*` attributes (`export_spans`,
  `aep otel-export`; OTLP/HTTP with the `[otlp]` extra).
- `Session(hooks=[...])`, a dependency-free extension point; `Session.bind()`
  for request/task tagging; `analyze()` to attribute and report in one call;
  `Session.from_env(default_mode=...)`.
- `mode_source` provenance (`caller` / `default` / `aep_run`) and the report
  warning `DIRECT_MODE_BY_DEFAULT`.
- Package metadata (classifiers, SPDX licence), `CONTRIBUTING.md`,
  `SECURITY.md`, `docs/API.md`, CI for Python 3.9–3.14 with wheel
  installation checks (`tools/check_wheel.sh`).

### Unchanged

- No measured value and no existing field changed meaning. v1 and v2 golden
  fixtures and the frozen research runs are reproduced exactly.

## 0.2.0

Developed over milestones M2–M5 (the version was not bumped between them).

- Schema v2: `Session` with nested spans, trace/span ids, energy provenance
  (measured / unavailable / invalid, estimates kept separate), resolution
  awareness and inclusive/exclusive accounting; v1 files are read by upgrade.
- `aep` command-line interface (`validate`, `run`, `attribute`, `report`,
  `export`, `sample`), reports, examples.
- NVML counter-disturbance diagnostics (`aep validate
  --counter-disturbance`).
- Measurement modes `direct` / `sampled` / `hybrid` / `auto`; `aep run`
  defaults to `auto`.

## 0.1.0

Extracted from the research repository `Chain-of-Relations-Emissions` at
commit `08bbc96`, then hardened (M1): partial multi-device sums become
`null`, RAPL wraparound per counter, cancelled spans recorded, no hardware
access at import.
