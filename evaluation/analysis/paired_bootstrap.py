"""配对 bootstrap：两个 packet 的 per-case 指标差值的 95% CI（零模型调用）。

用途：避免只看整体 rate 的一个点估计；RAG 指标分母小，单跑几 pp 的差异经常不显著。
"""
from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

METRICS = ("context_precision", "context_recall", "faithfulness", "answer_relevancy")


def load(path: Path) -> dict[str, dict[str, float]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = (data.get("summary") or {}).get("per_case") or []
    return {row["case_id"]: {m: row.get(m) for m in METRICS if row.get(m) is not None}
            for row in rows}


def paired(a: dict, b: dict, metric: str, n_boot: int = 10000, seed: int = 20261020):
    ids = sorted(set(a) & set(b))
    diffs = [b[c][metric] - a[c][metric] for c in ids
             if metric in a[c] and metric in b[c]]
    if not diffs:
        return {"n": 0}
    rnd = random.Random(seed)
    means = []
    for _ in range(n_boot):
        sample = [rnd.choice(diffs) for _ in diffs]
        means.append(sum(sample) / len(sample))
    means.sort()
    lo = means[max(0, int(n_boot * 0.025) - 1)]
    hi = means[min(n_boot - 1, int(n_boot * 0.975))]
    return {
        "n": len(diffs), "mean_diff": sum(diffs) / len(diffs),
        "ci95": [lo, hi],
        "share_positive": sum(1 for value in means if value > 0) / n_boot,
        "zero_in_ci": lo <= 0 <= hi,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--a", type=Path, required=True)
    parser.add_argument("--b", type=Path, required=True)
    parser.add_argument("--label", default="B - A")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    a, b = load(args.a), load(args.b)
    result = {"a": str(args.a), "b": str(args.b), "label": args.label,
              "metrics": {m: paired(a, b, m) for m in METRICS}}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.out:
        args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
