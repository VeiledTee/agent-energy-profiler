# Examples

Four independent, runnable examples. None needs a paid API, an internet
service or the research infrastructure this profiler came from.

| Example | Needs | Shows |
|---|---|---|
| [cpu_workflow](cpu_workflow/) | core package only | nested spans over ordinary CPU work; why a GPU still reports energy |
| [gpu_inference](gpu_inference/) | PyTorch with CUDA, an NVIDIA GPU | per-generation GPU energy; synchronising before a span closes |
| [agent_workflow](agent_workflow/) | core package only | retrieval → reasoning → verification traces, retries, and concurrent (`shared`) spans |
| [otel_tracing](otel_tracing/) | the `otel` extra | profiler spans inside an OpenTelemetry-traced service; one trace tree with energy attributes; `bind`, `analyze`, `export_spans` |

All four record spans with `Session.from_env(...)`, so they work both on
their own (writing a spans file) and under `aep run`, which adds the hardware
sampler, whole-program energy, attribution and a report. Each passes
`default_mode=` explicitly: it applies only when the program runs on its own
(see docs/MEASUREMENT_MODES.md, "Migrating towards conservative defaults").

LangGraph: decorate node functions with `session.trace(label)`; see
[docs/LANGGRAPH.md](../docs/LANGGRAPH.md).

Before measuring on a new machine, characterise the GPU counter once:

```bash
aep validate --counter-disturbance --json hw.json        # also prints what this host can measure
```

and pass `--hardware-report hw.json` to `aep run` so short spans can be
flagged as below the counter's resolution.

**Measurement mode.** `aep run` defaults to `--mode auto`: with a hardware
report whose disturbance test found no effect it reads the GPU counter per
span (`direct`); otherwise (`sampled`) per-span GPU values are estimates and
the run counter is the measurement. Force a mode with `--mode`. See
[docs/MEASUREMENT_MODES.md](../docs/MEASUREMENT_MODES.md).

**About the GPU numbers in these READMEs.** They predate measurement modes:
spans read the counter per span and the sampler read it at 10 Hz. They were recorded on an RTX 4090
under WSL2 with the 10 Hz sampler running. On that host, reading the NVML
energy counter makes it under-report (~10–11% at the sampler's 10 Hz, more
with frequent span reads; see docs/NVML_COUNTER_VALIDATION.md). Treat the GPU figures
shown as lower bounds that illustrate the report's structure, not as
reference values.
