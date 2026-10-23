# Releasing

How this repository is published and how a release is made. As of 0.4.0
the repository is public on GitHub and hosted CI passes (first green run:
`d7e996a`, 2026-10-23, all eight jobs). There is no PyPI release yet.

| Item | Decision |
|---|---|
| Repository | `https://github.com/VeiledTee/agent-energy-profiler` (public) |
| Distribution, command | `agent-energy-profiler` (both were free on PyPI and TestPyPI on 2026-10-17) |
| Import name | `agent_energy_profiler` |
| Commit identity | kept as recorded; the history is not rewritten |
| `CLAUDE.md` | untracked from 0.4.0 and listed in `.gitignore`; still present in earlier commits |
| `results/` | published, documented in `results/README.md`; host name and local paths redacted from 0.4.0 |
| Security contact | GitHub private vulnerability reporting (`SECURITY.md`) |

## What earlier commits contain

The audit before publication (0.4.0) searched every blob in the history,
including decompressed fixtures, for credentials, tokens, keys, personal
paths, host names, e-mail addresses and device identifiers. It found:

- **No credentials, tokens, keys or GPU serial numbers or UUIDs.**
- **The commit author e-mail** on every commit. This is deliberate.
- **`CLAUDE.md`** in two versions, holding local development instructions
  and the local paths `/home/<user>/...`.
- **`results/`** files containing the development machine's host name and
  the virtual environment's absolute path, which includes the local user
  name. These were redacted in 0.4.0.
- **`docs/PROVENANCE.md`** containing the research repository's local path,
  replaced in 0.4.0.

Removing these from earlier commits would mean rewriting the history (for
example with `git filter-repo`). That changes every commit id, and
`docs/PROVENANCE.md` cites those ids (`2310de4`, `baf6108`, ...) as
evidence. The current decision is not to rewrite.

## First publication

### 1. Create the repository (GitHub web UI, ~3 minutes)

1. <https://github.com/new>. Owner `VeiledTee`, name `agent-energy-profiler`,
   **Public**.
2. Leave "Add a README", ".gitignore" and "license" **unticked**: the
   repository must be empty, or the first push will be rejected.
3. Create.
4. Optional: so that the commits link to your account, add the commit
   e-mail address under <https://github.com/settings/emails>.

### 2. Enable private vulnerability reporting (~1 minute)

1. In the repository: **Settings → Advanced Security** (called "Code
   security" on some accounts).
2. **Private vulnerability reporting → Enable.**
3. Check that the **Security** tab shows **Report a vulnerability**. The link
   in `SECURITY.md` (`/security/advisories/new`) works only after this.

### 3. First push (~2 minutes)

```bash
cd agent-energy-profiler
git status                      # clean; CLAUDE.md is ignored, not staged
git config core.hooksPath scripts/git-hooks
git remote add origin git@github.com:VeiledTee/agent-energy-profiler.git
#   (HTTPS: https://github.com/VeiledTee/agent-energy-profiler.git)
git push -u origin main
```

### 4. Hosted CI (~10 minutes, mostly waiting)

The push triggers `.github/workflows/ci.yml`: the suite on Python 3.9–3.14,
the wheel checks and the LangGraph tests. Done for 0.4.0: in the first
hosted run (`dbb937e`) the Python 3.9 jobs (`tests (Python 3.9)` and the
wheel job) failed in `actions/setup-python` on the `ubuntu-latest` image,
before any test ran; every other job passed. `d7e996a` pinned every job to `ubuntu-24.04` and moved to
`actions/checkout@v7`, `actions/setup-python@v7` and
`actions/upload-artifact@v6` (the Node.js 20 versions are deprecated); its
run passed every job.

```bash
gh run list --workflow CI --limit 1
gh run watch                    # or the repository's Actions tab
gh run view --log-failed        # if a job fails
```

A release needs one green CI run on the commit it is built from.

## Publishing a release to PyPI (when you decide to)

Prefer PyPI Trusted Publishing: GitHub Actions uploads the files CI built,
and no long-lived token is needed. First time:

1. On <https://test.pypi.org/manage/account/publishing/> and
   <https://pypi.org/manage/account/publishing/>, add a **pending
   publisher**: project `agent-energy-profiler`, owner `VeiledTee`,
   repository `agent-energy-profiler`, workflow `release.yml`, environment
   `pypi` (`testpypi` on TestPyPI).
2. In the repository, **Settings → Environments**: create `testpypi` and
   `pypi`, with yourself as a required reviewer for `pypi`.
3. Add `.github/workflows/release.yml` (not included yet):

```yaml
name: Release
on:
  workflow_dispatch:
    inputs:
      target: {type: choice, options: [testpypi, pypi], default: testpypi}
permissions:
  contents: read
jobs:
  build:
    runs-on: ubuntu-24.04             # pinned, as in ci.yml
    steps:
      - uses: actions/checkout@v7
      - uses: actions/setup-python@v7
        with: {python-version: "3.13"}
      - run: python -m pip install build twine && python -m build && python -m twine check --strict dist/*
      - uses: actions/upload-artifact@v6
        with: {name: dist, path: dist/}
  publish:
    needs: build
    runs-on: ubuntu-24.04
    environment: ${{ inputs.target }}
    permissions:
      id-token: write
    steps:
      - uses: actions/download-artifact@v6  # same major as upload-artifact; check before use
        with: {name: dist, path: dist/}
      - uses: pypa/gh-action-pypi-publish@release/v1
        with:
          repository-url: ${{ inputs.target == 'testpypi' && 'https://test.pypi.org/legacy/' || 'https://upload.pypi.org/legacy/' }}
```

Then, for each release:

1. Set the version in `pyproject.toml` and `src/agent_energy_profiler/__init__.py`
   (`tests/test_release.py` checks they agree), and date the
   `CHANGELOG.md` entry.
2. Run locally: the full suite, the frozen-run check, `python -m build`,
   `twine check --strict dist/*`, and `tools/check_wheel.sh` on the wheel.
3. Push. Wait for green CI on that commit.
4. Run the Release workflow with `target: testpypi`, then check:

   ```bash
   python -m venv /tmp/tp && /tmp/tp/bin/pip install \
       --index-url https://test.pypi.org/simple/ --extra-index-url https://pypi.org/simple/ \
       agent-energy-profiler==0.4.0
   /tmp/tp/bin/agent-energy-profiler --version
   ```

5. Run it with `target: pypi`. Then tag and create the GitHub release:

   ```bash
   git tag -a v0.4.0 -m "Agent Energy Profiler 0.4.0"
   git push origin v0.4.0
   gh release create v0.4.0 --title "0.4.0" --notes-file <(sed -n '/^## 0.4.0/,/^## 0.3.0/p' CHANGELOG.md | sed '$d')
   ```

A version number can be uploaded to PyPI only once, even if it is deleted
afterwards. Use TestPyPI to rehearse.

## What a release does not establish

CI has no GPU and no RAPL. Hardware behaviour is validated separately
([HARDWARE_VALIDATION_PROTOCOL.md](HARDWARE_VALIDATION_PROTOCOL.md)), and
no value has been checked against an external power meter.
