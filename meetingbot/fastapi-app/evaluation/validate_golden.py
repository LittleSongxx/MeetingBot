"""校验评测 golden 数据结构，防止评测集在后续改动中腐化。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def validate_qa(data: dict) -> list[str]:
    issues: list[str] = []
    questions = data.get("questions")
    if not isinstance(questions, list):
        return ["questions 必须是数组"]
    seen = set()
    for index, item in enumerate(questions):
        qid = item.get("id")
        if not qid or qid in seen:
            issues.append(f"question[{index}] id 缺失或重复: {qid}")
        seen.add(qid)
        if not isinstance(item.get("question"), str) or not item["question"].strip():
            issues.append(f"{qid} question 为空")
        if not isinstance(item.get("answerable"), bool):
            issues.append(f"{qid} answerable 必须是 bool")
        expected = item.get("expected_segments")
        if not isinstance(expected, list) or not all(isinstance(no, int) for no in expected):
            issues.append(f"{qid} expected_segments 必须是 int 数组")
            continue
        if item.get("answerable") and not expected:
            issues.append(f"{qid} 可回答题必须有 expected_segments")
        if not item.get("answerable") and expected:
            issues.append(f"{qid} 不可回答题不应有 expected_segments")
    return issues


def validate_multiturn(data: dict) -> list[str]:
    issues: list[str] = []
    conversations = data.get("conversations")
    if not isinstance(conversations, list):
        return ["conversations 必须是数组"]
    for conversation in conversations:
        cid = conversation.get("id")
        turns = conversation.get("turns")
        if not isinstance(turns, list) or len(turns) < 2:
            issues.append(f"{cid} 至少需要两轮")
            continue
        for index, turn in enumerate(turns):
            if not isinstance(turn.get("question"), str) or not turn["question"].strip():
                issues.append(f"{cid} turn {index} question 为空")
            expected = turn.get("expected_segments")
            if not isinstance(expected, list) or not all(isinstance(no, int) for no in expected):
                issues.append(f"{cid} turn {index} expected_segments 非法")
    return issues


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--qa", default="evaluation/golden/qa_demo.json")
    parser.add_argument("--multiturn", default="evaluation/golden/qa_multiturn.json")
    args = parser.parse_args()
    issues = []
    for label, path, validators in (
        ("qa", args.qa, validate_qa),
        ("multiturn", args.multiturn, validate_multiturn),
    ):
        file = Path(path)
        if not file.exists():
            issues.append(f"{label}: 文件不存在 {path}")
            continue
        for issue in validators(json.loads(file.read_text(encoding="utf-8"))):
            issues.append(f"{label}: {issue}")
    if issues:
        print("\n".join(issues))
        return 1
    print("golden validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
