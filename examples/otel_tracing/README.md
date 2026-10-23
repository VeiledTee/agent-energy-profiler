# Example 4: tracing and energy profiling together

A small question-answering service that is already traced with
OpenTelemetry. The agent's operations are profiler spans inside the
application's request spans. After the run, the profiler's spans are
exported into the application's own exporter, so one trace per request shows
both kinds of span, with energy attributes on the profiler spans.

```bash
pip install '.[otel]'                       # from the repository root
python examples/otel_tracing/app.py --out-dir runs/otel
```

On its own, `Session.profile(..., hooks=[Correlation(propagate=True)])`
starts the sampler (averaged power at 10 Hz; GPU counter only at the first
and final row) and records in the default mode, `auto`, which is `sampled`
unless a hardware report says the counter is undisturbed. When the session
closes, it attributes the run (`quiet=True`, so nothing is printed then). The
program then prints a short report from `session.report` and the combined
trace tree, and writes every span as OpenTelemetry JSON lines. Without
`--out-dir`, the files go to `agent-energy-profiler-runs/otel_tracing-<timestamp>/`.

Under `agent-energy-profiler run`, `agent-energy-profiler run` owns the sampler and the mode. Export afterwards:

```bash
agent-energy-profiler run --out-dir runs/otel -- python examples/otel_tracing/app.py
agent-energy-profiler otel-export runs/otel --out runs/otel/otel_spans.jsonl
# or to a collector (needs the otlp extra):
agent-energy-profiler otel-export runs/otel --endpoint http://localhost:4318/v1/traces
```

## What it shows

```
one trace per request (application spans + profiler spans):
  POST /ask                   314.7 ms   (application span)
    agent:request             314.6 ms   [gpu unavailable (not_read_by_mode), est. 8.976 J; sole]
      retrieval:search         20.2 ms   [gpu unavailable (not_read_by_mode), est. 0.575 J; sole]
        vector_db.query        20.1 ms   (application span)
      llm:reason              294.2 ms   [gpu unavailable (not_read_by_mode), est. 8.394 J; sole]
      verify:check              0.0 ms   [gpu unavailable (not_read_by_mode), est. 0.000 J; sole]
  ...
```

Under `agent-energy-profiler run` the application's own spans stay in the program's
in-memory exporter. `agent-energy-profiler otel-export` then exports the 12 profiler spans,
which still carry the application's trace ids through their roots'
`external_parent`.

(RTX 4090 under WSL2, `sampled` mode; no RAPL in WSL2. Your numbers will
differ, and the GPU here is idle apart from the desktop, so they are mostly
idle draw.)

- `POST /ask` and `vector_db.query` are the application's spans.
  `vector_db.query` was started inside `retrieval:search`, and with
  `Correlation(propagate=True)` it is that span's child.
- In `sampled` mode the GPU counter is not read per span. The span carries
  `aep.energy.gpu.status = unavailable`, `reason = not_read_by_mode`, and
  **no** `aep.energy.gpu.energy_j`. The integrated-power value is under
  `aep.estimate.gpu.energy_j`. The measured GPU energy is the run counter in
  the report, which covers the whole sampler window and cannot be split
  between spans.
- Every exported span also says how it was measured (`aep.measurement.*`)
  and that nothing was checked against an external meter
  (`aep.energy.external_meter_validated = false`).

Full attribute list: [docs/OPENTELEMETRY.md](../../docs/OPENTELEMETRY.md).

## Patterns used

| Need | Code |
|---|---|
| join the application's trace | `Session.profile(..., hooks=[Correlation()])` (also `Session(...)`) |
| application spans nest under profiler spans | `Correlation(propagate=True)` |
| tag all spans of a request without an extra span | `with session.bind(request_id=rid):` |
| own sampler and conservative mode when run on its own, `agent-energy-profiler run`'s otherwise | `Session.profile("otel_tracing", quiet=True, ...)` |
| the report from Python | `print(report.render_text(session.report, top=5))` |
| export into the application's pipeline | `export_spans(session.out_dir, exporter, resource=provider.resource)` |

The application keeps its own `TracerProvider`. Nothing in the profiler
sets OpenTelemetry's global provider or propagators.
