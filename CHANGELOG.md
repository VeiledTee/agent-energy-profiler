# Changelog

Notable changes per release. Measurement-semantics changes and their effect
on the reference artifacts are recorded in more detail in
[docs/PROVENANCE.md](docs/PROVENANCE.md#change-log-relative-to-the-source).
Versions follow [Semantic Versioning](https://semver.org/); before 1.0, a
minor release may change behaviour, and every such change is announced one
minor release ahead with a warning.

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
