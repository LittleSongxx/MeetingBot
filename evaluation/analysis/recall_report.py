"""可检索事实召回报告（零模型调用）：旧口径 vs recall-v2。

用法：
    python3 evaluation/analysis/recall_report.py
输出：`evaluation/results/recall_quality_report.json` + stdout 表格。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

EVAL = Path(__file__).resolve().parents[1]
if str(EVAL) not in sys.path:
    sys.path.insert(0, str(EVAL))

from lib import recall_quality, stats  # noqa: E402

RESULTS = EVAL / "results"


def _load(name: str) -> dict:
    return json.loads((RESULTS / name).read_text(encoding="utf-8"))


SCOPES = [
    ("p0", "p0_fresh6_r1_packet.json", "cross_30",
     ["p0_fresh6_r2_packet.json", "p0_fresh6_r3_packet.json"]),
    ("cross30", "cross30_fresh_packet.json", "cross_30", []),
    ("new_meeting", "new_meeting_packet.json", "new_meeting_15", []),
    ("mixed100", "eval100_scope_fixed_packet.json", "eval_100_mixed", []),
    ("extended68", "rag_packet_extended_tools2_final.json", "qa_seed_extended", []),
]


def main() -> int:
    manifest = recall_quality.load_manifest()
    rows = []
    for scope, first, set_name, others in SCOPES:
        packets = [_load(first)] + [_load(name) for name in others]
        if not recall_quality.has_set(set_name, manifest):
            rows.append({"scope": scope, "status": "清单缺该集", "set": set_name})
            continue
        new_success = new_total = legacy_success = legacy_total = excluded = 0
        skipped = 0
        for packet in packets:
            measured = recall_quality.retrievable_recall(packet, set_name, manifest)
            legacy = recall_quality.legacy_recall(packet)
            new_success += measured["successes"]
            new_total += measured["total"]
            excluded += measured["excluded"]
            skipped += measured["skipped_cases"]
            legacy_success += int(legacy["successes"] or 0)
            legacy_total += int(legacy["total"] or 0)
        row = {
            "scope": scope,
            "set": set_name,
            "legacy": (stats.proportion_report(legacy_success, legacy_total)
                       if legacy_total else None),
            "recall_v2": (stats.proportion_report(new_success, new_total)
                          if new_total else None),
            "excluded_non_retrievable": excluded,
            "skipped_cases": skipped,
            "delta_pp": ((new_success / new_total) - (legacy_success / legacy_total)) * 100
                        if new_total and legacy_total else None,
            "manifest_generated_at": manifest.get("generated_at"),
            "manifest_human_checked": manifest.get("human_checked"),
        }
        rows.append(row)
    payload = {
        "instrument": "recall-v2",
        "manifest": str(recall_quality.MANIFEST.relative_to(EVAL.parent)),
        "note": "recall-v2 只收窄分母（剔除 metadata/fixture/conflict）；新读数不能与旧口径相减。",
        "scopes": rows,
    }
    out = RESULTS / "recall_quality_report.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{'scope':12} {'legacy':>10} {'recall_v2':>10} {'excluded':>9} {'delta_pp':>9}")
    for row in rows:
        legacy = row.get("legacy") or {}
        new = row.get("recall_v2") or {}
        print(f"{row['scope']:12} "
              f"{(legacy.get('rate') or 0):10.4f} "
              f"{(new.get('rate') or 0):10.4f} "
              f"{row.get('excluded_non_retrievable', 0):9d} "
              f"{(row.get('delta_pp') or 0):+9.2f}")
    print(f"→ {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
