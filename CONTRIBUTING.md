# Contributing

Thanks for helping. This project measures energy, so the bar for changing
what a number means is high. Most of this file is about protecting that.

## Set up

```bash
git clone <this repository> && cd agent-energy-profiler
python -m venv .venv
.venv/bin/pip install -e '.[test]'          # add ,gpu on a machine with an NVIDIA GPU
git config core.hooksPath scripts/git-hooks  # commit-message guard (see below)
.venv/bin/python -m pytest
```

Run the suite with `TMPDIR` outside any git working tree: one provenance test
checks that a temporary directory is not a repository. No GPU is needed;
every test uses synthetic readers unless explicitly opted in (README, Test).

Before a release, also check the built wheel in clean environments:

```bash
python -m build -o build/dist .
tools/check_wheel.sh python3.9 build/dist/*.whl "" build/w-min
tools/check_wheel.sh python3 build/dist/*.whl "test,gpu" build/w-full
```

CI (`.github/workflows/ci.yml`) runs the suite on Python 3.9–3.14, these
wheel checks, and the LangGraph compatibility tests.

## Rules for changes

1. **Measurement semantics are preserved unless a change says otherwise.**
   `tests/golden/` (schema v1) and `tests/golden_v2/` (schema v2) are
   reference artifacts. Never regenerate them to make a failing test pass. A
   deliberate change to what a value means needs a regenerated fixture with
   a reason, and an entry in the `docs/PROVENANCE.md` change log and
   `CHANGELOG.md`.
2. **Missing is not zero.** Never coerce missing energy to 0, never add RAPL
   `core` to a total, never put an estimate in a measured field without
   marking it.
3. **Labels come from the caller.** The profiler never derives an operation
   label from prompts, function names or the call stack
   (`tests/test_package_boundary.py` enforces this).
4. **The core has no dependencies.** Optional features go behind an extra in
   `pyproject.toml` and are imported only when used
   (`tests/test_import_side_effects.py`).
5. **No accuracy claims without evidence.** Documentation states what was
   measured, on which host, and what was not validated. Nothing in this
   repository has been checked against an external power meter yet.
6. **Behaviour changes are announced first.** A changed default warns for one
   minor release before it changes (see the `Session` mode migration in
   `docs/MEASUREMENT_MODES.md`).

## Pull requests

- Keep a change focused; add tests next to the behaviour they pin.
- Hardware-dependent behaviour needs a synthetic test that runs in CI, plus
  a note of what was checked on which real hardware, if any.
- Update `CHANGELOG.md` under the unreleased version.

## Commit authorship

Commits are authored by people. Do not add AI tools as authors or
co-authors, and do not add `Co-Authored-By` or similar AI attribution
trailers; `scripts/git-hooks/commit-msg` rejects them.

## Reporting problems

Bugs and questions: open an issue with the output of `aep validate` (or the
file written by `aep validate --json hw.json`) and the profiler version
(`aep --version`). Security
issues: see [SECURITY.md](SECURITY.md).
