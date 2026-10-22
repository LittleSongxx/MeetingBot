"""RAG 全链路质量闸：零模型调用，读取既有 packet，按阈值失败退出。

用法：python evaluation/quality_gate.py
"""
from __future__ import annotations

import json
from pathlib import Path

EVAL = Path(__file__).resolve().parent
RESULTS = EVAL / "results"

THRESHOLDS = {
    "p0": {"context_recall": 0.90, "context_precision": 0.58,
           "faithfulness": 0.85, "answer_relevancy": 0.95},
    "cross30": {"context_recall": 0.70, "context_precision": 0.60,
                "faithfulness": 0.85, "answer_relevancy": 0.90},
    "new_meeting": {"context_recall": 0.80, "context_precision": 0.60,
                    "faithfulness": 0.75, "answer_relevancy": 0.90},
    "mixed100": {"context_recall": 0.85, "context_precision": 0.65,
                 "faithfulness": 0.87, "answer_relevancy": 0.93},
    "claim_not_in_any_context": 0.12,
}
METRICS = ("context_recall", "context_precision", "faithfulness", "answer_relevancy")


def _rates(path: Path) -> dict[str, float]:
    data = json.loads(path.read_text(encoding="utf-8"))
    summary = data.get("summary") or {}
    return {metric: float(summary[metric]["rate"]) for metric in METRICS
            if summary.get(metric) and summary[metric].get("rate") is not None}


def main() -> int:
    p0_paths = [RESULTS / f"p0_fresh6_r{i}_packet.json" for i in (1, 2, 3)]
    missing = [str(path) for path in p0_paths if not path.exists()]
    if missing:
        print(f"缺少 P0 packet: {missing}")
        return 2
    p0 = {metric: sum(_rates(path)[metric] for path in p0_paths) / 3 for metric in METRICS}
    cross30 = _rates(RESULTS / "cross30_fresh_packet.json")
    new_meeting = _rates(RESULTS / "new_meeting_packet.json")
    new_meeting_smoke = _rates(RESULTS / "new_meeting_scoped_packet.json")
    mixed100 = _rates(RESULTS / "eval100_packet.json")
    claim = json.loads((RESULTS / "claim_audit_cross30_fresh.json").read_text(encoding="utf-8"))
    claim_rate = float(claim.get("not_in_any_context_rate") or 0)
    rows = []
    failed = False
    for name, values in (("p0", p0), ("cross30", cross30),
                     ("new_meeting", new_meeting), ("mixed100", mixed100)):
        for metric in METRICS:
            threshold = THRESHOLDS[name][metric]
            ok = values[metric] >= threshold
            rows.append((name, metric, values[metric], threshold, ok))
            failed = failed or not ok
    ok = claim_rate <= THRESHOLDS["claim_not_in_any_context"]
    rows.append(("cross30", "claim_not_in_any_context", claim_rate,
                 THRESHOLDS["claim_not_in_any_context"], ok))
    failed = failed or not ok
    print(f"{'scope':8} {'metric':28} {'value':>8} {'threshold':>9} ok")
    for metric, value in new_meeting_smoke.items():
        print(f"{'smoke':8} {metric:28} {value:8.3f} {'(no gate)':>9} yes")
    for name, metric, value, threshold, ok in rows:
        print(f"{name:8} {metric:28} {value:8.3f} {threshold:9.3f} {'yes' if ok else 'NO'}")
    print("QUALITY GATE:", "FAIL" if failed else "PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
