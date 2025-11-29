"""构建「可检索事实召回」金标分类清单（零模型调用）。

背景（2026-10-22 指标审计）：context_recall 的分母里混入了**检索上下文按定义不可能包含**
的 expected facts（系统元数据/否定性结论/tool notice/DRAFT 状态/fixture 限制/题目冲突）。
cross30 的 14 条缺失事实里初判 11 条属于此类（round2 独立验证用同题 packet 反证 3 条可检索后为 8 条），
把 recall 从可检索口径的 88.0% 压到 75.86%。

本脚本把「事实类别」从既有归因产物（`evaluation/results/quality_gap_report.json` 的
`rag_attribution.missing_facts`，metric-auditor 逐条人工分类）固化成一份**独立清单**
`evaluation/gold/fact_classes.json`，不改动 gold 原始文件（provenance 不动）。

类别（只影响 context_recall 的分母）：
- `retrievable`：正常检索材料里可以出现（默认）；
- `metadata`：系统状态/元数据/否定性结论/tool notice，检索上下文按定义不包含；
- `fixture`：合成 fixture 的材料限制；
- `conflict`：题目/金标与转写冲突或歧义；
- `judge_uncertain`：疑似 judge 假阴性（**仍计入分母**，保守；需重判后转 retrievable）。

用法：
    python3 evaluation/gold/build_fact_classes.py
输出：`evaluation/gold/fact_classes.json`（human_checked=false，需人工确认后才可作最终金标）。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

EVAL = Path(__file__).resolve().parents[1]
RESULTS = EVAL / "results"
GOLD = EVAL / "gold"
OUT = GOLD / "fact_classes.json"

CLASS_MAP = {
    "gold_metadata_or_absence_fact": "metadata",
    "gold_fixture_limitation": "fixture",
    "gold_transcript_conflict_or_ambiguity": "conflict",
    "judge_false_negative_likely": "judge_uncertain",
    # 真缺口：仍计入分母（这是产品该补的）
    "retrieval_miss": "retrievable",
    "retrieval_miss_answer_wrote": "retrievable",
    "partial_context_coverage": "retrievable",
}

# 独立验证（2026-10-22 round2）用**同题 packet 反证**的单审计误分类：这些事实在
# eval100_scope_fixed_packet 的对应同题（m016↔cs06、m071↔n01）里 judge 判 found=true，
# 说明它们是可检索的（不能从分母剔除）。人工复核后应转成 gold 级分类。
FORCE_RETRIEVABLE: dict[str, set[tuple[str, int]]] = {
    "cross_30": {("cs06", 0), ("cs06", 1), ("cs06", 2)},
    "new_meeting_15": {("n01", 0)},
}

PACKET_TO_SET = {
    "cross30_fresh": "cross_30",
    "p0_r1": "cross_30",
    "p0_r2": "cross_30",
    "p0_r3": "cross_30",
    "new_meeting": "new_meeting_15",
    "mixed100": "eval_100_mixed",
    "extended68": "qa_seed_extended",
}

SET_GOLD = {
    "cross_30": "gold/cross_meeting_qa_30.json",
    "new_meeting_15": "gold/new_meeting_15.json",
    "eval_100_mixed": "gold/eval_100_mixed.json",
    "qa_seed_extended": "gold/qa_seed_extended.json",
}


def _load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _gold_facts(rel: str) -> dict[str, list[str]]:
    path = EVAL / rel
    data = _load(path)
    rows: dict[str, list[str]] = {}
    for case in data.get("cases") or []:
        case_id = str(case.get("case_id") or case.get("id") or "")
        if case_id:
            rows[case_id] = [str(fact) for fact in case.get("expected_facts") or []]
    return rows


def _fact_index(facts: list[str], fact: str, fallback: int | None) -> int | None:
    """优先按原文精确匹配（去首尾空白），否则用归因产物的 fact_index。"""
    stripped = [text.strip() for text in facts]
    needle = fact.strip()
    if needle in stripped:
        return stripped.index(needle)
    if needle in facts:
        return facts.index(needle)
    if fallback is not None:
        # 归因产物的 fact_index 是 1-based；先试 1-based，再试邻位（防御脏值）。
        candidates = (fallback - 1, fallback, fallback - 2)
        for offset in candidates:
            if 0 <= offset < len(facts):
                return offset
    return None


def main() -> int:
    gap = _load(RESULTS / "quality_gap_report.json")
    missing = gap["rag_attribution"]["missing_facts"]
    facts_by_set = {name: _gold_facts(rel) for name, rel in SET_GOLD.items()}
    sets: dict[str, dict] = {}
    for row in missing:
        packet = str(row.get("packet") or "")
        set_name = PACKET_TO_SET.get(packet)
        classification = str(row.get("classification") or "")
        if not set_name or not classification:
            continue
        case_id = str(row.get("case_id") or "")
        facts = facts_by_set[set_name].get(case_id)
        if facts is None:
            print(f"skip: {packet}/{case_id} 不在 {SET_GOLD[set_name]}")
            continue
        index = _fact_index(facts, str(row.get("fact") or ""), row.get("fact_index"))
        if index is None:
            print(f"skip: {packet}/{case_id} fact 定位失败: {row.get('fact')}")
            continue
        bucket = sets.setdefault(set_name, {"default": "retrievable", "cases": {}, "sources": {}})
        klass = CLASS_MAP.get(classification, "retrievable")
        if (case_id, index) in FORCE_RETRIEVABLE.get(set_name, set()):
            klass = "retrievable"
        bucket["cases"].setdefault(case_id, {})[str(index)] = klass
        bucket["sources"][f"{case_id}#{index}"] = {
            "packet": packet,
            "classification": classification,
            "fact": row.get("fact"),
            "note": row.get("note") or "",
        }
    manifest = {
        "instrument": "recall-v2（可检索事实召回）",
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "human_checked": False,
        "zero_model_calls": True,
        "source": (
            "evaluation/results/quality_gap_report.json#rag_attribution.missing_facts"
            "（metric-auditor 逐条分类，见 docs/质量指标全景与缺口分析.md §4）"),
        "rule": (
            "只对归因产物里逐条分类过的缺失事实打非 retrievable 标签；其余事实默认 retrievable。"
            "metadata/fixture/conflict 从 context_recall 分母剔除；judge_uncertain 仍计入分母（保守）。"
            "FORCE_RETRIEVABLE 记录 round2 独立验证反证的单审计误分类（cs06#0-2、n01#0）。"
            "改类别 = 换仪器，需新预注册并重述历史数。"),
        "class_map": CLASS_MAP,
        "sets": {
            name: {
                "default": payload["default"],
                "cases": payload["cases"],
                "sources": payload["sources"],
            }
            for name, payload in sorted(sets.items())
        },
    }
    OUT.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {OUT}")
    for name, payload in manifest["sets"].items():
        print(f"  {name}: {len(payload['cases'])} cases, "
              f"{sum(len(v) for v in payload['cases'].values())} annotated facts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
