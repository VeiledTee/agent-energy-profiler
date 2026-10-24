# Examples

**New here? Run [quickstart.py](quickstart.py) first** (about a minute,
see the [README](../README.md#quick-start)):

```bash
python examples/quickstart.py
```

It uses `Session.profile`, the recommended way to profile from Python: it
starts the hardware sampler, attributes energy when the `with` block ends,
prints a brief summary to stderr and saves everything (including the full
report) in
`agent-energy-profiler-runs/quickstart-<timestamp>/`. The same script also
works under `agent-energy-profiler run`.

**Then read [agent_workflow](agent_workflow/).** It is a complete
walkthrough covering install, instrumentation, running, reporting, and
measured versus estimated energy. It uses fixed inputs and needs nothing
beyond the core package.

All examples are runnable and independent. None needs a paid API, an
internet service or the research infrastructure this profiler came from.

| Example | Needs | Shows |
|---|---|---|
| [quickstart.py](quickstart.py) | core package only | three named steps per request, nested spans, token counts; the README's quick start |
| [agent_workflow](agent_workflow/) | core package only | retrieval → reasoning → verification traces, retries, concurrent (`shared`) spans; the walkthrough |
| [cpu_workflow](cpu_workflow/) | core package only | nested spans over ordinary CPU work; why a GPU still reports energy |
| [gpu_inference](gpu_inference/) | PyTorch with CUDA, an NVIDIA GPU | per-generation GPU energy; synchronising before a span closes |
| [otel_tracing](otel_tracing/) | the `otel` extra | profiler spans inside an OpenTelemetry-traced service; one trace tree with energy attributes; `bind`, `session.report`, `export_spans` |

All of them record with `Session.profile(...)`, so each works two ways:

- **On its own** (`python examples/...`): the profile starts the hardware
  sampler, attributes the spans when it closes, prints a brief summary to
  stderr and saves everything, including the full report, in `agent-energy-profiler-runs/<example>-<timestamp>/`
  (or `--out-dir`). The measurement mode is the default, `auto`.
- **Under `agent-energy-profiler run`:** `run`'s sampler, mode and report
  apply, and no second sampler is started. `run` adds whole-program energy
  (`run.json`).

LangGraph: decorate node functions with `session.trace(label)`; see
[docs/LANGGRAPH.md](../docs/LANGGRAPH.md).

Before measuring on a new machine, characterise the GPU counter once:

```bash
agent-energy-profiler validate --counter-disturbance --json hw.json   # also prints what this host can measure
```

Then pass `--hardware-report hw.json` to `agent-energy-profiler run`, so that
short spans can be flagged as below the counter's resolution.

**Measurement mode.** `agent-energy-profiler run` defaults to `--mode auto`.
With a hardware report whose disturbance test found no effect on this host,
driver and GPU, it reads the GPU counter per span (`direct`). Otherwise it
uses `sampled`: per-span GPU values are estimates and the run counter is the
measurement. Force a mode with `--mode`. See
[docs/MEASUREMENT_MODES.md](../docs/MEASUREMENT_MODES.md).

**About the GPU numbers in the cpu_workflow and gpu_inference READMEs.**
They predate measurement modes: spans read the counter per span
and the sampler read it at 10 Hz. They were recorded on an RTX 4090 under
WSL2. On that host, reading the NVML energy counter makes it under-report
(~10–11% at the sampler's 10 Hz, more with frequent span reads; see
docs/NVML_COUNTER_VALIDATION.md). Treat those GPU figures as lower bounds
that illustrate the report's structure, not as reference values. The
agent_workflow walkthrough was recorded with 0.4.0's default mode.
