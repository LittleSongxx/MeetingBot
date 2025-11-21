"""AI 复核替代人工：意图 100 条 + 扩展集 68 条（**标签记为 ai_review，不冒充 human**）。

## 为什么单独一个脚本

用户要求用 AI 替代人工复核。这件事可以做，但必须留下来源：`intent_source="ai_review"`、
扩展集 `case_source="ai_verified"`。**不写 human**——否则 F1 会被读成人工金标，
而它仍是一个模型的判断。

## 两个模式

- `--intent`：DeepSeek（与被测 qwen 不同族、关思考）按四类判据逐题复核；
  原代理标签保留在 `agent_draft_intent`，AI 标签写进 `intent`。
- `--extended`：DeepSeek 看“问题 + 金标事实 + 标注片段原文”，判 worth_asking /
  gold_correct；两项都 true 才把 `case_source` 改成 `ai_verified`。

## 预算与可恢复

`--authorized-llm-calls` 是硬闸；每判完一条就写盘，重跑会跳过已有 `ai_review` 的条目。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping

EVALUATION_DIR = Path(__file__).resolve().parents[1]
if str(EVALUATION_DIR) not in sys.path:
    sys.path.insert(0, str(EVALUATION_DIR))

from lib import model_client  # noqa: E402

from gold import verify_gold  # noqa: E402

DEFAULT_QA_SET = EVALUATION_DIR / "gold" / "qa_seed.json"
DEFAULT_EXTENDED = EVALUATION_DIR / "gold" / "qa_seed_extended.json"
DEFAULT_INTENT_OUT = EVALUATION_DIR / "gold" / "qa_seed_ai_reviewed.json"
DEFAULT_EXTENDED_OUT = EVALUATION_DIR / "gold" / "qa_seed_extended_ai_reviewed.json"
DEFAULT_REPORT = EVALUATION_DIR / "results" / "ai_review_report.json"
INTENT_LABELS = {"single_fact", "aggregation", "cross_meeting", "smalltalk"}

INTENT_SYSTEM = """你在复核会议问答的意图标签。四类，只能选一个：
- single_fact：一个具体事实/一段流程，答案一句话能说清；
- aggregation：答案天然是列表（≥2 项），或问「哪些/哪几项/分别」；
- cross_meeting：答案必须来自两段录音或多场会议（注意：这取决于语料布局，不能只看问题）；
- smalltalk：寒暄/与会议无关，答案不该来自会议资料。
只输出 JSON：{"intent": "one_of_the_four", "why": "一句话"}。"""

EXTENDED_SYSTEM = """你在复核一条会议问答评测题。给你问题、金标事实、以及金标标注的
转写片段原文。判断两件事：
1. worth_asking：这条问题是否值得作为评测题（可回答、不是纯寒暄/重复）；
2. gold_correct：金标事实是否准确、且能由给定片段支持（允许近义改写，不允许编造）。
只输出 JSON：{"worth_asking": true/false, "gold_correct": true/false, "why": "一句话"}。"""


def normalize_intent_review(payload: Mapping[str, Any]) -> dict[str, Any]:
    label = str(payload.get("intent") or "").strip()
    if label not in INTENT_LABELS:
        raise ValueError(f"意图标签不在四类里：{label!r}")
    return {"intent": label, "why": str(payload.get("why") or "").strip()[:300]}


def normalize_extended_review(payload: Mapping[str, Any]) -> dict[str, Any]:
    worth = payload.get("worth_asking")
    correct = payload.get("gold_correct")
    if not isinstance(worth, bool) or not isinstance(correct, bool):
        raise ValueError("worth_asking/gold_correct 必须是布尔值")
    return {"worth_asking": worth, "gold_correct": correct,
            "why": str(payload.get("why") or "").strip()[:300]}


def _call(url: str, model: str, key: str, system: str, user: str) -> dict[str, Any]:
    reply = model_client.call_json(url, model, key, system, user,
                                   total_deadline=180, thinking=False)
    # call_json 已经解析成对象并放在 parsed 里；不再二次解析文本。
    return dict(reply.get("parsed") or {})


def _write(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def review_intent(qa_set: dict[str, Any], *, url: str, model: str, key: str,
                  authorized_calls: int, progress=print) -> dict[str, Any]:
    calls = errors = agree = reviewed = 0
    for case in qa_set.get("cases") or []:
        if case.get("unanswerable") or not case.get("intent"):
            continue
        if case.get("intent_source") == "ai_review":
            reviewed += 1
            agree += 1 if case["intent"] == case.get("agent_draft_intent") else 0
            continue
        if calls >= authorized_calls:
            progress(f"授权调用用完：已复核 {reviewed} 条")
            break
        user = f"问题：{case['question']}\n请按四类判据输出 JSON。"
        try:
            payload = _call(url, model, key, INTENT_SYSTEM, user)
            review = normalize_intent_review(payload)
        except Exception as error:  # noqa: BLE001 - 单条失败不中断整批
            errors += 1
            progress(f"  [{case['id']}] 复核失败：{type(error).__name__}: {error}")
            continue
        calls += 1
        case["agent_draft_intent"] = case.get("intent")
        case["intent"] = review["intent"]
        case["intent_source"] = "ai_review"
        case["ai_review"] = {**review, "model": model, "agrees_with_agent_draft":
                             review["intent"] == case["agent_draft_intent"]}
        agree += 1 if case["ai_review"]["agrees_with_agent_draft"] else 0
        reviewed += 1
        progress(f"  [{case['id']}] {case['agent_draft_intent']} → {review['intent']}")
    return {"mode": "intent", "reviewed": reviewed, "calls": calls, "errors": errors,
            "agreement": agree / reviewed if reviewed else None,
            "labels": {label: sum(1 for c in qa_set["cases"]
                                  if c.get("intent") == label) for label in INTENT_LABELS}}


def review_extended(qa_set: dict[str, Any], *, url: str, model: str, key: str,
                    segments: Mapping[int, Mapping[int, str]], authorized_calls: int,
                    progress=print) -> dict[str, Any]:
    calls = errors = verified = rejected = 0
    for case in qa_set.get("cases") or []:
        if case.get("case_source") == "ai_verified" or case.get("ai_review"):
            verified += 1 if case.get("case_source") == "ai_verified" else 0
            rejected += 1 if case.get("case_source") != "ai_verified" else 0
            continue
        if calls >= authorized_calls:
            progress(f"授权调用用完：已验证 {verified} 条")
            break
        source_parts = []
        for key_name in ("source", "also_in"):
            entry = case.get(key_name) or {}
            task_id = int(entry.get("task_id") or 0)
            for segment_no in entry.get("segments") or []:
                text = (segments.get(task_id) or {}).get(int(segment_no))
                if text:
                    source_parts.append(f"[task {task_id} 片段 {segment_no}] {text}")
        if not source_parts:
            errors += 1
            continue
        facts = "\n".join(f"- {fact}" for fact in case.get("expected_facts") or [])
        user = (f"问题：{case['question']}\n\n金标事实：\n{facts}\n\n"
                f"标注片段原文：\n" + "\n".join(source_parts)[:6000])
        try:
            payload = _call(url, model, key, EXTENDED_SYSTEM, user)
            review = normalize_extended_review(payload)
        except Exception as error:  # noqa: BLE001
            errors += 1
            progress(f"  [{case['id']}] 复核失败：{type(error).__name__}: {error}")
            continue
        calls += 1
        case["ai_review"] = {**review, "model": model}
        if review["worth_asking"] and review["gold_correct"]:
            case["case_source"] = "ai_verified"
            verified += 1
        else:
            case["case_source"] = "agent_draft"
            rejected += 1
        progress(f"  [{case['id']}] worth={review['worth_asking']} gold={review['gold_correct']}")
    return {"mode": "extended", "verified": verified, "rejected": rejected,
            "calls": calls, "errors": errors}


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--intent", action="store_true")
    parser.add_argument("--extended", action="store_true")
    parser.add_argument("--qa-set", type=Path, default=DEFAULT_QA_SET)
    parser.add_argument("--extended-qa-set", type=Path, default=DEFAULT_EXTENDED)
    parser.add_argument("--intent-output", type=Path, default=DEFAULT_INTENT_OUT)
    parser.add_argument("--extended-output", type=Path, default=DEFAULT_EXTENDED_OUT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--base-url", default="http://127.0.0.1:19091")
    parser.add_argument("--token", default="")
    parser.add_argument("--meeting-id", type=int, default=2)
    parser.add_argument("--provider", default="deepseek", choices=sorted(model_client.PROVIDERS))
    parser.add_argument("--authorized-llm-calls", type=int, required=True)
    args = parser.parse_args(list(argv) if argv is not None else None)
    if not args.intent and not args.extended:
        parser.error("至少给 --intent 或 --extended")
    if args.authorized_llm_calls <= 0:
        parser.error("--authorized-llm-calls 必须为正整数")
    url, model, key = model_client.endpoint(args.provider)
    report: dict[str, Any] = {"provider": args.provider, "model": model,
                              "thinking": False, "authorized_calls": args.authorized_llm_calls}
    remaining = args.authorized_llm_calls
    if args.intent:
        qa_set = json.loads(args.qa_set.read_text(encoding="utf-8"))
        result = review_intent(qa_set, url=url, model=model, key=key,
                               authorized_calls=remaining)
        _write(args.intent_output, qa_set)
        report["intent"] = result
        remaining -= result["calls"]
    if args.extended:
        if not args.token:
            print("拒绝执行：--extended 需要 --token（要读转写片段原文）", file=sys.stderr)
            return 2
        qa_set = json.loads(args.extended_qa_set.read_text(encoding="utf-8"))
        segments = verify_gold.fetch_segments(args.base_url, args.token, args.meeting_id)
        result = review_extended(qa_set, url=url, model=model, key=key, segments=segments,
                                 authorized_calls=max(0, remaining))
        _write(args.extended_output, qa_set)
        report["extended"] = result
    _write(args.report, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["INTENT_LABELS", "normalize_intent_review", "normalize_extended_review",
           "review_intent", "review_extended", "main"]
