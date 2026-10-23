"""Example 1: a CPU-only Python workflow, instrumented with spans.

Four stages of ordinary CPU work, each recorded as a span:

    data:generate   build a synthetic dataset
    compute:sort    sort it
    compute:primes  sieve primes
    io:serialize    JSON-encode and compress the results

Run it on its own (the report is printed to stderr at the end):

    python examples/cpu_workflow/workflow.py

or under the profiler, which also records whole-program energy:

    agent-energy-profiler run --out-dir runs/cpu -- python examples/cpu_workflow/workflow.py

Needs only the core package (no GPU, no network).
"""

import argparse
import json
import random
import zlib

from agent_energy_profiler import Session


def sieve(limit):
	flags = bytearray([1]) * (limit + 1)
	flags[0:2] = b"\x00\x00"
	for i in range(2, int(limit ** 0.5) + 1):
		if flags[i]:
			flags[i * i::i] = bytearray(len(flags[i * i::i]))
	return [i for i, f in enumerate(flags) if f]


def main():
	ap = argparse.ArgumentParser(description=__doc__,
	                             formatter_class=argparse.RawDescriptionHelpFormatter)
	ap.add_argument("--out-dir", default=None,
	                help="run directory when run on its own (default: a new "
	                     "agent-energy-profiler-runs/cpu_workflow-<timestamp>/)")
	ap.add_argument("--scale", type=float, default=1.0,
	                help="work multiplier (1.0 is a few seconds)")
	args = ap.parse_args()
	n = int(1_000_000 * args.scale)

	# On its own: a sampler, the default (conservative) mode and a report at the
	# end. Under `agent-energy-profiler run`: run's sampler, mode and report.
	with Session.profile("cpu_workflow", out_dir=args.out_dir,
	                     attributes={"example": "cpu_workflow", "scale": args.scale}) as session:
		with session.span("pipeline:run"):
			with session.span("data:generate") as s:
				rng = random.Random(42)
				data = [rng.random() for _ in range(n)]
				s.set_attribute("n_items", n)
			with session.span("compute:sort"):
				data.sort()
			with session.span("compute:primes") as s:
				primes = sieve(n * 4)
				s.set_attribute("n_primes", len(primes))
			with session.span("io:serialize") as s:
				blob = zlib.compress(json.dumps({"head": data[:1000], "primes": primes[:5000]})
				                     .encode(), 6)
				s.set_attribute("bytes", len(blob))
	print(f"done: {n} items sorted, {len(primes)} primes, {len(blob)} bytes compressed")
	print(f"run directory: {session.out_dir}")


if __name__ == "__main__":
	main()
