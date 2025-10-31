"""可靠性与开销指标：纯计数，不引入任何阈值或词表。

## 为什么这个模块刻意"朴素"

前面的 `grounding.py` 积累了 16 张具名词表，几乎每张都是为了压掉一个具体假阳性而加的。
那是典型的过拟合路径：换一批会议或换一个模型，词表会静默失效。

本模块覆盖的是**可靠性、成功率、延迟、成本**这类量——它们全部是**数出来的**，
不需要判断"什么算对"：
* 成功率 = 成功次数 / 尝试次数；
* 失败分布 = 按错误类型计数；
* 延迟 = 已记录的毫秒数取分位；
* 成本 = 已记录的 token 数。

因此这里**没有阈值、没有词表、没有可调参数**，只有"数哪个字段"。新增一个失败类型
不需要加规则，它会自动出现在分布里。这是与 `grounding.py` 相反的设计取向，
也是本模块存在的理由。

## 口径声明

* 延迟分位用 **nearest-rank**（报出的值都是真实观测到的），并同时报 N；
  **不平均分位**，也不用插值。
* 尝试次数以**调度账本**为准（`dispatch-ledger.json`），它记录每次真实派发；
  失败与结果未知的调用都计入分母，这与项目"失败不退还预算"的纪律一致。
* 结果是相对比较：不同模型/分块/契约配置下的数字不可直接相减。
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from lib import stats

LEDGER_NAME = "dispatch-ledger.json"
REPORT_NAME = "report.json"

# 从错误文本里识别失败大类。**这不是词表判断语义，只是把已记录的错误字符串
# 归到一个更粗的桶里**；识别不到的归 "other" 并原样保留，不会丢信息。
#
# 桶标记必须同时覆盖**已观测到的异常类名**，否则会出现"分类器把最该被看见的失败
# 藏进 other"：实测 2 次输出截断记录的是 `LengthFinishReasonError`（驼峰、无空格），
# 而当时的标记只有 "length limit"，于是两次截断都落进 other——失败分布本来是排查
# 方向的第一手依据，这样反而更难看懂。
FAILURE_BUCKETS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("output_truncated", ("长度上限", "length limit", "lengthfinishreasonerror",
                          "finish_reason=length")),
    ("json_unparseable", ("不是JSON对象", "Invalid json", "json")),
    ("schema_invalid", ("validation error", "ValidationError")),
    ("timeout", ("timeout", "超时", "Timeout")),
    ("transport", ("Connection", "connect", "502", "503", "504")),
    ("request_rejected", ("invalidrequest", "badrequest", "invalid_request")),
    ("budget_or_precondition", ("reserved budget", "precondition", "idle application")),
)


def classify_failure(text: str) -> str:
    lowered = (text or "").lower()
    for bucket, markers in FAILURE_BUCKETS:
        if any(marker.lower() in lowered for marker in markers):
            return bucket
    return "other"


def iter_ledgers(results_root: Path) -> Iterable[tuple[str, dict[str, Any]]]:
    """遍历所有运行目录里的调度账本。按目录名（含时间戳）排序保证可复现。"""
    for directory in sorted(results_root.glob("*/")):
        ledger = directory / LEDGER_NAME
        if not ledger.exists():
            continue
        try:
            payload = json.loads(ledger.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        yield directory.name, payload


def _calls_of(ledger: Any) -> list[dict[str, Any]]:
    """取出账本里的调用记录。

    **按结构识别而不是按键名匹配**：找第一个"元素是含 status 字段的字典"的列表。
    实测踩过：我以为键名是 `calls`，实际是 `dispatches`，于是统计静默返回 0 条——
    而这种"空结果看起来像正常"的失败正是本项目反复出现的坑。按结构识别可以在
    将来改名时继续工作；真要找不到，也会由下面的 `unrecognized_ledger_shape`
    显式暴露，而不是安静地报 0。
    """
    candidates: list[Any] = []
    if isinstance(ledger, Mapping):
        candidates = list(ledger.values())
    elif isinstance(ledger, list):
        candidates = [ledger]
    for value in candidates:
        if isinstance(value, list) and value:
            if all(isinstance(item, Mapping) and "status" in item for item in value):
                return list(value)
    return []


def success_report(calls: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """按调用类型统计成功率。分母是所有**已派发**的调用。"""
    by_type: dict[str, Counter[str]] = defaultdict(Counter)
    overall = Counter()
    for call in calls:
        call_type = str(call.get("call_type") or "unknown")
        status = str(call.get("status") or "unknown")
        by_type[call_type][status] += 1
        overall[status] += 1

    def summarize(counter: Counter[str]) -> dict[str, Any]:
        dispatched = sum(counter.values())
        received = counter.get("RESPONSE_RECEIVED", 0)
        return {
            "dispatched": dispatched,
            "response_received": received,
            "failed_or_cancelled": counter.get("FAILED_OR_CANCELLED", 0),
            "other_statuses": {k: v for k, v in counter.items()
                               if k not in {"RESPONSE_RECEIVED", "FAILED_OR_CANCELLED"}},
            "success_rate": stats.proportion_report(received, dispatched),
        }

    return {
        "overall": summarize(overall),
        "by_call_type": {name: summarize(counter) for name, counter in sorted(by_type.items())},
        "note": (
            "分母是已派发次数，不是已授权额度。SDK 自动重试已关闭，因此一行账本对应"
            "模型服务的一次真实派发。"
        ),
    }


def failure_mode_report(calls: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """失败类型分布。按已记录的错误文本归档，未知的进 other 并保留原文。"""
    buckets: Counter[str] = Counter()
    examples: dict[str, list[str]] = defaultdict(list)
    for call in calls:
        if str(call.get("status")) != "FAILED_OR_CANCELLED":
            continue
        raw = str(call.get("error_type") or "")
        detail = str(call.get("error_detail") or call.get("error") or "")
        bucket = classify_failure(f"{raw} {detail}")
        buckets[bucket] += 1
        if len(examples[bucket]) < 3:
            examples[bucket].append((detail or raw)[:160])
    total = sum(buckets.values())
    return {
        "failed_call_count": total,
        "by_bucket": dict(buckets),
        "examples": {k: v for k, v in examples.items()},
        "note": (
            "这是**可观测性的度量**，不是质量指标：桶名越粗越稳，新增失败类型自动落入"
            "other 而不需要改代码。bucket 分布是排查方向的第一手依据。"
        ),
    }


def latency_report(calls: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """按调用类型的延迟分布。顺序调用链的端到端延迟是各段之和，不是分位之和。"""
    samples: dict[str, list[float]] = defaultdict(list)
    for call in calls:
        value = call.get("elapsed_ms")
        if isinstance(value, (int, float)) and value > 0:
            samples[str(call.get("call_type") or "unknown")].append(float(value))
    report: dict[str, Any] = {
        name: stats.describe(values) for name, values in sorted(samples.items())
    }
    all_values = [v for values in samples.values() for v in values]
    if all_values:
        report["all_calls"] = stats.describe(all_values)
    return {
        "by_call_type": report,
        "note": (
            "nearest-rank 分位，报出的值均为真实观测；同时给 N。**不平均分位**。"
            "顺序链路的端到端耗时是各阶段耗时之和，与逐阶段分位不可互换。"
        ),
    }


def token_report(calls: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """按调用类型的 token 用量。推理 token 计入输出，因此单看输出会低估开销。"""
    rows: dict[str, Counter[str]] = defaultdict(Counter)
    for call in calls:
        call_type = str(call.get("call_type") or "unknown")
        for key in ("input_tokens", "output_tokens", "total_tokens"):
            value = call.get(key)
            if isinstance(value, (int, float)):
                rows[call_type][key] += int(value)
    totals = Counter()
    for counter in rows.values():
        totals.update(counter)
    return {
        "by_call_type": {k: dict(v) for k, v in sorted(rows.items())},
        "total": dict(totals),
        "note": (
            "口径：token 数是模型服务返回的用量，**不是账单金额**。"
            "该供应商的推理 token 计入 output，所以只报输出会低估开销。"
        ),
    }


def end_to_end_success_report(results_root: Path) -> dict[str, Any]:
    """端到端任务成功率：按**尝试**而非按最终成功的场次统计。

    这一条是刻意纠正一种呈现偏差：把多轮修复后的"最终全通过"说成成功率，
    会掩盖过程中真实的失败与重试。分母是每一次尝试。
    """
    attempts: Counter[str] = Counter()
    failures: list[dict[str, str]] = []
    for name, ledger in iter_ledgers(results_root):
        for record in _calls_of(ledger):
            case_id = str(record.get("case_id") or "")
            if not case_id:
                continue
            status = str(record.get("status") or "")
            if status == "RESPONSE_RECEIVED":
                attempts.setdefault(f"__seen__{case_id}", Counter())
                continue
        # 逐场判定以 report.json 为准（账本按调用记，报告按场记）
    for directory in sorted(results_root.glob("*/")):
        report_path = directory / REPORT_NAME
        if not report_path.exists():
            continue
        try:
            payload = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for record in payload.get("records", []) or []:
            if not isinstance(record, Mapping):
                continue
            status = str(record.get("status") or "unknown")
            attempts[status] += 1
            if status != "completed":
                failures.append({
                    "run": directory.name,
                    "case_id": str(record.get("case_id") or ""),
                    "error": str(record.get("error_detail") or record.get("error_type") or "")[:160],
                })
    completed = attempts.get("completed", 0)
    total = sum(v for k, v in attempts.items() if not k.startswith("__seen__"))
    return {
        "attempted_case_runs": total,
        "completed_case_runs": completed,
        "success_rate": stats.proportion_report(completed, total),
        "status_breakdown": {k: v for k, v in attempts.items() if not k.startswith("__seen__")},
        "failures": failures[:40],
        "note": (
            "分母是**跨全部运行目录的逐场尝试**，因此修复过程中的失败也计入——"
            "这是刻意的：只报最终全通过会掩盖真实可靠性。"
        ),
    }


def build_report(results_root: Path) -> dict[str, Any]:
    """汇总全部可靠性/开销指标。零模型调用。"""
    calls: list[dict[str, Any]] = []
    run_count = 0
    unparsed: list[str] = []
    for name, ledger in iter_ledgers(results_root):
        found = _calls_of(ledger)
        if not found:
            unparsed.append(name)
        calls.extend(found)
        run_count += 1
    return {
        "run_directories_scanned": run_count,
        "dispatched_call_count": len(calls),
        "ledgers_without_recognizable_calls": unparsed,
        "end_to_end": end_to_end_success_report(results_root),
        "call_success": success_report(calls),
        "failure_modes": failure_mode_report(calls),
        "latency_ms": latency_report(calls),
        "tokens": token_report(calls),
        "design_note": (
            "本模块只有计数，没有阈值与词表——这是与 grounding.py 相反的取向，"
            "目的是让可靠性指标不会随会议分布或模型版本静默失效。"
        ),
    }
