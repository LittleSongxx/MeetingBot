"""多时间点自适应检索深度 A/B（LEDGER §58，零模型调用复算）。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

EVAL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(EVAL))
from lib import recall_quality  # noqa: E402

SETS = {
    "cross30": {
        "set": "cross_30",
        "off": [f"cross30_enrichon{i}_packet.json" for i in (1, 2, 3, 4)],
        "on": [f"cross30_depthon{i}_packet.json" for i in (1, 2, 3, 4)],
    },
    "new_meeting": {
        "set": "new_meeting_15",
        "off": ["newmeeting_off_packet.json", "newmeeting_off2_packet.json",
                "newmeeting_off3_packet.json"],
        "on": [f"newmeeting_depthon{i}_packet.json" for i in (1, 2, 3, 4)],
    },
    "mixed20": {
        "set": "eval_100_mixed",
        "off": ["mixed20_off_packet.json"],
        "on": [f"mixed20_depthon{i}_packet.json" for i in (1, 2)],
    },
}
KEYS = ("context_precision", "faithfulness_content", "answer_relevancy")


def rate(value):
    return value.get("rate") if isinstance(value, dict) else value


def load_arm(paths: list[str], set_name: str) -> dict:
    rows = []
    for name in paths:
        packet = json.loads((EVAL / "results" / name).read_text(encoding="utf-8"))
        recall = recall_quality.retrievable_recall(packet, set_name,
                                                  recall_quality.load_manifest())
        rows.append({"recall_v2": recall["rate"], "total": recall["total"],
                     **{key: rate((packet.get("summary") or {}).get(key)) for key in KEYS}})
    return rows


def mean(rows: list[dict], key: str):
    values = [row[key] for row in rows if row.get(key) is not None]
    return round(sum(values) / len(values), 4) if values else None


out = {}
for name, spec in SETS.items():
    off_rows = load_arm(spec["off"], spec["set"])
    on_rows = load_arm(spec["on"], spec["set"])
    means = {"off": {key: mean(off_rows, key) for key in ("recall_v2", *KEYS)},
             "on": {key: mean(on_rows, key) for key in ("recall_v2", *KEYS)}}
    delta = {key: round((means["on"][key] - means["off"][key]) * 100, 2)
             for key in ("recall_v2", *KEYS)}
    out[name] = {"n_off": len(off_rows), "n_on": len(on_rows),
                 "off_runs": off_rows, "on_runs": on_rows,
                 "means": means, "delta_pp": delta}
main = out["cross30"]
checks = {
    "recall_v2_ge_3pp": main["delta_pp"]["recall_v2"] >= 3.0,
    "precision_non_inferior_2pp": main["delta_pp"]["context_precision"] >= -2.0,
    "faithfulness_non_inferior_2pp": main["delta_pp"]["faithfulness_content"] >= -2.0,
    "denominator_consistent": all(row["total"] == main["off_runs"][0]["total"]
                                  for row in main["off_runs"] + main["on_runs"]),
}
out["policy"] = ("LEDGER §58：多时间点/时间线问题 top_k 3→5；主集 recall-v2 +3pp，"
                 "precision/faith 非劣 2pp；泛化集同向或中性")
out["checks"] = checks
out["cross30_pass"] = all(checks.values())
out["generalization"] = {}
for name in ("new_meeting", "mixed20"):
    delta = out[name]["delta_pp"]
    out["generalization"][name] = {
        "neutral_or_better": (delta["recall_v2"] >= -1.0
                              and delta["context_precision"] >= -2.0
                              and delta["faithfulness_content"] >= -2.0),
    }
out["pass"] = out["cross30_pass"] and all(
    item["neutral_or_better"] for item in out["generalization"].values())
path = EVAL / "results/adaptive_depth_ab.json"
path.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(json.dumps({"cross30": {key: out["cross30"][key] for key in
                                ("means", "delta_pp", "n_off", "n_on")},
                   "new_meeting": out["new_meeting"]["delta_pp"],
                   "mixed20": out["mixed20"]["delta_pp"],
                   "checks": out["checks"], "cross30_pass": out["cross30_pass"],
                   "generalization": out["generalization"], "pass": out["pass"]},
                  ensure_ascii=False, indent=2))
