"""后台任务的 Temporal 调度层。

三类长任务（转写/纪要/自检）从进程内 asyncio 字典 + MySQL 状态机的自研调度，
迁到 Temporal durable execution：

- **workflow 即任务**：workflow id = `{kind}-{业务id}`，重复启动同一 id 被
  Temporal 以 WorkflowExecutionAlreadyStarted 拒绝——这正是旧 `_running_tasks`
  字典"同一任务不双跑"判断的标准等价物，且跨进程、跨重启成立；
- **activity 即 execute_* 协程**：转写/纪要/自检的 `execute_*` 保持可直接
  await 的普通协程（评测的 manage_fixture 在自己进程里直接调用它们并拦截
  模型调用——这个预算护栏依赖"协程不经调度层也能跑"）；
- **恢复原生**：worker 崩溃后 Temporal 自动续跑未完 workflow，`recover_*`
  只剩"重新入队曾建行但 workflow 未启动"的兜底扫尾（API 写行与 start_workflow
  之间的窗口）。

DB 的 status/stage/progress 仍是 UI 与观测的唯一真源，activity 内照旧更新。
"""

from __future__ import annotations

import os
from typing import Any

from temporalio.client import Client
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.common import RetryPolicy
from temporalio.worker import Worker

TEMPORAL_ADDRESS = os.getenv("TEMPORAL_ADDRESS", "temporal:7233")
TEMPORAL_NAMESPACE = os.getenv("TEMPORAL_NAMESPACE", "default")
TEMPORAL_TASK_QUEUE = os.getenv("TEMPORAL_TASK_QUEUE", "aimeeting")
# 单类任务的并发上限：与旧信号量容量一致（同时 2 路转写/纪要/自检）。
MAX_CONCURRENT_ACTIVITIES = int(os.getenv("TEMPORAL_MAX_CONCURRENCY", "2"))

_client: Client | None = None

KIND_TRANSCRIPTION = "transcription"
KIND_MINUTES = "minutes"
KIND_AGENT_RUN = "agent-run"


async def get_client() -> Client:
    """进程级单例客户端。首次调用建连，之后复用（gRPC 长连接）。"""
    global _client
    if _client is None:
        _client = await Client.connect(
            TEMPORAL_ADDRESS, namespace=TEMPORAL_NAMESPACE)
    return _client


def workflow_id(kind: str, business_id: int) -> str:
    return f"{kind}-{business_id}"


async def enqueue(kind: str, business_id: int, input_args: tuple = ()) -> None:
    """启动一个任务 workflow；同名 workflow 已在运行/已完成时静默忽略（幂等）。

    幂等语义与旧 `_running_tasks.get(id).done()` 判断等价：
    - 已在运行 → 不双跑；
    - 已跑完（同 id 历史存在）→ 也不重跑；需要重跑的语义（retry 接口）
      由调用方换新 workflow id（`{kind}-{id}-r{n}`）表达，见各服务的 retry 路径。
    """
    client = await get_client()
    from services import temporal_workflows
    workflow_cls = temporal_workflows.WORKFLOW_BY_KIND[kind]
    try:
        await client.start_workflow(
            workflow_cls.run,
            args=input_args,
            id=workflow_id(kind, business_id),
            task_queue=TEMPORAL_TASK_QUEUE,
        )
    except WorkflowAlreadyStartedError:
        # 同 id 已在运行：这正是"同一任务不重复调度"要的结果
        pass


async def cancel(kind: str, business_id: int) -> None:
    """请求取消一个任务 workflow（activity 收到 CancelledError 后各服务自行收尾）。"""
    client = await get_client()
    handle = client.get_workflow_handle(workflow_id(kind, business_id))
    try:
        await handle.cancel()
    except Exception:
        # 已完成/不存在的 workflow 取消失败不算错误（与旧字典里没有该任务同义）
        pass


async def run_worker() -> None:
    """worker 进程入口：注册三类 workflow + 各自的 activity 实现。"""
    from services import temporal_workflows
    from tortoise import Tortoise
    from settings import TORTOISE_ORM

    await Tortoise.init(config=TORTOISE_ORM)
    try:
        client = await get_client()
        activities = temporal_workflows.all_activities()
        worker = Worker(
            client,
            task_queue=TEMPORAL_TASK_QUEUE,
            workflows=list(temporal_workflows.WORKFLOW_BY_KIND.values()),
            activities=activities,
            max_concurrent_activities=MAX_CONCURRENT_ACTIVITIES,
        )
        await worker.run()
    finally:
        await Tortoise.close_connections()
