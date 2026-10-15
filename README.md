# agent-energy-profiler

Hardware-grounded, operation-level energy profiling for agentic AI systems.

A host application labels spans of work (`llm:plan`, `tool:search`, …). The
profiler records them on a semantic timeline. A separate sampler records
hardware energy counters (NVIDIA NVML, Linux RAPL), and an attribution step
joins the two. The profiler never infers what an operation is: the label comes
from the caller that knows why the work is happening.

> **Status: 0.2.0.** Extracted from a PhD research codebase (see
> [docs/PROVENANCE.md](docs/PROVENANCE.md)). Milestone 1 fixed measurement
> defects; Milestone 2 added schema v2: the `Session` API, nested spans,
> explicit energy provenance and resolution awareness. Schema v1 and its
> functional API are unchanged, and the golden tests show the research runs
> still attribute byte for byte as before. The API may still change.

## Install

```bash
python -m venv .venv
.venv/bin/pip install -e '.[dev]'          # core has no runtime dependencies
.venv/bin/pip install -e '.[gpu,plot]'     # NVML GPU counters, figures
```

To reproduce the exact environment used for baseline equivalence:

```bash
.venv/bin/pip install -e '.[dev,plot,gpu]' pandas -c constraints/baseline.txt
```

## Use: the Session API (schema v2)

```python
from agent_energy_profiler import Session, Resolution
from agent_energy_profiler.sampling import Sampler

resolution = Resolution.from_hardware_report("hw.json")   # from `aep-validate --json hw.json`

with Session("spans.jsonl",
             resolution=resolution,                        # unknown if omitted
             sampler=Sampler("power.csv", hz=10),          # optional: CPU/DRAM timeline
             attributes={"run_id": "run-1", "model": "gemma-3-4b"}) as session:

    with session.span("agent:step", attributes={"question_id": "q-17"}):
        with session.span("llm:plan") as s:                # child of agent:step
            response = client.generate(prompt)
            s.set_tokens(input=response.usage.prompt_tokens,
                         output=response.usage.completion_tokens)

        @session.trace("tool:search")                      # label is required
        async def search(query): ...
```

```bash
aep-attribute-v2 --spans spans.jsonl --power power.csv --out-dir out/
# -> out/spans_attributed.jsonl, out/session_summary.json
```

- **Explicit sessions.** There is no global default session. Each span belongs
  to the session that created it and writes to that session's file. A file
  that already exists is refused unless `overwrite=True`.
- **Nesting.** Parents come from the current task or thread context
  (contextvars). asyncio tasks inherit the span that created them; a new
  thread starts without a parent unless you pass `parent=handle`.
- **Lifecycle.** `with Session(...)` (or `async with`) starts GPU reading and
  the optional sampler process, and always closes both, also on exceptions.
  The sampler starts before the first span and takes one more sample after the
  last, so the timeline brackets every span.
- **Status.** `ok`, `error` (an exception), `cancelled` (`KeyboardInterrupt`,
  asyncio cancellation) or one you set (`s.set_status("timeout")`). The
  workload's exceptions always propagate unchanged.
- **GPU.** `gpu="auto"` reads NVML if present, `gpu=False` disables it, or pass
  a reader (`measurement.CallableReader`). Without a GPU every span records GPU
  energy as `unavailable` (`no_counter` / `disabled`), never 0.

Field definitions: [docs/SCHEMA_V2.md](docs/SCHEMA_V2.md).

## Measured, below resolution, missing, estimated

| | v2 field | Means |
|---|---|---|
| Measured | `energy.<domain>.energy_j`, `status: "measured"` | a hardware counter difference over the span's window |
| Below resolution | same, plus `below_resolution: true` | still the counter difference, unchanged; the span was shorter than the counter's characterised update interval, so it is quantised (often exactly 0 J) and biased low |
| Missing | `energy_j: null`, `status: "unavailable"` / `"invalid"`, `reason` | no defensible counter difference; never 0 |
| Estimated | `estimates.<domain>` only | integrated sampled power; never in `energy`, never in `total` |

A measured `0.0` is not "no energy was used". `below_resolution` is `null`
when no update interval was characterised for the host, and
`update_intervals` (duration ÷ interval) is recorded so analysis can pick its
own cut-off: the median interval is not a guaranteed minimum measurable
duration.

## Nested spans and concurrency

- **Inclusive** energy is the device's energy over a span's window.
  **Exclusive** energy (`accounting.exclusive`) is inclusive minus the direct
  children's, reported only when the span is `sole` (nothing outside its own
  subtree overlapped it), its children ran one after another inside it, and
  all of them were measured. Otherwise it is `not_defensible` or `unavailable`,
  with a reason.
- Summary totals add **root** spans only, so a parent and its child are never
  counted twice. If root spans overlap (concurrent tasks, threads), there is no
  span-based total; the independent whole-window counter reading in
  `session_summary.json` is the total.
- Concurrent spans are marked `shared`: their inclusive energy is reported, but
  it includes the other work. Energy is never split between concurrent spans.
- Two `Session`s active at once in one process record independently, but their
  spans are marked `other_sessions_active`: the device cannot tell their work
  apart.
- **Multiprocessing is not supported** for attribution across processes.
  Each process needs its own `Session` and file; a forked child refuses to
  write through the parent's session. Nothing links or separates device energy
  across processes.

## Legacy functional API (schema v1)

Unchanged, and still the writer used for the existing research runs:

```python
from agent_energy_profiler import profiler

profiler.configure(events_file="events.jsonl", run_id="run-1")
profiler.set_question("task-17")            # trajectory grouping key (schema v1 name)

with profiler.span("llm:plan") as s:
    response = client.generate(prompt)
    s["input_tokens"] = response.usage.prompt_tokens
    s["output_tokens"] = response.usage.completion_tokens
```

```bash
aep-validate                                  # what can this host measure?
aep-sample --out power.csv --hz 10 &          # hardware timeline + power.csv.meta.json (Ctrl-C / SIGTERM to stop)
python my_agent.py                            # writes events.jsonl
aep-attribute --events events.jsonl --power power.csv --out energy_summary.csv
aep-aggregate --help                          # long-format rollups
aep-visualize --help                          # standard figures (needs [plot])
```

Outputs: `events_attributed.jsonl`, `energy_summary.csv` and
`trajectory_summary.{csv,json}`. The v1 recorder keeps process-global state
(see PROVENANCE, limitation 9); use `Session` for threads, asyncio or several
experiments in one process.

## Migrating from v1 to v2

| v1 | v2 |
|---|---|
| `profiler.configure(events_file=..., run_id=...)` | `Session(path, attributes={"run_id": ...})` |
| `profiler.span("llm:plan") as s; s["input_tokens"] = n` | `session.span("llm:plan") as s; s.set_tokens(input=n)` (`s["input_tokens"] = n` also works) |
| `profiler.set_question(q)`, `begin_iteration()` | `attributes={"question_id": q, "iteration": i}` on the span, or a parent span per question |
| `meta=...` / `attributes=` | `attributes=` (`s["meta"] = {...}` merges into attributes) |
| `aep-attribute` | `aep-attribute-v2` |
| `gpu_energy_j` with `gpu_energy_source="power_integration"` | `estimates.gpu`; `energy.gpu` is `unavailable` |
| `measured_energy_j` | `total.energy_j` (null unless every term is measured) |

- v1 files stay readable: `schema_v2.read_records()` and `aep-attribute-v2`
  accept a v1 `events.jsonl` and upgrade each event on read (the file is not
  changed). A v1 file has no span hierarchy, so overlapping events come out
  `shared`.
- v2 files are not readable by the v1 tools (`aep-attribute`, `aggregate`,
  `visualize`, `trajectory`).
- For the same window, v2 CPU-package, DRAM and in-band GPU counter values are
  identical to v1's (tested against the golden fixture). The deliberate
  differences: estimates leave the measured fields, unusable windows carry a
  reason, and a decreasing GPU counter in the timeline invalidates the window
  instead of producing a negative or understated value.

## Measurement contract

```
measured_energy_j = gpu_energy_j + cpu_package_energy_j + dram_energy_j     (v1)
total.energy_j    = gpu + cpu_package + dram, all measured                    (v2)
```

- RAPL `core`/`uncore` are diagnostic and never added. `psys` is excluded.
- A domain the hardware did not report stays `null`, never `0`. A total with a
  missing term is `null`.
- Energy is **device-level**. NVML measures the whole GPU and RAPL the whole
  package. Per-span energy is time-window attribution and is most defensible on
  an otherwise idle machine running non-concurrent work. It is not
  process-level energy.
- The NVML counter updates at a host-specific interval, measured by
  `aep-validate` (median ~106 ms on the validation host). Spans shorter than
  that read 0 J or one whole quantum: quantisation, not measurement.

The full list is in [docs/PROVENANCE.md](docs/PROVENANCE.md#known-measurement-limitations).

## Test

```bash
.venv/bin/python -m pytest
# opt-in: re-attribute complete frozen research runs
AEP_FROZEN_RUNS_DIR=/path/to/runs .venv/bin/python -m pytest -m frozen_runs
# opt-in: read the real NVML counter through Session
AEP_REAL_GPU=1 .venv/bin/python -m pytest tests/test_session.py -k real_nvml
```

## Repository hygiene

- No AI tool appears as a Git author or co-author. Enable the commit-message
  guard once per clone: `git config core.hooksPath scripts/git-hooks`.
- `tests/golden/` holds the reference artifacts. Never regenerate them to make
  a failing test pass. A deliberate semantic change is documented in
  `docs/PROVENANCE.md`.
