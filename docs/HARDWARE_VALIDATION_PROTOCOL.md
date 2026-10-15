# Hardware validation protocol

**Status: not yet carried out.** No bare-metal measurement and no external
power reference exist for this project yet. Every accuracy statement
elsewhere in the documentation is limited to internal consistency on one
RTX 4090 under WSL2 (driver 610.88). This document says what has to be done
before any GPU (or CPU/DRAM) energy figure from this profiler can be called
accurate, and how a result passes or fails.

## 1. What is being validated

| Question | Instrument under test | Reference |
|---|---|---|
| Q1 Is the undisturbed counter accurate? | counter difference with no reads in between | external meter |
| Q2 Do reads disturb the counter on this host? | counter with reads in between vs without | the same counter, read-free |
| Q3 How good is integrated averaged power? | `GetPowerUsage` sampled and integrated | external meter |
| Q4 Do the measurement modes deliver what they claim? | `direct`, `sampled`, `hybrid` runs of fixed workloads | external meter |
| Q5 CPU package / DRAM (RAPL) | RAPL counters | external meter on the CPU 12 V rails, or a calibrated wall meter with the GPU removed from the reference |

## 2. Test matrix

- **Hosts.** Bare-metal Linux (required). WSL2 (to compare with the
  development host). Optionally a VM with GPU passthrough.
- **GPUs.** The RTX 4090 used so far, and at least one other NVIDIA GPU,
  preferably a data-centre part (A100/H100/L40S) and one other consumer card.
- **Drivers.** At least two driver branches on the same host and GPU,
  including 591.86 (the research runs) and 610.88 or newer, so driver effects
  are separated from hardware effects.
- **Workloads.** `tools/nvml_diagnostics/workload.py` idle, steady and pulsed;
  the GPU inference example (`examples/gpu_inference`); a load step
  (`tools/nvml_diagnostics/step_response.py`).
- **Monitoring hygiene.** No other NVML readers: stop `nvidia-smi` loops, DCGM,
  dashboards and, on Windows hosts, Task Manager's GPU view. Record what is
  running (`nvidia-smi --query-compute-apps`, process list) in the run notes.

## 3. External reference

- **GPU.** Measure every power input to the card: the PCIe slot (12 V and 3.3
  V) via an instrumented riser, and each auxiliary 12 V connector via an
  inline shunt or hall-effect sensor. Sample at ≥ 1 kHz; record voltage and
  current, not only power.
- **Calibration.** Calibrate each channel against a reference load before the
  campaign; state the meter's own uncertainty (gain, offset, bandwidth) as a
  percentage of reading plus a fixed term.
- **Time base.** Synchronise the meter with the host: record a sync pulse or
  a known power step (e.g. a 1 s burst) at the start and end of each session,
  and check clock drift over the session (target < 1 ms/min).
- **Scope.** The meter sees board input power. NVML reports may exclude or
  include parts of the board differently, so a constant offset is possible;
  report it rather than hiding it.

## 4. Procedure

For each host × GPU × driver:

1. `aep validate --counter-disturbance --json hw-<host>-<driver>.json`.
2. `tools/nvml_diagnostics/experiment.py` for idle, steady and pulsed (12
   conditions, ≥ 4 blocks, seed recorded), with the external meter recording
   throughout.
3. `tools/nvml_diagnostics/step_response.py` (≥ 5 steps).
4. `tools/nvml_diagnostics/sampler_interference.py` (≥ 5 blocks).
5. `aep run --mode direct|sampled|hybrid` on the GPU example and the agent
   example, ≥ 5 runs each, order randomised.
6. Integrate the meter over every trial window (using the synchronised
   time base) and store it beside the trial in the raw results.

## 5. Analysis and uncertainty

- Per trial: meter energy `E_ref`, counter energy `E_ctr`, integrated
  averaged power `E_pwr`. Report `E_ctr / E_ref` and `E_pwr / E_ref` with 95%
  confidence intervals over trials, per condition.
- Propagate the meter's uncertainty and the counter's quantisation (one
  update step at each end of a window) into each ratio.
- Report results per host, GPU and driver; do not pool across them unless the
  intervals overlap.

## 6. Pass and fail criteria

These are proposed thresholds, to be confirmed against the meter's own
uncertainty before the campaign.

| Check | Pass |
|---|---|
| C1 Undisturbed counter accuracy (Q1) | `E_ctr / E_ref` within ±3% (after any documented constant offset) on 8 s windows, all three workloads |
| C2 No read disturbance at the modes' rates (Q2) | for `boundaries` and 1 Hz counter reads, retained ≥ 0.98 with a 95% CI that includes 1 |
| C3 Counter disturbance verdict is correct | `aep validate --counter-disturbance` reports DISTURBED iff the experiment's 10 Hz energy condition retains < 0.95 |
| C4 Averaged-power estimate (Q3) | `E_pwr / E_ref` within ±5% in steady state; the step response's bias is reported, not judged |
| C5 Modes (Q4) | `run_counter` of every mode within ±3% of `E_ref` over the same window; `direct` per-span sums within ±5% of `E_ref` for spans ≥ 1 s |

A host failing C2 must not use `direct`. A host failing C1 cannot report
measured GPU energy without the documented bias. A failure is a finding, to
be recorded in PROVENANCE with its data, never patched with a correction
factor.

## 7. Outputs

- Raw data under `results/validation/<host>-<gpu>-<driver>/`, with meter
  traces, host metadata, and the exact commit of this repository.
- A summary table per check (C1–C5) with intervals.
- An entry in `docs/PROVENANCE.md` stating which claims each host now
  supports.
