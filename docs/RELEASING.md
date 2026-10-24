# Releasing

How this project is published and how a release is made. The repository
is public on GitHub, and hosted CI passes (first green run: `d7e996a`,
2026-10-23, all eight jobs). **Nothing has been published to PyPI or
TestPyPI yet.** 0.5.0 is prepared as the first published release;
0.1.0–0.4.0 were development milestones.

| Item | Decision |
|---|---|
| Repository | `https://github.com/VeiledTee/agent-energy-profiler` (public) |
| Distribution, command | `agent-energy-profiler` (both were free on PyPI and TestPyPI on 2026-10-17) |
| Import name | `agent_energy_profiler` |
| Files uploaded | the sdist and wheel built by GitHub Actions (below), not a local build. A local sdist records the local user name as file owner in its tar headers, and the build environment is not recorded |
| Publishing | PyPI trusted publishing from GitHub Actions; no API token. Not configured yet |
| Archive and DOI | Zenodo's GitHub integration, if enabled. **No DOI has been assigned**; `CITATION.cff` carries none |
| Commit identity | kept as recorded; the history is not rewritten |
| `CLAUDE.md` | untracked from 0.4.0 and listed in `.gitignore`; still present in earlier commits |
| `results/` | published in the repository, not in the distributions; documented in `results/README.md`; host name and local paths redacted from 0.4.0 |
| Security contact | GitHub private vulnerability reporting (`SECURITY.md`) |

**Uploads cannot be undone.** A file name can be uploaded to PyPI only
once, even after the release is deleted, and the same holds on TestPyPI.
A Zenodo record, once published, keeps its DOI and files.

## Release checklist

Copy the blocks in order. Steps 1–4 change nothing outside your clone;
steps 5–10 publish and need your decision. `V` is the version being
released.

```bash
V=0.5.0
```

### 1. Review and commit

```bash
git status --short                 # only the release changes; CLAUDE.md stays ignored
git diff --stat
git diff                           # read it
git config core.hooksPath scripts/git-hooks   # once per clone: rejects AI attribution trailers
git add -A && git status --short   # check the staged list before committing
git commit                         # message written by you
```

### 2. Push and confirm CI

```bash
git push origin main
gh run watch                       # or the repository's Actions tab
gh run list --workflow CI --limit 1   # must be green on the commit you release
```

### 3. Build the distributions (locally, to validate)

```bash
python3 -m venv build/release-tools
build/release-tools/bin/pip install build twine check-wheel-contents
export SOURCE_DATE_EPOCH=$(git log -1 --format=%ct)   # the wheel is then byte-reproducible
rm -rf build/dist
build/release-tools/bin/python -m build -o build/dist .
ls build/dist                      # agent_energy_profiler-$V.tar.gz and -$V-py3-none-any.whl
```

### 4. Validate the distributions

```bash
build/release-tools/bin/twine check --strict build/dist/*
build/release-tools/bin/check-wheel-contents build/dist/*.whl
unzip -l build/dist/*.whl          # agent_energy_profiler/ and its dist-info only
tar tzf build/dist/*.tar.gz | less # package, tests, docs, examples, tools; no results/, no runs
# clean environments outside the repository: the test suite against the
# installed wheel, then the commands a new user runs
tools/check_wheel.sh python3 build/dist/*.whl "" /tmp/aep-check-min
tools/check_wheel.sh python3 build/dist/*.whl "test,gpu" /tmp/aep-check-full
tools/smoke_install.sh python3 build/dist/*.whl "" /tmp/aep-smoke-min
tools/smoke_install.sh python3 build/dist/*.whl "gpu,otel" /tmp/aep-smoke-gpu
tools/smoke_install.sh python3 build/dist/*.tar.gz "" /tmp/aep-smoke-sdist
```

Also run the full suite and, where the research runs are available, the
frozen-run check (`CONTRIBUTING.md`).

### 5. Publish to TestPyPI (needs approval; irreversible for this version)

Needs the one-time setup below. Run the workflow on the commit from step 2:

```bash
gh workflow run release.yml --ref main -f target=testpypi
gh run watch
```

If something is wrong after this upload, TestPyPI will not take `$V`
again. Fix it, and rehearse the fix on TestPyPI with a pre-release
version (for example `0.5.1rc1`), or go on to PyPI with a new version.

### 6. Install from TestPyPI in a fresh environment

```bash
python3 -m venv /tmp/aep-testpypi && cd /tmp/aep-testpypi
bin/pip install --index-url https://test.pypi.org/simple/ \
    --extra-index-url https://pypi.org/simple/ "agent-energy-profiler[gpu]==$V"
bin/agent-energy-profiler --version
bin/python -c 'from agent_energy_profiler import Session
with Session.profile("testpypi") as s:
    with s.span("example:operation"):
        sum(i * i for i in range(100_000))'
bin/agent-energy-profiler report "$(ls -d agent-energy-profiler-runs/testpypi-* | tail -n 1)" --html report.html
cd - >/dev/null
```

Check the TestPyPI project page as well: the README, its image and links,
the licence and the project links.

### 7. Publish to PyPI (needs approval; irreversible)

```bash
gh workflow run release.yml --ref main -f target=pypi
gh run watch                       # approve the `pypi` environment when asked
```

### 8. Tag and create the GitHub release

Tag the commit that was published. If Zenodo is enabled, creating the
release archives it and assigns a DOI.

```bash
git tag -a "v$V" -m "Agent Energy Profiler $V"
git push origin "v$V"
sed -n "/^## $V/,/^## /p" CHANGELOG.md | sed '1d;$d' > "/tmp/notes-$V.md"
gh release create "v$V" --title "$V" --notes-file "/tmp/notes-$V.md"
```

To attach the distributions to the release, use the ones the Release
workflow built (`gh run download RUN_ID -n dist`), not a local build.

### 9. Verify the public package

```bash
python3 -m venv /tmp/aep-pypi && /tmp/aep-pypi/bin/pip install "agent-energy-profiler==$V"
/tmp/aep-pypi/bin/agent-energy-profiler --version
/tmp/aep-pypi/bin/agent-energy-profiler validate
```

### 10. Confirm the Zenodo archive (if enabled)

1. On <https://zenodo.org/account/settings/github/>, the release appears
   under the repository with a DOI badge.
2. Open the record: check title, author, version, licence and files.
3. In a follow-up commit, add the **concept DOI** (it covers every version)
   to `CITATION.cff` as `doi:` and to the README. Do not add a DOI before
   Zenodo has assigned it.

## One-time setup (not done)

### PyPI trusted publishing

GitHub Actions uploads the files it built, with a short-lived token: no
long-lived API token exists anywhere.

1. On <https://test.pypi.org/manage/account/publishing/> and
   <https://pypi.org/manage/account/publishing/>, add a **pending
   publisher**: project `agent-energy-profiler`, owner `VeiledTee`,
   repository `agent-energy-profiler`, workflow `release.yml`, environment
   `testpypi` (on TestPyPI) or `pypi` (on PyPI).
2. In the repository, **Settings → Environments**: create `testpypi` and
   `pypi`, with yourself as a required reviewer for `pypi`.
3. Add `.github/workflows/release.yml` (not in the repository yet; it runs
   only when started by hand):

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
      - name: Build and check
        run: |
          python -m pip install build twine
          export SOURCE_DATE_EPOCH=$(git log -1 --format=%ct)
          python -m build
          python -m twine check --strict dist/*
          tools/smoke_install.sh python dist/*.whl "" "$RUNNER_TEMP/smoke"
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

Check the action versions when adding it. The `ci.yml` job pins have moved
before (see below).

### Zenodo archiving

1. Sign in at <https://zenodo.org> with your GitHub account.
2. **Account → GitHub**: sync, then switch on
   `VeiledTee/agent-energy-profiler`.
3. From then on, every **published GitHub release** is archived and gets a
   version DOI; all versions share one concept DOI. Zenodo builds the
   record's metadata from the repository's citation files, so keep
   `CITATION.cff` current, and check the first record by hand.
4. Enable it before step 8 if 0.5.0 should be archived; releases created
   earlier are not archived retroactively.

## Before every release

1. Set the version in `pyproject.toml`, `src/agent_energy_profiler/__init__.py`,
   `CITATION.cff` and the title of `docs/API.md` (`tests/test_release.py`
   checks they agree), and date the `CHANGELOG.md` entry.
2. The changelog says what was added, what was fixed and whether any
   measurement behaviour changed. A semantic change also goes into the
   `docs/PROVENANCE.md` change log.
3. Steps 1–4 above.

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

The 0.5.0 distributions were checked the same way: no home path, host
name, credential, GPU UUID or e-mail address (other than
`git@github.com` in this file). `tests/golden/` holds research-run
fixtures (public benchmark question ids, model names, timings and energy);
they are already public in the repository and are shipped in the sdist for
the tests.

## GitHub setup (done for 0.4.0)

1. Repository created at <https://github.com/new>: owner `VeiledTee`, name
   `agent-energy-profiler`, public, empty.
2. Private vulnerability reporting enabled (**Settings → Advanced
   Security**); the link in `SECURITY.md` (`/security/advisories/new`)
   depends on it.
3. First push of `main`, with `git config core.hooksPath scripts/git-hooks`.
4. Hosted CI: in the first run (`dbb937e`) the Python 3.9 jobs (`tests
   (Python 3.9)` and the wheel job) failed in `actions/setup-python` on the
   `ubuntu-latest` image, before any test ran; every other job passed.
   `d7e996a` pinned every job to `ubuntu-24.04` and moved to
   `actions/checkout@v7`, `actions/setup-python@v7` and
   `actions/upload-artifact@v6` (the Node.js 20 versions are deprecated);
   its run passed every job.

```bash
gh run list --workflow CI --limit 1
gh run view --log-failed        # if a job fails
```

## What a release does not establish

CI has no GPU and no RAPL. Hardware behaviour is validated separately
([HARDWARE_VALIDATION_PROTOCOL.md](HARDWARE_VALIDATION_PROTOCOL.md)), and
no value has been checked against an external power meter.
