"""S3：10 场 AI-draft 行动项 gold + 产品 pending_items 抽取 F1（预算 10 次调用）。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

EVAL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(EVAL))
from lib import model_client  # noqa: E402

BENCH = EVAL / "benchmarks"
MANIFEST = BENCH / "manifest_test_chunk6000.json"
RUN = BENCH / "results/vcsum-test26-preservation-20261005"
GOLD = EVAL / "gold/vcsum_action_items_ai.json"
OUT = EVAL / "results/action_item_gold_eval.json"

SYSTEM = """你是会议行动项标注员。输入是一场会议的完整转写。
请只抽取**明确的行动项/承诺/后续安排**（谁要做什么、何时完成、需要交付什么）；
不要抽泛泛讨论、观点、背景信息。每条输出：
- content：一句话，动宾结构，保留原文关键限定；
- owner：原文明确指定的负责人（人名/角色）；未指定写 null；
- deadline：原文明确的时间/期限；未指定写 null；
- evidence_quote：转写中能支撑这条的原文句子（逐字摘录，≤120 字）；
- kind：commitment / follow_up / request 之一。
只输出 JSON：{"items":[...]}。没有行动项时输出 {"items":[]}。"""


def caller() -> tuple[str, str, str]:
    return model_client.endpoint("deepseek")


def main() -> int:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    cases = sorted(manifest["cases"], key=lambda case: str(case["id"]))[:10]
    url, model, key = caller()
    gold_cases = []
    for case in cases:
        transcript = case.get("transcript") or ""
        user = f"会议 {case['id']} 的转写如下：\n\n{transcript}"
        try:
            parsed = model_client.call_json(
                url, model, key, SYSTEM, user, timeout=180,
                total_deadline=240, max_tokens=3000, thinking=False)
        except model_client.ModelClientError as error:
            gold_cases.append({"case_id": case["id"], "error": str(error)[:200]})
            print("FAIL", case["id"], error)
            continue
        payload = parsed.get("parsed") if isinstance(parsed, dict) else None
        items = payload.get("items") if isinstance(payload, dict) else None
        gold_cases.append({"case_id": case["id"], "items": items or []})
        print("ok", case["id"], len(items or []))
    GOLD.parent.mkdir(parents=True, exist_ok=True)
    GOLD.write_text(json.dumps({
        "label_source": "ai_draft_deepseek",
        "human_checked": False,
        "rubric": SYSTEM,
        "case_count": len(gold_cases),
        "cases": gold_cases,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("wrote", GOLD)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
