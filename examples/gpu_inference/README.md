# Example 2: GPU-backed model inference

Loads a model onto an NVIDIA GPU and generates text, one span per
generation. By default the model is a synthetic decoder (12 transformer
layers, width 1024, random weights) built locally with `torch.nn`: no
download, no account. `--hf-model distilgpt2` uses a real Hugging Face model
instead (needs `transformers` and a one-time download).

## Install

```bash
python -m venv .venv
.venv/bin/pip install '/path/to/agent-energy-profiler[gpu]' # a clone or wheel, with NVML bindings
.venv/bin/pip install torch --index-url https://download.pytorch.org/whl/cu124
.venv/bin/pip install transformers                      # only for --hf-model
```

Pick the PyTorch build that matches your driver (see pytorch.org). Check:
`python -c "import torch; print(torch.cuda.is_available())"`.

## Run

```bash
aep validate --counter-disturbance --json hw.json
aep run --hardware-report hw.json --out-dir runs/gpu -- \
    python examples/gpu_inference/inference.py
```

Options: `--prompts 8 --new-tokens 64 --layers 12 --width 1024`,
`--hf-model distilgpt2`, `--device cpu`.

**Mode.** Per-generation *measured* GPU energy needs `direct` mode (counter
read at each span's boundaries). `auto` selects it only when the hardware
report's disturbance test found no effect; otherwise the run is `sampled`:
the run total is measured and each generation's GPU energy is an estimate
from averaged power. On a host where reads disturb the counter, `--mode
direct` costs about 2 × 10 ms of accumulation per span, ~6% of a 330 ms
generation on the development host.

## Output

Same files as the other examples (`spans.jsonl`, `power.csv`,
`spans_attributed.jsonl`, `session_summary.json`, `run.json`). Report from an
RTX 4090 under WSL2 (abridged):

```
MEASURED ENERGY (hardware counters; basis: root spans)
  gpu                        516.151 J
ESTIMATED ENERGY (spans with no GPU measurement; never added above)
  none: every top-level span with GPU energy was measured
  (diagnostic: on 10 measured spans, integrated power gives 498.723 J vs counter 516.151 J)
INDEPENDENT WINDOW MEASUREMENT
  sampler timeline over the session window: gpu 507.791 J, ...

BY OPERATION (energy: exclusive; top 3 by total time)
  operation                       n     total       p50       p95       gpu J
  llm:generate                    8    2.59 s  328.6 ms  382.5 ms     453.276
  model:load                      1  735.1 ms  735.1 ms  735.1 ms      21.914
  llm:warmup                      1  464.6 ms  464.6 ms  464.6 ms      40.961
```

## Reading it

- **Synchronisation.** CUDA kernels run asynchronously. The script calls
  `torch.cuda.synchronize()` before each span closes; without it, a span can
  end before its GPU work runs, and that energy lands in the next span.
- **Device-level, not process-level.** Everything else using the GPU at the
  same time (a desktop, another job) is in these numbers. Measure on an
  otherwise idle GPU.
- **Spans here are long** (≥300 ms, about three counter updates or more), so
  none is flagged below resolution. Shorter generations would be.
- **Estimate vs counter.** Integrated sampled power is printed only as a
  diagnostic: here it is ~3% below the counter. It is never used as the
  measurement.
- **`llm:warmup`** includes one-off CUDA/cuBLAS initialisation; compare
  generations, not warm-up.
- **Served models.** If the model runs in another process (vLLM, an
  inference server), a span bounds the *request*, not the kernels. Its energy
  is the device's energy during the request, including anything else the
  server did.
- With `--device cpu` the GPU only idles, and its idle energy is still
  recorded: a useful way to see the baseline.
