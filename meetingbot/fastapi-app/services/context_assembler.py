"""上下文装配：系统约束 + 历史 + 计划/覆盖提示 + 当前问题。

之前这些顺序散在 `chat_service.stream_answer` 里，预算也是隐式的。这里把
"哪些消息必须保留、哪些可以裁"写成一处：
- system 消息（引用协议/覆盖规则/会话范围）永远保留；
- 计划/覆盖提示保留在问题之前；
- 历史从旧到新裁，先丢最旧的非 system 消息。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any


def max_context_chars() -> int:
    raw = (os.getenv("CHAT_CONTEXT_MAX_CHARS") or "60000").strip()
    try:
        return max(4000, int(raw))
    except ValueError:
        return 60000


def _chars(message: Any) -> int:
    content = getattr(message, "content", "")
    if isinstance(content, list):
        return sum(len(str(part.get("text") or "")) for part in content
                   if isinstance(part, dict))
    return len(str(content or ""))


@dataclass
class ContextAssembler:
    limit: int = 0

    def __post_init__(self) -> None:
        if not self.limit:
            self.limit = max_context_chars()

    def assemble(self, history: list[Any], *,
                 extras: list[Any] | None = None,
                 question: Any) -> list[Any]:
        """按预算装配；始终保留 system、extras 与当前问题。"""
        system = [m for m in history if getattr(m, "type", "") == "system"]
        rest = [m for m in history if getattr(m, "type", "") != "system"]
        fixed = system + [m for m in (extras or []) if m is not None] + [question]
        budget = max(0, self.limit - sum(_chars(m) for m in fixed))
        kept: list[Any] = []
        # 从新到旧保留历史，再恢复时间顺序
        for message in reversed(rest):
            size = _chars(message)
            if size > budget:
                # 最新的一条都放不下时，再往前找旧的只会留下断裂的早期上下文
                break
            kept.append(message)
            budget -= size
        kept.reverse()
        return system + kept + [m for m in (extras or []) if m is not None] + [question]


__all__ = ["ContextAssembler", "max_context_chars"]
