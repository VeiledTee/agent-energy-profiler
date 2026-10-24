# Example: local RAG on an NVIDIA GPU

A small retrieval-augmented question-answering pipeline, run entirely on
your machine:

- real retrieval (BM25 over a 26-passage handbook);
- real generation by an open model on the GPU;
- deterministic reference checks of every answer.

Each stage is a named span, so the report shows time and energy per stage
and per question. One-off setup and the repeated question answering are
separate branches:

```text
setup:prepare         everything before the first question                    ×1
├── model:load        read the downloaded weights, move them to the GPU
├── index:build       build the BM25 index
└── llm:warmup        two generations on a question that is not scored
agent:answer          one question, start to finish                           ×12
├── retrieval:search  top 3 passages for the question (CPU)
├── llm:generate      the model answers from those passages (GPU)
└── verify:answer     reference answer present, source passage cited? (CPU)
```

The handbook ([data/corpus.jsonl](data/corpus.jsonl)) describes a fictional
research station, so its facts are not in the model's training data: to
pass the reference checks, a reply has to use the retrieved passages. The
12 questions and their reference answers are fixed
([data/questions.jsonl](data/questions.jsonl)).

**Why a separate example.** [gpu_inference](../gpu_inference/) times one
operation, generation, with a synthetic model by default. It stays the
minimal example of synchronising GPU work inside a span. This example is a
workflow with stages that cost very different amounts. It needs a real model
because its answers are checked.

## Model

| | |
|---|---|
| Model | [`Qwen/Qwen2.5-1.5B-Instruct`](https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct) (1.54 B parameters) |
| Revision | `989aa7980e4cf806f80c7fef2b1adb7bc71aa306` (pinned in `rag.py`; `--revision` to change) |
| Licence | Apache-2.0; ungated, no account or token needed |
| Precision | bfloat16 (`--dtype`) |
| Backend | Hugging Face `transformers` + PyTorch, in this process, attention `sdpa` |
| Decoding | greedy, at most 64 new tokens, repetition penalty 1.0. The model's own sampling defaults are replaced. |
| Download | about 3.1 GB, once |
| GPU memory | 3.1 GB peak allocated by PyTorch on the development host |

## Install

```bash
python3 -m venv .venv-rag
.venv-rag/bin/pip install 'agent-energy-profiler[gpu]'       # or, in a clone: -e '.[gpu]'
.venv-rag/bin/pip install -r examples/local_rag/requirements.txt
.venv-rag/bin/python -c "import torch; print(torch.cuda.is_available())"   # True
```

`requirements.txt` gives minimum versions. To install the exact versions
the results below were recorded with, add `-c examples/local_rag/constraints.txt`
(its comments say how to get the matching CUDA build of PyTorch).
`.venv-rag/` is ignored by Git.

Installing the profiler never downloads a model. This example downloads one
only when you ask:

```bash
.venv-rag/bin/python examples/local_rag/rag.py --download
```

The weights go to the Hugging Face cache (`~/.cache/huggingface`, or
`$HF_HOME`), not into the repository. After the download, a run needs no
network; set `HF_HUB_OFFLINE=1` to make sure of it.

## Run

```bash
source .venv-rag/bin/activate
mkdir -p runs
agent-energy-profiler validate --counter-disturbance --json runs/hw.json   # once per host and driver (~40 s, loads the GPU)
agent-energy-profiler run --hardware-report runs/hw.json --out-dir runs/rag -- \
    python examples/local_rag/rag.py
agent-energy-profiler report runs/rag --html runs/rag/report.html
```

- `run` measures the whole program and prints the full report.
- `report --html` writes the workflow explorer: one offline file.
- `runs/` is ignored by Git.

On its own, the script records with the same measurement and prints the
brief summary to stderr. It writes no `run.json`:

```bash
python examples/local_rag/rag.py --hardware-report runs/hw.json
```

Options:

- `--repeats N`: passes over the 12 questions;
- `--top-k 3`;
- `--dtype`;
- `--model`/`--revision`: another causal LM with a chat template;
- `--device cpu`;
- `--out-dir`.

**Mode.** The measurement mode is the default, `auto`, and is never forced.
`auto` reads the GPU counter per span only when `runs/hw.json` shows those
reads do not disturb it on this host. Otherwise it uses `sampled`:

- each span's GPU energy is an estimate from sampled power;
- the GPU counter is read only at the start and end of the run, and that
  whole-run value is the measurement.

### Output

In the run directory:

| File | Contents |
|---|---|
| `spans.jsonl`, `power.csv`, `spans_attributed.jsonl`, `session_summary.json`, `run.json` | the profiler's usual files |
| `rag_results.json` | every question: the passages retrieved, the reply, its token counts, generation time and checks. Also the setup: model, requested revision and the snapshot actually loaded, licence, dtype, decoding parameters, seed, SHA-256 of the corpus and questions, Python, platform and library versions (torch, CUDA, transformers, tokenizers, huggingface_hub, safetensors), GPU, and GPU utilisation and memory before the run. It stays in your run directory. The script also passes the setup's plain text and number fields (model, revision, Python, platform, …) to the profile as attributes: they are in `spans.jsonl` and `session_summary.json`, and `report --html` omits them unless you ask for attributes (`--attributes`, `--all-attributes`). |
| `report.html` | from `report --html` |

## Results from one host

**Conditions:**
- RTX 4090 under WSL2, NVIDIA driver 610.88, PyTorch 2.14.1+cu130, transformers 5.20.2.
- No RAPL under WSL2, so CPU and DRAM energy are unavailable.
- The hardware report found counter reads `DISTURBED` (retained median 0.82), so `auto` chose `sampled`.
- The Windows desktop shared the GPU: about 2.5 GB of memory in use and 1–12% utilisation before each run.
- Three runs back to back, with the commands above.
- Recorded before setup was grouped under `setup:prepare`, so the run
  directories hold 51 spans, not 52. In `sampled` mode a parent span reads
  no counter, and each leaf span's estimate depends only on its own time
  window, so the grouping changes no value below. The setup subtotal is the
  sum of its three steps' estimates.

**Measured** (hardware counter):

| | run 1 | run 2 | run 3 |
|---|---|---|---|
| GPU at start (`nvidia-smi`) | P8, 30 W | P0, 104 W | P0, 97 W |
| program wall time | 11.11 s | 11.21 s | 10.16 s |
| **whole program, GPU** (counter at program start and end) | **1,119.713 J** | **1,167.544 J** | **1,058.967 J** |

**Estimated** (`~`, sampled power integrated over each span; never added
to the measured row):

| | run 1 | run 2 | run 3 |
|---|---|---|---|
| *Setup, once* (`setup:prepare`): time | 3.78 s | 3.16 s | 3.31 s |
| — `model:load` | ~63.455 J | ~86.961 J | ~92.020 J |
| — `llm:warmup` (2 unscored generations) | ~166.570 J | ~98.542 J | ~106.831 J |
| — `index:build` | ~0.031 J | ~0.034 J | ~0.034 J |
| — setup subtotal | ~230.056 J | ~185.537 J | ~198.885 J |
| *Answering, 12 questions* (`agent:answer` ×12): time | 5.44 s | 6.26 s | 5.07 s |
| — `agent:answer` ×12, whole traces | ~749.737 J | ~821.897 J | ~696.441 J |
| — per question (÷ 12) | ~62.5 J | ~68.5 J | ~58.0 J |
| — of which `llm:generate` ×12 | ~749.192 J | ~821.448 J | ~695.930 J |
| — of which `retrieval:search` ×12 / `verify:answer` ×12 | ~0.086 / ~0.158 J | ~0.079 / ~0.150 J | ~0.091 / ~0.167 J |
| all spans, estimated (covers the span window only) | ~979.794 J over 9.22 s | ~1,007.433 J over 9.42 s | ~895.326 J over 8.38 s |

**Reference checks passed:** 9 of 12 answers in each run (75%).

The replies were identical in all three runs. The brief summary of run 1
(`agent-energy-profiler report runs/rag --brief`; recorded before the
`setup:prepare` grouping, which adds one `parent` row and changes nothing
else):

```text
ENERGY SUMMARY (brief)
  run        9.22 s (first span start to last span end), 51 spans, 7 operations
  mode       sampled (requested auto, aep_run)
  whole run  GPU          1,126.202 J measured (sampler counter, 11.24 s)
             CPU package  n/a, not measured (no_counter)
             DRAM         n/a, not measured (no_counter)

Operations ranked by ESTIMATED GPU energy (~): 10 Hz sampled power integrated over each span,
  not a counter measurement; leaf spans only, so nothing is counted twice
    #  operation                    n     total       p50       p95    GPU J est.   share  notes
    1  llm:generate                12    5.44 s  460.8 ms  523.0 ms      ~749.192   76.5%
    2  llm:warmup                   1    1.81 s    1.81 s    1.81 s      ~166.570   17.0%
    3  model:load                   1    1.97 s    1.97 s    1.97 s       ~63.455    6.5%
    4  verify:answer               12    1.1 ms     91 us    114 us        ~0.158   <0.1%  short
    5  retrieval:search            12    627 us     53 us     59 us        ~0.086   <0.1%  short
    6  index:build                  1    593 us    593 us    593 us        ~0.031   <0.1%  short
    -  agent:answer                12    5.44 s  461.1 ms  523.3 ms           n/a          parent
  share: of the ranked operations' GPU J only. ~ marks an estimate.
```

These are one host's numbers, not a benchmark. They depend on the GPU,
driver, libraries, clocks and whatever else uses the GPU.

## Reading it

- **Measured vs estimated.** Two values here are measured:
  - the whole program's GPU energy, from the counter read when the program
    starts and ends (`run`);
  - the sampler's run counter (in the report).

  Every per-stage GPU value is an estimate, marked `~`: averaged board power
  sampled at 10 Hz and integrated over the span. Estimates are never added
  to measured values. On a host whose hardware report says `NOT_DETECTED`,
  `auto` chooses `direct` and the per-span values become measured counter
  differences.
- **The estimated span total is not a share of the measured total.** The
  spans cover 9.22 s of run 1; the measured 1,119.713 J covers the whole
  11.11 s program, including interpreter start, importing PyTorch and
  `transformers`, and the profile closing. Neither number is subtracted
  from the other: the gap is time outside any span plus estimation error,
  not "profiler overhead".
- **Shares include setup.** The `share` column (brief summary, HTML
  explorer) is each operation's part of the *ranked estimated* rows, setup
  included. `llm:generate` at 76.5% means 76.5% of ~979 J estimated, not of
  the measured 1,120 J, and not of question answering alone. For the
  steady-state cost, read the `agent:answer` rows (time and full-report
  `est. gpu J`), or `setup:prepare` vs `agent:answer` in the hierarchy.
- **Idle draw is included.** The values are whole-device energy: the GPU's
  idle and static power, the desktop and anything else running. Nothing is
  subtracted. A stage's share says where energy was used while it ran, not
  the model's marginal cost.
- **Short stages.** Retrieval, verification and indexing take microseconds
  to a millisecond, far below the 100 ms power sampling interval. Their
  estimates are interpolated (the `short` note), so they show only that
  these stages are negligible next to generation in this pipeline, not how
  much they cost. Each generation lasts 0.3–0.8 s, a few power samples, so
  its estimate is coarse too.
- **Setup is kept apart from answering.** `model:load`, `index:build` and
  `llm:warmup` run once, under `setup:prepare`; no question's trace
  contains them.
  - `model:load` reads weights that are already downloaded (from the OS page
    cache after the first run) and copies them to the GPU.
  - `llm:warmup` includes one-off CUDA and kernel initialisation.

  Setup cost does not scale with the number of questions, so it is not part
  of the per-question figure. With `--repeats N`, answering grows N-fold
  and setup stays once. Compare `llm:generate` and `agent:answer` spans, not
  these.
- **Synchronisation.** `HFGenerator.generate` calls `torch.cuda.synchronize()`
  before it returns, so each `llm:generate` span ends after its GPU work.
- **Back-to-back runs.** Runs 2 and 3 started with the GPU still at P0
  (about 100 W) from the run before. Leave the GPU idle between runs if you
  compare them.
- **Other GPU programs.** A game, 3D editor or video running on the same
  GPU is measured too. On the development host, a later set of runs with
  such programs open (40–75% GPU utilisation before each run) measured up
  to 4,813 J for the same 12 replies. `gpu_before_start` in
  `rag_results.json` records what the GPU was doing; check it before
  trusting a run.

## What the reference checks measure

The reported figure is a **reference-check pass rate** (9 of 12 above),
not factual accuracy. `verify:answer` checks three things, deterministically:

- the reply contains a reference answer as whole words, after Unicode
  normalisation (citations are removed first);
- it cites the passage holding that answer;
- it cites only passages it was given.

The three failures in the runs above are real:

- Q06 cites the worked example `[P03]`, which it was not given for that
  question;
- Q09 cites `[P01]` instead of `[P15]`;
- Q12 leaves out the freezer's label, `F2`, and cites `[P18]`.

String checks are not a quality evaluation. They cannot tell a correct
paraphrase that misses every listed form (`twelve` and `12` are listed;
`a dozen` is not) from a wrong answer. They also cannot judge fluency or
reasoning. "Verified" (`verified` in `rag_results.json` and on each
`agent:answer` span) means only that these checks passed.

## Use your own pipeline

Nothing here is specific to this model or to BM25. `run_workflow` takes a
`load_generator` callable, and each stage is an ordinary function inside a
span. Replace `BM25.search` with your vector store, `HFGenerator` with vLLM,
llama.cpp or an agent framework's call, and `verify` with your evaluator.
Keep the labels, or choose your own: the profiler records what you name and
infers nothing. The tests run this same workflow with a stand-in generator
and no GPU (`tests/test_local_rag_example.py`).

## Reproducibility

- **Fixed inputs.** Corpus, questions, worked example and warm-up question
  are in the repository; their hashes are in `rag_results.json`.
- **Pinned model.** The model is pinned to a commit. The commit actually
  loaded is recorded as `setup.snapshot`. The chat template and tokenizer
  come from that same snapshot.
- **Pinned libraries.** `constraints.txt` holds the exact versions used
  here; every run records the versions it actually used in
  `rag_results.json`.
- **Deterministic decoding.** Decoding is greedy and seeds are fixed. GPU
  kernels can still differ between PyTorch, CUDA and driver versions, or
  between GPUs, so replies can change across setups. Here they were
  identical across three runs.
- **Energy and time are not fixed.** They vary between runs on the same host
  (see the table) and between hosts.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `this example needs PyTorch and transformers` | `pip install -r examples/local_rag/requirements.txt` in the same environment |
| `... is not downloaded yet` | `python examples/local_rag/rag.py --download` (needs network once) |
| `no CUDA device visible to PyTorch` | `nvidia-smi` must work; install a CUDA build of torch for your driver. Under WSL2, use the Windows NVIDIA driver; do not install a Linux driver. `--device cpu` runs the pipeline on the CPU (slowly) while the GPU only idles. |
| Report shows GPU `n/a (no_counter)` | NVML is unavailable: install the profiler's `gpu` extra (`nvidia-ml-py`) in the same environment. Values stay `n/a`, never 0. |
| CPU package and DRAM `n/a (no_counter)` | no readable RAPL (WSL2, containers, or `/sys/class/powercap` not readable). See the main README. |
| CUDA out of memory | close other GPU programs, or pass a smaller `--model` with its `--revision` |
| `mode sampled` although you expected `direct` | expected unless `runs/hw.json` says `NOT_DETECTED` for this host, driver and GPU. See [MEASUREMENT_MODES.md](../../docs/MEASUREMENT_MODES.md). |
