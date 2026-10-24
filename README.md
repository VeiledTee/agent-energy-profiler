# Agent Energy Profiler

[![CI](https://github.com/VeiledTee/agent-energy-profiler/actions/workflows/ci.yml/badge.svg)](https://github.com/VeiledTee/agent-energy-profiler/actions/workflows/ci.yml)
![Python 3.9–3.14](https://img.shields.io/badge/python-3.9%E2%80%933.14-blue)
![License: MIT](https://img.shields.io/badge/license-MIT-green)

**See where the energy goes in a multi-step AI application.**

Agent Energy Profiler is a Python toolkit for operation-level energy
profiling. You mark the steps of your program, such as retrieval, model
calls, tool calls and verification, as named spans. The profiler reads the
hardware energy counters while the program runs and reports, for every
step:

- how long it took;
- how much energy the GPU, CPU package and DRAM used while it ran, where the
  hardware exposes that;
- whether each number is a **measurement**, an **estimate** or **missing**,
  and why.

It is built for agents, pipelines and other programs where one request
fans out into many operations, and it works with any Python code. The core
has no dependencies.

**Name the steps of your code, then see the energy of each one:**

```python
from agent_energy_profiler import Session

with Session.profile("rag-demo") as session:
    with session.span("agent:answer"):            # labels are your own names
        with session.span("retrieval:search"):
            docs = search(question)                # any Python code
        with session.span("llm:generate"):
            answer = generate(question, docs)
```

```bash
agent-energy-profiler report agent-energy-profiler-runs/rag-demo-<timestamp> --html report.html
```

Nested spans become a hierarchy. The HTML report is one offline file: a
workflow explorer of your operations, with time, energy and how each number
was obtained.

![Workflow explorer: agent:answer with its children llm:generate,
retrieval:search and verify:check, each with time, estimated GPU energy and
share](docs/images/workflow-explorer.png)

*A real report of the [quick start](#quick-start) below (NVIDIA RTX 4090,
WSL2, default mode). The workload is CPU-only, so its GPU values, estimates
marked `~`, mainly reflect the GPU's idle draw while each step ran.*

> **Status:** beta (0.4.0). Install from GitHub; not on PyPI yet. The
> numbers are whole-device hardware readings, not per-process energy, and
> their accuracy has not been checked against an external meter. See
> [Measurement limitations](#measurement-limitations) before relying on them.

## Quick start

You need Linux (or WSL2) and Python 3.9 or later. No GPU, API key or model
download is needed for this example. (On Debian and Ubuntu, `python3 -m
venv` needs the `python3-venv` package.)

**1. Install** (required):

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install "agent-energy-profiler @ git+https://github.com/VeiledTee/agent-energy-profiler"
```

With an NVIDIA GPU (optional), install the `gpu` extra instead, so that GPU
energy can be read:

```bash
pip install "agent-energy-profiler[gpu] @ git+https://github.com/VeiledTee/agent-energy-profiler"
```

**2. Save this as `quickstart.py`.** It is a three-step pipeline in pure
Python. `busy()` stands in for real work; in your own code you would wrap
your retrieval, model and tool calls the same way.

```python
"""Quick start: profile a small three-step pipeline.

Pure Python: no GPU, API key, model or network needed. `busy()` stands in for
real work; replace it with your own retrieval, model and tool calls.

    python quickstart.py

The report is printed to stderr and saved, with the raw data, in
agent-energy-profiler-runs/quickstart-<timestamp>/.
"""

import time

from agent_energy_profiler import Session


def busy(seconds):
    """Stand-in for real work: keep the CPU busy for `seconds`."""
    end = time.perf_counter() + seconds
    while time.perf_counter() < end:
        pass


with Session.profile("quickstart") as session:
    for question in ("q1", "q2", "q3"):
        with session.span("agent:answer", attributes={"question": question}):
            with session.span("retrieval:search"):
                busy(0.05)
            with session.span("llm:generate") as span:
                busy(0.30)
                span.set_tokens(input=120, output=40)
            with session.span("verify:check"):
                busy(0.02)
```

The same file is in the repository as
[examples/quickstart.py](examples/quickstart.py).

**3. Run it:**

```bash
python quickstart.py
```

`Session.profile` starts a hardware sampler, records your spans, and when
the `with` block ends attributes energy to them and prints a brief summary
to stderr (your program's stdout is left alone): the operations ranked by
energy where their values are comparable, what was measured, and what is
missing. Everything is saved in a new directory,
`agent-energy-profiler-runs/quickstart-<timestamp>/`, named on the first
line of output, including the full report (`report.txt`). Nothing is ever
overwritten.

**4. Look at the results again, or export them:**

```bash
RUN=$(ls -d agent-energy-profiler-runs/quickstart-* | tail -n 1)   # the newest run
agent-energy-profiler report "$RUN" --brief               # the summary printed at the end
agent-energy-profiler report "$RUN"                       # the full report, every diagnostic
agent-energy-profiler report "$RUN" --html report.html    # the workflow explorer, offline
agent-energy-profiler report "$RUN" --format json         # every number, machine-readable
agent-energy-profiler export "$RUN" --out-dir "$RUN/export"
#   -> spans.csv (one row per span), operations.csv, report.json
```

## Example output

The quick start above, run once on the development machine (NVIDIA RTX 4090,
driver 610.88, WSL2, `[gpu]` extra, no hardware report). This is the whole
summary printed to stderr; your numbers will differ with your hardware and
its load:

```
agent-energy-profiler: profiling into agent-energy-profiler-runs/quickstart-20261023-191430 (measurement mode sampled)

ENERGY SUMMARY (brief)
  run        1.11 s (first span start to last span end), 12 spans, 4 operations
  mode       sampled (requested auto, default)
  whole run  GPU          47.926 J measured (sampler counter, 1.31 s)
             CPU package  n/a, not measured (no_counter)
             DRAM         n/a, not measured (no_counter)

Operations ranked by ESTIMATED GPU energy (~): 10 Hz sampled power integrated over each span,
  not a counter measurement; leaf spans only, so nothing is counted twice
    #  operation                    n     total       p50       p95    GPU J est.   share  notes
    1  llm:generate                 3  900.0 ms  300.0 ms  300.0 ms       ~33.549   81.0%
    2  retrieval:search             3  150.0 ms   50.0 ms   50.0 ms        ~5.618   13.6%  short
    3  verify:check                 3   60.0 ms   20.0 ms   20.0 ms        ~2.228    5.4%  short
    -  agent:answer                 3    1.11 s  370.2 ms  370.2 ms           n/a          parent
  share: of the ranked operations' GPU J only. ~ marks an estimate.
  parent: has child spans; an estimate covers the children too, so it is not ranked (their own
    rows are)
  short: spans shorter than the 100.0 ms power sampling interval; their estimates are
    interpolated, not observed

Coverage per span
  GPU:         0 of 12 measured, 12 estimated (not measured: not_read_by_mode x12)
  CPU package: 0 of 12 measured (not measured: no_counter x12)
Device-level: energy covers every process on each device and is assigned to
  operations by time window. A large share shows where energy was used, not that
  an operation is inefficient: judge that with its output, throughput and workload.
Full report: agent-energy-profiler report agent-energy-profiler-runs/quickstart-20261023-191430
  its warnings: ESTIMATES_PRESENT, DOMAIN_UNAVAILABLE, SPAN_ESTIMATES_ONLY
```

How to read it:

- **Measured vs estimated.** A *measurement* is the difference of a
  hardware energy counter between two readings. An *estimate* is sampled
  average power integrated over a span's time window. In the default mode,
  the measured GPU figure is the whole-run counter (47.9 J) and each
  operation's GPU energy is an estimate, marked `~`. The two are different
  instruments: they are never added together or compared as one ranking.
  [Choosing a measurement mode](#choosing-a-measurement-mode) explains when
  per-span values are measured instead.
- **Per operation.** `llm:generate` is ranked first: about 81% of the
  ranked estimates. `agent:answer` is the parent of the other three. Its
  estimate would include theirs, so it is listed (`parent`) but not ranked.
- **Device-level.** This pipeline runs entirely on the CPU, so the GPU
  figures are mostly the GPU's idle draw while each step ran. That is why
  the ranking follows the time column here. A span's value is what the
  device used while the span was open, not what the span caused.
- **Missing is `n/a`, never 0.** WSL2 exposes no CPU (RAPL) counters, so
  CPU package energy is `n/a` with its reason.

[Finding where the energy goes](#finding-where-the-energy-goes) explains
each part of the summary and its limits. The screenshot at the top is the
HTML report of this same run.

**Without an NVIDIA GPU** (or without the `gpu` extra) the same commands
work. Timings, the span tree and attributes are recorded, and every energy
value is `n/a` with its reason (`no_counter`) instead of a zero. The
profiler says so before your code runs:

```
agent-energy-profiler: no GPU energy counter, so GPU values will be n/a. For an NVIDIA GPU, install the gpu extra: pip install 'agent-energy-profiler[gpu]'.
```

On Linux hosts with readable RAPL counters, CPU package and DRAM energy are
measured per span as well.

## Finding where the energy goes

The brief summary answers one question: while which operations was the
energy used? Read it top to bottom:

1. **The basis line** says what the ranking uses: measured GPU energy (a
   counter difference), *estimated* GPU energy (integrated sampled power,
   marked `~`) or measured CPU package energy. One basis per table:
   measured and estimated values are never ranked together.
2. **Ranked rows** are where to look first. `share` is each operation's
   part of the ranked operations' energy, not of the whole run.
3. **Unranked rows (`-`)** have no comparable value. The note says why:
   `parent`, `shared`, `partial`, `none` or `quantised`. They are listed,
   never shown as 0 J.
4. **Whole run** is measured device energy for the session. It includes
   time no span covers, and it is never split between operations.
5. **The last lines** say how many spans have values and which warnings the
   full report raises.

**Energy is not latency.** A span's energy is the device's energy during
its time window. A slow step that leaves the GPU idle still collects the
GPU's idle draw, and a short step at full load can outrank a long one. When
the device is mostly idle (as in the CPU-only quick start), the energy
ranking follows the time ranking because idle draw is roughly constant.
That is what the hardware reported, not a measure of the step's own work.
Use the time columns for latency and the energy column for energy.

**Exclusive and inclusive.** A measured value in the ranking is
*exclusive*: a parent span keeps only what its children do not cover, so
the ranked values add up without counting anything twice. An estimate has
no exclusive form. It covers the span's whole window, children included.
So only leaf spans are ranked by estimate, and a parent is listed as
`parent`. The full report's `est. gpu J` column and its hierarchy view show
the inclusive values.

**Overlapping work.** Spans that ran at the same time as other recorded
spans are `shared`: device energy cannot be divided between concurrent
operations, so they are not ranked. If everything overlapped, the summary
lists operations by time and says why. To compare operations by energy,
profile them running one at a time.

**Comparing runs.** Values are comparable only on the same basis, mode,
device, driver and machine load. A measured value and an estimate are
different quantities. Each report records its mode (`mode` line) and
hardware. Repeat a run before trusting a small difference: run-to-run
variation is not reported.

**What a ranking cannot tell you.** The operation with the most energy
is where energy was used, not necessarily the inefficient one. Whether it
is worth its energy depends on what it produced: its output, its
throughput (for example tokens per second, which you can record with
`set_tokens`), and the workload. The profiler does not suggest
optimisations.

**Full diagnostics.** `agent-energy-profiler report RUN` prints the full
report: measured and estimated totals, the independent window and run
counter measurements, per-domain coverage, the hierarchy and every warning
explained. It is also saved as `RUN/report.txt` (and `report.json`). Use
`Session.profile(..., report="full")` to print it at close instead of the
summary.

## What you can do with it

- **Profile named operations.** Spans nest, work across `async` code and
  threads, and carry attributes and token counts. Labels always come from
  your code; the profiler never guesses them from function names, prompts
  or the call stack.
- **Find the expensive steps.** Per-operation latency (total, p50, p95) and
  exclusive energy, plus a hierarchy view by label path.
- **Read GPU, CPU package and DRAM energy** from NVIDIA NVML and Linux RAPL,
  where available.
- **Know what each number is.** Every value is measured, estimated,
  flagged below the counter's resolution, or missing with a reason. Totals
  are withheld rather than computed from partial data.
- **Measure whole programs.** `agent-energy-profiler run` records
  whole-program energy in `run.json`, even for a program with no spans.
- **Export.** Text, JSON and CSV reports; per-span CSV; OpenTelemetry spans
  with energy attributes (OTLP/HTTP, JSON lines or console).
- **Characterise the machine.** `agent-energy-profiler validate` reports
  what the host can measure and the GPU counter's update interval, and can
  test whether reading the counter disturbs it.
- **Reproduce and compare runs.** Each run records the measurement mode
  and why it was chosen, the hardware, the driver and the sampler's
  settings.
- **Convert to emissions yourself.** `carbon.convert(joules, intensity)`
  with a grid intensity you supply.

## Using it in your code

### Two ways to run

`Session.profile(name)` is the recommended entry point. The same script
also runs under `agent-energy-profiler run` without changes:

| | `python app.py` | `agent-energy-profiler run -- python app.py` |
|---|---|---|
| Hardware sampler | started and stopped by `Session.profile` | started by `run`; `Session.profile` starts no second one |
| Output | `agent-energy-profiler-runs/<name>-<timestamp>/` | `--out-dir`, or `agent-energy-profiler-runs/<timestamp>/` |
| Report | when the `with` block ends: a brief summary printed to stderr; the full report saved as `report.json` and `report.txt` | the full report printed by `run` at the end |
| Whole-program energy (`run.json`) | no | yes, including interpreter start-up |

Use `run` when you start the program from a shell and also want
whole-program energy. Use `python app.py` (or a notebook, a test, a server
started from code) otherwise.

### Getting the results in code

```python
from agent_energy_profiler import Session

with Session.profile("rag-eval") as session:
    with session.span("retrieval:search"):
        ...                                        # your code
session.out_dir          # the run directory
session.report           # the report as a dict (report.json); None if it could not be built
session.profile_status   # "complete"; "incomplete" (see session.profile_error); "no_spans"
```

| Option | Use it when |
|---|---|
| `report="full"` | the full report should be printed instead of the brief summary (both are always saved: `report.txt`, `report.json`) |
| `report=False` | you attribute later or elsewhere (`analyze(session)`, `agent-energy-profiler attribute`): batch jobs, services, CI that collects raw files |
| `strict=True` | CI and research runs: closing raises if the sampler or attribution failed, unless your code itself raised |
| `quiet=True` | nothing should be printed |
| `mode=...`, `hardware_report="hw.json"` | see [Choosing a measurement mode](#choosing-a-measurement-mode) |
| `out_dir="runs/x"` | a fixed directory; it is refused if it already holds a run or a report |

**Close the session.** Use `with` (or `async with`). In a notebook, call
`session = Session.profile("nb").start()` in one cell and
`session.close()` in the last one; the report appears at `close()`. A
session that is never closed is not attributed. Its sampler stops by itself
when the Python process exits.

If attribution fails, your program's own exception (if any) is never
hidden: the raw files stay in the run directory, `profile_status` is
`"incomplete"`, and the message names the command that retries it.

**Lower-level API.** `Session(path, sampler=Sampler(...))` with
`analyze(session)` gives full control over paths and the sampler
([docs/API.md](docs/API.md)). `Session.from_env(path)` is for programs
written only for `run`; on its own it starts no sampler, so in the default
mode it records timings but no energy.

### Instrumenting

| Need | Code |
|---|---|
| a block | `with session.span("tool:search", attributes={...}) as s:` |
| async code | `async with session.span("llm:plan"):` (tasks created inside inherit the parent) |
| a function | `@session.trace("tool:search")` on a sync or async function; the label is required |
| tokens, attributes, status | `s.set_tokens(input=812, output=64)`, `s.set_attribute(k, v)`, `s.set_status("timeout")` |
| tag every span of a request | `with session.bind(request_id=rid):` (adds the attribute, opens no span) |
| work handed to a thread | `session.span(..., parent=handle)`; new threads start without a parent |

Labels are free text. A `kind:name` convention (`llm:plan`, `tool:search`,
`retrieval:bm25`) keeps reports easy to scan.

GPU work is often asynchronous: call `torch.cuda.synchronize()` (or your
framework's equivalent) before a span closes, or the span ends before its
kernels do. Work done in another process, such as an inference server,
appears in the span that waits for it.

### Frameworks and OpenTelemetry

- **LangGraph:** decorate node functions with `session.trace(label)`. Tested
  with LangGraph 1.2.15 for sync and async graphs with parallel branches
  ([docs/LANGGRAPH.md](docs/LANGGRAPH.md)).
- **OpenTelemetry** (`[otel]` extra): profiler spans join your application's
  current trace while the program runs (`Session(..., hooks=[otel.Correlation()])`).
  After attribution, `agent-energy-profiler otel-export` or
  `otel.export_spans(run_dir, exporter)` sends every span with its energy
  as `aep.*` attributes. Measured, estimated and missing values stay
  distinguishable. Your global OpenTelemetry configuration is never
  changed ([docs/OPENTELEMETRY.md](docs/OPENTELEMETRY.md)).

## Choosing a measurement mode

How often the GPU energy counter is read is a setting, because on at least
one machine every read lowered what the counter reported
([docs/NVML_COUNTER_VALIDATION.md](docs/NVML_COUNTER_VALIDATION.md)).

| Mode | Per-span GPU energy | Measured GPU energy |
|---|---|---|
| `auto` (default) | `direct` if a disturbance test found no effect on this host, driver and GPU; otherwise `sampled` | as the chosen mode |
| `sampled` | estimated from averaged power | the whole run |
| `hybrid` | estimated from averaged power | the whole run, plus a measured counter series once per second to check the estimates |
| `direct` | measured: counter read at each span's start and end | each span, and the whole run |

- **Keep the default** for most work. You get per-span estimates and a
  measured whole-run figure without risking a disturbed counter.
- **To get measured per-span GPU energy**, test the machine once and pass
  the report. `auto` then selects `direct` on that host, driver and GPU, if
  the test found no disturbance:

  ```bash
  agent-energy-profiler validate --counter-disturbance --json hw.json   # ~40 s, once per machine and driver
  agent-energy-profiler run --hardware-report hw.json --out-dir runs/next -- python app.py
  ```

  In Python: `Session.profile("next", hardware_report="hw.json")`.

- **Pass a mode explicitly** (`--mode`, or `mode=...` to `Session.profile`
  or `Session`) when results
  must be comparable across machines, since `auto` can choose differently
  on each. Code written for `Session` before 0.4.0 got `direct` by default;
  pass `mode="direct"` to keep that.

Every artifact and report records the mode, who chose it and why. Details:
[docs/MEASUREMENT_MODES.md](docs/MEASUREMENT_MODES.md).

## Installation and platform support

From GitHub (not on PyPI yet):

```bash
pip install "agent-energy-profiler @ git+https://github.com/VeiledTee/agent-energy-profiler"
```

Or from a clone: `pip install .`, or `pip install -e '.[test]'` to develop.
Optional extras, in either form (`"agent-energy-profiler[gpu,otel] @ git+..."`
or `'.[gpu,otel]'`):

| Extra | Adds | Needed for |
|---|---|---|
| `[gpu]` | `nvidia-ml-py` | NVIDIA GPU energy |
| `[otel]` | OpenTelemetry API and SDK | trace correlation, `otel-export` to the console or a file |
| `[otlp]` | `[otel]` + the OTLP/HTTP exporter | `otel-export --endpoint URL` |
| `[plot]` | matplotlib | figures from the legacy v1 pipeline |
| `[test]` | pytest and the optional libraries the suite covers | running the full test suite |

This installs the `agent-energy-profiler` command (also available as
`python -m agent_energy_profiler`) and the `agent_energy_profiler` package.

Python 3.9–3.14, tested in CI on every version.

| Host | GPU energy | CPU package / DRAM | Tested |
|---|---|---|---|
| Linux with an NVIDIA GPU (Volta or newer) | NVML counter | RAPL, if `/sys/class/powercap` (Intel or AMD) or `amd_energy` is readable | not yet on physical hardware; CI uses synthetic counters |
| WSL2 with an NVIDIA GPU | NVML counter | not available (WSL2 has no RAPL) | yes: RTX 4090, driver 610.88 (the development machine) |
| VMs, most containers, shared HPC nodes | often available | usually not available | no |
| Linux without an NVIDIA GPU | not available | as above | CI (GitHub-hosted Ubuntu runners) |
| macOS, native Windows | not supported | not supported | no |

Without any readable counter, everything except energy still works:
spans, timings, hierarchy, reports and exports. Run
`agent-energy-profiler validate` to see what your machine can measure.

## Troubleshooting

| Symptom | Cause | What to do |
|---|---|---|
| "no GPU energy counter"; GPU values `n/a` (`no_counter`) | the `gpu` extra is missing, there is no NVIDIA GPU, or the GPU has no energy counter (older than Volta) | install the `gpu` extra; `agent-energy-profiler validate` shows which |
| CPU package and DRAM `n/a` (`no_counter`) | RAPL is not exposed (WSL2, most VMs and containers) or not readable by your user; on many Linux systems the counters are readable only by root | `agent-energy-profiler validate` lists readable zones |
| Spans have timings but no energy, and no estimates | the program used `Session(...)` or `Session.from_env(...)` on its own, without a sampler | use `Session.profile(...)`, or run it under `agent-energy-profiler run` ([Two ways to run](#two-ways-to-run)) |
| "profile incomplete: ... failed" | the sampler or attribution failed after your code ran; the raw files are kept | run the `attribute` and `report` commands the message names |
| "spans fall outside the hardware timeline" | the sampler stopped early, or the system clock stepped during the run (seen a few times in several hundred short test runs under WSL2) | profile again; `agent-energy-profiler attribute ... --allow-uncovered` keeps the covered spans |
| Per-span GPU `n/a` with reason `not_read_by_mode`, and an `est. gpu J` column | expected in `sampled` mode | see [Choosing a measurement mode](#choosing-a-measurement-mode) |
| The brief summary says "Not ranked by energy" | no operation has a comparable value for all of its spans: they overlapped (`shared`), every value is below the counter's resolution, or nothing was measured or estimated | the reason is printed; see [Finding where the energy goes](#finding-where-the-energy-goes) |
| `RESOLUTION_UNKNOWN` warning | the GPU counter's update interval was not characterised | `agent-energy-profiler validate --json hw.json`, then `run --hardware-report hw.json` |
| `0 J` or `BELOW_RESOLUTION` on short spans | the span is shorter than the counter's update interval (~100 ms on the development GPU) | profile longer operations, or read these values as quantised |
| `run: ... already exists; use a new --out-dir`, or `FileExistsError` from `Session.profile(out_dir=...)` | a run is never overwritten | choose a new directory, or leave `out_dir` out for a generated one |
| `SUMMARY_MISSING` warning | `spans_attributed.jsonl` was copied without `session_summary.json` | report on the whole run directory |
| `otel-export` reports a missing module | the OpenTelemetry extras are not installed | install `[otel]`, or `[otlp]` for `--endpoint` |

## Measurement limitations

The profiler reports what the hardware counters report. In particular:

- **GPU energy is measured at device scope.** NVML reports the whole GPU,
  including idle draw, other processes and concurrent work. The same holds
  for RAPL and the CPU package. Spans that overlap in time are marked
  `shared` and their energy is not split between them.
- **Per-operation values may be estimated.** In the default mode, per-span
  GPU energy is integrated averaged power. Only the whole run is a counter
  measurement. Estimates are labelled and never added to measured totals.
- **Counter resolution.** The NVML counter updates about every 100 ms on
  the development GPU. Shorter spans get quantised values, which are
  flagged `below_resolution` when the interval is known
  (`agent-energy-profiler validate` measures it).
- **Counter disturbance has been demonstrated on one configuration.** On an
  RTX 4090 with driver 610.88 under WSL2, frequent reads of the NVML energy
  counter lowered what it reported (~10% at a 10 Hz sampler). Other GPUs and
  drivers are untested; `agent-energy-profiler validate --counter-disturbance`
  checks yours ([docs/NVML_COUNTER_VALIDATION.md](docs/NVML_COUNTER_VALIDATION.md)).
- **Accuracy has not been established against an independent meter.** No
  value has been compared with an external power meter
  ([docs/HARDWARE_VALIDATION_PROTOCOL.md](docs/HARDWARE_VALIDATION_PROTOCOL.md)
  describes how it would be).
- **RAPL (CPU package, DRAM) has not been validated on real hardware.** It is
  tested only with synthetic counters.
- **Not measured:** storage, network, fans, power-supply losses, facility
  overhead, and the energy of a single process on a shared device. Idle
  draw is not subtracted. One `Session` per process; energy is not split
  between processes.
- **Span overhead.** With per-span GPU reads (`direct`), each span adds
  about 2 ms of wall time on the development machine. Below ~20 ms per
  operation that is a large share of the work. Pass `gpu=False` (or
  `run --gpu off`) when you only need timings. `Session.profile` also
  runs the sampler as a separate process: on the development machine,
  starting it took about 50 ms and closing about 0.2 s, plus attribution.

The attribution logic is regression-tested against reference artifacts
from the research runs it was extracted from ([docs/PROVENANCE.md](docs/PROVENANCE.md)).
That shows the computation is reproduced exactly, not that the energy
values are correct.

## Command reference

| Command | Does |
|---|---|
| `agent-energy-profiler run [--mode M] [--hardware-report hw.json] [--out-dir D] -- CMD` | runs CMD with the sampler on, records whole-program energy in `run.json`, and attributes and reports the program's spans |
| `agent-energy-profiler report DIR_OR_FILE [--brief \| --format text\|json\|csv]` | the report of a run; `--brief` gives the one-screen summary |
| `agent-energy-profiler report DIR_OR_FILE --html report.html` | a self-contained, offline HTML report: workflow explorer, operation breakdown, provenance; attributes left out unless allowed, `--redact-labels` for labels that hold user input ([docs/HTML_REPORT.md](docs/HTML_REPORT.md)) |
| `agent-energy-profiler export DIR_OR_FILE --out-dir D` | `spans.csv`, `operations.csv`, `report.json` |
| `agent-energy-profiler validate [--json hw.json] [--counter-disturbance]` | what this host can measure, the GPU counter's update interval, and optionally the disturbance test (~30 s) |
| `agent-energy-profiler attribute FILE --power power.csv` | joins recorded spans with a hardware timeline (what `run` does for you) |
| `agent-energy-profiler otel-export DIR_OR_FILE [--endpoint URL \| --out FILE]` | spans as OpenTelemetry spans with energy attributes |
| `agent-energy-profiler sample --out power.csv [--hz N] [--counter-hz N]` | the hardware sampler on its own |

`agent-energy-profiler COMMAND --help` lists every option. Older
single-purpose scripts (`agent-energy-profiler-sample`, `-attribute`,
`-attribute-v2`, `-validate`, `-aggregate`, `-visualize`) are also
installed.

The short prefix `aep` remains only in recorded data and interfaces that
other code relies on: the `AEP_*` environment variables, the
OpenTelemetry attribute namespace `aep.*`, and the recorded value
`mode_source: "aep_run"` (a session configured by `agent-energy-profiler run`).

## Documentation

| Topic | Where |
|---|---|
| Full walkthrough: an agent from install to report, measured vs estimated, concurrency | [examples/agent_workflow](examples/agent_workflow/) |
| More examples: CPU-only work, GPU inference, an OpenTelemetry-traced service | [examples/](examples/) |
| Public Python API and compatibility policy | [docs/API.md](docs/API.md) |
| Measurement modes, defaults and migration | [docs/MEASUREMENT_MODES.md](docs/MEASUREMENT_MODES.md) |
| Span file format and every report field | [docs/SCHEMA_V2.md](docs/SCHEMA_V2.md) |
| HTML report: views, privacy, view-model contract | [docs/HTML_REPORT.md](docs/HTML_REPORT.md) |
| OpenTelemetry correlation and export | [docs/OPENTELEMETRY.md](docs/OPENTELEMETRY.md) |
| LangGraph | [docs/LANGGRAPH.md](docs/LANGGRAPH.md) |
| GPU counter disturbance study | [docs/NVML_COUNTER_VALIDATION.md](docs/NVML_COUNTER_VALIDATION.md), raw data in [results/](results/README.md) |
| How accuracy would be validated with an external meter | [docs/HARDWARE_VALIDATION_PROTOCOL.md](docs/HARDWARE_VALIDATION_PROTOCOL.md) |
| Origin, reference artifacts and change log of measurement semantics | [docs/PROVENANCE.md](docs/PROVENANCE.md) |
| Release notes | [CHANGELOG.md](CHANGELOG.md) |

### Reading the report

The brief summary is explained in
[Finding where the energy goes](#finding-where-the-energy-goes). The full
report (`agent-energy-profiler report RUN`, `report.txt`) has every
diagnostic:

| Kind | Where | Meaning |
|---|---|---|
| **Measured** | "MEASURED ENERGY", `energy.*` | a hardware counter difference over the span's window |
| **Below resolution** | measured, flagged `below_resolution` | the span was shorter than the counter's update interval; the reported energy is quantised and may not reliably represent the span's consumption |
| **Missing** | `n/a`, with a reason | no defensible counter difference: no counter, failed read, counter reset, outside the sampler's timeline |
| **Estimated** | "ESTIMATED ENERGY", `estimates.*` | integrated sampled power, never added to measured totals |

Totals add top-level spans only, and appear only when every contributing
span was measured; otherwise the report shows `n/a` beside the partial sum
and how many spans it covers. Warnings at the end of each report name what
limits the numbers above them.

### Legacy schema v1

Files written by the older functional API (`profiler.span`,
`profiler.configure`; `events.jsonl`) are still read by `report`, `export`
and `attribute`, and their attribution is unchanged. New code should use
`Session`. The v1 → v2 mapping is in
[docs/SCHEMA_V2.md](docs/SCHEMA_V2.md#reading-v1-as-v2).

## Contributing and development

```bash
git clone https://github.com/VeiledTee/agent-energy-profiler && cd agent-energy-profiler
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[test]'
python -m pytest          # run with TMPDIR outside any git working tree
```

[CONTRIBUTING.md](CONTRIBUTING.md) covers the opt-in hardware and
frozen-run tests, wheel checks and the rules for changes:
`tests/golden/` and `tests/golden_v2/` hold reference artifacts and are
never regenerated to make a test pass. No AI tool appears as a Git author
or co-author; enable the commit-message guard once per clone with
`git config core.hooksPath scripts/git-hooks`.

Security reports: [SECURITY.md](SECURITY.md) (private vulnerability
reporting). Licence: MIT ([LICENSE](LICENSE)).
