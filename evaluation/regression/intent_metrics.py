"""轴5 意图层：逐类 F1 + 混淆矩阵（**零模型调用**）。

## 为什么单列一套

四个质量指标回答"答得准不准"，这一套回答"**问题有没有被送到对的路径上**"。
路由判错比不路由更糟（把枚举问题送进单点检索、把寒暄送进知识库），所以它需要自己的
尺子，而且这把尺子必须是零调用的——否则"路由有没有帮忙"这件事本身就要先花一次判定。

## 数据从哪来

- **金标**：`gold/qa_seed.json` 每个 case 的 `intent` 字段（5 类枚举，见
  `annotation.intent_taxonomy`）。**全部由代理起草、待人工确认**，所以本模块报两个口径：
  人工已确认的题（`case_source != "agent_draft"`）与全部题。
- **产品决策**：`results/intent_decisions.json`——`judges/intent.py` 逐题打产品
  `/chat/plan` 端点抓下来的 `{question, intent, rewritten, suggested_tools}`。
  抓取要花钱（每题一次廉价调用），但**评分不需要**：本模块只读产物。

## 口径

逐类 precision / recall / F1 **逐类分列**（宪法第 6 条：不报加权总分），
另给宏平均一列并注明它是"类等权的平均，不是准确率"；混淆矩阵给真实→预测的计数。
P / R 各自带 Clopper–Pearson 精确区间（`lib/stats`）；支持数为 0 的类报 `null`
（判不了 ≠ 0 分）。样本小的事实写在 `reading` 里，**逐类结论只作方向信号**。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

EVALUATION_DIR = Path(__file__).resolve().parents[1]
DEFAULT_QA_SET = EVALUATION_DIR / "gold" / "qa_seed.json"
DEFAULT_DECISIONS = EVALUATION_DIR / "results" / "intent_decisions.json"

if __package__ in (None, ""):  # allow direct script execution
    import sys
    sys.path.insert(0, str(EVALUATION_DIR))
    from lib import stats  # type: ignore
else:
    from lib import stats


def load_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def load_inputs(decisions_path: Path | None = None,
                qa_path: Path | None = None) -> tuple[dict | None, dict | None]:
    return (load_json(decisions_path or DEFAULT_DECISIONS),
            load_json(qa_path or DEFAULT_QA_SET))


def _f1(precision: float | None, recall: float | None) -> float | None:
    if not precision or not recall:
        return None
    return 2 * precision * recall / (precision + recall)


def summarize(decisions: Mapping[str, Any] | None, qa_set: Mapping[str, Any] | None) -> dict[str, Any]:
    """逐类 F1 + 混淆矩阵。判不了的题（缺金标/缺决策）单独计数，不混进分母。"""
    taxonomy = list(((qa_set or {}).get("annotation") or {}).get("intent_taxonomy") or [])
    gold: dict[str, str] = {}
    draft_ids: set[str] = set()
    for case in (qa_set or {}).get("cases") or []:
        if case.get("intent"):
            gold[str(case["id"])] = str(case["intent"])
        if case.get("case_source") == "agent_draft":
            draft_ids.add(str(case["id"]))
    decided: dict[str, str] = {}
    for row in (decisions or {}).get("decisions") or []:
        case_id = str(row.get("case_id") or "")
        if case_id and row.get("intent"):
            decided[case_id] = str(row["intent"])
    if not gold or not decided:
        return {
            "sample": 0,
            "reading": ("无样本：需要金标意图（`gold/qa_seed.json` 的 `intent`）与产品决策"
                        "（`results/intent_decisions.json`，由 `judges/intent.py` 打 `/chat/plan` 抓取）。"
                        "**不拿「没有决策」当「预测为空」**——那会把没测的东西算成 0 分。"),
            "skipped": [{"reason": "缺金标意图" if not gold else "缺产品决策产物"}],
        }

    # 只统计两边都有的题；缺决策的题如实列出来
    paired = [(cid, gold[cid], decided[cid]) for cid in sorted(gold) if cid in decided]
    missing = [cid for cid in sorted(gold) if cid not in decided]
    if not paired:
        return {"sample": 0, "reading": "无样本：金标与决策没有交集（case_id 对不上？）",
                "skipped": [{"case_id": cid, "reason": "这一题没有产品决策"} for cid in missing]}

    labels = list(taxonomy)
    for _, actual, predicted in paired:
        for label in (actual, predicted):
            if label not in labels:
                labels.append(label)   # 金标/决策里出现未登记的类：照实报，不静默丢

    confusion: dict[str, dict[str, int]] = {
        actual: dict.fromkeys(labels, 0) for actual in labels}
    for _, actual, predicted in paired:
        confusion[actual][predicted] += 1

    per_class: dict[str, Any] = {}
    f1_values: list[float] = []
    for label in labels:
        true_positive = confusion.get(label, {}).get(label, 0)
        predicted_total = sum(confusion.get(actual, {}).get(label, 0) for actual in labels)
        actual_total = sum(confusion.get(label, {}).values())
        precision = true_positive / predicted_total if predicted_total else None
        recall = true_positive / actual_total if actual_total else None
        f1 = _f1(precision, recall)
        if f1 is not None:
            f1_values.append(f1)
        per_class[label] = {
            "support": actual_total,          # 金标里这个类的题数
            "predicted": predicted_total,     # 产品判成这个类的题数
            "true_positive": true_positive,
            "precision": precision,
            "precision_interval": stats.clopper_pearson_interval(true_positive, predicted_total)
            if predicted_total else None,
            "recall": recall,
            "recall_interval": stats.clopper_pearson_interval(true_positive, actual_total)
            if actual_total else None,
            "f1": f1,
        }

    # 两个口径：人工已确认的题 vs 全部（含代理起草）
    verified = [row for row in paired if row[0] not in draft_ids]
    verified_hits = sum(1 for _, actual, predicted in verified if actual == predicted)
    all_hits = sum(1 for _, actual, predicted in paired if actual == predicted)

    return {
        "sample": len(paired),
        "sample_verified": len(verified),
        "sample_draft": len(paired) - len(verified),
        "per_class": per_class,
        "confusion": confusion,
        # 宏平均是"类等权的平均"，与准确率不是一回事——类别不平衡时准确率会骗人
        "macro_f1": (sum(f1_values) / len(f1_values)) if f1_values else None,
        "accuracy_for_reference_only": {
            "verified": stats.proportion_report(verified_hits, len(verified)) if verified else None,
            "all": stats.proportion_report(all_hits, len(paired)),
            "why_not_primary": ("类别不平衡时准确率会被多数类吞掉（本集 single_fact 占四成），"
                                "因此主口径是**逐类 F1**，准确率只作参照"),
        },
        "skipped": [{"case_id": cid, "reason": "这一题没有产品决策"} for cid in missing],
        "reading": (
            "逐类 precision/recall/F1 + 混淆矩阵（真实→预测）。"
            "**没有加权总分**：宏平均只是「类等权的平均」，逐类数字一个都不能省；"
            "支持数为 0 的类报 null（判不了不等于 0 分）。"
            f"两类标注来源分开报：人工已确认 {len(verified)} 题、代理起草 {len(paired) - len(verified)} 题——"
            "**代理起草的标签尚未人工确认，逐类结论只作方向信号**。"),
        "not_measured": ("本模块只测「意图分类对不对」，不测「路由对答案质量有没有帮助」——"
                         "后者需要带/不带路由的两臂答案 + 判定，属于需授权的 A/B。"),
    }


__all__ = ["DEFAULT_DECISIONS", "DEFAULT_QA_SET", "load_inputs", "summarize"]
