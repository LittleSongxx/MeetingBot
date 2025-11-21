"""Agent 首轮工具选择：strict / acceptable 两种准确率（零模型调用）。

## 为什么单列

意图 F1 回答"问题被判成哪一类"，但 Agent 真正执行的是**工具**。一个 aggregation
被判对了、却仍然只用 search 而不用 list_facts，F1 看不出来。这里用问题 → 首轮
工具的映射补上这一格。

## 口径与边界

- 金标是**代理起草的启发式**（`gold/tool_choice_50.json`），不是人工金标：
  single_fact→search、aggregation→先列清单（list_facts）、cross_meeting→search、
  smalltalk→不检索。
- `strict_accuracy`：首轮工具 == expected_first_tool；
  `acceptable_accuracy`：首轮工具 ∈ acceptable_first_tools（给"枚举题也可以用检索
  起步"留出空间）。
- **只衡量第一步**：不衡量多来源覆盖、不衡量最终答案正确性，也不能替代人工金标。

## 数据从哪来

`regression/rag.py --ask` 的产物里的 `tools` 轨迹（每条工具调用的 `name`）。
多个产物（27 题、68 题扩展集）可以一起传，按问题合并。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping

EVALUATION_DIR = Path(__file__).resolve().parents[1]
DEFAULT_GOLD = EVALUATION_DIR / "gold" / "tool_choice_50.json"


def _first_tool(turn: Mapping[str, Any]) -> str | None:
    tools = turn.get("tools") or []
    for tool in tools:
        name = tool.get("name") if isinstance(tool, Mapping) else None
        if name:
            return str(name)
    return None


def summarize(gold: Mapping[str, Any], artifacts: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """按问题合并多个产物，逐题比较首轮工具。"""
    turns: dict[str, Mapping[str, Any]] = {}
    for artifact in artifacts:
        for turn in (artifact or {}).get("turns") or []:
            question = turn.get("question")
            if question:
                turns[str(question)] = turn
    per_intent: dict[str, dict[str, int]] = {}
    mismatches: list[dict[str, Any]] = []
    strict_hits = acceptable_hits = paired = 0
    missing: list[str] = []
    for case in gold.get("cases") or []:
        turn = turns.get(str(case.get("question") or ""))
        if turn is None:
            missing.append(str(case.get("case_id")))
            continue
        paired += 1
        first = _first_tool(turn)
        expected = case.get("expected_first_tool")
        acceptable = list(case.get("acceptable_first_tools") or [])
        strict = (first is None and expected is None) or (first == expected)
        ok_acceptable = (first is None and expected is None) or (first in acceptable)
        strict_hits += 1 if strict else 0
        acceptable_hits += 1 if ok_acceptable else 0
        bucket = per_intent.setdefault(str(case.get("intent")), {
            "support": 0, "strict_hits": 0, "acceptable_hits": 0})
        bucket["support"] += 1
        bucket["strict_hits"] += 1 if strict else 0
        bucket["acceptable_hits"] += 1 if ok_acceptable else 0
        if not ok_acceptable:
            mismatches.append({
                "case_id": case.get("case_id"), "intent": case.get("intent"),
                "first_tool": first, "expected": expected, "acceptable": acceptable,
            })
    for bucket in per_intent.values():
        bucket["strict_accuracy"] = (bucket["strict_hits"] / bucket["support"]
                                     if bucket["support"] else None)
        bucket["acceptable_accuracy"] = (bucket["acceptable_hits"] / bucket["support"]
                                         if bucket["support"] else None)
    return {
        "sample": len(gold.get("cases") or []),
        "paired": paired,
        "missing": missing,
        "strict_accuracy": strict_hits / paired if paired else None,
        "acceptable_accuracy": acceptable_hits / paired if paired else None,
        "per_intent": per_intent,
        "mismatches": mismatches,
        "reading": ("金标是代理起草的启发式，不是人工金标；strict 要求首轮工具完全匹配，"
                    "acceptable 允许枚举题用检索起步。只衡量第一步，不衡量多来源覆盖或答案正确性。"),
    }


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gold", type=Path, default=DEFAULT_GOLD)
    parser.add_argument("--artifact", type=Path, action="append", required=True,
                        help="rag.py --ask 的产物，可重复传多个（按问题合并）")
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        gold = json.loads(args.gold.read_text(encoding="utf-8"))
        artifacts = [json.loads(path.read_text(encoding="utf-8")) for path in args.artifact]
    except (OSError, ValueError) as error:
        print(f"读不到输入：{error}", file=sys.stderr)
        return 2
    summary = summarize(gold, artifacts)
    print(json.dumps({k: v for k, v in summary.items() if k != "mismatches"},
                     ensure_ascii=False, indent=2))
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                               encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["DEFAULT_GOLD", "summarize", "main"]
