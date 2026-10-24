# Walkthrough: profiling an agent

This walkthrough profiles a small question-answering agent from installation
to a report. It shows which numbers are measured and which are estimated.
The agent answers six fixed questions against a built-in corpus:

```
agent:question          one trace per question
  retrieval:search      BM25 over ten built-in sentences (pure Python)
  llm:reason            a deterministic stand-in "reasoner" (pure Python)
  verify:check          is the answer supported by the evidence?
  (one retry with a wider search if verification fails)
```

The inputs are fixed and the reasoner is deterministic, so every run gives
the same answers (5 of 6 correct) and the same span tree. Energy values
differ from run to run, as any measurement does. The example needs no API key,
model download, network access or GPU. To profile a real agent, wrap your own
LLM calls, tool calls and checks in the same way.

## 1. Install

```bash
git clone https://github.com/VeiledTee/agent-energy-profiler
cd agent-energy-profiler
python3 -m venv .venv && . .venv/bin/activate
pip install '.[gpu]'          # [gpu] adds NVIDIA counters; plain `pip install .` works without a GPU
```

Linux (or WSL2) and Python 3.9 or later are required. The clone is for the
example's files; the package alone installs from PyPI with
`pip install "agent-energy-profiler[gpu]"`.

## 2. Instrument the agent

All instrumentation lives in [agent.py](agent.py), simplified here. The
profiler records only what the program marks, under the labels the program
chooses:

```python
from agent_energy_profiler import Session

with Session.profile("agent_workflow") as session:
    with session.span("agent:question", attributes={"question": question}) as q:
        with session.span("retrieval:search", attributes={"k": k}):
            evidence = index.search(question, k)
        with session.span("llm:reason") as r:
            result = reason(question, evidence, work)
            r.set_tokens(input=n_in, output=n_out)
        with session.span("verify:check") as v:
            v.set_attribute("verified", ok)
        q.set_attribute("answer", result)
```

`Session.profile` works both ways. Run on its own
(`python examples/agent_workflow/agent.py --work 100000`), it starts the
hardware sampler, and when the session closes it attributes the spans,
prints a brief summary to stderr and saves everything, including the full
report (`report.txt`), in
`agent-energy-profiler-runs/agent_workflow-<timestamp>/` (or `--out-dir`).
Under `agent-energy-profiler run`, it records into `run`'s directory with
`run`'s sampler and measurement mode, and `run` prints the report.

## 3. Run it under the profiler

```bash
# optional, once per machine (~40 s): what can this host measure, and do
# frequent counter reads disturb the GPU counter here?
agent-energy-profiler validate --counter-disturbance --json hw.json

agent-energy-profiler run --out-dir runs/agent -- \
    python examples/agent_workflow/agent.py --work 100000
```

Add `--hardware-report hw.json` to `run` when you have the report. `--work`
sets how long each reasoning step takes. With 100000, a step takes ~270 ms on
the development machine, which is longer than the GPU counter's ~100 ms
update interval. The default (3000) makes steps too short to resolve.

`run` starts the hardware sampler (10 Hz), runs the program, attributes the
spans and prints the report. `runs/agent/` then holds:

| File | Contents |
|---|---|
| `spans.jsonl` | the spans as recorded (schema v2) |
| `power.csv`, `power.csv.meta.json` | the sampler's hardware timeline |
| `spans_attributed.jsonl` | spans with energy, estimates and accounting |
| `run.json` | whole-program energy and how the measurement mode was chosen |

## 4. Generate a report

```bash
agent-energy-profiler report runs/agent --brief               # one screen: operations ranked by energy where comparable
agent-energy-profiler report runs/agent                       # the full text report, as printed by run
agent-energy-profiler report runs/agent --format json         # every number, machine-readable
agent-energy-profiler export runs/agent --out-dir runs/agent/export   # spans.csv, operations.csv, report.json
```

From Python, after a standalone run, `session.report` is the same report
dictionary (`report.render_text(session.report)` prints it) and
`session.out_dir` is the run directory.

## 5. Read measured and estimated energy

One run on the development machine: RTX 4090, driver 610.88, WSL2, no RAPL.
It used the default mode with no hardware report. The report is abridged:

```
measurement  mode sampled (requested auto, aep_run); per-span GPU counter reads off
             per-span GPU energy: estimated only: integrated averaged power (estimates.gpu)
             whole-run GPU energy: measured: sampler counter at its first and final row

MEASURED ENERGY (hardware counters; basis: root spans)
  gpu                            n/a    6 of 6 top-level spans lack a measured value (not_read_by_mode x6)
  cpu_package                    n/a    6 of 6 top-level spans lack a measured value (no_counter x6)
ESTIMATED ENERGY (spans with no GPU measurement; never added above)
  gpu (power integration)    45.724 J over 6 of 6 top-level spans
RUN COUNTER MEASUREMENT (sampler's GPU counter, its own first to last reading)
  gpu 52.161 J over 1.91 s, 2 readings; brackets all spans: True

BY OPERATION (energy: exclusive; top 4 by total time)
  operation                       n     total       p50       p95       gpu J   cpu_pkg J  est. gpu J
  agent:question                  6    1.63 s  270.3 ms  327.1 ms         n/a         n/a      45.724
  llm:reason                      6    1.63 s  270.2 ms  327.0 ms         n/a         n/a      45.699
  retrieval:search                6    204 us     34 us     42 us         n/a         n/a       0.006
  verify:check                    6     15 us      2 us      3 us         n/a         n/a       0.000
```

(`aep_run` is the recorded value meaning "mode chosen by
`agent-energy-profiler run`". It predates the command's current name, and
recorded values never change.)

How to read it:

1. **The measurement is the run counter: 52.2 J over 1.91 s.** That is the
   GPU's own energy counter, read when the sampler started and when it
   stopped. It covers the whole sampler window, including interpreter
   start-up, and cannot be split between spans.
2. **Per-operation values are estimates.** The default mode (`auto`) does not
   read the GPU counter per span, because on this host frequent reads lower
   what the counter reports
   ([NVML_COUNTER_VALIDATION.md](../../docs/NVML_COUNTER_VALIDATION.md)).
   Without a hardware report proving the reads harmless, each span's GPU
   value is integrated 10 Hz averaged power: 45.7 J for the six questions,
   in its own column and section. The measured total stays `n/a` instead of
   absorbing the estimate. The two are different instruments, so do not
   subtract one from the other.
3. **The energy is the device's, not the agent's.** This agent runs entirely
   on the CPU. The ~45 J is mostly the idle draw of the RTX 4090 (~27 W)
   during 1.6 s, plus anything else using the GPU at the time. GPU energy is
   measured at device scope. A span's value says what the device used while
   the span was open, not what the span caused.
4. **Estimates are not exclusive.** Measured per-operation energy (`gpu J`,
   `cpu_pkg J`) is exclusive: a parent keeps only what its children do not
   cover. An estimate covers the span's whole window, so `est. gpu J` for
   `agent:question` (45.724 J) includes its children's, mostly
   `llm:reason` (45.699 J): do not add the two. Totals add only the six
   top-level spans. The brief summary (`report runs/agent --brief`) ranks
   estimates by leaf spans only and lists `agent:question` as `parent`.
5. **Missing is `n/a`, never 0.** CPU package and DRAM need RAPL, which WSL2
   does not expose, so every span says `no_counter`.

### The same run in `direct` mode

`--mode direct` reads the GPU counter at every span boundary, which makes
per-span values measurements:

```
MEASURED ENERGY (hardware counters; basis: root spans)
  gpu                         43.601 J
ESTIMATED ENERGY (spans with no GPU measurement; never added above)
  none needed: every top-level span has a measured GPU value
  (diagnostic: on 6 measured spans, integrated power gives 46.573 J vs counter 43.601 J)
RUN COUNTER MEASUREMENT (sampler's GPU counter, its own first to last reading)
  gpu 48.277 J over 1.91 s, 2 readings; brackets all spans: True
WARNINGS
  [COUNTER_DISTURBANCE_UNKNOWN] The GPU energy counter was read often (per span and/or by the
  sampler) and counter disturbance on this host is UNKNOWN. ...
```

The report warns because nothing showed those reads to be harmless. On this
host they are not. In this pair of runs the run counter read 48.3 J with
per-span reads against 52.2 J and 52.6 J in two runs without them. That gap
is consistent with the documented disturbance, but one run is not a
measurement of it. Use `direct` only where
`agent-energy-profiler validate --counter-disturbance` reports
`NOT_DETECTED`. `auto` then selects it for you, on that host, driver and GPU.

### Concurrency

```bash
agent-energy-profiler run --out-dir runs/agent-par -- \
    python examples/agent_workflow/agent.py --work 100000 --parallel
```

The questions now run concurrently, so their spans overlap. The device
cannot tell them apart. The report marks them `shared`
(`[SHARED_ATTRIBUTION] 23 spans overlapped other recorded work`) and gives
them no exclusive energy.

### Without an NVIDIA GPU

The same commands work. Every GPU value is `n/a` with its reason. There are
no estimates (no power samples) and no run counter. The report says so
rather than showing zeros. Timings, the span tree and the attributes are
still recorded.

## What this example does not show

- **Accuracy.** No value here has been compared with an external power
  meter. The numbers are what the hardware counters report.
- **CPU and DRAM energy.** RAPL handling is tested only with synthetic
  counters; it has not yet been validated on physical hardware.
- **Other GPUs.** The disturbance behaviour was demonstrated on one GPU,
  driver and operating system. Run `validate --counter-disturbance` on yours.
