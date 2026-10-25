"""可检索事实召回（recall-v2）：把 context_recall 的分母从"全部金标事实"收窄到
"检索材料里按定义可能出现的事实"。

仪器变更（2026-10-22，评测侧）：`evaluation/gold/fact_classes.json` 由
`evaluation/gold/build_fact_classes.py` 从既有逐条归因产物生成（零模型调用，human_checked=false）。
`metadata / fixture / conflict` 三类不计入召回分母；`judge_uncertain` 仍计入（保守）。
**这是换仪器**：新读数不能与旧 `context_recall` 直接相减；旧读数保留为 `context_recall_legacy`。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

EVAL = Path(__file__).resolve().parents[1]
MANIFEST = EVAL / "gold" / "fact_classes.json"

# 这些类别的事实无法由检索上下文承载，不计入召回分母。
EXCLUDED_CLASSES = frozenset({"metadata", "fixture", "conflict"})
# 引入但计入分母的类别：judge 假阴性嫌疑（保守处理）。
COUNTED_CLASSES = frozenset({"retrievable", "judge_uncertain"})


def load_manifest(path: Path = MANIFEST) -> dict[str, Any]:
    if not path.is_file():
        return {"sets": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def has_set(set_name: str, manifest: Mapping[str, Any] | None = None) -> bool:
    data = manifest or load_manifest()
    return bool((data.get("sets") or {}).get(set_name))


def _judgement_of(case: Mapping[str, Any]) -> Mapping[str, Any]:
    """取该 case 的判定：主判 `judgement`，没有时退回 `judgements.f`（跨族标签产物）。"""
    judgement = case.get("judgement")
    if isinstance(judgement, Mapping) and judgement.get("context"):
        return judgement
    fallback = (case.get("judgements") or {}).get("f")
    return fallback if isinstance(fallback, Mapping) else {}


def retrievable_recall(packet: Mapping[str, Any], set_name: str,
                       manifest: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """按 fact_classes 清单计算可检索事实召回。

    返回 `{"successes", "total", "rate", "excluded", "by_class", "skipped_cases"}`；
    packet 里没有判定的 case 整题跳过（不把"没判"当"没找到"）。
    """
    data = manifest or load_manifest()
    set_payload = (data.get("sets") or {}).get(set_name)
    if not set_payload:
        return {"successes": 0, "total": 0, "rate": None, "excluded": 0,
                "by_class": {}, "skipped_cases": 0, "note": f"清单里没有 {set_name}"}
    default = set_payload.get("default") or "retrievable"
    by_case = set_payload.get("cases") or {}
    successes = total = excluded = skipped = 0
    by_class: dict[str, int] = {}
    for case in packet.get("cases") or []:
        case_id = str(case.get("case_id") or "")
        expected = [str(fact) for fact in case.get("expected_facts") or []]
        judgement = _judgement_of(case)
        context = judgement.get("context") if isinstance(judgement, Mapping) else None
        facts_judged = [row for row in (context or {}).get("facts") or []
                        if isinstance(row, Mapping) and "found" in row]
        if not expected or not facts_judged:
            skipped += 1
            continue
        found_by_index: dict[int, bool] = {}
        for position, row in enumerate(facts_judged):
            index = row.get("index")
            try:
                index = int(index) - 1 if index is not None else position
            except (TypeError, ValueError):
                index = position
            found_by_index[index] = bool(row.get("found"))
        class_map = by_case.get(case_id) or {}
        for position, _fact in enumerate(expected):
            klass = str(class_map.get(str(position)) or default)
            by_class[klass] = by_class.get(klass, 0) + 1
            if klass in EXCLUDED_CLASSES:
                excluded += 1
                continue
            total += 1
            if found_by_index.get(position, False):
                successes += 1
    return {
        "successes": successes,
        "total": total,
        "rate": (successes / total) if total else None,
        "excluded": excluded,
        "by_class": by_class,
        "skipped_cases": skipped,
        "set": set_name,
        "manifest_human_checked": bool(data.get("human_checked")),
        "manifest_generated_at": data.get("generated_at"),
    }


def legacy_recall(packet: Mapping[str, Any]) -> dict[str, Any]:
    """旧口径（packet summary 的 context_recall），用于对照而非相减。

    跨族判定产物把汇总放在 `summary_f`；主判缺失时退回它（并记录来源 key）。
    """
    for key in ("summary", "summary_f"):
        summary = packet.get(key) or {}
        row = summary.get("context_recall") or {}
        if row.get("total"):
            return {"successes": row.get("successes"), "total": row.get("total"),
                    "rate": row.get("rate"), "instrument": "context_recall-legacy",
                    "summary_key": key}
    return {"successes": 0, "total": 0, "rate": None,
            "instrument": "context_recall-legacy", "summary_key": None}


__all__ = ["EXCLUDED_CLASSES", "COUNTED_CLASSES", "load_manifest", "has_set",
           "retrievable_recall", "legacy_recall"]
