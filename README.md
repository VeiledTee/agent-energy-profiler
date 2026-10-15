# agent-energy-profiler

Hardware-grounded, operation-level energy profiling for agentic AI systems.

A host application labels spans of work (`llm:plan`, `tool:search`, …). The
profiler records them on a semantic timeline. A separate sampler records
hardware energy counters (NVIDIA NVML, Linux RAPL), and an attribution step
joins the two. The profiler never infers what an operation is: the label comes
from the caller that knows why the work is happening.

> **Status: 0.1.0, initial extraction.** This repository was extracted from a
> PhD research codebase. Its behaviour is intentionally identical to the
> source implementation, and golden tests prove it. See
> [docs/PROVENANCE.md](docs/PROVENANCE.md) for the source commit, what was
> copied and the known measurement limitations. The API will change in later
> milestones.

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

## Use

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
aep-sample --out power.csv --hz 10 &          # hardware timeline (Ctrl-C / SIGTERM to stop)
python my_agent.py                            # writes events.jsonl
aep-attribute --events events.jsonl --power power.csv --out energy_summary.csv
aep-aggregate --help                          # long-format rollups
aep-visualize --help                          # standard figures (needs [plot])
```

Outputs: `events_attributed.jsonl` (the canonical per-event artifact),
`energy_summary.csv`, and `trajectory_summary.{csv,json}` (per-trajectory
reconciliation of the summed event energy against a whole-window counter
reading).

## Measurement contract

```
measured_energy_j = gpu_energy_j + cpu_package_energy_j + dram_energy_j
```

- RAPL `core`/`uncore` are diagnostic and never added. `psys` is excluded.
- A domain the hardware did not report stays `null`, never `0`. A total with a
  missing term is `null`.
- Energy is **device-level**. NVML measures the whole GPU and RAPL the whole
  package. Per-span energy is time-window attribution on an otherwise idle
  machine, not process-level energy.
- The NVML counter updates at a host-specific interval (~106 ms on the
  validation host). Spans shorter than that read 0 J or one whole quantum,
  which is quantization, not measurement.

The full list is in [docs/PROVENANCE.md](docs/PROVENANCE.md#known-measurement-limitations).

## Test

```bash
.venv/bin/python -m pytest
# opt-in: re-attribute complete frozen research runs
AEP_FROZEN_RUNS_DIR=/path/to/runs .venv/bin/python -m pytest -m frozen_runs
```

## Repository hygiene

- No AI tool appears as a Git author or co-author. Enable the commit-message
  guard once per clone: `git config core.hooksPath scripts/git-hooks`.
- `tests/golden/` holds the reference artifacts. Never regenerate them to make
  a failing test pass. A deliberate semantic change is documented in
  `docs/PROVENANCE.md`.
