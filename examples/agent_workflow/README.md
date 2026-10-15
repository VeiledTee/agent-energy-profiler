# Example 3: an agent-like workflow

Six questions answered against a small built-in corpus. Each question is a
trace:

```
agent:question
  retrieval:search    BM25 (pure Python)
  llm:reason          a deterministic stand-in "reasoner" (pure Python)
  verify:check        is the answer supported by the evidence?
  (one retry with a wider search if verification fails)
```

No API keys, downloads or GPU are needed. The labels are chosen by the
program; the profiler never infers them. To profile a real agent, wrap your
own LLM calls, tool calls and checks the same way.

## Install

```bash
python -m venv .venv
.venv/bin/pip install /path/to/agent-energy-profiler        # not on PyPI: a clone or wheel
```

## Run

```bash
aep validate --counter-disturbance --json hw.json
aep run --hardware-report hw.json --out-dir runs/agent -- \
    python examples/agent_workflow/agent.py
# concurrent version: shows shared attribution
aep run --hardware-report hw.json --out-dir runs/agent-par -- \
    python examples/agent_workflow/agent.py --parallel
```

`--work N` sets how long each reasoning step takes (default 3000).

## Output

Same files as the other examples. Sequential run, RTX 4090 under WSL2
(abridged):

```
MEASURED ENERGY (hardware counters; basis: root spans)
  gpu                          1.710 J
BY OPERATION (energy: exclusive; top 4 by total time)
  operation                       n     total       p50       p95       gpu J
  agent:question                  6  115.9 ms   19.3 ms   21.0 ms       0.000
  llm:reason                      6   54.5 ms    9.2 ms   10.3 ms       1.710
  retrieval:search                6    277 us     46 us     69 us       0.000
  verify:check                    6     27 us      5 us      6 us       0.000
HIERARCHY (by label path; exclusive GPU energy)
  agent:question                      6  115.9 ms       0.000 J
    llm:reason                        6   54.5 ms       1.710 J
    retrieval:search                  6    277 us       0.000 J
    verify:check                      6     27 us       0.000 J
WARNINGS
  [BELOW_RESOLUTION] 24 of 24 measured GPU values (100.0%) come from spans shorter than the counter's update interval (~102.4 ms) ...
  [ZERO_READINGS] 22 spans read exactly 0 J of GPU energy ...
```

With `--parallel`:

```
  [SHARED_ATTRIBUTION] 24 spans overlapped other recorded work; their inclusive energy includes that work and they have no exclusive energy.
```

## Reading it

- **Every span is below resolution.** These steps take microseconds to
  ~20 ms; the GPU counter updates every ~102 ms. Per-step GPU figures here are
  quantisation (mostly exact zeros, occasionally one whole update), so do not
  read them as the cost of retrieval vs reasoning. Lengthen the steps
  (`--work 30000`) and the per-step figures become measurements.
- **Frequent reads depress the counter on some hosts.** This run reads the
  GPU counter at every span boundary, hundreds of times per second. On the
  WSL2 development host that makes the counter under-report (here ~14 W
  recorded against ~28 W of idle board power), so even the run total is a
  lower bound there.
- **Hierarchy, not double counting.** Totals add the six `agent:question`
  roots; per-operation energy is exclusive (each step's own share). Adding the
  inclusive values of parents and children would count the same joules twice.
- **Concurrency.** With `--parallel` the questions overlap in time. The
  device cannot tell them apart, so the report marks them `shared`, gives no
  exclusive energy and no root total, and points to the independent window
  measurement instead.
- **On a CPU-heavy agent** CPU-package and DRAM are the domains that matter;
  they need bare-metal Linux with RAPL (`aep validate` shows whether you have
  it).
