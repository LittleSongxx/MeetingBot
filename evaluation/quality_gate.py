"""RAG 全链路质量闸：相对冻结基线回归 + 绝对下限，零模型调用。

阈值不是按当前分数“贴着”设的：基线冻结后只允许 2pp 波动；
低于绝对下限或低于基线 2pp 才失败。修改基线需要显式提交。
"""
from __future__ import annotations

import json
from pathlib import Path

EVAL = Path(__file__).resolve().parent
RESULTS = EVAL / "results"
BASELINE = EVAL / "baselines/quality_gate_baseline.json"

METRICS = ("context_recall", "context_precision", "faithfulness", "answer_relevancy")
FLOORS = {
    "p0": {"context_recall": 0.90, "context_precision": 0.55,
           "faithfulness": 0.85, "answer_relevancy": 0.95},
    "cross30": {"context_recall": 0.70, "context_precision": 0.55,
                "faithfulness": 0.80, "answer_relevancy": 0.90},
    "new_meeting": {"context_recall": 0.80, "context_precision": 0.55,
                    "faithfulness": 0.75, "answer_relevancy": 0.90},
    "mixed100": {"context_recall": 0.85, "context_precision": 0.60,
                 "faithfulness": 0.80, "answer_relevancy": 0.90},
}
TOLERANCE = 0.02
CLAIM_FLOOR = 0.15


def _rates(path: Path) -> dict[str, float]:
    data = json.loads(path.read_text(encoding="utf-8"))
    summary = data.get("summary") or {}
    return {metric: float(summary[metric]["rate"]) for metric in METRICS
            if summary.get(metric) and summary[metric].get("rate") is not None}


def main() -> int:
    if not BASELINE.exists():
        print(f"缺少冻结基线: {BASELINE}")
        return 2
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    p0_paths = [RESULTS / f"p0_fresh6_r{i}_packet.json" for i in (1, 2, 3)]
    missing = [str(path) for path in p0_paths if not path.exists()]
    if missing:
        print(f"缺少 P0 packet: {missing}")
        return 2
    p0 = {metric: round(sum(_rates(path)[metric] for path in p0_paths) / 3, 4)
          for metric in METRICS}
    values = {
        "p0": p0,
        "cross30": _rates(RESULTS / "cross30_fresh_packet.json"),
        "new_meeting": _rates(RESULTS / "new_meeting_packet.json"),
        "mixed100": _rates(RESULTS / "eval100_scope_fixed_packet.json"),
    }
    new_meeting_smoke = _rates(RESULTS / "new_meeting_scoped_packet.json")
    claim = json.loads((RESULTS / "claim_audit_cross30_fresh.json").read_text(encoding="utf-8"))
    claim_rate = float(claim.get("not_in_any_context_rate") or 0)
    rows = []
    failed = False
    for scope, metrics in values.items():
        for metric, value in metrics.items():
            floor = FLOORS[scope][metric]
            frozen = float(baseline[scope][metric])
            ok = value >= floor and value >= frozen - TOLERANCE
            rows.append((scope, metric, value, frozen, value - frozen, floor, ok))
            failed = failed or not ok
    frozen_claim = float(baseline["claim_not_in_any_context"])
    claim_ok = claim_rate <= CLAIM_FLOOR and claim_rate <= frozen_claim + TOLERANCE
    rows.append(("cross30", "claim_not_in_any_context", claim_rate, frozen_claim,
                 claim_rate - frozen_claim, CLAIM_FLOOR, claim_ok))
    failed = failed or not claim_ok
    print(f"{'scope':10} {'metric':24} {'value':>7} {'frozen':>7} {'delta':>7} {'floor':>7} ok")
    for metric, value in new_meeting_smoke.items():
        print(f"{'smoke':10} {metric:24} {value:7.3f} {'-':>7} {'-':>7} {'-':>7} yes")
    for scope, metric, value, frozen, delta, floor, ok in rows:
        print(f"{scope:10} {metric:24} {value:7.3f} {frozen:7.3f} {delta:+7.3f} {floor:7.3f} {'yes' if ok else 'NO'}")
    print(f"baseline: {BASELINE} (tolerance {TOLERANCE:.2f})")
    print("QUALITY GATE:", "FAIL" if failed else "PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
