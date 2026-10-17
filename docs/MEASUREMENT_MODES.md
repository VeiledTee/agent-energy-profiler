# Measurement modes (Milestone 5)

GPU energy can be obtained in two ways, and they are different kinds of
number:

| Instrument | Gives | Kind |
|---|---|---|
| Cumulative energy counter (`nvmlDeviceGetTotalEnergyConsumption`), differenced between two readings | energy between those two readings | **measured** |
| Averaged board power (`nvmlDeviceGetPowerUsage`), sampled and integrated over a window | energy over any window | **estimated** (driver-averaged power, lags load changes) |

On the development host (RTX 4090, WSL2, driver 610.88) every counter read
lowered the counter's accumulation by ~10 ms of energy, while averaged-power
reads did not ([NVML_COUNTER_VALIDATION.md](NVML_COUNTER_VALIDATION.md)).
Whether that holds elsewhere is unknown. The modes let you choose how often
the counter is read, and every artifact records what was chosen and why.

## The modes

| Mode | Counter read per span | Sampler's counter reads | Per-span GPU energy | Measured GPU energy |
|---|---|---|---|---|
| `direct` | yes, at both boundaries | first and final row | **measured** (`energy.gpu`, in-band counter difference) | each span; the run (`run_counter`) |
| `sampled` | no | first and final row | **estimated** (`estimates.gpu`); `energy.gpu` is `unavailable`, reason `not_read_by_mode` | the run only (`run_counter`) |
| `hybrid` | no | every n-th row (default 1 Hz) and the final row | **estimated**, as in `sampled` | the run, plus a measured counter series; each interval is compared with integrated power (`counter_intervals`, diagnostic) |
| `auto` | — | — | picks one of the above | — |

In every mode averaged power is read on every sampler row (`--power-hz`,
default 10 Hz) and RAPL on every row.

What these numbers do and do not support:

- **A run counter is not per-operation energy.** `run_counter` is the
  counter's difference between the sampler's first and last readings. It
  covers sampler start-up and idle tails, and it cannot be divided among
  spans. It is reported with its own start and end times and whether they
  bracket every span.
- **A power-integrated span value is an estimate.** It lives in
  `estimates.gpu`, never in `energy.gpu`, and never enters `total`.
- **Concurrent spans are not split** in any mode. Overlapping spans are
  `shared`; their measured or estimated values include the other work.
- **Sparse counter readings do not become window measurements by
  interpolation.** With a declared sparse counter policy, the session-window
  GPU value is `unavailable` (reason `counter_not_sampled`) instead of a value
  interpolated across seconds without readings.

### `auto`

`auto` never runs a diagnostic. It uses the counter-disturbance verdict it is
given (from `aep validate --counter-disturbance --json hw.json`, passed as
`--hardware-report`, or `Session(disturbance=...)`):

| Verdict | Mode chosen | Why |
|---|---|---|
| `NOT_DETECTED`, tested on this configuration | `direct` | per-span reads were tested on this host, driver and GPU models and lowered nothing detectably |
| `NOT_DETECTED`, tested elsewhere (0.4.0) | `sampled` | the host name, driver version or GPU models in the report differ from the running ones |
| `DISTURBED` | `sampled` | per-span reads would bias span and run energy low |
| `INCONCLUSIVE` | `sampled` | not shown to be harmless |
| none available (`UNKNOWN`) | `sampled` | an unknown host is not assumed to be unaffected |

The chosen mode, the requested mode, the reason and the verdict with its
source are written to the session header and `run.json`, and shown in every
report.

## Defaults

| Entry point | Default | Changed in M5? |
|---|---|---|
| `Session(...)` (SDK) | **`auto` since 0.4.0** (`direct` before) | no; changed in 0.4.0 ([migration](#sdk-default-mode-migration)). The header says the mode was defaulted (`mode_source: "default"`) |
| `Session.from_env(...)` | under `aep run`: `aep run`'s mode (`mode_source: "aep_run"`); on its own: `default_mode=` if given (M6), else as `Session` | — |
| `aep run` | `--mode auto` (so `sampled` unless a hardware report says `NOT_DETECTED`); sampler power 10 Hz, counter at boundaries | **yes**: M3 read the counter per span and on every sampler row |
| `aep sample` / `sampling.Sampler` | counter on every row | no; new `--counter-hz` / `counter_hz=` |
| `aep attribute` (v1 and v2), `aep report` | unchanged for existing inputs | new sections only for inputs that declare M5 provenance |

`mode_source` (M6) records who chose the requested mode: `caller` (passed
explicitly, including `default_mode=`), `default` (an entry point's default),
`aep_run` (a session configured by `aep run`). In `run.json`, `default` means
`aep run` was not given `--mode`.

## Hardware-dependent decisions

- **The disturbance finding is host-specific.** Run
  `aep validate --counter-disturbance --json hw.json` on each host (and after
  driver changes) and pass `--hardware-report hw.json`.
- **With no GPU** every mode records GPU energy as `unavailable`
  (`no_counter` or `disabled`); the run counter is unavailable too.
- **With `DISTURBED`** prefer `sampled` or `hybrid`; `direct` is still allowed
  when requested explicitly, and reports then carry `COUNTER_DISTURBED`.
- **Instantaneous power** (`NVML_FI_DEV_POWER_INSTANT`) is not used: on the
  development host it disturbed the counter like a counter read.

## Recorded provenance

Session header `config.measurement` (model version 2):
`mode`, `requested_mode`, `mode_reason`, `mode_source` (M6), `span_counter_reads`, `semantics`,
`timeline` (`power_hz`, `power_api`, `counter_policy`,
`counter_every_n_rows`, `counter_hz_effective`), `counter_disturbance`
(`verdict`, `source`, `retained_median`, `read_hz`, `measured_at_unix_s`,
`driver_version`). `config.gpu.driver_version` is recorded when NVML is
read. The sampler's sidecar (`power.csv.meta.json`, version 2) carries the
same `timeline` block. Field definitions: [SCHEMA_V2.md](SCHEMA_V2.md).

## Migrating

- **SDK users** keep `direct` unless they opt in:

  ```python
  from agent_energy_profiler import Session
  from agent_energy_profiler.measurement import CounterDisturbance
  from agent_energy_profiler.sampling import Sampler

  hw = "hw.json"   # aep validate --counter-disturbance --json hw.json
  with Session("spans.jsonl", mode="auto",
               disturbance=CounterDisturbance.from_hardware_report(hw),
               sampler=Sampler("power.csv", hz=10, counter_hz=0)) as session:
      ...
  ```

  Pick the sampler's `counter_hz` to match the mode (`0` for `direct` and
  `sampled`, e.g. `1` for `hybrid`); `aep run` does this for you.
- **`aep run` users** get `sampled` by default now. To keep per-span counter
  measurement, either test the host and pass `--hardware-report hw.json`
  (`auto` then chooses `direct` if nothing was detected) or pass
  `--mode direct` explicitly.
- **Existing artifacts** are unchanged and readable. Reports mark them
  `MEASUREMENT_NOT_RECORDED` and, because their counter-read rate is not
  known to be harmless, `COUNTER_DISTURBANCE_UNKNOWN`.

## SDK default mode migration

**Since 0.4.0, `Session()` without `mode=` uses `auto`**, like `aep run`.
Before 0.4.0 it used `direct`. On a host without a disturbance test result
(the usual case) `auto` resolves to `sampled`, so code that passes no mode
now records **estimated** per-span GPU energy where it used to record
**measured** per-span GPU energy.

| Stage | Release | What changed | Status |
|---|---|---|---|
| 1. Visible | M6 (0.2.0 development) | Headers record `mode_source`. A defaulted session's `mode_reason` says so. Reports warn `DIRECT_MODE_BY_DEFAULT`. `Session.from_env(default_mode=...)`. Examples pass a mode explicitly. | done |
| 2. Announced | 0.3.0 | `Session()` without `mode=` emitted `SessionDefaultModeWarning` (a `FutureWarning`) once per process. Nothing measured changed. | done |
| 3. Switched | **0.4.0** | `Session()` defaults to `auto`. The warning is no longer emitted. | **done** |

### Before and after

| Code | 0.3.0 | 0.4.0 |
|---|---|---|
| `Session(path)` | `direct`, warns once | `auto` → `sampled` (or `direct`, see below); no warning |
| `Session(path, mode="direct")` | `direct` | `direct`, unchanged |
| `Session(path, mode="sampled")` / `"hybrid"` | as requested | unchanged |
| `Session(path, mode="auto", ...)` | `auto` | `auto`, plus the configuration check below |
| `Session.from_env(path)`, run on its own | `direct`, warns once | `auto`; no warning |
| `Session.from_env(path, default_mode=...)`, run on its own | `default_mode` | unchanged |
| `Session.from_env(...)` under `aep run` | `aep run`'s choice | unchanged |

Every session records `mode` (what ran), `requested_mode`, `mode_reason`,
`mode_source` (`caller`, `default` or `aep_run`) and the disturbance verdict
with its source. A defaulted 0.4.0 session records `requested_mode: "auto"`
and `mode_source: "default"`; a defaulted 0.2.0/0.3.0 session recorded
`requested_mode: "direct"` and `mode_source: "default"`. The two are
distinguishable in the artifact itself.

### When `auto` selects `direct`

Only when all of these hold:

1. a disturbance verdict was supplied
   (`disturbance=CounterDisturbance.from_hardware_report("hw.json")`, or
   `aep run --hardware-report hw.json`), and it is `NOT_DETECTED`;
2. **(new in 0.4.0)** the configuration that test ran on matches the running
   one: host name, NVIDIA driver version and GPU models, each compared when
   both the report and the running host know it. A report from another
   machine, or from before a driver update, selects `sampled`, and
   `mode_reason` names the difference.

Otherwise `sampled`. `auto` never runs the disturbance test itself: the test
takes about 40 s and loads the GPU, so it runs only through
`aep validate --counter-disturbance`. For a `Session`, the configuration is
checked when the session starts (after NVML is initialised), so
`session.mode` can change from `direct` to `sampled` at `start()`. Under
`aep run` the parent process checks it once and the program's session keeps
that choice.

### What changes for code that passes no mode

When `auto` resolves to `sampled`:

- **Per-span GPU energy is an estimate.** `energy.gpu` is `unavailable`
  with reason `not_read_by_mode`; the integrated-power value is under
  `estimates.gpu` (`status: "estimated"`), and span totals that need GPU are
  `null`. Exclusive GPU energy of parents is not available.
- **Measured GPU energy covers the whole run**, from a sampler (`aep run`
  or `Session(sampler=...)`) reading the counter at its first and last row.
  A `Session` without a sampler has no hardware timeline: spans then carry
  no GPU estimate either, until attributed against one. The report says so
  (`SPAN_ESTIMATES_ONLY`).
- **No per-span counter reads**, so no per-span NVML overhead (~2.5 ms per
  span on the development host) and no counter disturbance from them.
- CPU package and DRAM (RAPL) are unaffected: they always come from the
  sampler's timeline.

### Comparing experiments across versions

- **Check the mode before comparing per-span GPU numbers.** A per-span value
  from a `direct` session is a counter difference; from a `sampled` session
  it is an estimate in a different field (`estimates.gpu`). Do not put the
  two in one column. Every report prints the mode and who chose it on its
  `measurement` line; `jq '.config.measurement | {mode, requested_mode,
  mode_source}'` on the first line of `spans.jsonl` shows the same.
- **Whole-run measured GPU energy is comparable across modes**: the sampler
  reads the same counter at the run's boundaries in every mode. Fewer
  counter reads (as in `sampled`) can make the counter report *more* energy
  on hosts where reads disturb it, so a `direct` run on such a host may read
  lower than a `sampled` run of the same work.
- **To reproduce a pre-0.4.0 experiment exactly**, pass `mode="direct"` (or
  `default_mode="direct"` to `from_env`). Explicit modes behave exactly as
  in 0.3.0: same counter reads, same fields, same values.
- Artifacts written before 0.4.0 are unchanged and keep their meaning. They
  record the mode that actually ran; readers and reports need no
  migration. Reports still warn `DIRECT_MODE_BY_DEFAULT` for a pre-0.4.0
  session that ran `direct` by default without a `NOT_DETECTED` verdict.

### What to do

| You want | Write |
|---|---|
| the 0.3.0 behaviour (per-span counter measurement) | `Session(path, mode="direct")` |
| the conservative default | nothing, or `mode="auto"` explicitly |
| `direct` where it is shown to be harmless | `Session(path, mode="auto", disturbance=CounterDisturbance.from_hardware_report("hw.json"))` after `aep validate --counter-disturbance --json hw.json` on that host |
| `aep run`'s choice under `aep run`, your own otherwise | `Session.from_env(path, default_mode=...)` |

`measurement.SessionDefaultModeWarning` still exists, so code that filters it
keeps working, but it is never emitted; it will be removed in a later minor
release. Tests: `tests/test_default_mode.py` (the default, the configuration
check, explicit modes unchanged, no warning), `tests/test_session_hooks.py`
and `tests/test_measurement_modes.py`.

## Measured effect of the modes (development host)

Same method as M4 (`tools/nvml_diagnostics/sampler_interference.py`, idle GPU,
5 blocks, raw data in `results/nvml_m5/`). Counter energy retained by an
independent endpoint reader while a real sampler ran:

| Sampler configuration | Retained (mean ± 95% CI) |
|---|---|
| no sampler | 1 |
| original default: counter on every row, 10 Hz | 0.866 ± 0.007 |
| `direct` / `sampled`: counter at boundaries, power 10 Hz | 0.996 ± 0.018 |
| boundaries, power 50 Hz | 0.991 ± 0.016 |
| `hybrid`: counter 1 Hz, power 10 Hz | 0.984 ± 0.019 |

Per-span instrumentation cost (median added wall time; NVML on WSL2):
`direct` 1.9–4.5 ms (p90 ~9.5 ms); `sampled` and `hybrid` 0.014–0.042 ms.
