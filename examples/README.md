# Examples

Three independent, runnable examples. None needs a paid API, an internet
service or the research infrastructure this profiler came from.

| Example | Needs | Shows |
|---|---|---|
| [cpu_workflow](cpu_workflow/) | core package only | nested spans over ordinary CPU work; why a GPU still reports energy |
| [gpu_inference](gpu_inference/) | PyTorch with CUDA, an NVIDIA GPU | per-generation GPU energy; synchronising before a span closes |
| [agent_workflow](agent_workflow/) | core package only | retrieval → reasoning → verification traces, retries, and concurrent (`shared`) spans |

All three record spans with `Session.from_env(...)`, so they work both on
their own (writing a spans file in the current directory) and under
`aep run`, which adds the hardware sampler, whole-program energy, attribution
and a report.

Before measuring on a new machine, characterise the GPU counter once:

```bash
aep validate --json hw.json        # also prints what this host can measure
```

and pass `--resolution-report hw.json` to `aep run` so short spans can be
flagged as below the counter's resolution.

**About the GPU numbers in these READMEs.** They were recorded on an RTX 4090
under WSL2 with the 10 Hz sampler running. On that host, reading the NVML
energy counter makes it under-report (~14% at 10 Hz, much more with frequent
span reads; see the main README's Known limitations). Treat the GPU figures
shown as lower bounds that illustrate the report's structure, not as
reference values.
