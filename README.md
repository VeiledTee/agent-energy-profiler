# agent-energy-profiler

Hardware-grounded, operation-level energy profiling for Python AI agents and
other multi-step programs.

You mark the steps of your program as spans (`llm:plan`, `tool:search`,
`verify:check`, …). The profiler reads hardware energy counters (NVIDIA NVML
for GPUs, Linux RAPL for CPU package and DRAM) and reports how much energy
the device used during each span, how long each step took, and how confident
you can be in each number.

> **Status: 0.2.0, research-grade.** Extracted from a PhD research codebase
> ([docs/PROVENANCE.md](docs/PROVENANCE.md)). Measurements are validated
> against golden research artifacts, but read
> [Known limitations](#known-limitations) before relying on GPU figures:
> on the development host, reading the NVML energy counter frequently makes
> it under-report.

## What it measures, and what it does not

| Measures | Does not measure |
|---|---|
| Whole-**device** energy from hardware counters: NVIDIA GPU (NVML), CPU package and DRAM (RAPL) | Energy of one process, thread or request on a shared device |
| Energy over each span's time window, attributed by time | Which code inside a span used the energy |
| Inclusive and exclusive (children removed) energy of nested spans | A split of energy between spans that ran at the same time |
| Latency of every span; whole-program energy under `aep run` | What an uninstrumented program was doing internally |
| Integrated sampled power, as a labelled **estimate** | Storage, network, fans, PSU losses, facility overhead, carbon (unless you convert with your own grid intensity, `carbon.py`) |

Energy is reported in joules. A missing measurement is `null`, never 0.

## Install

Not on PyPI yet. From a clone or a built wheel:

```bash
python -m venv .venv
.venv/bin/pip install /path/to/agent-energy-profiler          # core: no dependencies
.venv/bin/pip install '/path/to/agent-energy-profiler[gpu]'   # + NVIDIA GPU counters (nvidia-ml-py)
.venv/bin/pip install '/path/to/agent-energy-profiler[plot]'  # + figures (matplotlib)
.venv/bin/pip install -e '.[test]'                            # development: full test suite
```

Python 3.9 or later.

**Supported hardware.** Anything works; what gets measured depends on the
host. `aep validate` tells you exactly.

| Host | GPU | CPU package / DRAM |
|---|---|---|
| Linux, bare metal, NVIDIA GPU (Volta or newer) | measured | measured if `/sys/class/powercap` (Intel or AMD RAPL) or `amd_energy` is readable |
| WSL2 with an NVIDIA GPU | measured (see limitations) | not available (no RAPL in WSL2) |
| VMs, most containers, shared HPC nodes | often available | usually not available |
| No NVIDIA GPU | not available | as above |
| macOS, Windows native | not supported | not supported |

## Minimal example

```python
from agent_energy_profiler import Session

with Session.from_env("spans.jsonl") as session:          # `aep run` sets the path
    with session.span("agent:question", attributes={"id": "q1"}):
        with session.span("retrieval:search"):
            docs = search(query)
        with session.span("llm:reason") as s:
            answer = llm(query, docs)
            s.set_tokens(input=812, output=64)
```

```bash
aep validate --json hw.json                               # once per machine
aep run --resolution-report hw.json --out-dir runs/demo -- python my_agent.py
```

`aep run` starts the hardware sampler, runs the program, attributes its spans
and prints a report. Three complete examples (CPU-only, GPU inference, an
agent loop) are in [examples/](examples/).

## CLI

| Command | Does |
|---|---|
| `aep validate [--json hw.json] [--counter-disturbance]` | probes NVML and RAPL, measures the GPU counter's update interval, says whether a complete gpu+cpu+dram total is possible here; with `--counter-disturbance` (~30 s) also tests whether reading the counter lowers what it reports |
| `aep run [opts] -- CMD` | runs CMD with the sampler on; writes `run.json` (whole-program energy), and if the program records spans, attributes them and prints the report |
| `aep attribute FILE --power power.csv` | joins spans (v2) or events (v1) with the hardware timeline |
| `aep report DIR_OR_FILE [--format text\|json\|csv]` | readable or structured summary of a v1 or v2 run |
| `aep export DIR_OR_FILE --out-dir D` | `spans.csv` (one row per span), `operations.csv`, `report.json` |
| `aep sample --out power.csv` | the sampler on its own |

`python -m agent_energy_profiler …` is equivalent to `aep …`.

**`aep run` does not look inside your program.** Without spans it reports
only whole-program energy (`run.json`). Operation-level numbers exist only
for the spans the program records through the SDK.

## Interpreting the numbers

The report separates four kinds of value:

| Kind | Where | Meaning |
|---|---|---|
| **Measured** | "MEASURED ENERGY", `energy.*` | a hardware counter difference over the span's window |
| **Below resolution** | measured, flagged `below_resolution` | the span was shorter than the counter's update interval (~102 ms for the NVML counter on the development GPU); the value is quantised, usually exactly 0 J or one update, and biased low |
| **Missing** | `n/a`, with a reason | no defensible counter difference: no counter, failed read, counter reset, outside the sampler's timeline |
| **Estimated** | "ESTIMATED ENERGY", `estimates.*` | integrated sampled power, shown only for spans without a measurement; never added to measured totals |

Rules the report follows:

- **No double counting.** Totals add top-level spans only. Per-operation
  energy is *exclusive*: a parent's own share with its children removed.
- **No partial totals.** A total appears only if every contributing span was
  measured; otherwise the report shows "n/a" and, separately, the sum over the
  measured spans with how many that covers.
- **Concurrency is not split.** Spans that overlap other recorded work are
  `shared`: their (inclusive) energy includes the other work and they get no
  exclusive energy. Concurrent top-level spans mean no span-based total; use
  the independent window measurement.
- **0 J is not "no energy".** For short spans it is quantisation.
- **Warnings** at the end of every report name what limits the numbers above
  them (unknown resolution, below-resolution share, shared spans, missing
  domains, estimates, clock anomalies).

Short operations: with an NVIDIA GPU, each span costs about 1.7–2.2 ms of
extra wall time on the development host (two NVML reads; p90 ~8 ms), against
0.01–0.05 ms without GPU reading. The reads lie outside the span's measured
duration but lengthen the workload, and below ~20 ms per operation that
overhead is a large fraction of the work. Use `gpu=False` (or
`aep run --gpu off`) when you only need timings.

## Schema compatibility

| | Schema v1 (`events.jsonl`) | Schema v2 (`spans.jsonl`) |
|---|---|---|
| Written by | `profiler.span`, `profiler.configure` (legacy functional API) | `Session` |
| Attributed by | `aep attribute` (v1 pipeline, unchanged, byte-identical to the research results) | `aep attribute` / `aep-attribute-v2` |
| Report / export | `aep report`, `aep export` | `aep report`, `aep export` |
| Hierarchy | none | trace, span and parent ids; exclusive energy |
| Estimates | mixed into `gpu_energy_j` (marked by `gpu_energy_source`) | separate `estimates` |

- v1 files stay readable by everything; v2 reads them by upgrading each event
  on the fly (the file is not changed). v1's power-integration values are then
  treated as estimates.
- v1-only tools (`aep-aggregate`, `aep-visualize`, `trajectory`) do not read
  v2.
- Field definitions: [docs/SCHEMA_V2.md](docs/SCHEMA_V2.md). Migration table
  from the v1 API: [docs/SCHEMA_V2.md](docs/SCHEMA_V2.md#reading-v1-as-v2)
  and the table below.

| v1 | v2 |
|---|---|
| `profiler.configure(events_file=..., run_id=...)` | `Session(path, attributes={"run_id": ...})` |
| `profiler.span("llm:plan") as s; s["input_tokens"] = n` | `session.span("llm:plan") as s; s.set_tokens(input=n)` (`s["input_tokens"] = n` also works) |
| `profiler.set_question(q)` | `attributes={"question_id": q}` or a parent span per question |
| `aep-attribute` | `aep attribute` (detects the schema) |

## Known limitations

1. **NVML counter disturbance (development host).** On WSL2 with driver
   610.88, each read of the GPU energy counter (or of the instantaneous-power
   field) removes ~10 ms of accumulation from it, for all readers: the 10 Hz
   sampler lowers the counter ~10–11%, and frequent span reads lower it
   further. Averaged-power and temperature reads do not. Check your host with
   `aep validate --counter-disturbance`; where it reports DISTURBED, treat GPU
   figures taken with frequent reads as **lower bounds**. Details and bare-metal
   requirements: [docs/NVML_COUNTER_VALIDATION.md](docs/NVML_COUNTER_VALIDATION.md).
2. **Device-level only.** Concurrent work, other processes and background
   services on the same device are inside every number.
3. **Counter resolution.** The NVML counter updates every ~100–107 ms (it
   varied by run and driver on one GPU); characterise it per host with
   `aep validate`.
4. **CPU/DRAM untested on real hardware.** RAPL handling (wraparound,
   multi-socket) is tested synthetically only.
5. **Asynchronous GPU work.** Synchronise (e.g. `torch.cuda.synchronize()`)
   before a span closes; work in another process (vLLM, an inference server)
   is bounded by the request, not its kernels.
6. **No multiprocessing attribution.** One `Session` per process; nothing
   separates device energy between processes.
7. **No idle subtraction.** Idle draw is part of every figure.

## Test

```bash
.venv/bin/pip install -e '.[test]'
.venv/bin/python -m pytest
# opt-in: re-attribute complete frozen research runs
AEP_FROZEN_RUNS_DIR=/path/to/runs .venv/bin/python -m pytest -m frozen_runs
# opt-in: read the real NVML counter through Session
AEP_REAL_GPU=1 .venv/bin/python -m pytest tests/test_session.py -k real_nvml
# testing an installed wheel from a copy of tests/
AEP_TEST_INSTALLED=1 python -m pytest tests
```

Run the suite with `TMPDIR` outside any git working tree: one provenance test
checks that a temporary directory is *not* a repository.

## Repository hygiene

- No AI tool appears as a Git author or co-author. Enable the commit-message
  guard once per clone: `git config core.hooksPath scripts/git-hooks`.
- `tests/golden/` (v1) and `tests/golden_v2/` (v2) hold reference artifacts.
  Never regenerate them to make a failing test pass. A deliberate semantic
  change is documented in `docs/PROVENANCE.md`.
