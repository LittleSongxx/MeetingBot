"""后台任务的 Temporal 调度层。

三类长任务（转写/纪要/自检）从进程内 asyncio 字典 + MySQL 状态机的自研调度，
迁到 Temporal durable execution：

- **workflow 即任务**：workflow id = `{kind}-{业务id}`，仍在运行的同一 id 会被
  Temporal 以 WorkflowExecutionAlreadyStarted 拒绝——这正是旧 `_running_tasks`
  字典"同一任务不双跑"判断的标准等价物，且跨进程、跨重启成立。注意它只拦
  "正在运行"：已完成的同 id 在默认 ID 复用策略（ALLOW_DUPLICATE）下可以再启
  新 run，retry 复用同一 id 正是靠这一点；"不重复执行"的另一半保障在各
  activity 开头的 DB 条件抢占（如 execute_transcription 的 PENDING→PROCESSING）；
- **activity 即 execute_* 协程**：转写/纪要/自检的 `execute_*` 保持可直接
  await 的普通协程（评测的 manage_fixture 在自己进程里直接调用它们并拦截
  模型调用——这个预算护栏依赖"协程不经调度层也能跑"）；
- **恢复**：worker 崩溃后 Temporal 自动续跑未完 workflow；`recover_*` 同时补投
  “曾建行但 workflow 未启动”的任务，并回收超过阈值的 PROCESSING 行（单副本口径），
  否则进程被杀后行会永久卡在执行中。

DB 的 status/stage/progress 仍是 UI 与观测的唯一真源，activity 内照旧更新。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os

from temporalio.client import Client
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.worker import Worker

logger = logging.getLogger("meetingbot.task_queue")

TEMPORAL_ADDRESS = os.getenv("TEMPORAL_ADDRESS", "temporal:7233")
TEMPORAL_NAMESPACE = os.getenv("TEMPORAL_NAMESPACE", "default")
TEMPORAL_TASK_QUEUE = os.getenv("TEMPORAL_TASK_QUEUE", "meetingbot")
# 兜底并发上限的默认值。**按类型可配**见 `max_concurrent()`：
# 转写要占 CPU 与 ASR 配额、索引只打嵌入接口、自检是长 LLM 调用，
# 它们的合理并发从来不是同一个数，共用一个上限时"改一处、别的类型跟着变"。
DEFAULT_MAX_CONCURRENT = 2

_client: Client | None = None

KIND_TRANSCRIPTION = "transcription"
KIND_MINUTES = "minutes"
KIND_AGENT_RUN = "agent-run"
# 知识库索引：转写成功 / 纪要确认后自动入队（见 kb_service 的幂等说明）
KIND_KB_INDEX = "kb-index"

KINDS = (KIND_TRANSCRIPTION, KIND_MINUTES, KIND_AGENT_RUN, KIND_KB_INDEX)


def task_queue_for(kind: str) -> str:
    """每类任务一个队列。

    为什么按类型分队列而不是共用一个：Temporal 的 `max_concurrent_activities`
    是 **worker 级**的，共用队列时转写的洪峰会把纪要/自检的额度吃掉（它们都在
    同一个 worker 的并发池里排队）。分队列后每类有自己的 worker 与并发上限，
    一类打满不影响另一类——这是 Temporal 里表达"按类型隔离"的标准做法。
    """
    return f"{TEMPORAL_TASK_QUEUE}-{kind}"


def _positive_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as error:
        raise ValueError(f"{name} 必须为正整数") from error
    if value <= 0:
        raise ValueError(f"{name} 必须为正整数")
    return value


def max_concurrent(kind: str) -> int:
    """该类任务的并发上限：`TEMPORAL_MAX_CONCURRENCY_<KIND>` 优先于全局值。

    每次调用都读环境（不在导入期固化）：它只在 worker 启动时用到，而读环境
    让运维改完配置重启即生效，也让测试能直接验证取值规则。
    """
    per_kind = f"TEMPORAL_MAX_CONCURRENCY_{kind.upper().replace('-', '_')}"
    if os.getenv(per_kind, "").strip():
        return _positive_int(per_kind, DEFAULT_MAX_CONCURRENT)
    return _positive_int("TEMPORAL_MAX_CONCURRENCY", DEFAULT_MAX_CONCURRENT)


async def get_client() -> Client:
    """进程级单例客户端。首次调用建连，之后复用（gRPC 长连接）。"""
    global _client
    if _client is None:
        _client = await Client.connect(
            TEMPORAL_ADDRESS, namespace=TEMPORAL_NAMESPACE)
    return _client


def workflow_id(kind: str, business_id: int) -> str:
    return f"{kind}-{business_id}"


async def enqueue(kind: str, business_id: int, input_args: tuple = (),
                  trace_id: str | None = None) -> bool:
    """启动一个任务 workflow。同名 workflow 仍在运行时跳过并返回 False，否则返回 True。

    幂等语义分两半：
    - 正在运行 → start_workflow 抛 WorkflowAlreadyStartedError，跳过（防双跑）；
    - 已跑完 → 默认 ID 复用策略允许同 id 再启新 run，retry 复用同一 id 即靠此；
    因此"不重复执行"的完整保障是两层：这里拦"正在跑"，activity 开头的 DB
    条件抢占拦"重复执行"。调用方需要区分两种结果时（如自检的 resume）应检查
    返回值，静默忽略会让"还想再跑"的意图无声丢失。
    """
    client = await get_client()
    from common import tracing

    from services import temporal_workflows
    workflow_cls = temporal_workflows.WORKFLOW_BY_KIND[kind]
    # 长任务也带同一条 trace：activity 里模型/工具调用都落同一个 trace_id
    trace_id = trace_id or tracing.current_trace_id() or tracing.new_trace_id()
    args = tuple(input_args) + (trace_id,)
    try:
        await client.start_workflow(
            workflow_cls.run,
            args=args,
            id=workflow_id(kind, business_id),
            task_queue=task_queue_for(kind),
        )
        return True
    except WorkflowAlreadyStartedError:
        # 同 id 仍在运行：这正是"同一任务不重复调度"要的结果
        return False


async def cancel(kind: str, business_id: int) -> None:
    """请求取消一个任务 workflow（activity 收到 CancelledError 后各服务自行收尾）。"""
    client = await get_client()
    handle = client.get_workflow_handle(workflow_id(kind, business_id))
    # 已完成/不存在的 workflow 取消失败不算错误（与旧字典里没有该任务同义）
    with contextlib.suppress(Exception):
        await handle.cancel()


async def run_worker() -> None:
    """worker 进程入口：每类任务一个 worker（各自队列 + 各自并发上限）。

    还额外跑一个**基础队列的兼容 worker**：升级前入队的任务停留在旧队列上，
    没有它那些任务会一直没人消费（表现为"任务卡在等待执行"，而日志里什么都没有）。
    新任务按类型进各自的队列，所以隔离对它们生效；兼容 worker 只为把在途任务跑完。
    """
    from settings import TORTOISE_ORM
    from tortoise import Tortoise

    from services import (
        agent_service,
        kb_outbox,
        kb_service,
        minutes_service,
        pg_kb,
        temporal_workflows,
        transcription_service,
    )

    await Tortoise.init(config=TORTOISE_ORM)
    await pg_kb.init()
    # worker 也会重启：只靠 API lifespan 回收，会让“worker 崩溃后重启”留下的
    # PROCESSING 任务一直卡住。这里再跑一遍，workflow id 保证不会双跑。
    for name, recover in (
        ("transcription", transcription_service.recover_transcription_tasks),
        ("minutes", minutes_service.recover_minutes_tasks),
        ("agent", agent_service.recover_agent_runs),
        ("kb-index", kb_service.recover_kb_index_tasks),
    ):
        try:
            await recover()
        except Exception:  # noqa: BLE001 - 恢复失败不能阻止 worker 消费新任务
            logger.exception("worker 启动恢复失败（%s）", name)
    stop_event = asyncio.Event()
    outbox_task = asyncio.create_task(kb_outbox.loop(stop_event))
    try:
        client = await get_client()
        workers = [
            Worker(
                client,
                task_queue=task_queue_for(kind),
                workflows=[temporal_workflows.WORKFLOW_BY_KIND[kind]],
                activities=temporal_workflows.activities_for(kind),
                max_concurrent_activities=max_concurrent(kind),
            )
            for kind in KINDS
        ]
        workers.append(Worker(
            client,
            task_queue=TEMPORAL_TASK_QUEUE,          # 旧队列：只为消费在途任务
            workflows=list(temporal_workflows.WORKFLOW_BY_KIND.values()),
            activities=temporal_workflows.all_activities(),
            max_concurrent_activities=_positive_int("TEMPORAL_MAX_CONCURRENCY",
                                                    DEFAULT_MAX_CONCURRENT),
        ))
        await asyncio.gather(*(worker.run() for worker in workers))
    finally:
        stop_event.set()
        outbox_task.cancel()
        with contextlib.suppress(BaseException):
            await outbox_task
        await pg_kb.close()
        await Tortoise.close_connections()
