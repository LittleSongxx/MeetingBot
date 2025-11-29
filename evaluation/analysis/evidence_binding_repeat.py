import json
import sys
from pathlib import Path

EVAL = Path("/home/song/code/Agent/media_agent/AIMeeting/evaluation")
sys.path.insert(0, str(EVAL))
from analysis.claim_citation_audit import audit_case  # noqa: E402

ARMS = {
    "off1": "cross30_bindoff_packet.json",
    "off2": "cross30_bindoff2_packet.json",
    "off3": "cross30_bindoff3_packet.json",
    "off4": "cross30_bindoff4_packet.json",
    "on1": "cross30_bindon_packet.json",
    "on2": "cross30_bindon2_packet.json",
    "on3": "cross30_bindon3_packet.json",
    "on4": "cross30_bindon4_packet.json",
}
KEYS = ("context_precision", "context_recall", "faithfulness",
        "faithfulness_content", "answer_relevancy")


def rate(value):
    return value.get("rate") if isinstance(value, dict) else value


def audit(packet):
    checked = bad_cited = bad_any = 0
    for case in packet["cases"]:
        row = audit_case(case)
        checked += row["numbers_checked"]
        bad_cited += len(row["unsupported_cited"])
        bad_any += len(row["unsupported_any"])
    return {"checked": checked, "cited": bad_cited, "any": bad_any,
            "cited_gap": round(bad_cited / checked, 4) if checked else None,
            "any_gap": round(bad_any / checked, 4) if checked else None}


rows = {}
for arm, name in ARMS.items():
    packet = json.loads((EVAL / "results" / name).read_text(encoding="utf-8"))
    rows[arm] = {"attribution": audit(packet),
                 "metrics": {key: rate((packet.get("summary") or {}).get(key)) for key in KEYS}}


def mean(arm_list, field):
    values = [rows[arm]["metrics"].get(field) for arm in arm_list]
    values = [value for value in values if value is not None]
    return round(sum(values) / len(values), 4) if values else None


def mean_att(arm_list, key):
    values = [rows[arm]["attribution"].get(key) for arm in arm_list]
    values = [value for value in values if value is not None]
    return round(sum(values) / len(values), 4) if values else None


off_arms = ["off1", "off2", "off3", "off4"]
on_arms = ["on1", "on2", "on3", "on4"]
off_m = {key: mean(off_arms, key) for key in KEYS}
on_m = {key: mean(on_arms, key) for key in KEYS}
off_att = {key: mean_att(off_arms, key) for key in ("cited_gap", "any_gap")}
on_att = {key: mean_att(on_arms, key) for key in ("cited_gap", "any_gap")}
delta = {key: round(((on_m.get(key) or 0) - (off_m.get(key) or 0)) * 100, 2)
         for key in KEYS if off_m.get(key) is not None}
paired = json.loads((EVAL / "results/evidence_binding_paired.json").read_text(encoding="utf-8"))
paired_delta = dict(paired["delta_pp"])
paired_before = (paired.get("arms") or {}).get("same_answers") or {}
paired_after = (paired.get("arms") or {}).get("same_answers_bound") or {}
paired_delta["any_gap_pp"] = round(
    ((paired_after.get("attribution", {}).get("any_gap") or 0)
     - (paired_before.get("attribution", {}).get("any_gap") or 0)) * 100, 2)
checks = {
    "cited_gap_abs_ge_5pp": bool((off_att["cited_gap"] or 0) - (on_att["cited_gap"] or 0) >= 0.05),
    "faithfulness_content_non_inferior_2pp": bool((on_m.get("faithfulness_content") or 0)
                                                  >= (off_m.get("faithfulness_content") or 0) - 0.02),
    "context_precision_non_inferior_2pp": bool((on_m.get("context_precision") or 0)
                                               >= (off_m.get("context_precision") or 0) - 0.02),
    "relevancy_non_inferior_2pp": bool((on_m.get("answer_relevancy") or 0)
                                       >= (off_m.get("answer_relevancy") or 0) - 0.02),
    "paired_replay_any_gap_unchanged": abs(paired_delta.get("any_gap_pp", 99)) <= 1.0,
    "paired_replay_faithfulness_non_inferior": paired_delta.get("faithfulness_content", -99) >= -2.0,
}
result = {
    "policy": "LEDGER §49c/§49d：3 次运行/臂的均值 + 同答案配对回放；判定线未改",
    "arms": rows,
    "means": {"off": {"metrics": off_m, "attribution": off_att},
              "on": {"metrics": on_m, "attribution": on_att}},
    "delta_pp": {**delta,
                 "cited_gap_pp": round(((on_att["cited_gap"] or 0) - (off_att["cited_gap"] or 0)) * 100, 2),
                 "any_gap_pp": round(((on_att["any_gap"] or 0) - (off_att["any_gap"] or 0)) * 100, 2)},
    "paired_replay": paired,
    "checks": checks,
    "s49_pass": all(checks.values()),
    "limitations": [
        "单裁判（deepseek-flash，非人工金标）",
        "每臂 3 次 cross30 运行；cross30 单次波动约 5pp",
        "配对回放是机制级验证（同一答案/上下文），跨臂是外部效度验证；两者并列",
    ],
}
OUT = EVAL / "results/evidence_binding_ab.json"
OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(json.dumps({"means": result["means"], "delta_pp": result["delta_pp"],
                  "checks": checks, "s49_pass": result["s49_pass"]},
                 ensure_ascii=False, indent=2))
