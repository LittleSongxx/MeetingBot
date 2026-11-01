"""质量门禁：把 QA / claim support / QMSum / Agent scorecard 指标固化为 PASS/FAIL。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

THRESHOLDS = {
    "qa": {
        "citation_precision": 0.90,
        "citation_recall": 0.80,
        "found_rate": 0.90,
        "refusal_accuracy": 0.95,
    },
    "support": {
        "claim_support_rate": 0.80,
        "fully_supported_rate": 0.60,
    },
    "qmsum": {
        "mean_keypoint_coverage": 0.35,
        "valid_quote_rate": 0.95,
    },
    "scorecard": {
        "calls.success_rate": 0.99,
        "evidence.item_coverage": 0.95,
        "evidence.validity": 0.99,
        "tasks.agent_completed_rate": 0.95,
        "actions.owner_coverage": 0.80,
        "actions.due_coverage": 0.70,
        "actions.duplicate_rate": 0.05,  # max
    },
}


def nested(data: dict, path: str):
    value = data
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def evaluate(name: str, data: dict) -> list[dict]:
    rows = []
    thresholds = THRESHOLDS[name]
    for metric, threshold in thresholds.items():
        value = nested(data, metric)
        if value is None:
            rows.append({"metric": metric, "value": None, "threshold": threshold, "passed": False})
            continue
        passed = value <= threshold if metric.endswith("duplicate_rate") else value >= threshold
        rows.append({"metric": metric, "value": value, "threshold": threshold, "passed": passed})
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--qa")
    parser.add_argument("--support")
    parser.add_argument("--qmsum")
    parser.add_argument("--scorecard")
    args = parser.parse_args()
    inputs = {"qa": args.qa, "support": args.support, "qmsum": args.qmsum, "scorecard": args.scorecard}
    all_rows = []
    for name, path in inputs.items():
        if not path:
            continue
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if name == "qa":
            data = data.get("metrics") or {}
        elif name == "support":
            data = data.get("summary") or {}
        elif name == "qmsum":
            data = data.get("summary") or data
        for row in evaluate(name, data):
            all_rows.append({"group": name, **row})
    if not all_rows:
        print("no inputs")
        return 2
    for row in all_rows:
        status = "PASS" if row["passed"] else "FAIL"
        print(f"{status:4} {row['group']:10} {row['metric']:32} value={row['value']} threshold={row['threshold']}")
    failed = [row for row in all_rows if not row["passed"]]
    print(f"\n{len(all_rows) - len(failed)}/{len(all_rows)} passed")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
