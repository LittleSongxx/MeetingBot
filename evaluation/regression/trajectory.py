"""Agent 轨迹评测：工具选择、工具集合、必引、pass^k（**零模型调用**）。

## 为什么单列

`regression/tool_choice.py` 只看**首轮工具**；`judges/rag.py` 的四个指标看**答案内容**。
两者之间缺一格：一次问答到底调了哪些工具、有没有覆盖该覆盖的工具、有没有越界、
重复跑 k 次是否稳定。本模块用 `gold/trajectory_27.json` 的 required/acceptable/
forbidden 工具集补上这一格，只读产物、不调模型。

## 口径（报数必须连口径一起报）

- **首轮工具 strict**：第一次工具调用的名字 ∈ `required_tools`；没有 required 的题
  （smalltalk）要求首轮不调工具。**acceptable**：首轮工具 ∈ `acceptable_tools`。
- **工具调用集合 P/R/F1**：每题取**第一次 trial** 的工具集合（同一工具多次只算一次）：
  `precision = |used ∩ required| / |used|`，`recall = |used ∩ required| / |required|`。
  required 为空时：没调工具记 P=R=F1=1，调了工具记 F1=0（考的就是"不该调"）。
  同时报 micro（跨题求和）与 macro（逐题平均）。
- **必引率**：`must_cite=true` 的题里，第一次 trial 的 `citations` 非空的比例。
- **强制工具覆盖率**：required 全被覆盖（且调用次数 ≥ `min_tool_calls`；
  smalltalk 要求 0 次调用）的题占比。
- **禁止工具命中**：所有 trial 里命中 `forbidden_tools` 的次数（逐条列出）。
- **pass^k**：同一题 k 次 trial 全部满足「required 覆盖 + must_cite」才算 pass。
  每题 trial 数 = 该 question 在各 `--artifact` 里出现的次数（按传入次序）；
  不足 k 次的题单列为 `insufficient_trials`，不进分母。
- **max_tool_calls 预算**：单列为 `tool_budget`，**不混入 pass**（pass 只按
  required+must_cite 定义，见任务口径）。
- 所有比例带 **Clopper-Pearson 95% 区间**（复用 `lib/stats.py`，Wilson 也一并给出）。

## 数据从哪来

`judges/rag.py --ask` 的产物（`artifact_version=1`）：`turns[]` 里每条带
`question/answer/citations/tools`，工具名在 `turns[].tools[].name`。多份产物按
question 合并；**每一份 --artifact 视为一次 trial**——同一配置的重复运行才能算
重复性，不同实验臂/配置的产物不要混传。

## 不测什么

不评工具执行成功率（`tools[].ok`，模型选了就算选）、不评检索内容相关性、不评最终
答案正确性；跨会议"两段都查"目前用 `min_tool_calls` + 工具集合近似，不逐段核验来源。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

EVALUATION_DIR = Path(__file__).resolve().parents[1]
if str(EVALUATION_DIR) not in sys.path:
    sys.path.insert(0, str(EVALUATION_DIR))

from lib import stats  # noqa: E402

DEFAULT_GOLD = EVALUATION_DIR / "gold" / "trajectory_27.json"

REQUIRED_FIELDS = (
    "case_id", "question", "intent", "required_tools", "acceptable_tools",
    "forbidden_tools", "max_tool_calls", "must_cite", "cross_meeting",
    "label_source", "rule",
)
DEFAULT_TOOL_NAMES = (
    "search_knowledge", "list_facts", "get_minutes", "get_transcript_excerpt",
    "query_meeting_state",
)
INTENTS = ("single_fact", "aggregation", "cross_meeting", "smalltalk", "state")


# --------------------------------------------------------------------------- #
# 装载与基础工具
# --------------------------------------------------------------------------- #


def load_artifact(path: Path) -> dict[str, Any]:
    """读一份 `rag.py --ask` 产物；读不到就抛错，由 CLI 统一报错退出。"""
    return json.loads(path.read_text(encoding="utf-8"))


def _tool_entries(turn: Mapping[str, Any]) -> list[dict[str, Any]]:
    """工具调用记录（含失败调用）：名字取 `tools[].name`，兼容旧产物的字符串列表。"""
    entries: list[dict[str, Any]] = []
    for tool in turn.get("tools") or []:
        if isinstance(tool, Mapping):
            name = tool.get("name")
            if name:
                entries.append({"name": str(name), "ok": tool.get("ok")})
        elif isinstance(tool, str) and tool:
            entries.append({"name": tool, "ok": None})
    return entries


def tool_names(turn: Mapping[str, Any]) -> list[str]:
    """一次 trial 的工具名序列（按调用次序，重复调用保留重复）。"""
    return [entry["name"] for entry in _tool_entries(turn)]


def merge_trials(artifacts: Iterable[Mapping[str, Any]]) -> dict[str, list[Mapping[str, Any]]]:
    """按 question 合并多份产物：每个 question → 按传入次序的 trial 列表。"""
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for artifact in artifacts:
        for turn in (artifact or {}).get("turns") or []:
            if not isinstance(turn, Mapping):
                continue
            question = turn.get("question")
            if question:
                grouped.setdefault(str(question), []).append(turn)
    return grouped


def _rate(successes: int, total: int) -> dict[str, Any]:
    """比例 + Clopper-Pearson 95%（Wilson 一并给，便于对照）；分母 0 时如实报 null。"""
    report = stats.proportion_report(int(successes), int(total))
    return {
        "successes": report["successes"],
        "total": report["total"],
        "rate": report["rate"],
        "clopper_pearson_95": report["clopper_pearson_95"],
        "wilson_95": report["wilson_95"],
    }


def _mean(values: Sequence[float]) -> float | None:
    return (sum(values) / len(values)) if values else None


# --------------------------------------------------------------------------- #
# 单 trial / 单题
# --------------------------------------------------------------------------- #


def required_status(case: Mapping[str, Any], names: Sequence[str]) -> dict[str, Any]:
    """required 工具覆盖 + 最少调用次数 + smalltalk 的「不调工具」要求。"""
    required = [str(name) for name in (case.get("required_tools") or [])]
    used = set(names)
    missing = [name for name in required if name not in used]
    intent = str(case.get("intent") or "")
    min_calls = int(case.get("min_tool_calls", len(required)))
    expect_no_tools = intent == "smalltalk" and not required
    min_calls_ok = len(names) >= min_calls
    no_tools_ok = (not names) if expect_no_tools else True
    return {
        "missing_required": missing,
        "min_tool_calls": min_calls,
        "min_calls_ok": min_calls_ok,
        "expect_no_tools": expect_no_tools,
        "no_tools_ok": no_tools_ok,
        "required_ok": not missing and min_calls_ok and no_tools_ok,
    }


def evaluate_trial(case: Mapping[str, Any], turn: Mapping[str, Any]) -> dict[str, Any]:
    """一次 trial 的逐题读数（首轮工具、集合 P/R/F1、引用、禁止、预算）。"""
    entries = _tool_entries(turn)
    names = [entry["name"] for entry in entries]
    used = set(names)
    required = [str(name) for name in (case.get("required_tools") or [])]
    acceptable = [str(name) for name in (case.get("acceptable_tools") or [])]
    forbidden = [str(name) for name in (case.get("forbidden_tools") or [])]
    status = required_status(case, names)

    first_tool = names[0] if names else None
    strict = (first_tool in required) if required else (first_tool is None)
    acceptable_hit = (first_tool in acceptable) if acceptable else (first_tool is None)

    matched = [name for name in required if name in used]
    # required 为空的题（smalltalk）按"没调工具=1，调了=0"计精确率；
    # 有 required 却没调工具时 precision 记 0（不是未定义，方便逐题平均）。
    precision = (len(matched) / len(used)) if used else (1.0 if not required else 0.0)
    recall = (len(matched) / len(required)) if required else 1.0
    f1 = (2 * precision * recall / (precision + recall)
          if (precision + recall) > 0 else 0.0)

    citations = turn.get("citations") or []
    must_cite = bool(case.get("must_cite"))
    citation_ok = (not must_cite) or bool(citations)
    forbidden_hits = [name for name in names if name in forbidden]
    max_calls = case.get("max_tool_calls")
    within_budget = max_calls is None or len(names) <= int(max_calls)
    failed_calls = [entry["name"] for entry in entries if entry["ok"] is False]

    return {
        "first_tool": first_tool,
        "strict": bool(strict),
        "acceptable": bool(acceptable_hit),
        "used_tools": sorted(used),
        "tool_calls": len(names),
        "failed_tool_calls": failed_calls,
        "required_tools": required,
        "missing_required": status["missing_required"],
        "min_tool_calls": status["min_tool_calls"],
        "min_calls_ok": status["min_calls_ok"],
        "no_tools_ok": status["no_tools_ok"],
        "required_ok": status["required_ok"],
        "matched_required": matched,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "citations": len(citations),
        "must_cite": must_cite,
        "citation_ok": bool(citation_ok),
        "forbidden_hits": forbidden_hits,
        "max_tool_calls": max_calls,
        "within_budget": bool(within_budget),
        "stop_reason": turn.get("stop_reason"),
        "pass": bool(status["required_ok"] and citation_ok),
    }


def evaluate_case(case: Mapping[str, Any], trials: Sequence[Mapping[str, Any]],
                  k: int) -> dict[str, Any]:
    """一题的完整读数：第一次 trial 的逐项指标 + 前 k 次 trial 的 pass^k。"""
    case_id = str(case.get("case_id"))
    if not trials:
        return {
            "case_id": case_id,
            "question": case.get("question"),
            "intent": case.get("intent"),
            "cross_meeting": bool(case.get("cross_meeting")),
            "must_cite": bool(case.get("must_cite")),
            "trials": 0,
            "has_trial": False,
            "first_trial": None,
            "pass_k": {"k": k, "evaluated": False, "passed": None,
                       "reason": "没有这一题的产物（question 不匹配）"},
        }
    first = evaluate_trial(case, trials[0])
    passes = [evaluate_trial(case, turn)["pass"] for turn in trials[:k]]
    evaluated = len(trials) >= k
    return {
        "case_id": case_id,
        "question": case.get("question"),
        "intent": case.get("intent"),
        "cross_meeting": bool(case.get("cross_meeting")),
        "must_cite": bool(case.get("must_cite")),
        "trials": len(trials),
        "has_trial": True,
        "first_trial": first,
        "pass_k": {
            "k": k,
            "evaluated": evaluated,
            "passed": (all(passes) if evaluated else None),
            "trial_passes": passes,
            "reason": ("" if evaluated
                       else f"trial 数不足：{len(trials)} < k={k}"),
        },
    }


# --------------------------------------------------------------------------- #
# 金标自检
# --------------------------------------------------------------------------- #


def _as_bool(value: Any) -> bool:
    return isinstance(value, bool) and value


def validate_gold(gold: Mapping[str, Any]) -> dict[str, Any]:
    """金标结构自检（零调用）：字段、工具集合关系、意图→required 规则。"""
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(gold, Mapping):
        return {"ok": False, "cases": 0, "errors": ["金标根节点必须是对象"], "warnings": []}
    cases = gold.get("cases")
    if not isinstance(cases, list) or not cases:
        return {"ok": False, "cases": 0, "errors": ["cases 必须是非空列表"], "warnings": []}
    taxonomy = gold.get("tool_taxonomy")
    known = set(taxonomy) if isinstance(taxonomy, Mapping) and taxonomy else set(DEFAULT_TOOL_NAMES)
    seen: set[str] = set()
    by_intent: dict[str, int] = {}
    for index, case in enumerate(cases):
        where = f"cases[{index}]"
        if not isinstance(case, Mapping):
            errors.append(f"{where}: 必须是对象")
            continue
        where = f"{case.get('case_id') or where}"
        missing_fields = [field for field in REQUIRED_FIELDS if field not in case]
        if missing_fields:
            errors.append(f"{where}: 缺字段 {missing_fields}")
            continue
        case_id = str(case.get("case_id") or "")
        if not case_id:
            errors.append(f"{where}: case_id 为空")
        elif case_id in seen:
            errors.append(f"{where}: case_id 重复")
        seen.add(case_id)
        if not str(case.get("question") or "").strip():
            errors.append(f"{where}: question 为空")
        intent = str(case.get("intent") or "")
        by_intent[intent] = by_intent.get(intent, 0) + 1
        required = list(case.get("required_tools") or [])
        acceptable = list(case.get("acceptable_tools") or [])
        forbidden = list(case.get("forbidden_tools") or [])
        if intent not in INTENTS:
            errors.append(f"{where}: intent={intent!r} 不在 {list(INTENTS)} 里")
        for label, names in (("required", required), ("acceptable", acceptable),
                             ("forbidden", forbidden)):
            unknown = [name for name in names if str(name) not in known]
            if unknown:
                errors.append(f"{where}: {label} 里有未知工具 {unknown}")
        if not set(required) <= set(acceptable):
            errors.append(f"{where}: required 不是 acceptable 的子集")
        if set(required) & set(forbidden):
            errors.append(f"{where}: required 与 forbidden 有交集")
        if not _as_bool(case.get("must_cite")):
            pass  # must_cite 允许 False（smalltalk），只校验类型
        if not isinstance(case.get("must_cite"), bool):
            errors.append(f"{where}: must_cite 必须是 bool")
        if not isinstance(case.get("cross_meeting"), bool):
            errors.append(f"{where}: cross_meeting 必须是 bool")
        max_calls = case.get("max_tool_calls")
        min_calls = case.get("min_tool_calls")
        if not isinstance(max_calls, int) or max_calls < 0:
            errors.append(f"{where}: max_tool_calls 必须是非负整数")
        if not isinstance(min_calls, int) or min_calls < 0:
            errors.append(f"{where}: min_tool_calls 必须是非负整数")
        if isinstance(max_calls, int) and isinstance(min_calls, int) and min_calls > max_calls:
            errors.append(f"{where}: min_tool_calls={min_calls} > max_tool_calls={max_calls}")
        label_source = str(case.get("label_source"))
        if label_source not in {"agent_draft_heuristic", "agent_ai_reviewed"}:
            errors.append(f"{where}: label_source 必须是 agent_draft_heuristic 或 agent_ai_reviewed")
        reviewed = label_source == "agent_ai_reviewed"
        if reviewed:
            review = case.get("review") or {}
            if not isinstance(review, Mapping) or review.get("human_checked") is not False:
                errors.append(f"{where}: AI 复核标签必须带 review.human_checked=false")
        if not str(case.get("rule") or "").strip():
            errors.append(f"{where}: rule 为空")
        # 意图 → required 的启发式规则（金标自身的口径）；AI 复核过的标签允许
        # 按证据修正意图工具，所以跳过启发式硬规则（人工复核前仍标记未确认）。
        if reviewed:
            pass
        elif intent == "single_fact" and "search_knowledge" not in required:
            errors.append(f"{where}: single_fact 要求 required 至少含 search_knowledge")
        elif intent == "aggregation" and "list_facts" not in required:
            errors.append(f"{where}: aggregation 要求 required 至少含 list_facts")
        elif intent == "cross_meeting":
            if not case.get("cross_meeting"):
                errors.append(f"{where}: cross_meeting 题必须 cross_meeting=true")
            if "search_knowledge" not in required:
                errors.append(f"{where}: cross_meeting 要求 required 含 search_knowledge")
            if isinstance(min_calls, int) and min_calls < 2:
                errors.append(f"{where}: cross_meeting 要求 min_tool_calls>=2")
        elif intent == "smalltalk":
            if required:
                errors.append(f"{where}: smalltalk 不应有 required 工具")
            if max_calls != 0:
                errors.append(f"{where}: smalltalk 的 max_tool_calls 必须为 0")
            if case.get("must_cite"):
                errors.append(f"{where}: smalltalk 不要求引用（must_cite 应为 false）")
            if set(forbidden) != known:
                warnings.append(f"{where}: smalltalk 的 forbidden 建议覆盖全部工具")
        elif intent == "state" and "query_meeting_state" not in required:
            errors.append(f"{where}: state 要求 required 含 query_meeting_state")
    return {
        "ok": not errors,
        "cases": len(cases),
        "by_intent": dict(sorted(by_intent.items())),
        "known_tools": sorted(known),
        "errors": errors,
        "warnings": warnings,
    }


# --------------------------------------------------------------------------- #
# 汇总
# --------------------------------------------------------------------------- #


def _no_sample(cases: int, artifacts: int, k: int, missing: Sequence[str],
               reason: str) -> dict[str, Any]:
    return {
        "sample": 0,
        "gold_cases": cases,
        "artifact_count": artifacts,
        "k": k,
        "missing": list(missing),
        "reading": f"无样本：{reason}",
    }


def summarize(gold: Mapping[str, Any], artifacts: Iterable[Mapping[str, Any]],
              k: int = 3) -> dict[str, Any]:
    """按题汇总轨迹指标；无匹配样本时返回 `{"sample": 0, "reading": "无样本…"}`。"""
    if k < 1:
        raise ValueError("k 必须 >= 1")
    artifact_list = [artifact for artifact in artifacts if isinstance(artifact, Mapping)]
    cases = [case for case in (gold.get("cases") or []) if isinstance(case, Mapping)]
    trials_by_question = merge_trials(artifact_list)

    per_case: list[dict[str, Any]] = []
    paired: list[dict[str, Any]] = []
    missing: list[str] = []
    rate = _rate
    counters = {
        "strict": 0, "acceptable": 0,
        "required_ok": 0, "must_cite_ok": 0, "must_cite_total": 0,
    }
    tool_sums = {"matched": 0, "used": 0, "required": 0}
    macro_precision: list[float] = []
    macro_recall: list[float] = []
    macro_f1: list[float] = []
    buckets: dict[str, dict[str, Any]] = {}
    forbidden_details: list[dict[str, Any]] = []
    budget_details: list[dict[str, Any]] = []
    trial_count_total = 0
    pass_k = {"k": k, "evaluated": 0, "passed": 0, "insufficient": [],
              "trial_pass_total": 0, "trial_pass_hits": 0}

    for case in cases:
        intent = str(case.get("intent") or "")
        bucket = buckets.setdefault(intent, {
            "support": 0, "strict": 0, "acceptable": 0,
            "matched": 0, "used": 0, "required": 0,
            "macro_precision": [], "macro_recall": [], "macro_f1": [],
            "required_ok": 0, "must_cite_ok": 0, "must_cite_total": 0,
            "pass_evaluated": 0, "pass_passed": 0,
        })
        trials = trials_by_question.get(str(case.get("question") or "")) or []
        row = evaluate_case(case, trials, k)
        per_case.append(row)
        if not trials:
            missing.append(str(case.get("case_id")))
            continue
        paired.append(row)
        first = row["first_trial"]
        trial_count_total += len(trials)
        bucket["support"] += 1

        counters["strict"] += 1 if first["strict"] else 0
        counters["acceptable"] += 1 if first["acceptable"] else 0
        bucket["strict"] += 1 if first["strict"] else 0
        bucket["acceptable"] += 1 if first["acceptable"] else 0

        tool_sums["matched"] += len(first["matched_required"])
        tool_sums["used"] += len(first["used_tools"])
        tool_sums["required"] += len(first["required_tools"])
        bucket["matched"] += len(first["matched_required"])
        bucket["used"] += len(first["used_tools"])
        bucket["required"] += len(first["required_tools"])
        for key, target in (("precision", macro_precision), ("recall", macro_recall),
                            ("f1", macro_f1)):
            target.append(float(first[key]))
        bucket["macro_precision"].append(float(first["precision"]))
        bucket["macro_recall"].append(float(first["recall"]))
        bucket["macro_f1"].append(float(first["f1"]))

        counters["required_ok"] += 1 if first["required_ok"] else 0
        bucket["required_ok"] += 1 if first["required_ok"] else 0
        if case.get("must_cite"):
            counters["must_cite_total"] += 1
            bucket["must_cite_total"] += 1
            counters["must_cite_ok"] += 1 if first["citation_ok"] else 0
            bucket["must_cite_ok"] += 1 if first["citation_ok"] else 0

        # 所有 trial 的禁止命中 / 预算超限（不混入 pass）
        for trial_index, trial in enumerate(trials):
            detail = evaluate_trial(case, trial)
            for name in detail["forbidden_hits"]:
                forbidden_details.append({
                    "case_id": case.get("case_id"), "trial": trial_index + 1,
                    "tool": name, "intent": intent,
                })
            if not detail["within_budget"]:
                budget_details.append({
                    "case_id": case.get("case_id"), "trial": trial_index + 1,
                    "tool_calls": detail["tool_calls"],
                    "max_tool_calls": detail["max_tool_calls"], "intent": intent,
                })

        status = row["pass_k"]
        if status["evaluated"]:
            pass_k["evaluated"] += 1
            bucket["pass_evaluated"] += 1
            if status["passed"]:
                pass_k["passed"] += 1
                bucket["pass_passed"] += 1
        else:
            pass_k["insufficient"].append({
                "case_id": case.get("case_id"), "trials": len(trials),
            })
        for passed in status["trial_passes"]:
            pass_k["trial_pass_total"] += 1
            pass_k["trial_pass_hits"] += 1 if passed else 0

    if not paired:
        if not cases:
            return _no_sample(0, len(artifact_list), k, [], "金标里没有 cases，先校验 --gold")
        return _no_sample(len(cases), len(artifact_list), k, missing,
                          "没有读到与金标 question 匹配的 turns；核对产物是否为 "
                          "`judges/rag.py --ask` 的 artifact_version=1 产物、"
                          "以及问题文本是否与本金标逐字一致。")

    trial_counts = [row["trials"] for row in paired]
    tool_set = {
        "sample": len(paired),
        "first_trial_per_question": True,
        "tool_retries_counted_once": True,
        "micro": {
            "matched_required": tool_sums["matched"],
            "used_total": tool_sums["used"],
            "required_total": tool_sums["required"],
            "precision": (tool_sums["matched"] / tool_sums["used"]
                          if tool_sums["used"] else None),
            "recall": (tool_sums["matched"] / tool_sums["required"]
                       if tool_sums["required"] else None),
        },
        "macro": {
            "precision": _mean(macro_precision),
            "recall": _mean(macro_recall),
            "f1": _mean(macro_f1),
            "note": "逐题等权平均；required 为空的题按「不调工具=1，调了=0」计",
        },
        "per_intent": {},
    }
    if tool_set["micro"]["precision"] is not None and tool_set["micro"]["recall"] is not None:
        p, r = tool_set["micro"]["precision"], tool_set["micro"]["recall"]
        tool_set["micro"]["f1"] = (2 * p * r / (p + r)) if (p + r) else 0.0
    else:
        tool_set["micro"]["f1"] = None

    # LEDGER 预注册（2026-10-19）把「逐题 required_tools 集合 F1 的平均」定为唯一主指标，
    # 这里用同一个名字暴露，避免报数时口径对不上（不合成其他分数）。
    tool_set["primary_metric"] = "tool_call_f1"
    tool_set["tool_call_f1"] = tool_set["macro"]["f1"]

    per_intent: dict[str, Any] = {}
    for intent, bucket in sorted(buckets.items()):
        if not bucket["support"]:
            continue
        support = bucket["support"]
        entry: dict[str, Any] = {
            "support": support,
            "first_tool_strict": rate(bucket["strict"], support),
            "first_tool_acceptable": rate(bucket["acceptable"], support),
            "tool_set": {
                "precision": _mean(bucket["macro_precision"]),
                "recall": _mean(bucket["macro_recall"]),
                "f1": _mean(bucket["macro_f1"]),
                "matched_required": bucket["matched"],
                "used_total": bucket["used"],
                "required_total": bucket["required"],
            },
            "required_coverage": rate(bucket["required_ok"], support),
            "pass_k": rate(bucket["pass_passed"], bucket["pass_evaluated"]),
        }
        if bucket["must_cite_total"]:
            entry["must_cite"] = rate(bucket["must_cite_ok"], bucket["must_cite_total"])
        per_intent[intent] = entry
    tool_set["per_intent"] = per_intent

    pass_k_report = {
        "k": k,
        "status": "evaluated" if pass_k["evaluated"] else "not_evaluable",
        "status_reason": ("" if pass_k["evaluated"]
                          else "需要同一配置重复 k 次 trial 产物；单份产物只给 single_trial_reference"),
        "evaluated_cases": pass_k["evaluated"],
        "passed": pass_k["passed"],
        "rate": (pass_k["passed"] / pass_k["evaluated"]) if pass_k["evaluated"] else None,
        "clopper_pearson_95": (_rate(pass_k["passed"], pass_k["evaluated"])
                               ["clopper_pearson_95"] if pass_k["evaluated"] else None),
        "insufficient_trials": pass_k["insufficient"],
        "trial_level": {
            "trials": pass_k["trial_pass_total"],
            "passed": pass_k["trial_pass_hits"],
            "rate": (pass_k["trial_pass_hits"] / pass_k["trial_pass_total"])
            if pass_k["trial_pass_total"] else None,
        },
        "single_trial_reference": rate(
            sum(1 for row in paired if row["first_trial"]["pass"]), len(paired)),
        "per_intent": per_intent,
        "criterion": "同一题前 k 次 trial 全部满足 required 覆盖 + must_cite 才记 pass；"
                     "禁止工具命中与 max_tool_calls 超限另报，不混入 pass。",
        "reading": ("每题 trial 数 = 该 question 在传入产物里出现的次数；"
                    "没有足够 trial 的题不进 pass^k 分母。"
                    "single_trial_reference 只是单 trial 参考读数，不是 pass^k。"),
    }

    return {
        "sample": len(paired),
        "gold_cases": len(cases),
        "artifact_count": len(artifact_list),
        "k": k,
        "missing": missing,
        "trial_counts": {
            "min": min(trial_counts),
            "max": max(trial_counts),
            "mean": sum(trial_counts) / len(trial_counts),
            "questions_with_k_trials": sum(1 for value in trial_counts if value >= k),
            "sum_trials": trial_count_total,
            "note": "每份 --artifact 视为一次 trial；同一配置的重复运行才是重复性。",
        },
        "first_tool": {
            "sample": len(paired),
            "strict": rate(counters["strict"], len(paired)),
            "acceptable": rate(counters["acceptable"], len(paired)),
            "per_intent": per_intent,
        },
        "tool_set": tool_set,
        "must_cite": {
            "sample": counters["must_cite_total"],
            "citations_present": rate(counters["must_cite_ok"], counters["must_cite_total"]),
            "note": "只统计 must_cite=true 的题的第一次 trial。",
        },
        "required_coverage": {
            "sample": len(paired),
            "covered": rate(counters["required_ok"], len(paired)),
            "per_intent": {intent: entry["required_coverage"]
                           for intent, entry in per_intent.items()},
        },
        "forbidden": {
            "trials": trial_count_total,
            "cases_with_hits": len({detail["case_id"] for detail in forbidden_details}),
            "hits": len(forbidden_details),
            "details": forbidden_details,
        },
        "tool_budget": {
            "trials": trial_count_total,
            "cases_over_budget": len({detail["case_id"] for detail in budget_details}),
            "trials_over_budget": len(budget_details),
            "details": budget_details,
            "note": "max_tool_calls 是预算口径，单独报；pass 不因超预算改判。",
        },
        "pass_k": pass_k_report,
        "per_case": per_case,
        "reading": ("口径：首轮工具 strict=首轮工具∈required_tools（无 required 的题要求不调工具）；"
                    "acceptable=首轮工具∈acceptable_tools。工具集合 P/R/F1 取每题第一次 trial，"
                    "同一工具多次只算一次。必引率只统计 must_cite 题。pass^k 要求前 k 次 trial "
                    "全部满足 required 覆盖+must_cite；不足 k 次单列。禁止工具与预算单独报。"
                    "所有比例带 Clopper-Pearson 95% 区间（Wilson 一并给）。零模型调用。"),
        "not_measured": ("不评工具执行成功率（tools[].ok）、不评检索内容相关性、不评最终答案"
                         "正确性；跨会议「两段都查」用 min_tool_calls+工具集合近似，"
                         "未逐段核验来源。"),
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _usage_hint() -> str:
    return (
        "用法（零模型调用）：\n"
        "  # 1) 只校验金标、不评指标（无产物时如实报「无样本」）\n"
        "  ../meetingbot/fastapi-app/.venv/bin/python regression/trajectory.py "
        "--gold gold/trajectory_27.json\n"
        "  # 2) 评一份 rag.py --ask 产物（每题只有 1 次 trial）\n"
        "  ../meetingbot/fastapi-app/.venv/bin/python regression/trajectory.py "
        "--artifact results/chat_answers.json --output results/trajectory_report.json\n"
        "  # 3) pass^k：同一配置重复抓 k 次，产物按 trial 次序传入\n"
        "  ../meetingbot/fastapi-app/.venv/bin/python regression/trajectory.py "
        "--artifact results/t1.json --artifact results/t2.json "
        "--artifact results/t3.json --k 3 --output results/trajectory_report.json"
    )


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Agent 轨迹评测（工具集合/首轮工具/必引/pass^k；零模型调用）")
    parser.add_argument("--gold", type=Path, default=DEFAULT_GOLD,
                        help="轨迹金标 JSON（默认 gold/trajectory_27.json）")
    parser.add_argument("--artifact", type=Path, action="append", default=None,
                        help="rag.py --ask 产物；可重复传，每份视为一次 trial")
    parser.add_argument("--k", type=int, default=3, help="pass^k 的 k（默认 3）")
    parser.add_argument("--output", type=Path, default=None,
                        help="把完整读数（含逐题）写成 JSON")
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.k < 1:
        print("--k 必须 >= 1", file=sys.stderr)
        return 2
    try:
        gold = json.loads(args.gold.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        print(f"读不到金标：{args.gold}（{error}）", file=sys.stderr)
        return 2

    validation = validate_gold(gold)
    artifacts: list[Mapping[str, Any]] = []
    for path in args.artifact or []:
        try:
            artifacts.append(load_artifact(path))
        except (OSError, ValueError) as error:
            print(f"读不到产物：{path}（{error}）", file=sys.stderr)
            return 2

    summary = summarize(gold, artifacts, k=args.k)
    summary["zero_model_calls"] = True
    summary["gold_validation"] = validation
    if not args.artifact:
        summary["usage"] = _usage_hint()
        summary["reading"] = (
            "无样本：未提供 --artifact（judges/rag.py --ask 产物），本次只做金标校验；"
            f"金标自检{'通过' if validation['ok'] else '未通过'}"
            f"（cases={validation['cases']}）。有产物后再评轨迹指标（见 usage），"
            "本脚本零模型调用。"
        )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
                               encoding="utf-8")
    if not args.artifact and not validation["ok"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "DEFAULT_GOLD", "REQUIRED_FIELDS", "DEFAULT_TOOL_NAMES", "INTENTS",
    "load_artifact", "tool_names", "merge_trials", "required_status",
    "evaluate_trial", "evaluate_case", "validate_gold", "summarize", "main",
]
