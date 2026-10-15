"""Statistics (and an optional figure) for experiment.py results.

    .venv/bin/python tools/nvml_diagnostics/analyze.py results/nvml_m4/idle \
        results/nvml_m4/steady results/nvml_m4/pulsed --out results/nvml_m4

Per trial, the counter rate is (E_end - E_start) / window, from the two
endpoint reads that every condition takes identically. Within each block it is
divided by that block's `endpoints` trial (no reads in between): `relative` is
how much of the undisturbed counter energy a condition retains. Means carry a
95% t-interval over blocks.

Power is never treated as ground truth. Where a condition read power, the
report gives counter / integrated-power for those same trials, keeping the two
instruments separate.
"""

import argparse
import json
import math
import os
import statistics
from collections import defaultdict

#: two-sided 95% t quantiles by degrees of freedom
T95 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365,
       8: 2.306, 9: 2.262, 10: 2.228, 15: 2.131, 20: 2.086, 30: 2.042}


def t95(df):
	if df <= 0:
		return math.nan
	keys = [k for k in T95 if k <= df]
	return T95[max(keys)] if keys else T95[1]


def ci(values):
	values = [v for v in values if v is not None and not math.isnan(v)]
	if not values:
		return None, None, 0
	mean = statistics.fmean(values)
	if len(values) < 2:
		return mean, None, len(values)
	half = t95(len(values) - 1) * statistics.stdev(values) / math.sqrt(len(values))
	return mean, half, len(values)


def load(run_dir):
	with open(os.path.join(run_dir, "meta.json")) as f:
		meta = json.load(f)
	with open(os.path.join(run_dir, "trials.jsonl")) as f:
		trials = [json.loads(line) for line in f if line.strip()]
	return meta, trials


def counter_w(t):
	return (t["energy_end_mj"] - t["energy_start_mj"]) / 1000.0 / t["window_s"]


def reads_per_s(t):
	return sum(len(v) for v in t["reads"].values()) / t["window_s"]


def power_stats(t):
	"""(mean W, trapezoid-integrated W) of the power reads in a trial, or None."""
	for kind in ("power_avg", "power_instant"):
		pts = [((a + b) / 2, v / 1000.0) for a, b, v in t["reads"].get(kind, []) if v is not None]
		if len(pts) >= 2:
			mean = statistics.fmean(p for _, p in pts)
			# integrate over the read span, extend the end values to the window edges
			e = sum(0.5 * (pts[i][1] + pts[i + 1][1]) * (pts[i + 1][0] - pts[i][0])
			        for i in range(len(pts) - 1))
			e += pts[0][1] * pts[0][0] + pts[-1][1] * (t["window_s"] - pts[-1][0])
			return kind, mean, e / t["window_s"]
	return None


def counter_steps(t):
	"""Observed counter updates in an energy-polled trial."""
	reads = [(b, v) for a, b, v in t["reads"].get("energy", []) + t["reads"].get("energy_field", [])
	         if v is not None]
	reads.sort()
	changes = []
	prev = t["energy_start_mj"]
	for when, value in reads:
		if value != prev:
			changes.append((when, value - prev))
			prev = value
	if len(changes) < 3:
		return None
	intervals = [changes[i + 1][0] - changes[i][0] for i in range(len(changes) - 1)]
	steps = [s for _, s in changes[1:]]
	return {"n_updates": len(changes), "median_interval_ms": statistics.median(intervals) * 1000,
	        "median_step_mj": statistics.median(steps),
	        "step_power_w": statistics.median(s / i for s, i in zip(steps, intervals) if i > 0) / 1000}


def analyze(run_dir):
	meta, trials = load(run_dir)
	workload = meta["args"]["workload"]
	baseline, base_state = {}, {}
	for t in trials:
		if t["condition"] == "endpoints":
			baseline[t["block"]] = counter_w(t)
			base_state[t["block"]] = t["state_after"].get("pstate")
	by_cond = defaultdict(list)
	for t in trials:
		base = baseline.get(t["block"])
		t["_counter_w"] = counter_w(t)
		t["_relative"] = t["_counter_w"] / base if base else None
		t["_reads_per_s"] = reads_per_s(t)
		by_cond[t["condition"]].append(t)

	conditions = {}
	for cond, ts in by_cond.items():
		rel_mean, rel_half, n = ci([t["_relative"] for t in ts])
		w_mean, w_half, _ = ci([t["_counter_w"] for t in ts])
		rels = [t["_relative"] for t in ts if t["_relative"] is not None]
		row = {
			"n_trials": n,
			"read_streams": meta["conditions"][cond],
			"reads_per_s": statistics.fmean(t["_reads_per_s"] for t in ts),
			"counter_w_mean": w_mean, "counter_w_ci95": w_half,
			"relative_to_endpoints_mean": rel_mean, "relative_ci95": rel_half,
			"relative_median": statistics.median(rels) if rels else None,
			"relative_per_block": [round(t["_relative"], 4) if t["_relative"] else None for t in ts],
			"state_changed_per_block": [t["state_after"].get("pstate") != base_state.get(t["block"])
			                            for t in ts],
			# trials whose GPU ended in a different performance state than the
			# block's endpoints trial: the workload itself changed (e.g. host
			# activity woke an idle GPU), so the comparison is confounded
			"n_state_changed": sum(1 for t in ts
			                       if t["state_after"].get("pstate") != base_state.get(t["block"])),
		}
		ps = [power_stats(t) for t in ts]
		if all(p is not None for p in ps):
			row["power_kind"] = ps[0][0]
			row["power_mean_w"] = statistics.fmean(p[1] for p in ps)
			ratios = [t["_counter_w"] / p[2] for t, p in zip(ts, ps)]
			row["counter_over_integrated_power_mean"], row["counter_over_power_ci95"], _ = ci(ratios)
		steps = [counter_steps(t) for t in ts]
		steps = [s for s in steps if s]
		if steps:
			row["counter_updates"] = {
				"median_interval_ms": statistics.median(s["median_interval_ms"] for s in steps),
				"median_step_mj": statistics.median(s["median_step_mj"] for s in steps),
				"step_power_w": statistics.median(s["step_power_w"] for s in steps)}
		lat = defaultdict(list)
		for t in ts:
			for kind, reads in t["reads"].items():
				lat[kind] += [b - a for a, b, _ in reads]
		row["read_latency_ms_median"] = {k: statistics.median(v) * 1000 for k, v in lat.items() if v}
		conditions[cond] = row

	# carry-over: is an endpoints trial lower right after a fast-polling trial?
	carry = {"after_fast_energy_polling": [], "after_other": []}
	ordered = sorted(trials, key=lambda t: (t["block"], t["position"]))
	for prev, cur in zip(ordered, ordered[1:]):
		if cur["condition"] != "endpoints" or prev["block"] != cur["block"]:
			continue
		fast = any(k in ("energy", "energy_field") and p <= 0.02
		           for k, p in meta["conditions"][prev["condition"]])
		carry["after_fast_energy_polling" if fast else "after_other"].append(cur["_counter_w"])
	temps = [t["state_after"].get("temperature_c") for t in trials
	         if isinstance(t["state_after"].get("temperature_c"), int)]
	return {
		"workload": workload,
		"gpu": meta["gpu"], "host": meta["host"], "args": meta["args"],
		"aborted": meta.get("aborted"),
		"endpoints_counter_w_per_block": [round(baseline[b], 3) for b in sorted(baseline)],
		"temperature_c_range": [min(temps), max(temps)] if temps else None,
		"carry_over_endpoints_w": {k: [round(v, 3) for v in vs] for k, vs in carry.items()},
		"conditions": dict(sorted(conditions.items(),
		                          key=lambda kv: (kv[1]["reads_per_s"], kv[0]))),
	}


def to_markdown(results):
	L = []
	for r in results:
		L.append(f"### {r['workload']}  ({r['gpu'].get('name')}, driver "
		         f"{r['gpu'].get('driver_version')}, {r['args']['reps']} blocks x "
		         f"{r['args']['duration']} s, temperature {r['temperature_c_range']} C)")
		L.append("")
		L.append("| condition | reads/s | counter W (95% CI) | retained vs endpoints, mean (95% CI) "
		         "| median | power W | counter / integrated power | counter updates | P-state changed |")
		L.append("|---|---|---|---|---|---|---|---|---|")
		def fmt(m, h, p=3):
			if m is None:
				return "n/a"
			return f"{m:.{p}f}" + (f" ± {h:.{p}f}" if h is not None else "")

		for cond, row in r["conditions"].items():
			upd = row.get("counter_updates")
			power = f"{row['power_mean_w']:.2f}" if "power_mean_w" in row else ""
			ratio = (fmt(row["counter_over_integrated_power_mean"], row["counter_over_power_ci95"])
			         if "power_mean_w" in row else "")
			updates = (f"every {upd['median_interval_ms']:.1f} ms, {upd['median_step_mj']:.0f} mJ"
			           if upd else "")
			L.append(f"| {cond} | {row['reads_per_s']:.1f} "
			         f"| {fmt(row['counter_w_mean'], row['counter_w_ci95'], 2)} "
			         f"| {fmt(row['relative_to_endpoints_mean'], row['relative_ci95'])} "
			         f"| {fmt(row['relative_median'], None)} "
			         f"| {power} | {ratio} | {updates} | {row['n_state_changed'] or ''} |")
		c = r["carry_over_endpoints_w"]
		L.append("")
		L.append(f"Endpoints-only counter W per block: {r['endpoints_counter_w_per_block']}. "
		         f"Carry-over check, endpoints trials right after >= 50 Hz energy polling: "
		         f"{c['after_fast_energy_polling']}; after anything else: {c['after_other']}.")
		L.append("")
	return "\n".join(L)


def figure(results, path):
	try:
		import matplotlib
		matplotlib.use("Agg")
		import matplotlib.pyplot as plt
	except ImportError:
		return False
	# reference palette, categorical slots 1-3 (validated all-pairs), light
	# mode; the two power APIs share a hue and differ by marker shape
	series = {
		"energy counter read": ("#2a78d6", "o"),
		"power read: GetPowerUsage (averaged)": ("#eb6834", "o"),
		"power read: POWER_INSTANT field": ("#eb6834", "^"),
		"unrelated query (temperature)": ("#1baf7a", "s"),
	}
	ink, muted, surface = "#0b0b0b", "#52514e", "#fcfcfb"

	def family(streams):
		kinds = {k for k, _ in streams}
		if kinds & {"energy", "energy_field"}:
			return "energy counter read"
		if "power_instant" in kinds:
			return "power read: POWER_INSTANT field"
		if "power_avg" in kinds:
			return "power read: GetPowerUsage (averaged)"
		if kinds:
			return "unrelated query (temperature)"
		return None

	# small horizontal offsets so series sharing a read rate stay visible
	offset = {"energy counter read": 0.78, "power read: POWER_INSTANT field": 0.93,
	          "unrelated query (temperature)": 1.10, "power read: GetPowerUsage (averaged)": 1.30}
	changed_label = "hollow: GPU performance state changed during the trial (confounded)"
	fig, axes = plt.subplots(1, len(results), figsize=(4.2 * len(results), 3.8),
	                         sharey=True, facecolor=surface)
	if len(results) == 1:
		axes = [axes]
	top = 1.0
	for ax, r in zip(axes, results):
		ax.set_facecolor(surface)
		for cond, row in r["conditions"].items():
			fam = family(row["read_streams"])
			if fam is None:
				continue
			color, marker = series[fam]
			x = row["reads_per_s"] * offset[fam]
			for rel, changed in zip(row["relative_per_block"], row["state_changed_per_block"]):
				if rel is None:
					continue
				top = max(top, rel)
				size = 52 if marker == "^" else 34
				if changed:
					ax.scatter(x, rel, s=size, facecolor="none", edgecolor=color, marker=marker,
					           linewidth=1.2, zorder=3, label=changed_label)
				else:
					ax.scatter(x, rel, s=size, color=color, marker=marker,
					           edgecolor=surface, linewidth=1.2, zorder=3, label=fam)
		ax.axhline(1.0, color=muted, linewidth=1, linestyle=(0, (4, 3)), zorder=1)
		ax.text(0.02, 1.02, "no reads between endpoints", color=muted, fontsize=8,
		        va="bottom", ha="left", transform=ax.get_yaxis_transform())
		ax.set_xscale("log")
		ax.set_xlim(0.5, 3000)
		ax.set_ylim(0, max(1.2, top + 0.08))
		ax.set_title(f"{r['workload']}", color=ink, fontsize=11, loc="left")
		ax.set_xlabel("NVML reads per second during the window", color=muted, fontsize=9)
		ax.grid(axis="y", color="#e4e3df", linewidth=0.8)
		for side in ("top", "right"):
			ax.spines[side].set_visible(False)
		for side in ("left", "bottom"):
			ax.spines[side].set_color("#c9c8c2")
		ax.tick_params(colors=muted, labelsize=8)
	axes[0].set_ylabel("counter energy retained\n(÷ endpoints-only trial, same block)", color=muted, fontsize=9)
	uniq = {}
	for ax in axes:
		for h, l in zip(*ax.get_legend_handles_labels()):
			uniq.setdefault(l, h)
	fig.tight_layout(rect=(0, 0, 1, 0.80))
	fig.suptitle("Some NVML reads lower the energy the cumulative counter reports",
	             x=0.01, y=0.99, ha="left", va="top", color=ink, fontsize=12)
	fig.legend(uniq.values(), uniq.keys(), loc="upper left", bbox_to_anchor=(0.01, 0.92),
	           ncol=2, frameon=False, fontsize=8.5, labelcolor=ink, handletextpad=0.3)
	fig.savefig(path, dpi=150, bbox_inches="tight", facecolor=surface)
	return True


def main():
	ap = argparse.ArgumentParser()
	ap.add_argument("runs", nargs="+")
	ap.add_argument("--out", required=True)
	args = ap.parse_args()
	results = [analyze(r) for r in args.runs]
	os.makedirs(args.out, exist_ok=True)
	with open(os.path.join(args.out, "summary.json"), "w") as f:
		json.dump(results, f, indent=2)
	md = to_markdown(results)
	with open(os.path.join(args.out, "summary.md"), "w") as f:
		f.write(md + "\n")
	drew = figure(results, os.path.join(args.out, "retained_vs_read_rate.png"))
	print(md)
	print("figure:", "written" if drew else "skipped (matplotlib not installed)")


if __name__ == "__main__":
	main()
