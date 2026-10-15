"""Build tests/golden_v2/<case>: a recorded v2 session and its M2 attribution.

The INPUT is recorded with this repository's Session (synthetic GPU counter,
fixed RNG seed for the sampled timeline). The EXPECTED output is produced by
a REFERENCE source tree given with --reference-src (for the committed case:
commit baf6108, Milestone 2, extracted with
`git archive baf6108 src | tar -x -C build/m2`). tests/test_golden_v2.py then
requires the current attribution to reproduce it byte for byte.

Usage:
  .venv/bin/python tools/build_v2_fixture.py --reference-src build/m2/src
"""

import argparse
import asyncio
import gzip
import hashlib
import json
import os
import random
import subprocess
import sys
import tempfile
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

from agent_energy_profiler import Session, Resolution  # noqa: E402
from agent_energy_profiler.measurement import CallableReader  # noqa: E402

CASE = "session_mixed"


def record(path):
	state = {"v": 5_000_000, "n": 0}
	lock = threading.Lock()

	def read():
		with lock:
			state["n"] += 1
			if state["n"] in (37, 38):
				return [None]  # a failed read -> read_failed
			state["v"] += 40
			return [state["v"]]

	with Session(path, gpu=CallableReader(read, devices=[{"index": 0, "name": "synthetic"}]),
	             resolution=Resolution.configured(0.1022, "synthetic fixture"),
	             attributes={"run_id": "fixture"}, overwrite=True) as s:
		for q in range(3):
			with s.span("agent:question", attributes={"question_id": f"q{q}"}):
				with s.span("tool:retrieve"):
					pass
				with s.span("llm:reason") as h:
					h.set_tokens(input=100 + q, output=10)
					with s.span("tool:lookup"):
						pass
				try:
					with s.span("tool:verify"):
						if q == 1:
							raise ValueError("verification failed")
				except ValueError:
					pass

		async def fanout():
			async def worker(name, delay):
				async with s.span(name):
					await asyncio.sleep(delay)
			async with s.span("agent:parallel"):
				await asyncio.gather(worker("tool:a", 0.02), worker("tool:b", 0.01))
			async with s.span("llm:cancelled"):
				task = asyncio.ensure_future(asyncio.sleep(10))
				await asyncio.sleep(0)
				task.cancel()
				try:
					await task
				except asyncio.CancelledError:
					pass
		asyncio.run(fanout())

		def in_thread():
			with s.span("tool:thread"):
				pass
		t = threading.Thread(target=in_thread)
		t.start()
		t.join()


def write_power(spans_path, power_path):
	with open(spans_path) as f:
		spans = [json.loads(l) for l in f if '"record_type": "span"' in l]
	t0 = min(s["start_unix_s"] for s in spans) - 0.5
	t1 = max(s["end_unix_s"] for s in spans) + 0.5
	rng = random.Random(1015)
	rows = ["t,gpu0_w,gpu0_energy_mj,rapl_package-0_intel-rapl:0_uj,"
	        "rapl_core_intel-rapl:0:0_uj,rapl_dram_intel-rapl:0:2_uj"]
	g = pkg = core = dram = 0
	n = 40
	for i in range(n + 1):
		t = t0 + (t1 - t0) * i / n
		g += rng.randint(500, 1500)
		pkg += rng.randint(1_000_000, 3_000_000)
		core += rng.randint(500_000, 900_000)
		dram += rng.randint(100_000, 300_000)
		rows.append(f"{t:.6f},{rng.uniform(50, 200):.2f},{g},{pkg},{core},{dram}")
	with open(power_path, "w") as f:
		f.write("\n".join(rows) + "\n")


def gz(path, data):
	with open(path, "wb") as raw:
		with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as z:
			z.write(data)


def main():
	ap = argparse.ArgumentParser()
	ap.add_argument("--reference-src", required=True)
	args = ap.parse_args()
	out = os.path.join(ROOT, "tests", "golden_v2", CASE)
	os.makedirs(os.path.join(out, "input"), exist_ok=True)
	os.makedirs(os.path.join(out, "expected"), exist_ok=True)
	with tempfile.TemporaryDirectory() as d:
		spans, power = os.path.join(d, "spans.jsonl"), os.path.join(d, "power.csv")
		record(spans)
		write_power(spans, power)
		env = dict(os.environ, PYTHONPATH=os.path.abspath(args.reference_src),
		           PYTHONDONTWRITEBYTECODE="1")
		res = subprocess.run([sys.executable, "-m", "agent_energy_profiler.attribution_v2",
		                      "--spans", spans, "--power", power,
		                      "--out-dir", os.path.join(d, "out")],
		                     capture_output=True, text=True, env=env)
		if res.returncode != 0:
			raise SystemExit(res.stderr)
		which = subprocess.run([sys.executable, "-c",
		                        "import agent_energy_profiler as a; print(a.__file__)"],
		                       capture_output=True, text=True, env=env).stdout.strip()
		assert os.path.abspath(args.reference_src) in which, which
		hashes = {}
		for name, src in (("input/spans.jsonl", spans), ("input/power.csv", power),
		                  ("expected/spans_attributed.jsonl",
		                   os.path.join(d, "out", "spans_attributed.jsonl")),
		                  ("expected/session_summary.json",
		                   os.path.join(d, "out", "session_summary.json"))):
			with open(src, "rb") as f:
				data = f.read()
			if name.endswith("session_summary.json"):
				# absolute temp paths are not part of the result
				summary = json.loads(data)
				summary["input"] = None
				summary["timeline"]["power_file"] = None
				data = (json.dumps(summary, indent=2, sort_keys=True) + "\n").encode()
			gz(os.path.join(out, name + ".gz"), data)
			hashes[name] = hashlib.sha256(data).hexdigest()
	with open(os.path.join(out, "manifest.json"), "w") as f:
		json.dump({"case": CASE, "reference_src": "commit baf6108 (Milestone 2)",
		           "sha256": hashes,
		           "note": "session_summary.json stored with input/power_file paths "
		                   "nulled and keys sorted"}, f, indent=2, sort_keys=True)
		f.write("\n")
	print("written", out)


if __name__ == "__main__":
	main()
