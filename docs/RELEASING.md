# Releasing

Checklist for publishing this repository and a release. Nothing here has
been done yet: the project has no remote, no hosted CI run and no PyPI
release.

## Before the first public push (maintainer decisions)

1. **Commit identity.** Every commit is authored with a personal e-mail
   address. Pushing publishes it. To use a GitHub `noreply` address instead,
   the history must be rewritten before the first push.
2. **Repository contents.** Decide whether to publish `CLAUDE.md` (local
   paths, assistant instructions) and `results/` (3.5 MB of raw M4/M5
   diagnostic data with the development host's name and local paths). Both
   are excluded from the sdist already. `docs/PROVENANCE.md` names local
   paths of the research repository.
3. **Security contact.** `SECURITY.md` points to the hosting platform's
   private vulnerability reporting. Enable it, or add a contact address.
4. **Project URLs.** Add `[project.urls]` (Homepage, Source, Issues,
   Changelog) to `pyproject.toml` once the repository URL exists.
5. **Package name.** Check that `agent-energy-profiler` is free on PyPI
   and that the `aep` command does not clash with something your users
   install.

## Each release

1. Set the version in `pyproject.toml` and `src/agent_energy_profiler/__init__.py`
   (`tests/test_release.py` checks they agree) and date the `CHANGELOG.md`
   entry.
2. Run the full suite, the frozen-run check, and the CI workflow on the
   hosted runner. A release needs one green CI run on the tagged commit.
3. Build once (`python -m build`), `twine check --strict dist/*`, and run
   `tools/check_wheel.sh` on the built wheel.
4. Publish the same files that CI tested, preferably from CI with PyPI
   Trusted Publishing (no long-lived token). Publish to TestPyPI first for
   the first release.
5. Tag the commit (`vX.Y.Z`) and attach the CHANGELOG entry to the release.

## What a release does not establish

CI has no GPU and no RAPL. Hardware behaviour is validated separately
([HARDWARE_VALIDATION_PROTOCOL.md](HARDWARE_VALIDATION_PROTOCOL.md)), and
no value has been checked against an external power meter.
