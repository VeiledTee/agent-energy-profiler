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
| `NOT_DETECTED` | `direct` | per-span reads were tested on this host and lowered nothing detectably |
| `DISTURBED` | `sampled` | per-span reads would bias span and run energy low |
| `INCONCLUSIVE` | `sampled` | not shown to be harmless |
| none available (`UNKNOWN`) | `sampled` | an unknown host is not assumed to be unaffected |

The chosen mode, the requested mode, the reason and the verdict with its
source are written to the session header and `run.json`, and shown in every
report.

## Defaults

| Entry point | Default | Changed in M5? |
|---|---|---|
| `Session(...)` (SDK) | `mode="direct"` | no: the behaviour of every earlier session; the header now records it |
| `aep run` | `--mode auto` (so `sampled` unless a hardware report says `NOT_DETECTED`); sampler power 10 Hz, counter at boundaries | **yes**: M3 read the counter per span and on every sampler row |
| `aep sample` / `sampling.Sampler` | counter on every row | no; new `--counter-hz` / `counter_hz=` |
| `aep attribute` (v1 and v2), `aep report` | unchanged for existing inputs | new sections only for inputs that declare M5 provenance |

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
`mode`, `requested_mode`, `mode_reason`, `span_counter_reads`, `semantics`,
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
