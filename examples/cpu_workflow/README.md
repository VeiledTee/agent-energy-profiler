# Example 1: CPU-only workflow

Four stages of ordinary CPU work (generate data, sort, sieve primes,
serialise), each a span, all inside one `pipeline:run` span.

## Install

```bash
python3 -m venv .venv
.venv/bin/pip install agent-energy-profiler            # or a clone or wheel
.venv/bin/pip install 'agent-energy-profiler[gpu]'     # with NVML bindings
```

## Run

```bash
agent-energy-profiler validate --counter-disturbance --json hw.json                        # once per machine
agent-energy-profiler run --hardware-report hw.json --out-dir runs/cpu -- \
    python examples/cpu_workflow/workflow.py
```

On its own, `Session.profile` starts the sampler itself and prints the
report to stderr at the end; the files go to
`agent-energy-profiler-runs/cpu_workflow-<timestamp>/` (or `--out-dir`), and
there is no `run.json`:

```bash
python examples/cpu_workflow/workflow.py
```

`--scale 0.2` makes it faster, `--scale 5` slower.

## Output

`runs/cpu/` contains:

| File | What |
|---|---|
| `spans.jsonl` | the raw schema-v2 spans written by the program |
| `power.csv`, `power.csv.meta.json` | the sampler's hardware timeline |
| `spans_attributed.jsonl` | spans with CPU/DRAM energy, estimates, totals and accounting |
| `session_summary.json` | totals, window measurement, per-label rollups |
| `run.json` | the command, exit code and whole-program energy |

Report from an RTX 4090 under WSL2 (abridged; your numbers will differ):

```
MEASURED ENERGY (hardware counters; basis: root spans)
  gpu                          6.609 J
  cpu_package                    n/a    1 of 1 top-level spans lack a measured value
  dram                           n/a    1 of 1 top-level spans lack a measured value
INDEPENDENT WINDOW MEASUREMENT
  sampler timeline over the session window: gpu 7.374 J, cpu_package n/a J, dram n/a J

BY OPERATION (energy: exclusive; top 5 by total time)
  operation                       n     total ...       gpu J   cpu_pkg J
  pipeline:run                    1  351.0 ms ...       0.000         n/a
  compute:sort                    1  134.5 ms ...       2.224         n/a
  compute:primes                  1  125.4 ms ...       2.291         n/a
  data:generate                   1   65.8 ms ...       2.094         n/a
  io:serialize                    1    3.4 ms ...       0.000         n/a
WARNINGS
  [BELOW_RESOLUTION] 2 of 5 measured GPU values (40.0%) come from spans shorter than the counter's update interval (~102.4 ms) ...
  [ZERO_READINGS] 1 spans read exactly 0 J of GPU energy ...
  [DOMAIN_UNAVAILABLE] cpu_package was not measured for any span ...
```

## Reading it

- **Why is there GPU energy for CPU work?** The GPU counter measures the whole
  device, which draws power (here ~19 W) while idle. That energy is real and
  is attributed to whatever span is open. It is not the cost of the CPU work.
- **`cpu_package` is n/a** on WSL2, VMs and most containers: there is no RAPL
  interface. On bare-metal Linux with readable `/sys/class/powercap` it is
  measured. `agent-energy-profiler validate` tells you which applies.
- **`io:serialize` reads 0 J** because it lasted 3.4 ms and the GPU counter
  updates every ~102 ms. That zero is quantisation, flagged below resolution,
  not "no energy".
- **`pipeline:run` exclusive 0.000 J**: its children ran back to back and
  cover almost all of its time, so its own share is what the counter saw
  outside them.
- The whole-program figure in `run.json` is larger than the spans' total: it
  also covers interpreter start-up and imports.
