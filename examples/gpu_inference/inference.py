"""Example 2: GPU-backed model inference, instrumented with spans.

    model:load      build (or download) the model and move it to the GPU
    llm:warmup      first forward pass (CUDA kernels compile/cache here)
    llm:generate    one span per prompt: autoregressive decoding

Two model choices, both free and local:

  default            a synthetic decoder (stacked transformer layers with
                     random weights) built with torch.nn. No download; it
                     exercises the GPU like a small LLM without producing
                     meaningful text.
  --hf-model NAME    a Hugging Face causal LM such as `distilgpt2` (needs the
                     `transformers` package and a one-time download).

GPU work is asynchronous: kernels are queued and run later. Every span calls
torch.cuda.synchronize() before it closes, so its energy window covers the
work it launched. Without that, energy leaks into whichever span is open when
the GPU actually runs the kernels.

    pip install torch            # a CUDA build for your driver
    aep validate --json hw.json  # characterise the GPU counter once
    aep run --resolution-report hw.json --out-dir runs/gpu -- \
        python examples/gpu_inference/inference.py
"""

import argparse
import sys
import time

from agent_energy_profiler import Session


def synthetic_decoder(torch, layers, width, vocab):
	nn = torch.nn

	class Decoder(nn.Module):
		def __init__(self):
			super().__init__()
			self.embed = nn.Embedding(vocab, width)
			layer = nn.TransformerEncoderLayer(width, nhead=max(1, width // 64),
			                                   dim_feedforward=4 * width,
			                                   batch_first=True)
			self.blocks = nn.TransformerEncoder(layer, layers)
			self.head = nn.Linear(width, vocab)

		def forward(self, ids):
			mask = nn.Transformer.generate_square_subsequent_mask(ids.shape[1]).to(ids.device)
			return self.head(self.blocks(self.embed(ids), mask=mask, is_causal=True))

	return Decoder()


def generate_synthetic(torch, model, prompt_len, new_tokens, vocab, device, seed):
	g = torch.Generator(device="cpu").manual_seed(seed)
	ids = torch.randint(0, vocab, (1, prompt_len), generator=g).to(device)
	with torch.no_grad():
		for _ in range(new_tokens):
			logits = model(ids)[:, -1, :]
			ids = torch.cat([ids, logits.argmax(-1, keepdim=True)], dim=1)
	return prompt_len, new_tokens


def main():
	ap = argparse.ArgumentParser(description=__doc__,
	                             formatter_class=argparse.RawDescriptionHelpFormatter)
	ap.add_argument("--out", default="gpu_spans.jsonl")
	ap.add_argument("--hf-model", default="", help="e.g. distilgpt2 (optional)")
	ap.add_argument("--prompts", type=int, default=8)
	ap.add_argument("--new-tokens", type=int, default=64)
	ap.add_argument("--layers", type=int, default=12)
	ap.add_argument("--width", type=int, default=1024)
	ap.add_argument("--device", default="cuda")
	args = ap.parse_args()

	try:
		import torch
	except ImportError:
		sys.exit("this example needs PyTorch: pip install torch (a CUDA build)")
	if args.device == "cuda" and not torch.cuda.is_available():
		sys.exit("no CUDA device visible to PyTorch. Use --device cpu to run anyway: "
		         "the GPU then only idles, and its energy is still recorded "
		         "(device-level), which is itself instructive.")
	device = torch.device(args.device)

	def sync():
		if device.type == "cuda":
			torch.cuda.synchronize()

	vocab = 32000
	attrs = {"example": "gpu_inference", "model": args.hf_model or
	         f"synthetic-{args.layers}x{args.width}", "device": str(device),
	         "torch": torch.__version__}
	if device.type == "cuda":
		attrs["gpu"] = torch.cuda.get_device_name(device)

	with Session.from_env(args.out, overwrite=True, attributes=attrs) as session:
		with session.span("model:load") as s:
			if args.hf_model:
				from transformers import AutoModelForCausalLM, AutoTokenizer
				tok = AutoTokenizer.from_pretrained(args.hf_model)
				model = AutoModelForCausalLM.from_pretrained(args.hf_model).to(device).eval()
			else:
				torch.manual_seed(0)
				model = synthetic_decoder(torch, args.layers, args.width, vocab).to(device).eval()
			sync()
			s.set_attribute("parameters", sum(p.numel() for p in model.parameters()))

		def run(i):
			if args.hf_model:
				ids = tok(f"Question {i}: what is energy efficiency?", return_tensors="pt").to(device)
				with torch.no_grad():
					out = model.generate(**ids, max_new_tokens=args.new_tokens, do_sample=False,
					                     pad_token_id=tok.eos_token_id)
				return ids["input_ids"].shape[1], out.shape[1] - ids["input_ids"].shape[1]
			return generate_synthetic(torch, model, 32, args.new_tokens, vocab, device, i)

		with session.span("llm:warmup"):
			run(0)
			sync()
		for i in range(args.prompts):
			with session.span("llm:generate", attributes={"prompt": i}) as s:
				t0 = time.perf_counter()
				n_in, n_out = run(i + 1)
				sync()  # close the span only after the GPU has finished
				s.set_tokens(input=n_in, output=n_out)
				s.set_attribute("tokens_per_s", n_out / (time.perf_counter() - t0))
	print(f"generated {args.prompts} x {args.new_tokens} tokens on {device}")
	print(f"spans written to {session.path}")


if __name__ == "__main__":
	main()
