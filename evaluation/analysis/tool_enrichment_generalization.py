"""S2 备选泛化检查：工具扩展在 new_meeting / mixed100 子集上的方向。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

EVAL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(EVAL))
from lib import recall_quality  # noqa: E402

CASES = {
    "new_meeting": {"set": "new_meeting_15",
                    "off": EVAL / "results/newmeeting_off_packet.json",
                    "on": EVAL / "results/newmeeting_on_packet.json"},
    "mixed20": {"set": "eval_100_mixed",
                "off": EVAL / "results/mixed20_off_packet.json",
                "on": EVAL / "results/mixed20_on_packet.json"},
}
KEYS = ("context_precision", "faithfulness_content", "answer_relevancy")


def rate(value):
    return value.get("rate") if isinstance(value, dict) else value


out = {}
for name, spec in CASES.items():
    row = {}
    for arm in ("off", "on"):
        packet = json.loads(spec[arm].read_text(encoding="utf-8"))
        recall = recall_quality.retrievable_recall(packet, spec["set"],
                                                  recall_quality.load_manifest())
        row[arm] = {"n": len(packet.get("cases") or []),
                    "recall_v2": recall,
                    **{key: rate((packet.get("summary") or {}).get(key)) for key in KEYS}}
    row["delta_pp"] = {
        "recall_v2": round((row["on"]["recall_v2"]["rate"] - row["off"]["recall_v2"]["rate"]) * 100, 2),
        **{key: round(((row["on"].get(key) or 0) - (row["off"].get(key) or 0)) * 100, 2)
           for key in KEYS},
    }
    row["neutral_or_better"] = (
        row["delta_pp"]["recall_v2"] >= -1.0
        and row["delta_pp"]["context_precision"] >= -2.0
        and row["delta_pp"]["faithfulness_content"] >= -2.0)
    out[name] = row
out["policy"] = "LEDGER §53：泛化集（new_meeting 15 题 / mixed100 子集 20 题）必须同向或中性"
out["pass"] = all(item["neutral_or_better"] for item in
                  (out["new_meeting"], out["mixed20"]))
path = EVAL / "results/tool_enrichment_generalization.json"
path.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(json.dumps(out, ensure_ascii=False, indent=2))
