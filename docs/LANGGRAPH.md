# LangGraph integration: evaluation and recommendation (Milestone 6)

**Recommendation: instrument LangGraph node functions explicitly with
`session.trace(label)` or `session.span(label)`. No LangGraph-specific
module is added.** It needs no integration code, it keeps labels in the
developer's hands, and it is pinned by tests against the installed LangGraph
(`tests/test_langgraph.py`).

Evaluated against LangGraph 1.2.15 and langchain-core 1.6.x (current at the
time of writing), using public APIs only.

## Recipe

```python
from agent_energy_profiler import Session
from langgraph.graph import StateGraph, START, END

with Session.from_env("spans.jsonl", default_mode="auto") as session:

    @session.trace("retrieval:search")
    def retrieve(state, config):            # config injection still works
        ...

    @session.trace("llm:reason")
    async def reason(state):                # async nodes too
        ...

    def answer(state):
        with session.span("llm:answer") as s:   # or a span inside the node
            ...
            s.set_tokens(input=n_in, output=n_out)

    g = StateGraph(State)
    g.add_node("retrieve", retrieve)
    ...
    graph = g.compile()

    with session.span("agent:request"):     # one root per request
        graph.invoke(inputs)
```

What the tests establish for this LangGraph version:

- `functools.wraps` keeps the node's signature visible (`inspect.signature`
  follows `__wrapped__`), so LangGraph still injects `config` (and other
  declared parameters) into decorated nodes.
- **Sync graphs:** parallel branches run in LangGraph's thread pool, which
  copies the caller's `contextvars`. Node spans are children of the span
  around `graph.invoke`.
- **Async graphs:** branches run as asyncio tasks, which inherit context.
  Node spans are children of the enclosing span. Parallel branches overlap,
  so the report marks them `shared`.
- Measurement is unchanged: each node span reads the counter as any span
  does (`direct`), or not at all (`sampled`/`hybrid`).

To correlate with LangGraph or LangChain OpenTelemetry instrumentation
already in the application, add `hooks=[Correlation()]`
([OPENTELEMETRY.md](OPENTELEMETRY.md)). Root spans opened inside an
instrumented run join its trace.

## Options considered

| Option | Supported public API | Labels | Context / timing | Testable without an LLM | Verdict |
|---|---|---|---|---|---|
| **Explicit decorators / context managers** in node code | `session.trace`, `session.span` (ours); plain functions as nodes (LangGraph) | written by the developer | correct in sync and async graphs (tests) | yes | **recommended** |
| LangChain callbacks (`BaseCallbackHandler.on_chain_start/end`, `config={"callbacks": [...]}`) | yes, stable | only node names (`metadata["langgraph_node"]`), which are graph keys or, by default, function names | sync handlers in async graphs run on an executor thread (observed: a different thread id from the node), so spans could not use the profiler's context nesting and their boundaries are offset from the node's work; needs a begin/end API keyed by run id | yes | not now |
| Existing OpenTelemetry instrumentation (OpenInference, OpenLLMetry, LangSmith OTel) | third-party packages; each has its own span names and attributes | theirs | they emit their own spans; energy would still need profiler spans | needs those packages | complement, via `Correlation`; not a replacement |
| Another mechanism (custom checkpointer, `RunnableConfig` middleware, wrapping `add_node`) | checkpointers are for persistence, not timing; no node middleware hook in the public API | — | — | — | no |

Why not callbacks now:

1. **Labels.** The profiler never infers labels from function names. A
   callback integration would need a caller-supplied map from node names to
   labels. That map is about as much code as the decorators, and spread over a
   second place.
2. **Context.** The probe observed sync callback handlers running on a
   different thread from async nodes. A callback-based span would need its
   own parent bookkeeping (by `run_id`/`parent_run_id`) and could not use the
   `contextvars` nesting that every other span uses. Spans opened inside the
   node would no longer nest under it.
3. **Timing.** Callback dispatch adds LangGraph and LangChain overhead
   inside the measured window, at a point the profiler does not control.

These reasons are about today's APIs. If a user needs zero-touch
instrumentation of a graph they cannot edit, a callback handler with an
explicit label map, built on a manual begin/end span API, is the next step.
Neither exists yet.

## Re-checking on a new LangGraph version

```bash
python -m venv build/venv-lg
build/venv-lg/bin/pip install langgraph pytest 'opentelemetry-sdk>=1.20' -e .
build/venv-lg/bin/python -m pytest tests/test_langgraph.py
```
