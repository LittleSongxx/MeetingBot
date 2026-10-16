"""问答引用的零调用检查：**引用能不能在被引内容里定位**、**引用有没有越权**。

## 这两条为什么必须是零调用的

它们判的是"检索到的内容与答案里的引用对不对得上"，与模型能力无关：
- 定位性：答案引用 `[3]` 时，那条引用的原句必须能在它指向的切片里逐字找到；
- 越权：那条切片所属会议必须在提问者当时可见的范围内。

两条都是**结构性**判定（复用产品的 `contracts.evidence_locatable` 与 `access` 口径），
所以每次改动都能跑，不需要授权调用——这与"契约不变量进 L0"是同一条纪律。
模型质量（faithfulness / relevancy）属于轴 5，需要授权调用，见 `judges/rag.py`。

## 输入是什么

`results/chat_answers.json`：由一次授权运行从产品抓取的问答记录（抓取脚本
`judges/rag.py --ask`）。每条引用带 `source_text`（它指向的切片正文），
这是离线判定的前提——**抓取时补上，而不是让检查器去连数据库**：
检查器一旦要连库，就不再是"随时可跑的零调用回归"了。

## 没有样本时怎么报

如实报"无样本"，不报 0% 也不报 100%：没有数据与全对、全错是三件事。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

if __package__ in (None, ""):  # allow direct script execution
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from lib import stats  # type: ignore

    from regression import contract_checks  # type: ignore
else:
    from lib import stats

    from regression import contract_checks

EVALUATION_DIR = Path(__file__).resolve().parents[1]
# 真实运行抓取的问答记录（一次授权运行产出，见 judges/rag.py --ask）
DEFAULT_ARTIFACT = EVALUATION_DIR / "results" / "chat_answers.json"
# 演示用样本：从**种子会议**的切片构造，用来验证检查器本身能跑（不是产品真实产出）
DEMO_ARTIFACT = EVALUATION_DIR / "gold" / "chat_answers_seed_demo.json"

ARTIFACT_VERSION = 1


def load_artifact(path: Path | None = None) -> dict[str, Any] | None:
    """读一份问答记录。文件不存在返回 None（由调用方如实报"无样本"）。"""
    candidate = path or DEFAULT_ARTIFACT
    if not candidate.is_file():
        return None
    return json.loads(candidate.read_text(encoding="utf-8"))


def _contracts():
    """契约判据从产品取（宪法第 5 条登记在案的例外②：评测读产品的定义）。

    产品目录不可用时**明确报不可用**，不退回本地近似——近似就是第二套判据，
    而"两套判据"正是这套评测要消灭的东西。
    """
    return contract_checks.load_contracts()


def check_turn(turn: Mapping[str, Any], contracts=None) -> dict[str, Any]:
    """检查一轮问答：引用定位 + 引用越权。返回本轮的问题清单。"""
    contracts = contracts if contracts is not None else _contracts()
    if contracts is None:
        return {"available": False, "reason": "产品契约层不可用（需要产品 venv）"}

    allowed = turn.get("allowed_meeting_ids", None)
    used_refs = [int(ref) for ref in turn.get("used_refs") or []]
    citations = {int(item.get("ref")): item for item in turn.get("citations") or []
                 if isinstance(item, Mapping)}
    unlocatable: list[dict[str, Any]] = []
    out_of_scope: list[dict[str, Any]] = []
    missing_ref: list[int] = []
    unchecked: list[int] = []      # 引用没带 source_text：判不了，单独报，不算通过
    checked = 0

    for ref in used_refs:
        citation = citations.get(ref)
        if citation is None:
            missing_ref.append(ref)
            continue
        meeting_id = citation.get("meeting_id")
        # 越权：允许集合是 None 表示不过滤（管理员）；列表则必须包含该会议
        if allowed is not None and int(meeting_id or 0) not in {int(i) for i in allowed}:
            out_of_scope.append({"ref": ref, "meeting_id": meeting_id})
        source_text = citation.get("source_text")
        quote = str(citation.get("quote") or "")
        if not source_text:
            unchecked.append(ref)
            continue
        checked += 1
        if not contracts.evidence_locatable(quote, contracts.normalize_source(source_text)):
            unlocatable.append({"ref": ref, "meeting_id": meeting_id,
                                "quote": quote[:120]})

    return {
        "available": True,
        "message_id": turn.get("message_id"),
        "question": (turn.get("question") or "")[:80],
        "used_refs": len(used_refs),
        "checked": checked,
        "unchecked": unchecked,
        "missing_ref": missing_ref,
        "unlocatable": unlocatable,
        "out_of_scope": out_of_scope,
        "unknown_refs": [int(ref) for ref in turn.get("unknown_refs") or []],
        "ok": not (unlocatable or out_of_scope or missing_ref),
    }


def summarize(turns: Sequence[Mapping[str, Any]], contracts=None) -> dict[str, Any]:
    """整份记录的汇总：两条不变量各自的比例与违约明细。"""
    contracts = contracts if contracts is not None else _contracts()
    if contracts is None:
        return {
            "available": False,
            "reason": "产品契约层不可用（评测不退回近似实现；用产品 venv 跑本套件）",
            "reading": "这一节需要产品目录可用：定位判据就是产品契约层的那一份",
        }
    if not turns:
        return {
            "available": True,
            "turn_count": 0,
            "reading": "无样本：没有抓取到的问答记录（先跑 judges/rag.py --ask），"
                       "因此这两条不变量既不报通过也不报失败",
        }

    rows = [check_turn(turn, contracts) for turn in turns]
    used_total = sum(row["used_refs"] for row in rows)
    checked_total = sum(row["checked"] for row in rows)
    unlocatable_total = sum(len(row["unlocatable"]) for row in rows)
    out_of_scope_total = sum(len(row["out_of_scope"]) for row in rows)
    unchecked_total = sum(len(row["unchecked"]) for row in rows)
    unknown_total = sum(len(row["unknown_refs"]) for row in rows)
    all_citations = [c for turn in turns for c in (turn.get("citations") or [])
                     if isinstance(c, Mapping)]
    by_kind: dict[str, int] = {}
    for citation in all_citations:
        kind = str(citation.get("kind") or "unknown")
        by_kind[kind] = by_kind.get(kind, 0) + 1

    return {
        "available": True,
        "turn_count": len(rows),
        "used_citation_count": used_total,
        "checked_citation_count": checked_total,
        # 定位率只在"判得了"的引用上算：没带 source_text 的那些单独报，
        # 把它们算进分母会把"没检查"稀释成"检查通过"
        "locatable": stats.proportion_report(checked_total - unlocatable_total, checked_total)
        if checked_total else None,
        "unlocatable_count": unlocatable_total,
        "unlocatable_examples": [item for row in rows for item in row["unlocatable"]][:10],
        "unchecked_citation_count": unchecked_total,
        "authorized": stats.proportion_report(used_total - out_of_scope_total, used_total)
        if used_total else None,
        "out_of_scope_count": out_of_scope_total,
        "out_of_scope_examples": [item for row in rows for item in row["out_of_scope"]][:10],
        "missing_ref_count": sum(len(row["missing_ref"]) for row in rows),
        "unknown_ref_count": unknown_total,
        "citation_kinds": by_kind,
        "turns_with_violation": sum(1 for row in rows if not row["ok"]),
        "per_turn": rows,
        "reading": (
            "locatable = 判得了的引用里能在**它指向的内容**中逐字定位的比例"
            "（判据是产品契约层的 evidence_locatable，与纪要证据用同一份定义）；"
            "authorized = 引用所属会议落在提问者可见范围内的比例（管理员不设限）。"
            "unknown_ref_count 是答案里引用了、但检索从未返回过的编号——它不该非零，"
            "非零说明模型在编编号。"
        ),
    }


def main(argv: Sequence[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--artifact", type=Path, default=DEFAULT_ARTIFACT)
    parser.add_argument("--demo", action="store_true",
                        help="改用演示样本（gold/chat_answers_seed_demo.json）")
    args = parser.parse_args(list(argv) if argv is not None else None)

    artifact = load_artifact(DEMO_ARTIFACT if args.demo else args.artifact)
    if artifact is None:
        print(json.dumps({"available": True, "turn_count": 0,
                          "reading": f"无样本：{args.artifact} 不存在"},
                         ensure_ascii=False, indent=2))
        return 0
    summary = summarize(artifact.get("turns") or [])
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary.get("out_of_scope_count", 0) == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
