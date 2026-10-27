#!/usr/bin/env python3
"""Paired comparison: baseline r1 runs vs a new r1 run (same packet questions/reference).

Reports per-metric mean diff + case-level bootstrap CI, win/tie/loss, and the
pre-registered OPT-1 gates. Never compares different instruments.
"""
from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

METRICS = ["faithfulness", "answer_relevancy", "context_precision", "context_recall"]


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def scores_by_case(run: dict) -> dict[str, dict[str, float | None]]:
    out = {}
    for case in run.get("cases", []):
        out[case["case_id"]] = {m: (case.get("scores") or {}).get(m) for m in METRICS}
    return out


def mean(values):
    return sum(values) / len(values) if values else None


def bootstrap_ci(values: list[float], seed: int = 42) -> list[float] | None:
    if not values:
        return None
    rng = random.Random(seed)
    n = len(values)
    samples = []
    for _ in range(2000):
        total = 0.0
        for _ in range(n):
            total += values[rng.randrange(n)]
        samples.append(total / n)
    samples.sort()
    return [samples[int(0.025 * (len(samples) - 1))], samples[int(0.975 * (len(samples) - 1))]]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", nargs=3, required=True)
    parser.add_argument("--new", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--report", default="")
    args = parser.parse_args()

    baseline_runs = [load(Path(p)) for p in args.baseline]
    new_run = load(Path(args.new))
    base_cases = [scores_by_case(run) for run in baseline_runs]
    new_cases = scores_by_case(new_run)
    common = sorted(set(base_cases[0]) & set(new_cases))
    if len(common) < 50:
        raise SystemExit(f"too few paired cases: {len(common)}")

    result = {"meta": {"baseline": args.baseline, "new": args.new, "n_paired": len(common),
                       "baseline_label": "r1", "new_label": "r1-m1"}, "metrics": {}, "cases": {}}
    for metric in METRICS:
        base_vals = [mean([run[c][metric] for run in base_cases if run[c][metric] is not None])
                     for c in common]
        new_vals = [new_cases[c][metric] for c in common]
        pairs = [(b, n) for b, n in zip(base_vals, new_vals) if b is not None and n is not None]
        diffs = [n - b for b, n in pairs]
        wins = sum(1 for d in diffs if d > 1e-9)
        losses = sum(1 for d in diffs if d < -1e-9)
        ties = len(diffs) - wins - losses
        result["metrics"][metric] = {
            "baseline_mean": mean([b for b, _ in pairs]),
            "new_mean": mean([n for _, n in pairs]),
            "mean_diff": mean(diffs),
            "diff_ci": bootstrap_ci(diffs),
            "wins": wins, "ties": ties, "losses": losses,
            "n": len(pairs),
            "nulls_new": sum(1 for c in common if new_cases[c][metric] is None),
        }
    faith = result["metrics"]["faithfulness"]
    result["gates"] = {
        "faith_pp": (faith["mean_diff"] or 0) * 100,
        "faith_ci_lower_pp": (faith["diff_ci"][0] * 100) if faith["diff_ci"] else None,
        "pass_faith": bool(faith["mean_diff"] is not None and faith["mean_diff"] >= 0.05 and faith["diff_ci"] and faith["diff_ci"][0] > 0),
        "noninferior_ar": (result["metrics"]["answer_relevancy"]["mean_diff"] or 0) >= -0.02,
        "noninferior_cr": (result["metrics"]["context_recall"]["mean_diff"] or 0) >= -0.02,
        "noninferior_cp": (result["metrics"]["context_precision"]["mean_diff"] or 0) >= -0.02,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# M1 A/B（paired，r1 仪器）", "",
             f"> n_paired={len(common)}；baseline={', '.join(args.baseline)}；new={args.new}", "",
             "| 指标 | baseline | M1 | Δ | 95% CI of Δ | W/T/L |", "|---|---:|---:|---:|---|---|"]
    for metric in METRICS:
        item = result["metrics"][metric]
        def p(x): return "null" if x is None else f"{x*100:.2f}%"
        def d(x): return "null" if x is None else f"{x*100:+.2f}pp"
        ci = item["diff_ci"]
        lines.append(f"| {metric} | {p(item['baseline_mean'])} | {p(item['new_mean'])} | **{d(item['mean_diff'])}** | "
                     f"[{d(ci[0]) if ci else 'null'}, {d(ci[1]) if ci else 'null'}] | {item['wins']}/{item['ties']}/{item['losses']} |")
    lines += ["", "## OPT-1 预注册门", "",
              f"- faithfulness ≥ +5pp 且 CI 下界 >0：**{'PASS' if result['gates']['pass_faith'] else 'FAIL'}**（Δ={result['gates']['faith_pp']:+.2f}pp, CI lower={result['gates']['faith_ci_lower_pp']}）",
              f"- AR 非劣（−2pp）：{'PASS' if result['gates']['noninferior_ar'] else 'FAIL'}",
              f"- CR 非劣（−2pp）：{'PASS' if result['gates']['noninferior_cr'] else 'FAIL'}",
              f"- CP 非劣（−2pp）：{'PASS' if result['gates']['noninferior_cp'] else 'FAIL'}"]
    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
