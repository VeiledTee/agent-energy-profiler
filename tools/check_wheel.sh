#!/usr/bin/env bash
# Install a built wheel into a fresh virtual environment and run a copy of
# tests/ against the installed package (never against src/). Used by CI and
# for local release checks.
#
#   tools/check_wheel.sh PYTHON WHEEL EXTRAS WORKDIR
#
#   PYTHON   interpreter to create the venv with, e.g. python3.9
#   WHEEL    path to the .whl
#   EXTRAS   comma-separated extras ("" for the bare package plus pytest)
#   WORKDIR  scratch directory; replaced. Keep it outside src/.
#
# Tests needing an optional dependency that is not installed are skipped, so
# the minimal install checks that the core needs nothing beyond the standard
# library. Run with TMPDIR outside any git working tree (see README, Test).
set -euo pipefail

if [ "$#" -ne 4 ]; then
	echo "usage: $0 PYTHON WHEEL EXTRAS WORKDIR" >&2
	exit 2
fi
python=$1
wheel=$(realpath "$2")
extras=$3
work=$(realpath -m "$4")
repo=$(cd "$(dirname "$0")/.." && pwd)

rm -rf "$work"
mkdir -p "$work"
"$python" -m venv "$work/venv"
pip=("$work/venv/bin/python" -m pip)
"${pip[@]}" install -q --upgrade pip
spec=$wheel
if [ -n "$extras" ]; then
	spec="$wheel[$extras]"
fi
"${pip[@]}" install -q "$spec" "pytest>=8"
# tests/ plus what some tests read beside it: examples/ (run under `aep
# run`) and src/ (scanned as text by test_package_boundary). The package
# itself is still imported from site-packages, which
# test_golden_equivalence checks under AEP_TEST_INSTALLED=1.
cp -r "$repo/tests" "$repo/examples" "$repo/src" "$work/"
find "$work" -name __pycache__ -prune -exec rm -rf {} +

cd "$work"
"$work/venv/bin/python" - <<'EOF'
import agent_energy_profiler as aep
assert "site-packages" in aep.__file__, aep.__file__
print("installed", aep.__version__, "from", aep.__file__)
EOF
"$work/venv/bin/aep" --version
AEP_TEST_INSTALLED=1 "$work/venv/bin/python" -m pytest -q -p no:cacheprovider tests
