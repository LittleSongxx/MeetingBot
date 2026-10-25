"""轴4 行动项槽位审计：26 场 VCSum 产品纪要（确定性，零模型调用）。

数据：`benchmarks/results/vcsum-test26-singlepass-r2-20261006/vcsum_*.json` 的
`revised.output.pending_items/decisions` + `benchmarks/manifest_test_chunk6000.json` 的转写。
指标：owner fabricated、deadline misuse、evidence unlocated；以及校验器会降级的条数。
"""
from __future__ import annotations

import argparse
import contextlib
import json
import sys
from collections import Counter
from pathlib import Path

EVAL = Path(__file__).resolve().parents[1]
REPO = EVAL.parent
sys.path.insert(0, str(REPO / "meetingbot/fastapi-app"))

try:  # 产品 venv 有 pydantic；纯评测 venv 没有时跳过契约升级（只支持类型化槽位）
    from services import contracts  # noqa: E402
except Exception:  # noqa: BLE001
    contracts = None
from services.action_validate import validate_item  # noqa: E402

BENCH = EVAL / "benchmarks"
DEFAULT_RUN = BENCH / "results/vcsum-test26-preservation-20261005"
DEFAULT_MANIFEST = BENCH / "manifest_test_chunk6000.json"
DEFAULT_OUT = EVAL / "results/action_item_audit.json"
DEFAULT_MD = EVAL / "reports/2026-10-23_action_item_audit.md"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", type=Path, default=DEFAULT_RUN)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--out-json", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--out-md", type=Path, default=DEFAULT_MD)
    args = parser.parse_args()
    run, manifest_path = args.run.resolve(), args.manifest.resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    transcripts = {case["id"]: case.get("transcript") or "" for case in manifest["cases"]}
    rows = []
    for path in sorted(run.glob("vcsum_*.json")):
        if not path.stem.split("_")[-1].isdigit():
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        case_id = payload.get("case_id") or path.stem
        transcript = transcripts.get(case_id) or ""
        output = ((payload.get("revised") or {}).get("output") or {})
        for field in ("pending_items", "decisions"):
            for index, item in enumerate(output.get(field) or []):
                if not isinstance(item, dict):
                    continue
                # 与产品运行时一致：先走契约升级（旧 schema 的 owner_suggestion 等转成类型化槽位）
                if contracts is not None:
                    with contextlib.suppress(Exception):  # 审计对坏条目 fail-open
                        item = contracts.upgrade_output(
                            {"pending_items": [item]}).get("pending_items", [item])[0]
                check = validate_item(item, transcript)
                rows.append({"case_id": case_id, "field": field, "index": index,
                             "content": str(item.get("content") or "")[:80], **check})
    total = len(rows)
    owner_counts = Counter(row["owner_status"] for row in rows)
    deadline_counts = Counter(row["deadline_status"] for row in rows)
    evidence_counts = Counter(row["evidence_status"] for row in rows)
    with_owner = total - owner_counts.get("missing", 0)
    with_deadline = total - deadline_counts.get("missing", 0)
    result = {
        "source": str(run.relative_to(EVAL)),
        "manifest": str(manifest_path.relative_to(EVAL)),
        "items": total,
        "owner": {
            "with_owner": with_owner,
            "fabricated": owner_counts.get("fabricated", 0),
            "fabricated_rate": round(owner_counts.get("fabricated", 0) / with_owner, 4) if with_owner else None,
            "located": owner_counts.get("located", 0),
        },
        "deadline": {
            "with_deadline": with_deadline,
            "misuse": deadline_counts.get("misuse", 0),
            "unlocated": deadline_counts.get("unlocated", 0),
            "misuse_rate": round(deadline_counts.get("misuse", 0) / with_deadline, 4) if with_deadline else None,
            "located": deadline_counts.get("located", 0),
        },
        "evidence": {
            "located": evidence_counts.get("located", 0),
            "unlocated": evidence_counts.get("unlocated", 0),
            "missing": evidence_counts.get("missing", 0),
            "unlocated_rate": round(evidence_counts.get("unlocated", 0) / total, 4) if total else None,
        },
        "validator_downgrades": {
            "owner": sum(1 for row in rows if row["drop_owner"]),
            "deadline": sum(1 for row in rows if row["drop_deadline"]),
            "evidence": sum(1 for row in rows if row["drop_evidence"]),
        },
        "by_case": {},
        "limitations": [
            "转写来自 VCSum 官方 test26（人工整理转写），不是产品 ASR；owner/deadline 是规则代理，不是人工金标",
            "只审计 pending_items/decisions 的槽位，不评行动项抽取召回（无 gold 行动项集）",
            "校验器降级 ≠ 指标提升：它保证的是'不把无依据槽位当事实写进待办'",
        ],
    }
    for row in rows:
        case = result["by_case"].setdefault(row["case_id"], {"items": 0, "owner_fabricated": 0,
                                                             "deadline_misuse": 0,
                                                             "evidence_unlocated": 0})
        case["items"] += 1
        case["owner_fabricated"] += 1 if row["owner_status"] == "fabricated" else 0
        case["deadline_misuse"] += 1 if row["deadline_status"] == "misuse" else 0
        case["evidence_unlocated"] += 1 if row["evidence_status"] == "unlocated" else 0
    args.out_json.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    lines = [
        "# 行动项槽位审计：26 场 VCSum 产品纪要（2026-10-23，零模型调用）",
        "",
        "> 数据：`benchmarks/results/vcsum-test26-singlepass-r2-20261006` 的产品 `revised` 纪要 +",
        "> `benchmarks/manifest_test_chunk6000.json` 官方转写；规则代理，非人工金标。",
        "",
        "## 结果",
        "",
        "| 量 | 读数 |",
        "|---|---|",
        f"| 行动项/决策条目 | {total} |",
        f"| 有负责人的条目 | {with_owner} |",
        f"| **owner fabricated** | **{result['owner']['fabricated']} / {with_owner} "
        f"= {result['owner']['fabricated_rate']}** |",
        f"| 有期限的条目 | {with_deadline} |",
        f"| **deadline field_misuse** | **{result['deadline']['misuse']} / {with_deadline} "
        f"= {result['deadline']['misuse_rate']}** |",
        f"| deadline 在转写中不可定位 | {result['deadline']['unlocated']} |",
        f"| evidence 不可定位（LCS<10） | {result['evidence']['unlocated']} / {total} "
        f"= {result['evidence']['unlocated_rate']} |",
        "",
        "## 校验器将降级的条数（不覆盖原文，只不把无依据槽位写成事实）",
        "",
        f"- owner 降级：{result['validator_downgrades']['owner']} 条",
        f"- deadline 降级：{result['validator_downgrades']['deadline']} 条",
        f"- evidence 降级：{result['validator_downgrades']['evidence']} 条",
        "",
        "## 限制",
        "",
    ]
    lines += [f"- {item}" for item in result["limitations"]]
    args.out_md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({key: result[key] for key in
                      ("items", "owner", "deadline", "evidence", "validator_downgrades")},
                     ensure_ascii=False, indent=2))
    print(f"→ {args.out_json}\n→ {args.out_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
