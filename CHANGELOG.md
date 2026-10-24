# Changelog

Notable changes per release. Measurement-semantics changes and their effect
on the reference artifacts are recorded in more detail in
[docs/PROVENANCE.md](docs/PROVENANCE.md#change-log-relative-to-the-source).
Versions follow [Semantic Versioning](https://semver.org/); before 1.0, a
minor release may change behaviour, and every such change is announced one
minor release ahead with a warning.

## 0.5.0 — unreleased

The first release published on PyPI. Versions 0.1.0–0.4.0 below were
development milestones and were never published; installing 0.5.0 gives
their changes too, so read 0.4.0 if you used the repository before.

**Measurement behaviour since 0.4.0: unchanged.** No measured or estimated
value, report key, CSV column, schema field or attribution rule changed
meaning. The v1 and v2 golden fixtures and the frozen research runs are
reproduced exactly. One full text report gained an explanatory line
(`est. gpu J`, below), and `Sampler.start()` returns slightly later (below);
neither changes a recorded value.

### Added: `Session.profile()`

- **`Session.profile(name)`** is the recommended way to profile from
  Python. It creates a run directory
  (`agent-energy-profiler-runs/<name>-<timestamp>/`, or `out_dir=`, which is
  never overwritten) and runs the hardware sampler with the measurement
  mode's counter policy (never the sampler's every-row default). On close it
  attributes the run, saves `report.json` and `report.txt`, and prints the
  brief summary to stderr (below). `hardware_report=` sets resolution and disturbance
  together. `report=False` stops after recording, `strict=True` raises at
  close when the sampler or attribution failed and the workload did not,
  and `quiet=True` prints nothing. The results are in `session.out_dir`,
  `session.report`, `session.profile_status` and `session.profile_error`.
  Under `agent-energy-profiler run` it behaves as `Session.from_env`: no
  second sampler and no second report.
- A failure after the workload (stopping the sampler, a non-zero sampler
  exit code, attribution or the report) never hides the workload's own
  exception. It keeps the raw files and prints the commands that retry it.
- `Sampler(watch_parent=True)` (sampler option `--parent-pid`) stops the
  sampler by itself once the process that started it has exited, as when a
  notebook kernel is killed or a session is never closed. `Session.profile`
  turns it on; the default is unchanged. No exit handler is installed.
- A profile with no span gets status `no_spans`. It attributes nothing and
  writes no report. It says there is no operation-level data, and prints the
  sampler's whole-session readings, labelled device-level. An explicit
  `out_dir` is refused if it already holds any file a profile writes,
  including `report.json`.
- `examples/quickstart.py`, `agent_workflow`, `cpu_workflow`, `gpu_inference`
  and `otel_tracing` use `Session.profile`. Run directly, they now record
  energy and print a report; under `agent-energy-profiler run` they still
  use `run`'s sampler, mode and report. The examples' `--out` (a spans file)
  became `--out-dir`; on their own they now use the default mode, `auto`,
  instead of `direct` (`gpu_inference` takes `--hardware-report`).

### Added: brief report

- **`agent-energy-profiler report RUN --brief`** and
  `brief.build(path)` / `brief.render(summary)`: one screen saying which
  operations energy went to. It shows the run's duration and whole-run
  measured energy per device, and each operation's count and latency
  (total, p50, p95). Operations are ranked on one basis, never mixed:
  measured exclusive GPU energy, else estimated GPU energy (marked `~`),
  else measured exclusive CPU package energy.
- An operation is ranked only when every one of its spans has a value on
  that basis. Estimates have no exclusive form, so only leaf spans that
  nothing overlapped are ranked by estimate. Operations that are `shared`,
  `partial`, a `parent` (for estimates) or entirely below the counter's
  resolution are listed with `n/a` or their value, with the reason, and
  never ranked or shown as 0. When nothing is comparable, operations are
  listed by total time with the reason. It ends with per-span coverage, a
  device-level caveat, the full report's command and its warning codes.
- **`Session.profile` prints the brief summary** at close instead of the
  full report. `report="full"` prints the full report; `report.txt` and
  `report.json` are unchanged and still the full report. If the summary
  cannot be built, the full report is printed instead.
- A formatting layer: no measured or estimated value, no report key and no
  CSV column changes.

### Added: HTML report

- **`agent-energy-profiler report RUN --html report.html`** writes one
  self-contained, offline file with three views:
  - a workflow explorer: the span hierarchy with repeated operations
    grouped, latency, energy on one ranking basis, notes, and every span's
    values with their kind and reason;
  - an operation breakdown, by path or by label;
  - provenance: mode, sampler, coverage, warnings and limitations.

  Opt-in: `Session.profile` does not write it.
- Built from `view_model.build()`, a versioned and validated view model that
  uses the reports' own rules. Path rows are the same accounting grouped by
  hierarchy path (`report._build(key=...)`). The page only displays, sorts
  and filters.
- Attributes and identifying system metadata are left out by default:
  attributes unless named (`--attributes`) or all allowed
  (`--all-attributes`); host names, file paths and span, trace and session
  ids always. A Content-Security-Policy allows only the page's own script
  and style, and no network access.
- Operation labels are not left out: the labels your code chose are kept
  as written unless `--redact-labels` replaces them with `operation N`.
  `report --html` warns when they look like free text (prompts or user
  input). Check a report before sharing it.

### Fixed: `est. gpu J` in the full report

- `BY OPERATION` is headed "energy: exclusive", but its `est. gpu J` column
  sums each span's whole-window estimate, so a parent's value includes its
  children's (estimates have no exclusive form). When such a run has nested
  spans, the full text report now says so below the table. The README
  wrongly described the column as exclusive. No value changed.

### Fixed: `run` with a session but no spans

- `agent-energy-profiler run` failed with a traceback (exit code 1) when the
  program opened a session but recorded no span: it attributed zero spans,
  then could not build a report. It now reports whole-program energy only,
  says why, and exits with the program's exit code.

### Fixed: report on attributed spans without `session_summary.json`

- `report.build` (and `agent-energy-profiler report` / `export`) raised
  `TypeError` when `spans_attributed.jsonl` had been copied without its
  `session_summary.json` and some spans were flagged below resolution. It
  now returns a partial report: totals, operations and coverage from the
  spans, a new `SUMMARY_MISSING` warning saying what is absent and how to
  get it, and the measurement and window sections marked unknown because
  the summary is missing (they used to claim an old session or no
  timeline). `BELOW_RESOLUTION` gives the interval only when it is known;
  `RESOLUTION_UNKNOWN` is raised only for spans that were not flagged.
- `agent-energy-profiler report` and `export` print an error and exit 2 for
  a missing, empty or unrecognised input instead of a traceback.

### Fixed: report wording

- `BELOW_RESOLUTION` no longer calls below-resolution readings "biased low":
  quantisation can err in either direction. It now says the reported energy
  is quantised and may not reliably represent the span's consumption (also
  in the README, `docs/SCHEMA_V2.md` and `docs/OPENTELEMETRY.md`). The
  counter-disturbance warnings, which do describe a downward bias, are
  unchanged.

### Measurement timing: sampler start

- `Sampler.start()` returns only once the clock has passed the first row's
  timestamp. Rows are stamped rounded to the millisecond, so a span opened
  within 0.5 ms of `start()` could begin before the timeline's first row and
  be refused by attribution as uncovered. Only the timing of `start()`
  changes; nothing recorded changes meaning.

### Project

- Packaged for PyPI as `agent-energy-profiler`: the README installs from
  PyPI, and its links and images are absolute so that they also work on the
  PyPI project page. The package docstring's quick start uses
  `Session.profile` instead of the legacy v1 `profiler.span`.
- `CITATION.cff` with the software's citation metadata. No DOI has been
  assigned yet.
- `docs/RELEASING.md`: a release checklist, PyPI trusted publishing and
  Zenodo archiving (both still to be enabled by the maintainer).
- Hosted CI on GitHub Actions passes on Python 3.9–3.14, the wheel checks
  and the LangGraph tests; jobs are pinned to `ubuntu-24.04`.
- README reorganised for first-time users: what the profiler does, a
  runnable quick start (`examples/quickstart.py`, checked by a test to
  match the README) with real output, the two ways to run (`run` vs
  `Session` in Python), choosing a measurement mode, platform support,
  troubleshooting, then the measurement limitations. Install commands use
  `python3 -m venv` (stock Ubuntu has no `python`). The v1 → v2 API table
  moved to `docs/SCHEMA_V2.md`, the opt-in test commands to
  `CONTRIBUTING.md`. A test checks that relative links in the README, docs
  and examples resolve.
- `agent-energy-profiler run` says when no GPU energy counter is available
  and what to do (install the `gpu` extra, or run `validate`). The message
  goes to stderr only; nothing recorded changes.

## 0.4.0 — never published

A development milestone, like 0.1.0–0.3.0 below. Its changes are
included in 0.5.0, the first published release.

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
