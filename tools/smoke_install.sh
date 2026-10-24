#!/usr/bin/env bash
# Install a built distribution (wheel or sdist) into a fresh virtual
# environment and use it as a new user would: profile a few spans, then
# produce every report from the installed command. Complements
# tools/check_wheel.sh, which runs the test suite against a wheel. Used for
# release checks (docs/RELEASING.md); measures nothing about accuracy.
#
#   tools/smoke_install.sh PYTHON DIST EXTRAS WORKDIR
#
#   PYTHON   interpreter to create the venv with, e.g. python3.9
#   DIST     path to the .whl or .tar.gz
#   EXTRAS   comma-separated extras ("" for the bare package)
#   WORKDIR  scratch directory; replaced. Use one outside the repository, so
#            that nothing can be imported from the source tree.
set -euo pipefail

if [ "$#" -ne 4 ]; then
	echo "usage: $0 PYTHON DIST EXTRAS WORKDIR" >&2
	exit 2
fi
python=$1
dist=$(realpath "$2")
extras=$3
work=$(realpath -m "$4")

rm -rf "$work"
mkdir -p "$work"
"$python" -m venv "$work/venv"
py="$work/venv/bin/python"
aep="$work/venv/bin/agent-energy-profiler"
"$py" -m pip install -q --upgrade pip
spec=$dist
if [ -n "$extras" ]; then
	spec="$dist[$extras]"
fi
"$py" -m pip install -q "$spec"
cd "$work"

step() { printf '\n== %s\n' "$*"; }
fail() { echo "FAIL: $*" >&2; exit 1; }
# expect CODE CMD...: the command exits with CODE
expect() {
	local want=$1 got=0
	shift
	"$@" >"$work/out.txt" 2>&1 || got=$?
	[ "$got" -eq "$want" ] || { cat "$work/out.txt"; fail "exit $got, expected $want: $*"; }
}

step "installed package"
"$py" - <<'EOF'
import agent_energy_profiler as aep
assert "site-packages" in aep.__file__, aep.__file__
print("agent_energy_profiler", aep.__version__, "from", aep.__file__)
EOF
"$aep" --version
expect 0 "$aep" --help
expect 0 "$py" -m agent_energy_profiler --help
for cmd in validate sample run attribute report export otel-export; do
	expect 0 "$aep" "$cmd" --help
done

step "validate"
expect 0 "$aep" validate
head -n 12 "$work/out.txt"

step "Session.profile (README example)"
"$py" - <<'EOF'
from agent_energy_profiler import Session

with Session.profile("release-smoke-test") as session:
    with session.span("example:operation"):
        sum(i * i for i in range(100_000))
assert session.profile_status == "complete", (session.profile_status, session.profile_error)
assert session.report is not None
print("run directory:", session.out_dir)
EOF
run=$(ls -d agent-energy-profiler-runs/release-smoke-test-* | tail -n 1)
[ -d "$run" ] || fail "no run directory"
for f in report.json report.txt spans_attributed.jsonl session_summary.json; do
	[ -s "$run/$f" ] || fail "missing $run/$f"
done

step "nested and failing spans"
"$py" - <<'EOF'
import asyncio
from agent_energy_profiler import Session

async def work(session):
    async with session.span("agent:answer"):
        async with session.span("retrieval:search"):
            sum(i for i in range(50_000))
        with session.span("llm:generate") as s:
            sum(i * i for i in range(200_000))
            s.set_tokens(input=10, output=5)

try:
    with Session.profile("release-smoke-nested") as session:
        asyncio.run(work(session))
        with session.span("tool:fails"):
            raise RuntimeError("expected failure")
except RuntimeError as e:
    assert str(e) == "expected failure"
else:
    raise AssertionError("the workload's exception was hidden")
assert session.profile_status == "complete", session.profile_status
EOF
nested=$(ls -d agent-energy-profiler-runs/release-smoke-nested-* | tail -n 1)

step "the same program under agent-energy-profiler run"
cat >"$work/app.py" <<'EOF'
from agent_energy_profiler import Session

with Session.profile("under-run") as session:
    with session.span("example:operation"):
        sum(i * i for i in range(100_000))
EOF
expect 0 "$aep" run --out-dir "$work/under-run" -- "$py" "$work/app.py"
[ -s "$work/under-run/run.json" ] || fail "run.json"
expect 2 "$aep" run --out-dir "$work/under-run" -- "$py" "$work/app.py"
grep -q "already exists" "$work/out.txt" || fail "an existing run was not refused"

step "reports from the command line ($nested)"
expect 0 "$aep" report "$nested" --brief
grep -q "ENERGY SUMMARY" "$work/out.txt" || fail "brief summary"
expect 0 "$aep" report "$nested"
expect 0 "$aep" report "$nested" --format json
"$py" -c 'import json,sys; json.load(open(sys.argv[1]))' "$work/out.txt"
expect 0 "$aep" report "$nested" --format csv
head -n 1 "$work/out.txt"
expect 0 "$aep" export "$nested" --out-dir "$work/export"
for f in spans.csv operations.csv report.json; do
	[ -s "$work/export/$f" ] || fail "missing export/$f"
done
expect 0 "$aep" report "$nested" --html "$work/report.html"
expect 0 "$aep" report "$nested" --html "$work/report-redacted.html" --redact-labels

step "HTML report is self-contained"
"$py" - "$work/report.html" "$work/report-redacted.html" <<'EOF'
import re, sys
page = open(sys.argv[1], encoding="utf-8").read()
assert page.lstrip().lower().startswith("<!doctype html>")
assert "Content-Security-Policy" in page
# nothing is fetched: no URL, no external src or href
assert not re.search(r"https?://", page), re.search(r".{40}https?://.{40}", page).group(0)
assert not re.search(r"""\b(?:src|href)\s*=""", page)
assert "agent:answer" in page and "llm:generate" in page
redacted = open(sys.argv[2], encoding="utf-8").read()
assert "llm:generate" not in redacted and "operation 1" in redacted
print(f"report.html: {len(page)} bytes, no network references")
EOF

if "$py" -c "import opentelemetry.sdk" 2>/dev/null; then
	step "OpenTelemetry export"
	expect 0 "$aep" otel-export "$nested" --out "$work/otel.jsonl"
	n=$(wc -l <"$work/otel.jsonl")
	[ "$n" -ge 4 ] || fail "otel-export wrote $n spans"
	echo "otel-export: $n spans"
fi

step "invalid input is refused with exit code 2"
expect 2 "$aep" no-such-command
expect 2 "$aep" report "$work/does-not-exist"
expect 2 "$aep" report "$nested" --brief --format json
expect 2 "$aep" report --html

step "no sampler left running"
if pgrep -f "$work/venv/bin/python -m agent_energy_profiler.sampling" >/dev/null; then
	pgrep -af "$work/venv/bin/python -m agent_energy_profiler.sampling" >&2
	fail "a sampler process is still running"
fi
echo "none"

printf '\nsmoke test passed: %s [%s] on %s\n' "$(basename "$dist")" "$extras" "$("$py" --version)"
