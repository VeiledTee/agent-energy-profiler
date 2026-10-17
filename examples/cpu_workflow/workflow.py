"""Example 1: a CPU-only Python workflow, instrumented with spans.

Four stages of ordinary CPU work, each recorded as a span:

    data:generate   build a synthetic dataset
    compute:sort    sort it
    compute:primes  sieve primes
    io:serialize    JSON-encode and compress the results

Run under the profiler (recommended):

    agent-energy-profiler run --out-dir runs/cpu -- python examples/cpu_workflow/workflow.py

or on its own, then attribute and report:

    python examples/cpu_workflow/workflow.py --out cpu_spans.jsonl
    agent-energy-profiler report cpu_spans.jsonl

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
	ap.add_argument("--out", default="cpu_spans.jsonl",
	                help="spans file when not run under `agent-energy-profiler run`")
	ap.add_argument("--scale", type=float, default=1.0,
	                help="work multiplier (1.0 is a few seconds)")
	args = ap.parse_args()
	n = int(1_000_000 * args.scale)

	# Under `agent-energy-profiler run`, its measurement mode applies. On its own there is no
	# sampler, so per-span counter reads (direct) are the only GPU measurement.
	with Session.from_env(args.out, overwrite=True, default_mode="direct",
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
	print(f"spans written to {session.path}")


if __name__ == "__main__":
	main()
