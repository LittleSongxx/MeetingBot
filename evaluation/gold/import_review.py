"""把人工复核 CSV 导回金标：意图 100 条 + 扩展集 68 条。

## 为什么单独一个工具

人工确认这一步不能在评测脚本里"顺手改"：一旦把代理标签和人工标签混在一个文件里，
F1 的分母就说不清来源。这里只做**机械导入 + 校验**，并把结果写到新文件
（`qa_seed_human_reviewed.json` / `qa_seed_extended_human.json`），原文件不动。

## 两条规则

- 意图 CSV：`label` 为空的行保持 `agent_draft`；填了合法四类之一才改成 `human`。
- 扩展集 CSV：`human_worth_asking` 与 `human_gold_correct` 都填 yes/是/1/true 才把
  `case_source` 改成 `human`；否则保留 `agent_draft` 并把人工意见留在
  `human_review` 里（"这个问题不值得问"本身是有价值的标注，不该被静默丢掉）。

## 不做的事

不猜标签、不把空行补成代理标签、不覆盖原文件。人工没填的行如实保持原状。
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping

EVALUATION_DIR = Path(__file__).resolve().parents[1]
DEFAULT_QA_SET = EVALUATION_DIR / "gold" / "qa_seed.json"
DEFAULT_EXTENDED = EVALUATION_DIR / "gold" / "qa_seed_extended.json"
INTENT_LABELS = {"single_fact", "aggregation", "cross_meeting", "smalltalk"}
YES = {"yes", "y", "是", "1", "true"}


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def apply_intent(qa_set: Mapping[str, Any], rows: Iterable[Mapping[str, str]]) -> dict[str, Any]:
    """把意图人工标签写进 qa_set 的副本；返回报告。"""
    by_id = {str(case["id"]): case for case in qa_set.get("cases") or []}
    updated = unknown = skipped = 0
    unknown_labels: list[dict[str, str]] = []
    for row in rows:
        case_id = str(row.get("anonymous_id") or "").strip()
        label = str(row.get("label") or "").strip()
        if not label:
            skipped += 1
            continue
        case = by_id.get(case_id)
        if case is None or case.get("unanswerable"):
            unknown += 1
            continue
        if label not in INTENT_LABELS:
            unknown_labels.append({"case_id": case_id, "label": label})
            continue
        case["intent"] = label
        case["intent_source"] = "human"
        note = str(row.get("note") or "").strip()
        if note:
            case["intent_review_note"] = note
        updated += 1
    return {
        "updated": updated,
        "blank_rows": skipped,
        "unknown_cases": unknown,
        "unknown_labels": unknown_labels,
        "remaining_agent_draft": sum(
            1 for case in qa_set.get("cases") or []
            if not case.get("unanswerable") and case.get("intent_source") != "human"),
    }


def apply_extended(qa_set: Mapping[str, Any],
                   rows: Iterable[Mapping[str, str]]) -> dict[str, Any]:
    """把扩展集人工复核写进副本；返回报告。"""
    by_id = {str(case["id"]): case for case in qa_set.get("cases") or []}
    verified = rejected = blank = unknown = 0
    for row in rows:
        case_id = str(row.get("case_id") or "").strip()
        case = by_id.get(case_id)
        if case is None:
            unknown += 1
            continue
        worth = str(row.get("human_worth_asking") or "").strip().lower()
        correct = str(row.get("human_gold_correct") or "").strip().lower()
        note = str(row.get("human_note") or "").strip()
        if not worth and not correct:
            blank += 1
            continue
        review = {"worth_asking": worth, "gold_correct": correct, "note": note}
        case["human_review"] = review
        if worth in YES and correct in YES:
            case["case_source"] = "human"
            verified += 1
        else:
            # 保留 agent_draft；人工的否定意见留在 human_review 里，供后续修订
            case["case_source"] = "agent_draft"
            rejected += 1
    return {"human_verified": verified, "human_rejected": rejected,
            "blank_rows": blank, "unknown_cases": unknown}


def _write(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--intent-csv", type=Path, default=None)
    parser.add_argument("--qa-set", type=Path, default=DEFAULT_QA_SET)
    parser.add_argument("--intent-output", type=Path,
                        default=EVALUATION_DIR / "gold" / "qa_seed_human_reviewed.json")
    parser.add_argument("--extended-csv", type=Path, default=None)
    parser.add_argument("--extended-qa-set", type=Path, default=DEFAULT_EXTENDED)
    parser.add_argument("--extended-output", type=Path,
                        default=EVALUATION_DIR / "gold" / "qa_seed_extended_human.json")
    args = parser.parse_args(list(argv) if argv is not None else None)
    if not args.intent_csv and not args.extended_csv:
        parser.error("至少给 --intent-csv 或 --extended-csv")
    try:
        if args.intent_csv:
            qa_set = json.loads(args.qa_set.read_text(encoding="utf-8"))
            report = apply_intent(qa_set, _read_csv(args.intent_csv))
            _write(args.intent_output, qa_set)
            print(json.dumps({"intent": report, "output": str(args.intent_output)},
                             ensure_ascii=False, indent=2))
        if args.extended_csv:
            qa_set = json.loads(args.extended_qa_set.read_text(encoding="utf-8"))
            report = apply_extended(qa_set, _read_csv(args.extended_csv))
            _write(args.extended_output, qa_set)
            print(json.dumps({"extended": report, "output": str(args.extended_output)},
                             ensure_ascii=False, indent=2))
    except (OSError, ValueError) as error:
        print(f"导入失败：{error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["INTENT_LABELS", "apply_intent", "apply_extended", "main"]
