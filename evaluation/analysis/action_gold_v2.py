"""S3 仪器 v2：严格行动项定义 + gold/抽取（deepseek gold，qwen 抽取）。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

EVAL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(EVAL))
from lib import model_client  # noqa: E402

MANIFEST = EVAL / "benchmarks/manifest_test_chunk6000.json"
OUT = EVAL / "gold/vcsum_action_items_ai_v2.json"

RUBRIC = """你是会议行动项标注员。严格定义：
Action item = 未来需要**某人/某方执行的可执行动作、承诺或后续安排**。
- 允许 owner/deadline 为空，但必须有明确动作与对象；
- 排除：纯观点/建议/评论；会议流程与活动安排（合影、下一场圆桌、直播串场）；
  过去事实；没有动作的泛泛目标。
正例： “张涛负责在周五前完成报表导出异步化改造”（承诺+负责人+期限）
      “会后由运营团队梳理客户反馈清单”（后续安排）
负例： “请大家合影留念”（流程） “建议家长多陪孩子读书”（建议非承诺）
      “AI 是未来趋势”（观点） “去年完成了云化”（过去事实）
每条输出：content（一句话，动宾结构）、owner（明确负责人，否则 null）、
deadline（明确期限，否则 null）、evidence_quote（原文逐字≤120字）、
kind（commitment/follow_up/assignment/other）。
只输出 JSON：{"items":[...]}；没有则 {"items":[]}。"""


def call(provider: str, case_id: str, transcript: str) -> dict:
    url, model, key = model_client.endpoint(provider)
    parsed = model_client.call_json(
        url, model, key, RUBRIC,
        f"会议 {case_id} 的完整转写：\n\n{transcript}",
        timeout=240, total_deadline=360, max_tokens=4000, thinking=False)
    payload = parsed.get("parsed") or {}
    items = payload.get("items") if isinstance(payload, dict) else []
    return {"items": items if isinstance(items, list) else [],
            "model": model, "usage": parsed.get("usage")}


def main() -> int:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    cases = {case["id"]: case for case in sorted(manifest["cases"], key=lambda c: str(c["id"]))[:10]}
    output = {"label_source": "ai_draft_strict_rubric", "human_checked": False,
              "rubric": RUBRIC, "gold_provider": "deepseek", "extract_provider": "qwen",
              "cases": []}
    for case_id, case in cases.items():
        transcript = case["transcript"]
        try:
            gold = call("deepseek", case_id, transcript)
        except model_client.ModelClientError as error:
            output["cases"].append({"case_id": case_id, "error": f"gold: {error}"})
            continue
        try:
            extracted = call("qwen", case_id, transcript)
        except model_client.ModelClientError as error:
            extracted = {"items": [], "error": str(error)}
        output["cases"].append({
            "case_id": case_id,
            "gold": gold["items"],
            "qwen": extracted.get("items") or [],
            "qwen_error": extracted.get("error"),
        })
        print(case_id, "gold", len(gold["items"]), "qwen", len(extracted.get("items") or []))
    OUT.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("wrote", OUT)


if __name__ == "__main__":
    main()
