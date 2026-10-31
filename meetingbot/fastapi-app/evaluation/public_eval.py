"""公开评测集冒烟评测：adapter → baseline 或预测文件 → 指标。

这是“公开集适配”的第一层：adapter 负责读原始数据，指标层复用；baseline 用确定性规则
验证管线，`--predictions` 可把项目/LLM 输出接进来做真实评测。

用法示例（数据按各数据集许可自行下载，不入库）：
  python -m evaluation.public_eval --dataset qmsum \
    --data /path/QMSum/data/ALL/test/Bed003.json --baseline lexical
  python -m evaluation.public_eval --dataset vcsum \
    --data /path/VCSum/vcsum_data --baseline lead --kind long --limit 26
  python -m evaluation.public_eval --dataset mug \
    --data /path/mug/except_TS_test1.csv --baseline keyword
  python -m evaluation.public_eval --dataset mug \
    --data /path/mug/except_TS_test1.csv --predictions predictions.json

predictions.json 结构（按 case id 索引）：
  MUG/VCSUM: {"M3115": {"action_segments": [2,3], "key_sentences": [2,3]}}
  VCSUM:    {"0": {"summary": "...", "evidence_segments": [1,2]}}
  QMSum:    {"qmsum_Bed003.json#0": {"queries": [
                {"query": "...", "answer": "...", "evidence_segments": [137,138]}]}}
"""

from __future__ import annotations

import argparse
import json
import re
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

# 行动项句子检测的轻量规则 baseline：强动作词或负责人/期限信号。
# 在 MUG Dev/Test1 上比较过多种 cue 组合，这个组合 F1 最稳（约 .07），比“前”单字规则好。
MUG_ACTION_VERBS = re.compile(r"负责|跟进|完成|联调|上线|交付|整改|排期|落地")
MUG_OWNER_OR_TIME = re.compile(r"\d{1,2}月|\d{1,2}日|下周|本周|月底|截止|负责人|由[^，。；]{1,8}(负责|跟进)")


def _lead_text(case: dict[str, Any], count: int = 3) -> str:
    return "".join(segment["content"] for segment in case["transcript"][:count])


def _keyword_action_segments(case: dict[str, Any]) -> set[int]:
    predicted = set()
    for segment in case["transcript"]:
        content = segment["content"]
        if MUG_ACTION_VERBS.search(content) or MUG_OWNER_OR_TIME.search(content):
            predicted.add(segment["segment_no"])
    return predicted


def _prediction_for(predictions: dict | None, case_id: str) -> dict:
    if not predictions:
        return {}
    payload = predictions.get(case_id)
    return payload if isinstance(payload, dict) else {}


def load_predictions(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict) and isinstance(payload.get("cases"), dict):
        return payload["cases"]
    if not isinstance(payload, dict):
        raise SystemExit("predictions 必须是 JSON 对象（case id -> prediction）")
    return payload


def evaluate_mug(cases: list[dict], baseline: str, predictions: dict | None = None) -> dict:
    rows = []
    for case in cases:
        gold_actions = set(case["gold"]["action_segments"])
        gold_keys = set(case["gold"]["key_sentences"])
        if predictions is not None:
            prediction = _prediction_for(predictions, case["id"])
            predicted_actions = set(prediction.get("action_segments") or [])
            predicted_keys = set(prediction.get("key_sentences") or [])
        elif baseline == "oracle":
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
    # 很多会议没有 action item，把 0 计入 macro 平均会人为压低指标；只在有 gold action 的会议上报 action 指标
    action_rows = [row for row in rows if row["gold_actions"] > 0]
    return {
        "dataset": "mug",
        "baseline": "predictions" if predictions is not None else baseline,
        "cases": len(rows),
        "action_cases": len(action_rows),
        "metrics": {
            "action_precision": average_metric(action_rows, "action_precision"),
            "action_recall": average_metric(action_rows, "action_recall"),
            "action_f1": average_metric(action_rows, "action_f1"),
            "key_sentence_f1": average_metric(rows, "key_sentence_f1"),
        },
        "details": rows,
    }


def evaluate_vcsum(cases: list[dict], baseline: str, predictions: dict | None = None) -> dict:
    rows = []
    for case in cases:
        gold_summary = case["gold"]["summary"]
        gold_evidence = set(case["gold"]["evidence_segments"])
        if predictions is not None:
            prediction = _prediction_for(predictions, case["id"])
            predicted_summary = str(prediction.get("summary") or "")
            predicted_evidence = set(prediction.get("evidence_segments") or [])
        elif baseline == "oracle":
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
        "baseline": "predictions" if predictions is not None else baseline,
        "cases": len(rows),
        "metrics": {
            "rouge1": average_metric(rows, "rouge1"),
            "rouge2": average_metric(rows, "rouge2"),
            "rougeL": average_metric(rows, "rougeL"),
            "evidence_f1": average_metric(rows, "evidence_f1"),
        },
        "details": rows,
    }


def _retrieve_segments(query: str, case: dict, top_k: int = 10) -> list[int]:
    scored = [
        (lexical_overlap_score(query, segment["content"]), segment["segment_no"])
        for segment in case["transcript"]
    ]
    scored.sort(reverse=True)
    return [segment_no for score, segment_no in scored[:top_k] if score > 0]


def evaluate_qmsum(
    cases: list[dict],
    baseline: str,
    top_k: int = 10,
    predictions: dict | None = None,
) -> dict:
    rows = []
    for case in cases:
        prediction = _prediction_for(predictions, case["id"]) if predictions is not None else {}
        pred_queries = prediction.get("queries") if isinstance(prediction.get("queries"), list) else []
        pred_by_query = {
            str(item.get("query")): item for item in pred_queries if isinstance(item, dict) and item.get("query")
        }
        for query in case["gold"]["queries"]:
            if query["kind"] != "specific":
                continue
            gold_evidence = set(query.get("evidence_segments") or [])
            if not gold_evidence:
                continue
            if predictions is not None:
                predicted = pred_by_query.get(query["query"], {})
                predicted_evidence = set(predicted.get("evidence_segments") or [])
                predicted_answer = str(predicted.get("answer") or "")
            elif baseline == "oracle":
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
        "baseline": "predictions" if predictions is not None else baseline,
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
    ]
    if "action_cases" in result:
        lines.append(f"- action cases: {result['action_cases']}")
    lines.extend(["", "| 指标 | 数值 |", "|---|---|"])
    for key, value in result["metrics"].items():
        lines.append(f"| {key} | {value} |")
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="公开会议评测集 adapter + baseline/predictions 评测")
    parser.add_argument("--dataset", choices=["mug", "vcsum", "qmsum"], required=True)
    parser.add_argument("--data", required=True, help="数据集文件或目录")
    parser.add_argument("--baseline", choices=["lead", "keyword", "lexical", "oracle"], default="lead")
    parser.add_argument("--predictions", help="预测 JSON；提供后覆盖 baseline")
    parser.add_argument("--kind", choices=["long", "short"], default="long", help="VCSUM 长/短摘要")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--top-k", type=int, default=10, help="QMSum lexical 检索条数")
    parser.add_argument("--output", help="写入 JSON 路径")
    args = parser.parse_args()

    path = Path(args.data)
    predictions = load_predictions(Path(args.predictions)) if args.predictions else None
    if args.dataset == "mug":
        cases = load_mug(path)[: max(1, args.limit)]
        result = evaluate_mug(cases, args.baseline, predictions)
    elif args.dataset == "vcsum":
        missing = validate_vcsum(path)
        if missing:
            raise SystemExit(f"VCSUM 目录缺少文件: {missing}")
        cases = load_vcsum(path, split="test", kind=args.kind)[: max(1, args.limit)]
        result = evaluate_vcsum(cases, args.baseline, predictions)
    else:
        cases = load_qmsum(path)[: max(1, args.limit)]
        result = evaluate_qmsum(cases, args.baseline, args.top_k, predictions)

    print(render_markdown(result))
    if args.output:
        output = Path(args.output)
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nwritten {output}")
    print(f"\n```json\n{json.dumps(result['details'][:2], ensure_ascii=False, indent=2)}\n```")


if __name__ == "__main__":
    main()
