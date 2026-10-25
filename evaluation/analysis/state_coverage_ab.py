"""S2 A/B 分析：系列/状态覆盖（off/on 各 2 次运行）。

零模型调用：recall-v2 和工具统计从 packets/answers 复算；四指标取已判定 packet 的 summary。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

EVAL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(EVAL))
from lib import recall_quality  # noqa: E402

PACKETS = {
    "off1": "cross30_stateoff1_packet.json",
    "off2": "cross30_stateoff2_packet.json",
    "on1": "cross30_stateon1_packet.json",
    "on2": "cross30_stateon2_packet.json",
}
ANSWERS = {
    "off1": "cross30_stateoff1_answers.json",
    "off2": "cross30_stateoff2_answers.json",
    "on1": "cross30_stateon1_answers.json",
    "on2": "cross30_stateon2_answers.json",
}
KEYS = ("context_precision", "context_recall", "faithfulness",
        "faithfulness_content", "answer_relevancy")
STATE_CASES = {f"m{i:03d}" for i in range(11, 21)}


def rate(value):
    return value.get("rate") if isinstance(value, dict) else value


def arm_stats(arm: str) -> dict:
    packet = json.loads((EVAL / "results" / PACKETS[arm]).read_text(encoding="utf-8"))
    recall = recall_quality.retrievable_recall(packet, "cross_30", recall_quality.load_manifest())
    legacy = recall_quality.legacy_recall(packet)
    metrics = {key: rate((packet.get("summary") or {}).get(key)) for key in KEYS}
    answers = json.loads((EVAL / "results" / ANSWERS[arm]).read_text(encoding="utf-8"))
    state_calls = 0
    state_contexts = 0
    cases_with_state = 0
    for turn in answers["turns"]:
        calls = sum(1 for item in (turn.get("tools") or [])
                    if item.get("name") == "query_meeting_state")
        contexts = sum(1 for item in (turn.get("retrieved") or [])
                       if item.get("kind") == "state")
        state_calls += calls
        state_contexts += contexts
        if calls:
            cases_with_state += 1
    return {
        "recall_v2": recall,
        "legacy": legacy,
        "metrics": metrics,
        "state_tool_calls": state_calls,
        "state_contexts": state_contexts,
        "cases_with_state_call": cases_with_state,
    }


rows = {arm: arm_stats(arm) for arm in PACKETS}
means = {}
for group, arms in (("off", ("off1", "off2")), ("on", ("on1", "on2"))):
    means[group] = {
        "recall_v2_rate": round(sum(rows[a]["recall_v2"]["rate"] for a in arms) / 2, 4),
        "recall_v2_total": [rows[a]["recall_v2"]["total"] for a in arms],
        "legacy_rate": round(sum(rows[a]["legacy"]["rate"] for a in arms) / 2, 4),
        **{key: round(sum((rows[a]["metrics"].get(key) or 0) for a in arms) / 2, 4)
           for key in KEYS},
        "state_tool_calls": round(sum(rows[a]["state_tool_calls"] for a in arms) / 2, 1),
        "state_contexts": round(sum(rows[a]["state_contexts"] for a in arms) / 2, 1),
        "cases_with_state_call": round(sum(rows[a]["cases_with_state_call"] for a in arms) / 2, 1),
    }
delta = {
    "recall_v2_pp": round((means["on"]["recall_v2_rate"] - means["off"]["recall_v2_rate"]) * 100, 2),
    "precision_pp": round((means["on"]["context_precision"] - means["off"]["context_precision"]) * 100, 2),
    "faithfulness_content_pp": round(
        (means["on"]["faithfulness_content"] - means["off"]["faithfulness_content"]) * 100, 2),
    "relevancy_pp": round((means["on"]["answer_relevancy"] - means["off"]["answer_relevancy"]) * 100, 2),
    "state_tool_calls": means["on"]["state_tool_calls"] - means["off"]["state_tool_calls"],
}
checks = {
    "recall_v2_ge_3pp": delta["recall_v2_pp"] >= 3.0,
    "precision_non_inferior_2pp": delta["precision_pp"] >= -2.0,
    "faithfulness_non_inferior_2pp": delta["faithfulness_content_pp"] >= -2.0,
    "state_calls_not_lower": delta["state_tool_calls"] >= 0,
    "denominator_consistent": means["off"]["recall_v2_total"] == means["on"]["recall_v2_total"],
}
result = {
    "policy": "LEDGER §50：主指标 cross30 recall-v2 +3pp；precision/faith 非劣；状态调用不降；分母一致",
    "arms": rows,
    "means": means,
    "delta": delta,
    "checks": checks,
    "s50_pass": all(checks.values()),
    "limitations": [
        "单裁判（deepseek-flash，非人工金标）",
        "每臂 2 次 cross30 运行；单次波动约 5pp",
        "状态覆盖只注入一次提示，不强制调用；模型可以不采纳",
    ],
}
OUT = EVAL / "results/state_coverage_ab.json"
OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(json.dumps({"means": means, "delta": delta, "checks": checks,
                  "s50_pass": result["s50_pass"]}, ensure_ascii=False, indent=2))
