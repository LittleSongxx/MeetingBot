"""一次问答的运行对象（不写入 checkpoint）。

LangGraph 的 checkpointer 只序列化图状态；模型调用器、ToolNode、Actor、引用登记表
都是**每轮/恢复时重建**的运行对象，不能进 checkpoint：
- ToolNode/Actor 里有数据库权限快照与闭包，不可序列化；
- 恢复时要按当前用户权限重建，而不是复用崩溃前的旧快照。

因此这些对象通过本协程的 contextvar 注入，节点只从 `current()` 取；不写入 checkpoint，
也不放在 config 里（LangGraph 会过滤自定义 configurable）。
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class ChatRuntime:
    call_model: Callable
    tools: Any
    cost_micro_of: Callable | None = None
    # CoveragePlan/EvidenceState：由 coverage 节点更新，不写入 checkpoint
    evidence: Any = None
    # 可选：模型调用前对“本轮累积工具结果”做 turn 级过滤（不写 checkpoint）。
    # 签名 async (messages) -> messages；None 表示不过滤。
    prepare_messages: Any = None


_current: ContextVar[ChatRuntime | None] = ContextVar("meetingbot_chat_runtime", default=None)


def set_current(runtime: ChatRuntime | None):
    return _current.set(runtime)


def reset_current(token) -> None:
    _current.reset(token)


def current() -> ChatRuntime | None:
    return _current.get()


__all__ = ["ChatRuntime", "set_current", "reset_current", "current"]
