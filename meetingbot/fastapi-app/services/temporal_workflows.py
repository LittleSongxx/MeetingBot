"""三类长任务的 Temporal workflow/activity 定义。

workflow 是薄壳：把 execute_* 协程包成 activity，durable/重试/恢复由 Temporal
提供。activity 内部已经处理业务失败（写 FAILED 状态后正常返回），因此只有
进程级崩溃（worker 被杀、活动超时）会触发 Temporal 的 activity 重试——
execute_* 的条件更新抢占（PENDING→PROCESSING）保证重入安全。
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import activity, workflow

with workflow.unsafe.imports_passed_through():
    from services import task_queue


@workflow.defn
class TranscriptionWorkflow:
    @workflow.run
    async def run(self, task_id: int) -> None:
        await workflow.execute_activity(
            "run_transcription",
            args=[task_id],
            start_to_close_timeout=timedelta(hours=4),
            retry_policy=_retry_policy(),
        )


@workflow.defn
class MinutesWorkflow:
    @workflow.run
    async def run(self, minutes_id: int) -> None:
        await workflow.execute_activity(
            "run_minutes",
            args=[minutes_id],
            start_to_close_timeout=timedelta(hours=1),
            retry_policy=_retry_policy(),
        )


@workflow.defn
class AgentRunWorkflow:
    @workflow.run
    async def run(self, run_id: int) -> None:
        await workflow.execute_activity(
            "run_agent",
            args=[run_id],
            start_to_close_timeout=timedelta(hours=2),
            retry_policy=_retry_policy(),
        )


def _retry_policy():
    # 崩溃续跑场景下的有界重试；业务失败在 activity 内部消化，不会走到这里
    from temporalio.common import RetryPolicy
    return RetryPolicy(maximum_attempts=5)


# ---------------------------------------------------------------------------
# activities：直接包装产品协程。**不得引入任何调度逻辑**——评测的
# manage_fixture 在自己的进程里 monkeypatch 模型工厂后直接 await 这些协程，
# 它们的行为必须与"不经 Temporal 调用"完全一致。
# ---------------------------------------------------------------------------

@activity.defn
async def run_transcription(task_id: int) -> None:
    from services.transcription_service import execute_transcription
    await execute_transcription(task_id)


@activity.defn
async def run_minutes(minutes_id: int) -> None:
    from services.minutes_service import execute_minutes
    await execute_minutes(minutes_id)


@activity.defn
async def run_agent(run_id: int) -> None:
    from services.agent_service import execute_agent_run
    await execute_agent_run(run_id)


WORKFLOW_BY_KIND = {
    task_queue.KIND_TRANSCRIPTION: TranscriptionWorkflow,
    task_queue.KIND_MINUTES: MinutesWorkflow,
    task_queue.KIND_AGENT_RUN: AgentRunWorkflow,
}


def all_activities() -> list:
    return [run_transcription, run_minutes, run_agent]
