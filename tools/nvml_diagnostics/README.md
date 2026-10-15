# NVML counter diagnostics (Milestone 4)

Experimental tools, **not part of the profiler**. They test whether reading
NVML changes the energy that `nvmlDeviceGetTotalEnergyConsumption` reports.
Results and interpretation: `docs/NVML_COUNTER_VALIDATION.md`; raw data:
`results/nvml_m4/`.

| File | Does |
|---|---|
| `experiment.py` | runs counterbalanced trials: identical endpoint counter reads around an 8 s window, with different NVML reads (or none) inside it |
| `workload.py` | reproducible GPU load in a separate process: `steady` (continuous matmul) or `pulsed` (0.3 s on / 0.3 s off) |
| `analyze.py` | per-condition statistics, carry-over check, counter-update cadence, read latency, figure |

## Run

```bash
# idle GPU: needs only nvidia-ml-py (the [gpu] extra)
.venv/bin/python tools/nvml_diagnostics/experiment.py --workload idle --out results/x/idle

# with load: an interpreter that has PyTorch + CUDA runs the workload
.venv/bin/python tools/nvml_diagnostics/experiment.py --workload steady \
    --workload-python /path/to/torch-venv/bin/python --out results/x/steady
.venv/bin/python tools/nvml_diagnostics/experiment.py --workload pulsed \
    --workload-python /path/to/torch-venv/bin/python --out results/x/pulsed

.venv/bin/python tools/nvml_diagnostics/analyze.py results/x/idle results/x/steady \
    results/x/pulsed --out results/x
```

Defaults: 12 conditions, 4 blocks, 8 s windows, 3 s read-free rest before
every trial, 30 s read-free warm-up, seed 20261015, abort at 83 °C. Each
workload takes ~10 minutes.

## Design

- **Same endpoints everywhere.** Every trial reads the counter once at the
  start and once at the end. Conditions differ only in the NVML calls made
  between the endpoints, so the counter difference measures the effect of
  those calls.
- **The comparison is counter vs counter.** `relative` divides each trial's
  counter rate by the read-free `endpoints` trial of the same block. That
  needs no assumption about which instrument is correct. Power appears only in
  the conditions that read it, reported beside the counter, never as a
  reference.
- **Counterbalanced.** Conditions are shuffled per block, so drift
  (temperature, background activity) spreads across conditions.
- **Controls.** `temperature_10hz` tests whether *any* NVML query disturbs the
  counter; `power_*` test whether power queries do (as reported on the NVIDIA
  forum); `energy_field_10hz` tests the field-value API path to the same
  counter.
- **Raw data kept.** Every in-window read is stored with the `perf_counter`
  time before and after the call.

## Limits of what this can show

- Within one machine it can show that reads change the counter's output. It
  cannot show which of the counter and the power sensor is correct: that needs
  an external meter (see the validation document).
- Uncontrolled on WSL2: the Windows host's own GPU monitoring (Task Manager,
  vendor overlays) queries the same driver and may add reads the experiment
  cannot see.
- One GPU, one driver version.
