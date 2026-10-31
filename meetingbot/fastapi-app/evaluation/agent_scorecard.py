"""Agent 产品指标 scorecard：从运行库统计任务成功率、证据接地、行动项可用性、接管负担、调用可靠性和延迟/成本。

运行（评测栈容器内）：
    python -m evaluation.agent_scorecard
    python -m evaluation.agent_scorecard --meeting-id 1 --output /tmp/scorecard.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
from datetime import datetime
from pathlib import Path

from models import (
    ActionItem,
    AgentRun,
    AgentStep,
    AiCallLog,
    MeetingMinutes,
    TranscriptionTask,
    TranscriptSegment,
)

from evaluation.metrics import edit_stats, evidence_stats, text_similarity


def percentile(values: list[float], ratio: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * ratio))))
    return round(ordered[index], 2)


def usage_tokens(usage: dict | None) -> int:
    if not isinstance(usage, dict):
        return 0
    for key in ("total_tokens", "totalTokens"):
        value = usage.get(key)
        if isinstance(value, (int, float)):
            return int(value)
    total = 0
    for value in usage.values():
        if isinstance(value, dict):
            total += usage_tokens(value)
    # LangChain usage_metadata 的常见键
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        value = usage.get(key)
        if isinstance(value, (int, float)):
            total += int(value)
    return total


def minutes_payload(minutes: MeetingMinutes) -> dict:
    return {
        "summary": minutes.summary or "",
        "topics": minutes.topics_json or [],
        "viewpoints": minutes.viewpoints_json or [],
        "decisions": minutes.decisions_json or [],
        "pending_items": minutes.pending_items_json or [],
        "risks": minutes.risks_json or [],
    }


async def segments_by_task(task_ids: set[int]) -> dict[int, dict[int, dict]]:
    result: dict[int, dict[int, dict]] = {}
    if not task_ids:
        return result
    for segment in await TranscriptSegment.filter(task_id__in=list(task_ids)):
        result.setdefault(segment.task_id, {})[segment.segment_no] = {
            "content": segment.content or "",
            "start_ms": segment.start_ms,
        }
    return result


def duration_seconds(started: datetime | None, finished: datetime | None) -> float | None:
    if not started or not finished:
        return None
    return max(0.0, (finished - started).total_seconds())


async def build_scorecard(meeting_id: int | None = None) -> dict:
    minutes_query = MeetingMinutes.all()
    if meeting_id is not None:
        minutes_query = minutes_query.filter(meeting_id=meeting_id)
    minutes_rows = await minutes_query
    action_query = ActionItem.all()
    if meeting_id is not None:
        action_query = action_query.filter(meeting_id=meeting_id)
    actions = await action_query
    run_query = AgentRun.all()
    if meeting_id is not None:
        run_query = run_query.filter(meeting_id=meeting_id)
    runs = await run_query.order_by("id")
    log_query = AiCallLog.all()
    if meeting_id is not None:
        minute_ids = {row.id for row in minutes_rows}
        run_ids = {run.id for run in runs}
        log_query = log_query.filter(biz_id__in=list(minute_ids | run_ids))

    logs = await log_query
    steps = await AgentStep.all()
    if meeting_id is not None:
        run_ids = {run.id for run in runs}
        steps = [step for step in steps if step.run_id in run_ids]

    # 任务与 Agent 成功率
    transcription_rows = await TranscriptionTask.all()
    minutes_status = {}
    for row in minutes_rows:
        minutes_status[row.status] = minutes_status.get(row.status, 0) + 1
    run_status = {}
    for run in runs:
        run_status[run.status] = run_status.get(run.status, 0) + 1
    successful_run_statuses = {"APPLIED", "REJECTED", "SUCCEEDED", "FINISHED"}
    completed_runs = [run for run in runs if run.status in successful_run_statuses]
    confirmation_runs = [run for run in runs if run.applied_by is not None]

    # 调用可靠性/延迟/Token
    call_status = {}
    call_type_counts = {}
    call_latencies = []
    failed_calls = []
    total_tokens = 0
    for log in logs:
        call_status[log.status] = call_status.get(log.status, 0) + 1
        call_type_counts[log.call_type] = call_type_counts.get(log.call_type, 0) + 1
        if log.elapsed_ms:
            call_latencies.append(log.elapsed_ms / 1000)
        if log.status != "SUCCEEDED":
            failed_calls.append({
                "id": log.id, "call_type": log.call_type, "status": log.status,
                "error": (log.error_message or "")[:160],
            })
        total_tokens += usage_tokens(log.usage_json)
    call_total = len(logs)
    call_success = call_status.get("SUCCEEDED", 0)

    # 证据接地
    task_ids = {row.source_task_id for row in minutes_rows if row.source_task_id}
    segment_map = await segments_by_task(task_ids)
    evidence_rows = []
    for minutes in minutes_rows:
        stats = evidence_stats(minutes_payload(minutes), segment_map.get(minutes.source_task_id, {}))
        evidence_rows.append(stats)
    evidence_items = sum(row["total_items"] for row in evidence_rows)
    evidence_with = sum(row["items_with_evidence"] for row in evidence_rows)
    evidence_total = sum(row["total_evidence"] for row in evidence_rows)
    evidence_valid = sum(row["valid_evidence"] for row in evidence_rows)

    # 人工修改率
    edit_rows = [
        edit_stats(minutes.ai_draft_json, minutes_payload(minutes))
        for minutes in minutes_rows
        if minutes.ai_draft_json
    ]
    edit_available = [row for row in edit_rows if row.get("available")]

    # 行动项可用性
    action_status = {}
    owner_count = 0
    due_count = 0
    for item in actions:
        action_status[item.status] = action_status.get(item.status, 0) + 1
        if item.owner_name or item.owner_user_id:
            owner_count += 1
        if item.due_date or item.due_text:
            due_count += 1
    duplicate_pairs = 0
    duplicate_items = set()
    for index, left in enumerate(actions):
        for right in actions[index + 1:]:
            if left.meeting_id != right.meeting_id:
                continue
            if text_similarity(left.content, right.content) >= 0.8:
                duplicate_pairs += 1
                duplicate_items.update({left.id, right.id})
    action_total = len(actions)
    confirmed_actions = sum(1 for item in actions if item.status in {"OPEN", "DONE", "CANCELLED"})

    # Agent 运行效率
    run_rows = []
    for run in runs:
        run_steps = [step for step in steps if step.run_id == run.id]
        run_logs = await AiCallLog.filter(call_type__in=["AGENT_REVIEW", "AGENT_REFINE"], biz_id=run.id)
        run_rows.append({
            "id": run.id,
            "status": run.status,
            "rounds": run.current_round,
            "max_rounds": run.max_rounds,
            "passed": run.passed,
            "steps": len(run_steps),
            "model_calls": len(run_logs),
            "tokens": sum(usage_tokens(log.usage_json) for log in run_logs),
            "duration_s": duration_seconds(run.started_at, run.finished_at),
            "issue_count": run.issue_count,
            "applied_by": run.applied_by,
            "decision_latency_s": duration_seconds(run.finished_at, run.applied_time),
        })
    run_durations = [row["duration_s"] for row in run_rows if row["duration_s"] is not None]
    run_calls = [row["model_calls"] for row in run_rows]
    run_steps_count = [row["steps"] for row in run_rows]
    # 超过 24h 的“决策延迟”来自历史种子运行/测试，不应混入产品接管负担；只保留近期真实决策
    decision_latencies = [
        row["decision_latency_s"] for row in run_rows
        if row["decision_latency_s"] is not None and row["decision_latency_s"] <= 24 * 3600
    ]

    # QA（日志层只统计调用可靠性/延迟；答案质量需要单独样本）
    qa_logs = [log for log in logs if log.call_type == "MINUTES_QA"]
    qa_success = sum(1 for log in qa_logs if log.status == "SUCCEEDED")
    step_total = len(steps)
    step_success = sum(1 for step in steps if step.status == "SUCCEEDED")

    return {
        "filters": {"meeting_id": meeting_id},
        "tasks": {
            "meetings": len({row.meeting_id for row in minutes_rows}),
            "minutes": len(minutes_rows),
            "minutes_status": minutes_status,
            "transcriptions": len(transcription_rows),
            "transcription_status": {
                status: sum(1 for row in transcription_rows if row.status == status)
                for status in {row.status for row in transcription_rows}
            },
            "agent_runs": len(runs),
            "agent_run_status": run_status,
            "agent_completed_rate": round(len(completed_runs) / len(runs), 4) if runs else None,
            "human_confirmation_runs": len(confirmation_runs),
            "human_confirmation_rate": round(len(confirmation_runs) / len(runs), 4) if runs else None,
        },
        "calls": {
            "total": call_total,
            "succeeded": call_success,
            "failed": call_total - call_success,
            "success_rate": round(call_success / call_total, 4) if call_total else None,
            "by_type": call_type_counts,
            "latency_p50_s": percentile(call_latencies, 0.5),
            "latency_p95_s": percentile(call_latencies, 0.95),
            "tokens": total_tokens,
            "failed_samples": failed_calls[:5],
        },
        "agent": {
            "runs": run_rows,
            "avg_steps_per_run": round(statistics.mean(run_steps_count), 2) if run_steps_count else None,
            "avg_model_calls_per_run": round(statistics.mean(run_calls), 2) if run_calls else None,
            "run_duration_p50_s": percentile(run_durations, 0.5),
            "run_duration_p95_s": percentile(run_durations, 0.95),
            "decision_latency_p50_s": percentile(decision_latencies, 0.5),
            "decision_latency_p95_s": percentile(decision_latencies, 0.95),
            "decision_latency_samples": len(decision_latencies),
            "step_success_rate": round(step_success / step_total, 4) if step_total else None,
        },
        "evidence": {
            "items": evidence_items,
            "items_with_evidence": evidence_with,
            "item_coverage": round(evidence_with / evidence_items, 4) if evidence_items else None,
            "evidence_total": evidence_total,
            "evidence_valid": evidence_valid,
            "validity": round(evidence_valid / evidence_total, 4) if evidence_total else None,
        },
        "human_edit": {
            "minutes_with_draft": len(edit_available),
            "avg_item_edit_rate": round(statistics.mean(row["item_edit_rate"] for row in edit_available), 4) if edit_available else None,
            "summary_changed_count": sum(1 for row in edit_available if row.get("summary_changed")),
        },
        "actions": {
            "total": action_total,
            "status": action_status,
            "confirmed": confirmed_actions,
            "confirmed_rate": round(confirmed_actions / action_total, 4) if action_total else None,
            "owner_coverage": round(owner_count / action_total, 4) if action_total else None,
            "due_coverage": round(due_count / action_total, 4) if action_total else None,
            "duplicate_pairs": duplicate_pairs,
            "duplicate_items": len(duplicate_items),
            "duplicate_rate": round(len(duplicate_items) / action_total, 4) if action_total else None,
        },
        "qa": {
            "calls": len(qa_logs),
            "success": qa_success,
            "success_rate": round(qa_success / len(qa_logs), 4) if qa_logs else None,
            "latency_p50_s": percentile([log.elapsed_ms / 1000 for log in qa_logs if log.elapsed_ms], 0.5),
            "latency_p95_s": percentile([log.elapsed_ms / 1000 for log in qa_logs if log.elapsed_ms], 0.95),
        },
    }


def render_markdown(card: dict) -> str:
    tasks = card["tasks"]
    calls = card["calls"]
    agent = card["agent"]
    evidence = card["evidence"]
    actions = card["actions"]
    qa = card["qa"]
    lines = [
        "# Agent 产品指标 Scorecard",
        "",
        f"- 过滤: meeting_id={card['filters']['meeting_id']}",
        f"- 会议/纪要: {tasks['meetings']} / {tasks['minutes']}（{tasks['minutes_status']}）",
        "",
        "## 任务与自主性",
        "",
        "| 指标 | 当前值 | 目标/说明 |",
        "|---|---|---|",
        f"| Agent 运行完成率 | {tasks['agent_completed_rate']} | 越高越好；WAITING 表示等待人工确认 |",
        f"| 人工确认运行占比 | {tasks['human_confirmation_rate']} | 产品设计需要人工确认，不是缺陷 |",
        f"| 平均步骤数/运行 | {agent['avg_steps_per_run']} | 越少越省；但审查/改稿步骤不可省 |",
        f"| 平均模型调用/运行 | {agent['avg_model_calls_per_run']} | 自检通常 2~4 次 |",
        f"| 运行耗时 P50/P95 | {agent['run_duration_p50_s']} / {agent['run_duration_p95_s']} s | 目标依会议长度定 |",
        f"| 人工决策延迟 P50/P95 | {agent['decision_latency_p50_s']} / {agent['decision_latency_p95_s']} s（样本 {agent['decision_latency_samples']}） | 衡量人工接管负担；历史种子超 24h 已排除 |",
        f"| Agent 步骤成功率 | {agent['step_success_rate']} | 应接近 100% |",
        "",
        "## 可靠性 / 成本",
        "",
        "| 指标 | 当前值 | 目标/说明 |",
        "|---|---|---|",
        f"| 模型调用成功率 | {calls['success_rate']} | ≥99% |",
        f"| 调用延迟 P50/P95 | {calls['latency_p50_s']} / {calls['latency_p95_s']} s | 越短越好 |",
        f"| Token 总量 | {calls['tokens']} | 结合成本看单任务成本 |",
        f"| 失败调用样本 | {calls['failed_samples']} | 应为空 |",
        "",
        "## 证据接地",
        "",
        "| 指标 | 当前值 | 目标 |",
        "|---|---|---|",
        f"| 条目证据覆盖率 | {evidence['item_coverage']} | ≥95% |",
        f"| 引用合法率 | {evidence['validity']} | ≥99% |",
        f"| 有 AI 草稿的纪要数 | {card['human_edit']['minutes_with_draft']} | 用于算人工修改率 |",
        f"| 平均条目修改率 | {card['human_edit']['avg_item_edit_rate']} | 越低越好（但需真实编辑） |",
        "",
        "## 行动项可用性",
        "",
        "| 指标 | 当前值 | 目标 |",
        "|---|---|---|",
        f"| 行动项数/状态 | {actions['total']} / {actions['status']} | - |",
        f"| 已确认生效比例 | {actions['confirmed_rate']} | 越高越好 |",
        f"| 负责人覆盖 | {actions['owner_coverage']} | ≥80% |",
        f"| 截止覆盖 | {actions['due_coverage']} | ≥70% |",
        f"| 近似重复条目率 | {actions['duplicate_rate']}（{actions['duplicate_items']} 条） | <5% |",
        "",
        "## 单场问答",
        "",
        "| 指标 | 当前值 | 说明 |",
        "|---|---|---|",
        f"| QA 调用数 | {qa['calls']} | 日志层统计 |",
        f"| QA 成功率 | {qa['success_rate']} | 调用可靠性 |",
        f"| QA 延迟 P50/P95 | {qa['latency_p50_s']} / {qa['latency_p95_s']} s | - |",
        "",
        "> 说明：QA 答案的引用正确性、拒答准确率需要单独跑样本评测，本脚本只统计调用层指标。",
        "",
    ]
    return "\n".join(lines)


async def main() -> None:
    parser = argparse.ArgumentParser(description="Agent 产品指标 scorecard")
    parser.add_argument("--meeting-id", type=int, default=None)
    parser.add_argument("--output", default=None)
    args = parser.parse_args()
    from settings import TORTOISE_ORM
    from tortoise import Tortoise

    await Tortoise.init(config=TORTOISE_ORM)
    try:
        card = await build_scorecard(args.meeting_id)
    finally:
        await Tortoise.close_connections()
    print(render_markdown(card))
    if args.output:
        Path(args.output).write_text(json.dumps(card, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"written {args.output}")


if __name__ == "__main__":
    asyncio.run(main())
