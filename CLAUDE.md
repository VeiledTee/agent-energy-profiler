# CLAUDE.md

## Project

`agent-energy-profiler`: a standalone, framework-agnostic energy profiler for
agentic AI systems. It was extracted from the PhD research repository
`Chain-of-Relations-Emissions` at commit `08bbc96` (see `docs/PROVENANCE.md`).
The two projects are maintained independently.

## Project Isolation

This repository is the standalone Agent Energy Profiler project.

- All development must remain within `/home/penguins/agent-energy-profiler`.
- Never modify files in `Chain-of-Relations-Emissions`.
- Never modify the thesis repository's dependencies, experiments, results, or Git history.
- Do not execute commands that write outside this repository without explicit approval.
- Treat the thesis repository as read-only reference material.
- Maintain an independent virtual environment and Git history.
- Do not push commits unless explicitly instructed.

## Hard rules

- Never modify the research repository (`/home/penguins/Chain-of-Relations-Emissions`)
  from here. Read its data only, and only when a task asks for it.
- Preserve measurement semantics unless a milestone explicitly changes them.
  `tests/golden/` holds the reference artifacts. Never regenerate them to make
  a failing test pass. Record every deliberate semantic change in the
  `docs/PROVENANCE.md` change log.
- Never silently coerce missing energy to zero; never add RAPL `core` to a
  total; never report an estimate in a measured field without marking it.
- The profiler never infers an operation label from prompts, function names or
  the call stack. Labels come from the caller.
- Core stays dependency-free. Optional features go behind extras.
- Use this repository's own `.venv`. Never share environments or editable
  installs with the research repository.

## Commit authorship

Never add Claude, any AI assistant or any tool as a Git author or co-author.
Never add `Co-Authored-By`, `Claude-Session` or similar AI attribution
trailers. `scripts/git-hooks/commit-msg` enforces this; enable it with
`git config core.hooksPath scripts/git-hooks`. Do not create remotes or push
unless asked.

## Test

```bash
.venv/bin/python -m pytest
AEP_FROZEN_RUNS_DIR=/home/penguins/Chain-of-Relations-Emissions/measurement/runs \
    .venv/bin/python -m pytest -m frozen_runs     # opt-in, slow
```
