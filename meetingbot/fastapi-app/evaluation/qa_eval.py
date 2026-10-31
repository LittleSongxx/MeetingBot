"""单场问答 Agent 评测：跑演示会议 golden QA，统计引用、拒答和调用可靠性。

用法（评测栈 backend 容器内）：
    python -m evaluation.qa_eval --base-url http://127.0.0.1:9090/api \
        --golden /app/evaluation/golden/qa_demo.json --output /tmp/qa_eval.json
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

import requests

from evaluation.metrics import text_similarity


def metrics_for(rows: list[dict]) -> dict:
    answerable = [row for row in rows if row["answerable"]]
    unanswerable = [row for row in rows if not row["answerable"]]
    found = sum(1 for row in answerable if row["found"])
    refused = sum(1 for row in unanswerable if not row["found"])
    citation_total = sum(row["cited"] for row in rows)
    citation_correct = sum(row["citation_correct"] for row in rows)
    expected_total = sum(row["expected"] for row in rows)
    expected_hit = sum(row["expected_hit"] for row in rows)
    precision = citation_correct / citation_total if citation_total else 0.0
    recall = expected_hit / expected_total if expected_total else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    answerable_rows = [row for row in answerable if row["found"]]
    return {
        "questions": len(rows),
        "answerable": len(answerable),
        "unanswerable": len(unanswerable),
        "found_rate": round(found / len(answerable), 4) if answerable else None,
        "refusal_accuracy": round(refused / len(unanswerable), 4) if unanswerable else None,
        "citation_precision": round(precision, 4),
        "citation_recall": round(recall, 4),
        "citation_f1": round(f1, 4),
        "answer_similarity": round(
            sum(row["answer_similarity"] for row in answerable_rows) / len(answerable_rows), 4
        ) if answerable_rows else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="演示会议 QA Agent 评测")
    parser.add_argument("--base-url", default="http://127.0.0.1:9090/api")
    parser.add_argument("--golden", default="evaluation/golden/qa_demo.json")
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    golden = json.loads(Path(args.golden).read_text(encoding="utf-8"))
    base = args.base_url.rstrip("/")
    login = requests.post(
        f"{base}/login",
        json={"username": os.getenv("APP_ADMIN_USERNAME", ""), "password": os.getenv("APP_ADMIN_PASSWORD", "")},
        timeout=20,
    ).json()
    token = login["data"]["token"]
    headers = {"token": token}

    rows = []
    for index, item in enumerate(golden["questions"]):
        # QA 接口同一用户有 3 秒冷却；评测脚本必须模拟真人间隔，否则会被误判成“未找到依据”
        if index:
            time.sleep(3.2)
        response = requests.post(
            f"{base}/meetingMinutes/ask/{golden['minutes_id']}",
            headers=headers,
            json={"question": item["question"], "history": []},
            timeout=180,
        )
        payload = response.json()
        data = payload.get("data") or {}
        citations = data.get("citations") or []
        cited_segments = {citation.get("segment_no") for citation in citations if citation.get("segment_no") is not None}
        expected = set(item.get("expected_segments") or [])
        rows.append({
            "id": item["id"],
            "question": item["question"],
            "answerable": item["answerable"],
            "found": data.get("found"),
            "answer": data.get("answer"),
            "citations": sorted(cited_segments),
            "expected": len(expected),
            "expected_hit": len(expected & cited_segments),
            "cited": len(cited_segments),
            "citation_correct": len(cited_segments & expected),
            "answer_similarity": text_similarity(data.get("answer"), item.get("answer")),
            "latency_ms": response.elapsed.total_seconds() * 1000,
            "code": payload.get("code"),
        })

    result = {"golden": str(args.golden), "metrics": metrics_for(rows), "details": rows}
    print(json.dumps(result["metrics"], ensure_ascii=False, indent=2))
    print("\n| 问题 | 可回答 | found | 引用片段 | 期望片段 | 答案相似度 |")
    print("|---|---|---|---|---|---|")
    for row in rows:
        print(
            f"| {row['id']} | {row['answerable']} | {row['found']} | {row['citations']} | "
            f"{row['expected']} | {row['answer_similarity']} |"
        )
    if args.output:
        Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nwritten {args.output}")


if __name__ == "__main__":
    main()
