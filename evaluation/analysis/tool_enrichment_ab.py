"""S2 备选 A/B：工具契约扩展（系列/状态元数据）off/on 各 2 次运行。零模型调用复算。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

EVAL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(EVAL))
from lib import recall_quality  # noqa: E402

PACKETS = {f"off{i}": f"cross30_enrichoff{i}_packet.json" for i in (1, 2, 3, 4)}
PACKETS.update({f"on{i}": f"cross30_enrichon{i}_packet.json" for i in (1, 2, 3, 4)})
ANSWERS = {f"off{i}": f"cross30_enrichoff{i}_answers.json" for i in (1, 2, 3, 4)}
ANSWERS.update({f"on{i}": f"cross30_enrichon{i}_answers.json" for i in (1, 2, 3, 4)})
KEYS = ("context_precision", "context_recall", "faithfulness",
        "faithfulness_content", "answer_relevancy")


def rate(value):
    return value.get("rate") if isinstance(value, dict) else value


def arm(arm_name: str) -> dict:
    packet = json.loads((EVAL / "results" / PACKETS[arm_name]).read_text(encoding="utf-8"))
    recall = recall_quality.retrievable_recall(packet, "cross_30", recall_quality.load_manifest())
    answers = json.loads((EVAL / "results" / ANSWERS[arm_name]).read_text(encoding="utf-8"))
    series_ctx = sum(1 for turn in answers["turns"] for item in (turn.get("retrieved") or [])
                     if "系列内会议" in str(item.get("source_text") or ""))
    state_ctx = sum(1 for turn in answers["turns"] for item in (turn.get("retrieved") or [])
                    if item.get("kind") == "state")
    meeting_status = sum(1 for turn in answers["turns"] for item in (turn.get("retrieved") or [])
                         if "来源纪要状态" in str(item.get("source_text") or ""))
    return {"recall_v2": recall, "metrics": {key: rate((packet.get("summary") or {}).get(key))
                                             for key in KEYS},
            "series_contexts": series_ctx, "state_contexts": state_ctx,
            "minutes_status_contexts": meeting_status}


rows = {name: arm(name) for name in PACKETS}
means = {}
for group, names in (("off", ("off1", "off2", "off3", "off4")),
                      ("on", ("on1", "on2", "on3", "on4"))):
    means[group] = {
        "recall_v2": round(sum(rows[n]["recall_v2"]["rate"] for n in names) / len(names), 4),
        "recall_total": [rows[n]["recall_v2"]["total"] for n in names],
        "series_contexts": round(sum(rows[n]["series_contexts"] for n in names) / len(names), 1),
        "minutes_status_contexts": round(sum(rows[n]["minutes_status_contexts"] for n in names) / len(names), 1),
        **{key: round(sum((rows[n]["metrics"].get(key) or 0) for n in names) / len(names), 4)
           for key in KEYS},
    }
delta = {
    "recall_v2_pp": round((means["on"]["recall_v2"] - means["off"]["recall_v2"]) * 100, 2),
    "precision_pp": round((means["on"]["context_precision"] - means["off"]["context_precision"]) * 100, 2),
    "faithfulness_content_pp": round(
        (means["on"]["faithfulness_content"] - means["off"]["faithfulness_content"]) * 100, 2),
    "relevancy_pp": round((means["on"]["answer_relevancy"] - means["off"]["answer_relevancy"]) * 100, 2),
}
checks = {
    "recall_v2_ge_3pp": delta["recall_v2_pp"] >= 3.0,
    "precision_non_inferior_2pp": delta["precision_pp"] >= -2.0,
    "faithfulness_non_inferior_2pp": delta["faithfulness_content_pp"] >= -2.0,
    "denominator_consistent": means["off"]["recall_total"] == means["on"]["recall_total"],
}
result = {"policy": "LEDGER §53：工具契约扩展（系列/状态元数据），不改提示词；主指标 recall-v2 +3pp",
          "arms": rows, "means": means, "delta": delta, "checks": checks,
          "pass": all(checks.values()),
          "limitations": ["单裁判", "每臂 4 次运行；单次波动约 5pp",
                          "cross30 是主集；泛化集（new_meeting/mixed100）待跑"]}
out = EVAL / "results/tool_enrichment_ab.json"
out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(json.dumps({"means": means, "delta": delta, "checks": checks, "pass": result["pass"]},
                 ensure_ascii=False, indent=2))
