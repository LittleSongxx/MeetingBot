"""问答工具装配（与 MCP 共用 kb_tools 工具表）。"""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.tools import StructuredTool

from services import (
    citations,
    kb_tools,
)
from services.agent_runtime import ToolRegistry

logger = logging.getLogger("meetingbot.chat")


def build_tools(actor: kb_tools.Actor, registry: citations.CitationRegistry) -> list[Any]:
    """把工具表包成 ToolNode 认的工具，同时挂上引用登记。

    每个工具的实现在 `kb_tools` 里只有一份；这里只做三件事：把 Actor 绑进去、
    把结果登记成引用、把结果序列化成 tool message。
    MCP 侧走的是同一条 `kb_tools.execute_tool`，只是没有引用登记（那边不产回答）。
    """
    tool_registry = ToolRegistry()
    tools: list[Any] = []
    for spec in tool_registry.specs:
        tools.append(StructuredTool(
            name=spec.name,
            description=spec.description,
            args_schema=spec.arguments,
            coroutine=_tool_runner(spec, actor, registry, tool_registry),
        ))
    return tools


def _tool_runner(spec: kb_tools.ToolSpec, actor: kb_tools.Actor,
                 registry: citations.CitationRegistry,
                 tool_registry: ToolRegistry):
    async def run(**kwargs: Any) -> str:
        result = await tool_registry.execute(actor, spec.name, kwargs)
        result = tool_registry.annotate(registry, spec.name, result)
        return tool_registry.serialize(result)

    return run

