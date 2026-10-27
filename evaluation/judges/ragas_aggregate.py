#!/usr/bin/env python3
"""Aggregate 3 RAGAS r1 runs: means, range, SD, case-level cluster bootstrap CI, per-source breakdown.

Usage:
  .venv-ragas/bin/python judges/ragas_aggregate.py \
    --runs results/ragas_official_run1.json results/ragas_official_run2.json results/ragas_official_run3.json \
    --out results/ragas_official_aggregate.json \
    --report reports/2026-10-27_ragas_official.md \
    --baseline baselines/ragas_official_baseline.json
"""
from __future__ import annotations

import argparse
import json
import random
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

EVAL_DIR = Path(__file__).resolve().parents[1]
METRICS = ["faithfulness", "answer_relevancy", "context_precision", "context_recall"]
BOOTSTRAP_N = 1000


def load_run(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def metric_values(run: dict, metric: str, source: str | None = None) -> list[float]:
    values = []
    for case in run.get("cases", []):
        if source and (case.get("source_set") or "unknown") != source:
            continue
        value = (case.get("scores") or {}).get(metric)
        if isinstance(value, (int, float)):
            values.append(float(value))
    return values


def mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def bootstrap_ci(per_case: list[float], seed: int = 42) -> list[float] | None:
    if not per_case:
        return None
    rng = random.Random(seed)
    n = len(per_case)
    samples = []
    for _ in range(BOOTSTRAP_N):
        total = 0.0
        for _ in range(n):
            total += per_case[rng.randrange(n)]
        samples.append(total / n)
    samples.sort()
    return [samples[int(0.025 * (BOOTSTRAP_N - 1))], samples[int(0.975 * (BOOTSTRAP_N - 1))]]


def rel(path: str) -> str:
    try:
        return str(Path(path).resolve().relative_to(EVAL_DIR.parent))
    except Exception:
        return str(path)


def fmt(value: float | None, digits: int = 4) -> str:
    return "null" if value is None else f"{value:.{digits}f}"


def build_markdown(aggregate: dict) -> str:
    meta = aggregate["meta"]
    lines = [
        "# RAGAS 官方口径重测报告（r1 仪器）",
        "",
        f"> 生成时间：{meta['generated_at']}",
        f"> 仪器：ragas `{meta['ragas_version']}`；judge `{meta['judge']['model']}`（temp=0）；embedding `{meta['embedding']['model']}`（dim={meta['embedding']['dim']}）；prompt 语言 `{meta['prompt_language']}`",
        f"> 数据：`{meta['packet_path']}` sha256 `{meta['packet_sha256']}`；reference `{meta['refs_path']}` sha256 `{meta['refs_sha256']}`（human_checked={meta['refs_human_checked']}）",
        f"> 运行：N={meta['n_runs']}；{meta['n_cases']} cases/run；标签 `r1`（**禁止与旧自研口径 71.83/90.02/88.34/99.00 相减**）",
        "> reference 校验：167/167 句与 gold facts 逐字一致（脚本 `gold/build_qa_ref_answers.py`）；人工抽检 30 条 CSV 待填写（`gold/qa_ref_spotcheck_30.csv`），`human_checked=false` 如实保留。",
        "",
        "## 1. 四指标（case 级均值；3 次运行）",
        "",
        "| 指标 | run1 | run2 | run3 | 3 次均值 | 极差 | SD | 逐题 bootstrap 95% CI | 有效 n / null |",
        "|---|---:|---:|---:|---:|---:|---:|---|---:|",
    ]
    for metric in METRICS:
        item = aggregate["metrics"][metric]
        runs = item["runs"]
        lines.append(
            "| {m} | {r1} | {r2} | {r3} | **{mean}** | {span} | {sd} | [{lo}, {hi}] | {n} / {nulls} |".format(
                m=metric,
                r1=fmt(runs[0]["mean"]) if len(runs) > 0 else "-",
                r2=fmt(runs[1]["mean"]) if len(runs) > 1 else "-",
                r3=fmt(runs[2]["mean"]) if len(runs) > 2 else "-",
                mean=fmt(item["mean"]),
                span=fmt(item["range"]),
                sd=fmt(item["sd"]),
                lo=fmt(item["bootstrap_ci"][0]) if item["bootstrap_ci"] else "null",
                hi=fmt(item["bootstrap_ci"][1]) if item["bootstrap_ci"] else "null",
                n=item["n_cases"],
                nulls=item["n_null_total"],
            )
        )
    lines += ["", "## 2. 分层读数（3 次运行合并）", "", "| 数据集 | n | " + " | ".join(METRICS) + " |", "|---|---:|" + "---:|" * len(METRICS)]
    for source, bucket in aggregate["by_source_set"].items():
        lines.append(
            f"| {source} | {bucket['n_cases']} | "
            + " | ".join(fmt(bucket["metrics"].get(m)) for m in METRICS)
            + " |"
        )
    lines += [
        "",
        "## 3. 口径与边界（必须随数字一起引用）",
        "",
        "- 四个分母不同：context_precision = 被判有用的切片数（AP 公式）；context_recall = reference 句子数；faithfulness = judge 拆出的断言数；answer_relevancy = case 数。",
        "- reference 由 `expected_facts` 逐条转成陈述句（1 fact = 1 sentence），`human_checked=false`；它是 AI-draft reference，不是人工金标。",
        "- 单裁判（deepseek-flash）+ 未做人工校准；`answer_relevancy` 依赖 embedding 模型，中文场景官方 issue #1829 提示可能偏低。",
        "- 本报告是**第二台仪器**（r1）的读数，只与同仪器前后对比；与旧自研指标不可相减。",
        "",
        "## 4. 每指标 3 次运行明细",
        "",
        "```json",
        json.dumps({m: aggregate["metrics"][m] for m in METRICS}, ensure_ascii=False, indent=2),
        "```",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", nargs="+", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--report", default="")
    parser.add_argument("--baseline", default="")
    args = parser.parse_args()

    run_paths = [Path(p) for p in args.runs]
    runs = [load_run(p) for p in run_paths]
    if len(runs) != 3:
        raise SystemExit("expected exactly 3 runs")
    first = runs[0]
    for run in runs:
        for key in ("packet_sha256", "refs_sha256", "ragas_version"):
            if run.get(key) != first.get(key):
                raise SystemExit(f"run provenance mismatch on {key}")

    by_case: dict[str, dict[str, list[float]]] = {}
    per_run_null: dict[str, int] = {}
    for run in runs:
        for metric in METRICS:
            per_run_null[metric] = per_run_null.get(metric, 0) + sum(
                1 for case in run.get("cases", []) if (case.get("scores") or {}).get(metric) is None
            )
        for case in run.get("cases", []):
            case_id = case["case_id"]
            bucket = by_case.setdefault(case_id, {"source_set": case.get("source_set") or "unknown"})
            for metric in METRICS:
                value = (case.get("scores") or {}).get(metric)
                bucket.setdefault(metric, []).append(float(value) if isinstance(value, (int, float)) else None)

    aggregate = {
        "meta": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "label": "r1",
            "ragas_version": first["ragas_version"],
            "judge": first["judge"],
            "embedding": first["embedding"],
            "prompt_language": first["prompt_language"],
            "packet_path": rel(first["packet_path"]),
            "packet_sha256": first["packet_sha256"],
            "refs_path": rel(first["refs_path"]),
            "refs_sha256": first["refs_sha256"],
            "refs_human_checked": first.get("refs_human_checked"),
            "n_runs": len(runs),
            "n_cases": len(by_case),
            "run_files": [rel(str(p)) for p in run_paths],
        },
        "metrics": {},
        "by_source_set": {},
        "cases": {},
    }

    for metric in METRICS:
        run_means = [mean(metric_values(run, metric)) for run in runs]
        run_values = [mean(metric_values(run, metric)) for run in runs]
        per_case = []
        for case_id, bucket in sorted(by_case.items()):
            values = [v for v in bucket.get(metric, []) if v is not None]
            per_case.append(mean(values))
        aggregate["metrics"][metric] = {
            "runs": [{"file": str(p), "mean": m, "n_numeric": len(metric_values(run, metric)), "n_null": sum(1 for c in run["cases"] if (c.get("scores") or {}).get(metric) is None)} for p, run, m in zip(run_paths, runs, run_means)],
            "mean": mean([m for m in run_means if m is not None]),
            "range": (max([m for m in run_means if m is not None]) - min([m for m in run_means if m is not None])) if [m for m in run_means if m is not None] else None,
            "sd": statistics.stdev([m for m in run_means if m is not None]) if len([m for m in run_means if m is not None]) > 1 else None,
            "bootstrap_ci": bootstrap_ci([v for v in per_case if v is not None]),
            "n_cases": sum(1 for v in per_case if v is not None),
            "n_null_total": per_run_null.get(metric, 0),
        }

    sources = sorted({bucket["source_set"] for bucket in by_case.values()})
    for source in sources:
        bucket_metrics: dict[str, Any] = {"n_cases": 0, "metrics": {}}
        for metric in METRICS:
            values = []
            for case_id, bucket in by_case.items():
                if bucket["source_set"] != source:
                    continue
                per_case_values = [v for v in bucket.get(metric, []) if v is not None]
                if per_case_values:
                    values.append(mean(per_case_values))
                else:
                    values.append(None)
            bucket_metrics["metrics"][metric] = mean([v for v in values if v is not None])
        bucket_metrics["n_cases"] = sum(1 for b in by_case.values() if b["source_set"] == source)
        aggregate["by_source_set"][source] = bucket_metrics

    aggregate["cases"] = {case_id: {metric: [v for v in bucket.get(metric, []) if v is not None] for metric in METRICS} for case_id, bucket in by_case.items()}

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(aggregate, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {out_path}")

    if args.report:
        report_path = Path(args.report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(build_markdown(aggregate), encoding="utf-8")
        print(f"wrote {report_path}")

    if args.baseline:
        baseline_path = Path(args.baseline)
        baseline = {
            "frozen_at": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "label": "r1",
            "ragas_version": first["ragas_version"],
            "judge": first["judge"],
            "embedding": first["embedding"],
            "prompt_language": first["prompt_language"],
            "packet_sha256": first["packet_sha256"],
            "refs_sha256": first["refs_sha256"],
            "refs_human_checked": first.get("refs_human_checked"),
            "n_runs": len(runs),
            "n_cases": len(by_case),
            "metrics": {
                metric: {
                    "mean": aggregate["metrics"][metric]["mean"],
                    "range": aggregate["metrics"][metric]["range"],
                    "sd": aggregate["metrics"][metric]["sd"],
                    "bootstrap_95": aggregate["metrics"][metric]["bootstrap_ci"],
                    "n_numeric": aggregate["metrics"][metric]["n_cases"],
                }
                for metric in METRICS
            },
            "run_files": [rel(str(p)) for p in run_paths],
        }
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        baseline_path.write_text(json.dumps(baseline, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"wrote {baseline_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

