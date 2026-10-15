"""Reproducible GPU workloads for the NVML counter experiment.

    steady   back-to-back float32 matrix multiplies (size --n), synchronised
             after each one: a near-constant, moderate-to-high board power
    pulsed   the same work for --on seconds, then idle for --off seconds,
             repeating: board power alternates on a known schedule

Runs until SIGTERM/SIGINT. Prints "ready" once the GPU is busy. Needs PyTorch
with CUDA. This is ordinary compute within the board's own power limit; the
experiment driver also aborts if the GPU temperature passes its limit.
"""

import argparse
import signal
import sys
import time


def main():
	ap = argparse.ArgumentParser()
	ap.add_argument("mode", choices=("steady", "pulsed"))
	ap.add_argument("--n", type=int, default=4096)
	ap.add_argument("--on", type=float, default=0.3)
	ap.add_argument("--off", type=float, default=0.3)
	args = ap.parse_args()

	import torch
	if not torch.cuda.is_available():
		sys.exit("workload needs a CUDA device")
	torch.backends.cuda.matmul.allow_tf32 = False
	torch.manual_seed(0)
	a = torch.randn(args.n, args.n, device="cuda")
	b = torch.randn(args.n, args.n, device="cuda")
	stop = {"flag": False}
	signal.signal(signal.SIGTERM, lambda *_: stop.update(flag=True))
	signal.signal(signal.SIGINT, lambda *_: stop.update(flag=True))

	def burn(seconds):
		end = time.perf_counter() + seconds
		while time.perf_counter() < end and not stop["flag"]:
			torch.mm(a, b)
			torch.cuda.synchronize()

	burn(0.5)
	print("ready", flush=True)
	while not stop["flag"]:
		if args.mode == "steady":
			burn(1.0)
		else:
			burn(args.on)
			end = time.perf_counter() + args.off
			while time.perf_counter() < end and not stop["flag"]:
				time.sleep(0.01)


if __name__ == "__main__":
	main()
