# Agent Energy Profiler

Hardware-grounded, operation-level energy profiling for Python AI agents and
other multi-step programs.

You mark the steps of your program as spans (`llm:plan`, `tool:search`,
`verify:check`, …). The profiler reads hardware energy counters (NVIDIA NVML
for GPUs, Linux RAPL for CPU package and DRAM) and reports how much energy
the device used during each span and how long each step took. It also says
how much each number can be trusted.

> **Status: 0.4.0, beta research software. Not yet on PyPI.** Read this
> before relying on any number:
>
> - **GPU energy is measured at device scope.** NVML reports the whole GPU,
>   including idle draw, other processes and concurrent work. A span's value
>   is what the device used while the span was open, not what the span
>   caused.
> - **Per-operation values may be estimated.** In the default mode, per-span
>   GPU energy is usually an estimate from averaged power, and only the whole
>   run is a counter measurement. Estimates are labelled, reported
>   separately, and never added to measured totals.
> - **Accuracy has not been established against an independent meter.** No
>   value has been compared with an external power meter.
> - **RAPL (CPU package, DRAM) has not been validated on real hardware.** It
>   is tested only with synthetic counters.
> - **Counter disturbance has been demonstrated on one configuration.** On
>   an RTX 4090 with driver 610.88 under WSL2, frequent reads of the NVML
>   energy counter lowered what it reported. Other GPUs and drivers are
>   untested. `agent-energy-profiler validate --counter-disturbance` checks
>   yours.
>
> The attribution logic is regression-tested against reference artifacts
> from the research runs it was extracted from
> ([docs/PROVENANCE.md](docs/PROVENANCE.md)). That checks the computation is
> reproduced exactly. It does not show the energy values are correct.

## What it measures, and what it does not

| Measures | Does not measure |
|---|---|
| Whole-**device** energy from hardware counters: NVIDIA GPU (NVML), CPU package and DRAM (RAPL) | Energy of one process, thread or request on a shared device |
| Energy over each span's time window, attributed by time | Which code inside a span used the energy |
| Inclusive and exclusive (children removed) energy of nested spans | A split of energy between spans that ran at the same time |
| Latency of every span; whole-program energy under `agent-energy-profiler run` | What an uninstrumented program was doing internally |
| Integrated sampled power, as a labelled **estimate** | Storage, network, fans, PSU losses, facility overhead, carbon (unless you convert with your own grid intensity, `carbon.py`) |

Energy is reported in joules. A missing measurement is `null`, never 0.

## Install

Not on PyPI yet. Install from GitHub or from a clone:

```bash
python -m venv .venv && . .venv/bin/activate
pip install "agent-energy-profiler @ git+https://github.com/VeiledTee/agent-energy-profiler"
# or, in a clone:
pip install .              # core: no dependencies
pip install '.[gpu]'       # + NVIDIA GPU counters (nvidia-ml-py)
pip install '.[plot]'      # + figures (matplotlib)
pip install '.[otel]'      # + OpenTelemetry correlation/export
pip install '.[otlp]'      # + OTLP/HTTP for `agent-energy-profiler otel-export --endpoint`
pip install -e '.[test]'   # development: full test suite
```

This installs the `agent-energy-profiler` command and the
`agent_energy_profiler` Python package. `python -m agent_energy_profiler` is
the same command.

## Supported platforms

Python 3.9–3.14 on Linux. The core has no dependencies, and anything it
cannot measure is reported as unavailable. `agent-energy-profiler validate`
shows what a given host can measure.

| Host | GPU energy | CPU package / DRAM | Tested |
|---|---|---|---|
| Linux, bare metal, NVIDIA GPU (Volta or newer) | NVML counter | RAPL, if `/sys/class/powercap` (Intel or AMD) or `amd_energy` is readable | no physical-hardware run yet; CI uses synthetic counters |
| WSL2 with an NVIDIA GPU | NVML counter (see [Known limitations](#known-limitations)) | not available (WSL2 has no RAPL) | yes: RTX 4090, driver 610.88 (the development machine) |
| VMs, most containers, shared HPC nodes | often available | usually not available | no |
| Linux without an NVIDIA GPU | not available | as above | CI (Ubuntu, GitHub-hosted runners) |
| macOS, native Windows | not supported | not supported | — |

## Quick start

A complete walkthrough with real output is in
[examples/agent_workflow](examples/agent_workflow/). It covers install,
instrumenting an agent, running, reporting, and measured versus estimated
energy. In short:

```python
from agent_energy_profiler import Session

# under `agent-energy-profiler run`, the run sets the path and mode
with Session.from_env("spans.jsonl", default_mode="auto") as session:
    with session.span("agent:question", attributes={"id": "q1"}):
        with session.span("retrieval:search"):
            docs = search(query)
        with session.span("llm:reason") as s:
            answer = llm(query, docs)
            s.set_tokens(input=812, output=64)
```

```bash
agent-energy-profiler validate --counter-disturbance --json hw.json   # once per machine (~40 s)
agent-energy-profiler run --hardware-report hw.json --out-dir runs/demo -- python my_agent.py
agent-energy-profiler report runs/demo
```

`agent-energy-profiler run` starts the hardware sampler, runs the program,
attributes its spans and prints a report. Four examples are in
[examples/](examples/): an agent loop, CPU-only work, GPU inference and an
OpenTelemetry-traced service.

## Instrumenting an application

| Need | Code |
|---|---|
| a session | `with Session("spans.jsonl", mode="auto") as session:` (or `Session.from_env(path, default_mode="auto")` to follow `agent-energy-profiler run` when run under it) |
| a block | `with session.span("tool:search", attributes={...}) as s:` |
| async code | `async with session.span("llm:plan"):` — tasks created inside inherit the parent |
| a function | `@session.trace("tool:search")` on a sync or async function; the label is required |
| tokens, attributes, status | `s.set_tokens(input=812, output=64)`, `s.set_attribute(k, v)`, `s.set_status("timeout")` |
| tag a request or task | `with session.bind(request_id=rid):` — every span opened inside gets the attribute, no extra span |
| work handed to a thread | `session.span(..., parent=handle)`; new threads start without a parent |
| attribute and report from Python | `rep = analyze(session)` then `print(report.render_text(rep))` |
| join OpenTelemetry traces / export | `Session(..., hooks=[otel.Correlation()])`, then `otel.export_spans(run_dir, exporter)` |

```python
import os
from agent_energy_profiler import Session, analyze, report
from agent_energy_profiler.sampling import Sampler

os.makedirs("run", exist_ok=True)
with Session("run/spans.jsonl", mode="auto",
             sampler=Sampler("run/power.csv", hz=10, counter_hz=0)) as session:
    with session.bind(request_id="r1"):
        with session.span("agent:request"):
            ...
print(report.render_text(analyze(session)))   # attributes run/, prints the report
```

Labels always come from your code. The profiler never derives one from a
function name, a prompt or the call stack.

## OpenTelemetry and frameworks

- **OpenTelemetry** (`[otel]` extra): root spans join the application's
  current trace. Optionally, application spans nest under profiler spans.
  After attribution, `export_spans` / `agent-energy-profiler otel-export`
  sends every span with its energy as `aep.*` attributes to your exporter. Measured, estimated,
  missing, shared and below-resolution values stay distinguishable. Global
  OpenTelemetry configuration is never touched. Details and the attribute
  list: [docs/OPENTELEMETRY.md](docs/OPENTELEMETRY.md).
- **LangGraph**: decorate node functions with `session.trace(label)`. Tested
  against LangGraph 1.2.15 for sync and async graphs with parallel branches.
  Why not callbacks: [docs/LANGGRAPH.md](docs/LANGGRAPH.md).

## Measurement modes

How often the GPU energy counter is read is a choice, because on at least one
host each read lowers what the counter reports
([docs/NVML_COUNTER_VALIDATION.md](docs/NVML_COUNTER_VALIDATION.md)):

| Mode | Per-span GPU energy | Measured GPU energy |
|---|---|---|
| `direct` | measured: counter read at each span's boundaries | each span, and the run |
| `sampled` | estimated from averaged power | the run (counter at the sampler's first and last row) |
| `hybrid` | estimated from averaged power | the run, plus a 1 Hz measured counter series checked against the estimate |
| `auto` | `direct` only if `agent-energy-profiler validate --counter-disturbance` found no effect on this host, driver and GPU; otherwise `sampled` | |

**Since 0.4.0 both `agent-energy-profiler run` and `Session` default to
`auto`.** Without a hardware report showing that counter reads are harmless on this host,
driver and GPU, that means `sampled`: per-span GPU energy is an **estimate**
from averaged power, and the measured GPU figure covers the whole run.
`Session` defaulted to `direct` (per-span counter measurement) before 0.4.0.
Pass `mode="direct"` to keep that behaviour, and check each artifact's mode
before comparing per-span GPU numbers across versions. Details, the
before/after table and how to compare experiments:
[docs/MEASUREMENT_MODES.md](docs/MEASUREMENT_MODES.md#sdk-default-mode-migration).
Every artifact records the mode, who chose it, the sampler's read rates,
the disturbance verdict and its source. Reports warn when frequent counter
reads met an unknown or detected disturbance.

## CLI

| Command | Does |
|---|---|
| `agent-energy-profiler validate [--json hw.json] [--counter-disturbance]` | probes NVML and RAPL, measures the GPU counter's update interval, says whether a complete gpu+cpu+dram total is possible here; with `--counter-disturbance` (~30 s) also tests whether reading the counter lowers what it reports |
| `agent-energy-profiler run [--mode M] [--hardware-report hw.json] [--power-hz N] [--counter-hz N] -- CMD` | runs CMD with the sampler on; writes `run.json` (whole-program energy, measurement mode); if the program records spans, attributes them and prints the report |
| `agent-energy-profiler attribute FILE --power power.csv` | joins spans (v2) or events (v1) with the hardware timeline |
| `agent-energy-profiler report DIR_OR_FILE [--format text\|json\|csv]` | readable or structured summary of a v1 or v2 run |
| `agent-energy-profiler export DIR_OR_FILE --out-dir D` | `spans.csv` (one row per span), `operations.csv`, `report.json` |
| `agent-energy-profiler otel-export DIR_OR_FILE [--endpoint URL \| --out FILE]` | spans as OpenTelemetry spans with energy attributes: OTLP/HTTP, JSON lines, or the console (`[otel]` / `[otlp]` extras) |
| `agent-energy-profiler sample --out power.csv [--hz N] [--counter-hz N]` | the sampler on its own (default: counter on every row, as before; `--counter-hz 0`: first and last row only) |

`python -m agent_energy_profiler …` is equivalent to `agent-energy-profiler …`.
The single-purpose scripts `agent-energy-profiler-sample`, `-attribute`,
`-attribute-v2`, `-validate`, `-aggregate` and `-visualize` are also
installed.

**Names.** The project is Agent Energy Profiler. The distribution and the
command are `agent-energy-profiler`, and the import is `agent_energy_profiler`.
The short prefix `aep` survives only in recorded data and interfaces that
other code depends on: the `AEP_*` environment variables, the OpenTelemetry
attribute namespace `aep.*`, and the recorded value `mode_source: "aep_run"`
(a session configured by `agent-energy-profiler run`). These stay unchanged so
that existing artifacts and dashboards keep their meaning.

**`agent-energy-profiler run` does not look inside your program.** Without
spans it reports only whole-program energy (`run.json`). Operation-level numbers exist only
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
`agent-energy-profiler run --gpu off`) when you only need timings.

## Schema compatibility

| | Schema v1 (`events.jsonl`) | Schema v2 (`spans.jsonl`) |
|---|---|---|
| Written by | `profiler.span`, `profiler.configure` (legacy functional API) | `Session` |
| Attributed by | `agent-energy-profiler attribute` (v1 pipeline, unchanged, byte-identical to the research results) | `agent-energy-profiler attribute` / `agent-energy-profiler-attribute-v2` |
| Report / export | `agent-energy-profiler report`, `agent-energy-profiler export` | `agent-energy-profiler report`, `agent-energy-profiler export` |
| Hierarchy | none | trace, span and parent ids; exclusive energy |
| Estimates | mixed into `gpu_energy_j` (marked by `gpu_energy_source`) | separate `estimates` |

- v1 files stay readable by everything; v2 reads them by upgrading each event
  on the fly (the file is not changed). v1's power-integration values are then
  treated as estimates.
- v1-only tools (`agent-energy-profiler-aggregate`, `agent-energy-profiler-visualize`, `trajectory`) do not read
  v2.
- Field definitions: [docs/SCHEMA_V2.md](docs/SCHEMA_V2.md). Migration table
  from the v1 API: [docs/SCHEMA_V2.md](docs/SCHEMA_V2.md#reading-v1-as-v2)
  and the table below.

| v1 | v2 |
|---|---|
| `profiler.configure(events_file=..., run_id=...)` | `Session(path, attributes={"run_id": ...})` |
| `profiler.span("llm:plan") as s; s["input_tokens"] = n` | `session.span("llm:plan") as s; s.set_tokens(input=n)` (`s["input_tokens"] = n` also works) |
| `profiler.set_question(q)` | `attributes={"question_id": q}` or a parent span per question |
| `agent-energy-profiler-attribute` | `agent-energy-profiler attribute` (detects the schema) |

## Known limitations

1. **NVML counter disturbance (development host).** On WSL2 with driver
   610.88, each read of the GPU energy counter (or of the instantaneous-power
   field) removes ~10 ms of accumulation from it, for all readers: the 10 Hz
   sampler lowers the counter ~10–11%, and frequent span reads lower it
   further. Averaged-power and temperature reads do not. Check your host with
   `agent-energy-profiler validate --counter-disturbance`; where it reports DISTURBED, use
   `sampled` or `hybrid` (what `auto` does) and treat GPU figures taken with
   frequent reads as **lower bounds**. With boundary-only counter reads the
   effect was no longer detectable on that host. Nothing here establishes
   accuracy against an external meter: see
   [docs/HARDWARE_VALIDATION_PROTOCOL.md](docs/HARDWARE_VALIDATION_PROTOCOL.md).
2. **Device-level only.** Concurrent work, other processes and background
   services on the same device are inside every number.
3. **Counter resolution.** The NVML counter updates every ~100–107 ms (it
   varied by run and driver on one GPU); characterise it per host with
   `agent-energy-profiler validate`.
4. **CPU/DRAM untested on real hardware.** RAPL handling (wraparound,
   multi-socket) is tested synthetically only.
5. **Asynchronous GPU work.** Synchronise (e.g. `torch.cuda.synchronize()`)
   before a span closes; work in another process (vLLM, an inference server)
   is bounded by the request, not its kernels.
6. **No multiprocessing attribution.** One `Session` per process; nothing
   separates device energy between processes.
7. **No idle subtraction.** Idle draw is part of every figure.
8. **Exported traces are not validation.** OpenTelemetry attributes carry
   the profiler's values and provenance unchanged. They say
   `aep.energy.external_meter_validated = false`, and they reach a backend
   only after the run is attributed and exported (no live energy).

## Test

```bash
.venv/bin/pip install -e '.[test]'
.venv/bin/python -m pytest
# opt-in: re-attribute complete frozen research runs (the research run data
# is not distributed with this repository)
AEP_FROZEN_RUNS_DIR=/path/to/runs .venv/bin/python -m pytest -m frozen_runs
# opt-in: read the real NVML counter through Session
AEP_REAL_GPU=1 .venv/bin/python -m pytest tests/test_session.py -k real_nvml
# testing an installed wheel from a copy of tests/
AEP_TEST_INSTALLED=1 python -m pytest tests
```

Run the suite with `TMPDIR` outside any git working tree: one provenance test
checks that a temporary directory is *not* a repository.

## Project

- Public API and compatibility policy: [docs/API.md](docs/API.md),
  [CHANGELOG.md](CHANGELOG.md).
- Contributing: [CONTRIBUTING.md](CONTRIBUTING.md). Security reports:
  [SECURITY.md](SECURITY.md) (private vulnerability reporting). Licence: MIT
  ([LICENSE](LICENSE)).
- Raw diagnostic data behind the counter-disturbance finding:
  [results/](results/README.md). Release process:
  [docs/RELEASING.md](docs/RELEASING.md).

## Repository hygiene

- No AI tool appears as a Git author or co-author. Enable the commit-message
  guard once per clone: `git config core.hooksPath scripts/git-hooks`.
- `tests/golden/` (v1) and `tests/golden_v2/` (v2) hold reference artifacts.
  Never regenerate them to make a failing test pass. A deliberate semantic
  change is documented in `docs/PROVENANCE.md`.
