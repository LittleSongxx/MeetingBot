"""聊天工具与护栏的薄 facade。

两个环（问答、纪要自检）不强行统一成一套引擎；这里只保留**已被使用**的
两个共享面：
- ToolRegistry：工具声明/执行/引用登记，MCP 与聊天共用同一份 TOOL_SPECS；
- GuardrailChain：输入/输出/实体检查，保留 block/redact/annotate 语义。
"""

from __future__ import annotations

from typing import Any

from services import citations, entity_check, kb_tools, moderation


class ToolRegistry:
    """唯一工具表的 facade；不改变 MCP/聊天的 wire schema。"""

    def __init__(self) -> None:
        self.specs = kb_tools.TOOL_SPECS

    async def execute(self, actor: kb_tools.Actor, name: str,
                      arguments: Any) -> dict[str, Any]:
        return await kb_tools.execute_tool(actor, name, arguments)

    def annotate(self, registry: citations.CitationRegistry, name: str,
                 result: dict[str, Any]) -> dict[str, Any]:
        return registry.annotate(name, result)

    def serialize(self, result: dict[str, Any]) -> str:
        return kb_tools.tool_result_text(result)


class GuardrailChain:
    """把独立护栏串成一条链，但**不把不同处置语义抹平**。

    - input/output moderation 返回 verdict，调用方决定 block/redact；
    - entity check 只产生标注。
    """

    def check_input(self, text: str):
        return moderation.check_input(text)

    def check_output(self, text: str):
        return moderation.check_output(text)

    def apply_output(self, text: str, verdict: Any):
        return moderation.apply_output(text, verdict)

    def entity_flags(self, answer: str, haystack: str) -> list[dict[str, Any]]:
        return entity_check.check_entities(answer, haystack)

    def entity_note(self, flags: list[dict[str, Any]]) -> str:
        return entity_check.describe(flags)


__all__ = ["ToolRegistry", "GuardrailChain"]
