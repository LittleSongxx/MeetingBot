"""多轮追问 QA 评测：主问题 + follow-up，验证历史上下文、引用和答案。"""

import argparse
import json
import os
import time
from pathlib import Path

import requests

from evaluation.metrics import text_similarity
from evaluation.public_metrics import span_prf


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:9090")
    parser.add_argument("--golden", default="evaluation/golden/qa_multiturn.json")
    parser.add_argument("--output", default=None)
    args = parser.parse_args()
    golden = json.loads(Path(args.golden).read_text(encoding="utf-8"))
    base = args.base_url.rstrip("/")
    login = requests.post(
        f"{base}/login",
        json={"username": os.getenv("APP_ADMIN_USERNAME", ""), "password": os.getenv("APP_ADMIN_PASSWORD", "")},
        timeout=20,
    ).json()
    headers = {"token": login["data"]["token"]}

    details = []
    for conversation in golden["conversations"]:
        history = []
        for index, turn in enumerate(conversation["turns"]):
            time.sleep(3.2)
            response = requests.post(
                f"{base}/meetingMinutes/ask/{golden['minutes_id']}",
                headers=headers,
                json={"question": turn["question"], "history": history},
                timeout=180,
            )
            payload = response.json()
            data = payload.get("data") or {}
            cited = {item.get("segment_no") for item in data.get("citations") or [] if item.get("segment_no") is not None}
            expected = set(turn.get("expected_segments") or [])
            metrics = span_prf(expected, cited)
            row = {
                "conversation": conversation["id"],
                "turn": index + 1,
                "question": turn["question"],
                "found": data.get("found"),
                "answer": data.get("answer"),
                "citations": sorted(cited),
                "expected": sorted(expected),
                "citation_precision": metrics["precision"],
                "citation_recall": metrics["recall"],
                "answer_similarity": text_similarity(data.get("answer"), turn.get("answer")),
            }
            details.append(row)
            print(conversation["id"], index + 1, "found", row["found"], "cites", row["citations"], "sim", row["answer_similarity"])
            history.extend([
                {"role": "user", "content": turn["question"]},
                {"role": "assistant", "content": data.get("answer") or ""},
            ])
    found = sum(1 for row in details if row["found"])
    summary = {
        "turns": len(details),
        "found_rate": round(found / len(details), 4),
        "citation_precision": round(sum(row["citation_precision"] for row in details) / len(details), 4),
        "citation_recall": round(sum(row["citation_recall"] for row in details) / len(details), 4),
        "answer_similarity": round(sum(row["answer_similarity"] for row in details) / len(details), 4),
        "followup_found_rate": round(
            sum(1 for row in details if row["turn"] == 2 and row["found"])
            / max(1, sum(1 for row in details if row["turn"] == 2)), 4
        ),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if args.output:
        Path(args.output).write_text(json.dumps({"summary": summary, "details": details}, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"written {args.output}")


if __name__ == "__main__":
    main()
