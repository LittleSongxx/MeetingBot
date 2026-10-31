"""公开评测集冒烟评测：adapter → 轻量 baseline → 指标。

这是“公开集适配”的第一层：不调用模型，用确定性 baseline 验证数据读取与指标管线。
后续可以把 baseline 替换成项目真实输出/LLM 输出，指标层保持不变。

用法示例（数据按各数据集许可自行下载，不入库）：
  python -m evaluation.public_eval --dataset qmsum \
    --data /path/QMSum/data/ALL/test/Bed003.json --baseline lexical
  python -m evaluation.public_eval --dataset vcsum \
    --data /path/VCSum/vcsum_data --baseline lead --kind long --limit 26
  python -m evaluation.public_eval --dataset mug \
    --data /path/alimeeting-mug-sample.json --baseline keyword
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from evaluation.adapters.mug import load_mug
from evaluation.adapters.qmsum import load_qmsum
from evaluation.adapters.vcsum import load_vcsum, validate_vcsum
from evaluation.public_metrics import (
    answer_similarity,
    average_metric,
    char_rouge,
    lexical_overlap_score,
    span_prf,
)

MUG_ACTION_KEYWORDS = (
    "负责", "完成", "前", "评估", "跟进", "方案", "上线", "联调", "同步", "确认",
    "推进", "安排", "准备", "输出", "评审", "截止", "交付", "整改", "排期", "落地",
)


def _lead_text(case: dict[str, Any], count: int = 3) -> str:
    return "".join(segment["content"] for segment in case["transcript"][:count])


def _keyword_action_segments(case: dict[str, Any]) -> set[int]:
    predicted = set()
    for segment in case["transcript"]:
        if any(keyword in segment["content"] for keyword in MUG_ACTION_KEYWORDS):
            predicted.add(segment["segment_no"])
    return predicted


def evaluate_mug(cases: list[dict], baseline: str) -> dict:
    rows = []
    for case in cases:
        gold_actions = set(case["gold"]["action_segments"])
        gold_keys = set(case["gold"]["key_sentences"])
        if baseline == "oracle":
            predicted_actions = set(gold_actions)
            predicted_keys = set(gold_keys)
        elif baseline == "keyword":
            predicted_actions = _keyword_action_segments(case)
            predicted_keys = {segment["segment_no"] for segment in case["transcript"][:10]}
        else:
            predicted_actions = set()
            predicted_keys = {segment["segment_no"] for segment in case["transcript"][:10]}
        action_metrics = span_prf(gold_actions, predicted_actions)
        key_metrics = span_prf(gold_keys, predicted_keys)
        rows.append({
            "id": case["id"],
            "action_f1": action_metrics["f1"],
            "action_precision": action_metrics["precision"],
            "action_recall": action_metrics["recall"],
            "key_sentence_f1": key_metrics["f1"],
            "gold_actions": len(gold_actions),
            "predicted_actions": len(predicted_actions),
        })
    return {
        "dataset": "mug",
        "baseline": baseline,
        "cases": len(rows),
        "metrics": {
            "action_precision": average_metric(rows, "action_precision"),
            "action_recall": average_metric(rows, "action_recall"),
            "action_f1": average_metric(rows, "action_f1"),
            "key_sentence_f1": average_metric(rows, "key_sentence_f1"),
        },
        "details": rows,
    }


def evaluate_vcsum(cases: list[dict], baseline: str) -> dict:
    rows = []
    for case in cases:
        gold_summary = case["gold"]["summary"]
        gold_evidence = set(case["gold"]["evidence_segments"])
        if baseline == "oracle":
            predicted_summary = gold_summary
            predicted_evidence = set(gold_evidence)
        elif baseline == "lead":
            predicted_summary = _lead_text(case)
            predicted_evidence = set()
        else:
            predicted_summary = ""
            predicted_evidence = set()
        rouge = char_rouge(predicted_summary, gold_summary)
        evidence = span_prf(gold_evidence, predicted_evidence)
        rows.append({
            "id": case["id"],
            "rouge1": rouge["rouge1"],
            "rouge2": rouge["rouge2"],
            "rougeL": rouge["rougeL"],
            "evidence_f1": evidence["f1"],
            "gold_evidence_segments": len(gold_evidence),
        })
    return {
        "dataset": "vcsum",
        "baseline": baseline,
        "cases": len(rows),
        "metrics": {
            "rouge1": average_metric(rows, "rouge1"),
            "rouge2": average_metric(rows, "rouge2"),
            "rougeL": average_metric(rows, "rougeL"),
            "evidence_f1": average_metric(rows, "evidence_f1"),
        },
        "details": rows,
    }


def _retrieve_segments(query: str, case: dict, top_k: int = 5) -> list[int]:
    scored = [
        (lexical_overlap_score(query, segment["content"]), segment["segment_no"])
        for segment in case["transcript"]
    ]
    scored.sort(reverse=True)
    return [segment_no for score, segment_no in scored[:top_k] if score > 0]


def evaluate_qmsum(cases: list[dict], baseline: str, top_k: int = 5) -> dict:
    rows = []
    for case in cases:
        for query in case["gold"]["queries"]:
            if query["kind"] != "specific":
                continue
            gold_evidence = set(query.get("evidence_segments") or [])
            if not gold_evidence:
                continue
            if baseline == "oracle":
                predicted_evidence = set(gold_evidence)
                predicted_answer = query["answer"]
            elif baseline == "lexical":
                predicted_evidence = set(_retrieve_segments(query["query"], case, top_k))
                predicted_answer = " ".join(
                    segment["content"]
                    for segment in case["transcript"]
                    if segment["segment_no"] in predicted_evidence
                )[:800]
            else:
                predicted_evidence = set()
                predicted_answer = ""
            evidence = span_prf(gold_evidence, predicted_evidence)
            rows.append({
                "id": f"{case['id']}/{query['query'][:40]}",
                "evidence_precision": evidence["precision"],
                "evidence_recall": evidence["recall"],
                "evidence_f1": evidence["f1"],
                "answer_similarity": answer_similarity(predicted_answer, query["answer"]),
                "gold_evidence_segments": len(gold_evidence),
            })
    return {
        "dataset": "qmsum",
        "baseline": baseline,
        "cases": len(rows),
        "metrics": {
            "evidence_precision": average_metric(rows, "evidence_precision"),
            "evidence_recall": average_metric(rows, "evidence_recall"),
            "evidence_f1": average_metric(rows, "evidence_f1"),
            "answer_similarity": average_metric(rows, "answer_similarity"),
        },
        "details": rows,
    }


def render_markdown(result: dict) -> str:
    lines = [
        f"# 公开集冒烟评测：{result['dataset']}",
        "",
        f"- baseline: `{result['baseline']}`",
        f"- cases: {result['cases']}",
        "",
        "| 指标 | 数值 |",
        "|---|---|",
    ]
    for key, value in result["metrics"].items():
        lines.append(f"| {key} | {value} |")
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="公开会议评测集 adapter + baseline smoke test")
    parser.add_argument("--dataset", choices=["mug", "vcsum", "qmsum"], required=True)
    parser.add_argument("--data", required=True, help="数据集文件或目录")
    parser.add_argument("--baseline", choices=["lead", "keyword", "lexical", "oracle"], default="lead")
    parser.add_argument("--kind", choices=["long", "short"], default="long", help="VCSUM 长/短摘要")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--output", help="写入 JSON 路径")
    args = parser.parse_args()

    path = Path(args.data)
    if args.dataset == "mug":
        cases = load_mug(path)[: max(1, args.limit)]
        result = evaluate_mug(cases, args.baseline)
    elif args.dataset == "vcsum":
        missing = validate_vcsum(path)
        if missing:
            raise SystemExit(f"VCSUM 目录缺少文件: {missing}")
        cases = load_vcsum(path, split="test", kind=args.kind)[: max(1, args.limit)]
        result = evaluate_vcsum(cases, args.baseline)
    else:
        cases = load_qmsum(path)[: max(1, args.limit)]
        result = evaluate_qmsum(cases, args.baseline)

    markdown = render_markdown(result)
    print(markdown)
    if args.output:
        output = Path(args.output)
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nwritten {output}")
    print(f"\n```json\n{json.dumps(result['details'][:2], ensure_ascii=False, indent=2)}\n```")


if __name__ == "__main__":
    main()
