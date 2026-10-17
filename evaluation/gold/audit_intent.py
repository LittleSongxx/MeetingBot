"""意图标签的机械一致性审计（零调用，不替代人工）。

## 它查什么

只查**标签与可机械判定的规则矛盾**，不判断问题"应该"是什么意图：

- 问句里有 `哪些/哪几项/哪几类/分别/几个`，却标成 single_fact；
- 标成 aggregation，却没有枚举标记（可能确实要罗列，但需要人看一眼）；
- 标成 cross_meeting，却没有 `also_in`（语料布局不支持"两段都答"）；
- 有 `also_in`，却没标 cross_meeting。

## 它不查什么

`cross_meeting` 的答案落在哪段录音是**语料布局信息**，问题本身看不出来；
smalltalk 的边界（"你能做什么"算不算寒暄）也要人判。审计只把可疑项排到人工
复核前面，不自动改标签。

## 产物

`results/intent_audit.json`：逐条 flags + 计数；`--output` 可改路径。
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, Iterable, Mapping

EVALUATION_DIR = Path(__file__).resolve().parents[1]
DEFAULT_QA_SET = EVALUATION_DIR / "gold" / "qa_seed.json"
# 数量词后的名词（"两个问题""三条结论""三类场景"）本身就是列表信号；
# 只在候选规则里使用，不判定题意（最终仍由人看）。
AGGREGATION_MARKERS = re.compile(
    r"哪些|哪[两一二三四五六七八九十0-9]+[个条类种步组项]|"
    r"[两一二三四五六七八九十0-9]+[个条类种步组项]|"
    r"几[个条类种步组项]|分别")


def audit(qa_set: Mapping[str, Any]) -> dict[str, Any]:
    flagged: list[dict[str, Any]] = []
    for case in qa_set.get("cases") or []:
        if case.get("unanswerable") or not case.get("intent"):
            continue
        intent = str(case["intent"])
        question = str(case.get("question") or "")
        markers = bool(AGGREGATION_MARKERS.search(question))
        reasons: list[str] = []
        if markers and intent == "single_fact":
            reasons.append("有枚举标记但标 single_fact")
        if intent == "aggregation" and not markers:
            reasons.append("标 aggregation 但无枚举标记（需人看是否列表答案）")
        if intent == "cross_meeting" and not case.get("also_in"):
            reasons.append("标 cross_meeting 但无 also_in（语料布局不支持）")
        if case.get("also_in") and intent != "cross_meeting":
            reasons.append("有 also_in 但未标 cross_meeting")
        if reasons:
            flagged.append({"case_id": case["id"], "question": question,
                            "intent": intent, "reasons": reasons})
    return {
        "checked": sum(1 for case in qa_set.get("cases") or []
                       if not case.get("unanswerable") and case.get("intent")),
        "flagged": len(flagged),
        "items": flagged,
        "reading": ("机械审计只查与可判定规则的矛盾，不证明标签正确；"
                    "cross_meeting 的答案落在哪段录音是语料布局，仍需人工确认。"),
    }


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qa-set", type=Path, default=DEFAULT_QA_SET)
    parser.add_argument("--output", type=Path,
                        default=EVALUATION_DIR / "results" / "intent_audit.json")
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        qa_set = json.loads(args.qa_set.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        print(f"读不到标注集：{error}")
        return 2
    report = audit(qa_set)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"checked": report["checked"], "flagged": report["flagged"],
                      "output": str(args.output)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["AGGREGATION_MARKERS", "audit", "main"]
