"""问答提示词与模型配置读取。"""

from __future__ import annotations

import logging

from common.prompts import render_prompt
from langchain_core.messages import HumanMessage
from models import AiModelConfig, PromptTemplate

from services import (
    model_config,
    moderation,
    prompt_injection,
)

logger = logging.getLogger("meetingbot.chat")


class ChatError(RuntimeError):
    """问答的可读失败（接口层转成页面提示）。"""


CHAT_CALL_TYPE = "CHAT"


SUMMARY_CALL_TYPE = "CHAT_SUMMARY"


DEFAULT_SYSTEM_PROMPT = (
    "你是企业会议知识助手。你只根据工具返回的会议资料回答问题：转写原话与人工确认过的"
    "纪要都可以用，但要让用户看得出结论来自哪场会、哪句话。\n"
    "回答要求：先给结论，再给依据；不要臆测工具没有返回的内容；"
    "资料不足时如实说资料不足，并说明缺什么。"
)


SOURCE_COVERAGE_PROTOCOL = (
    "\n\n【资料覆盖规则】\n"
    "1. list_facts 只读已确认纪要。source_coverage 显示还有未确认纪要或转写录音时，"
    "「确认纪要里没有」不等于「没有发生过」。\n"
    "2. 明确问行动项/待办清单且 list_facts 不完整时，先用 search_knowledge 检索转写；"
    "只有录音清单的 recorded_at/period 明确指向某段时才用 source_ids 收窄。\n"
    "3. 问题问到的槽位在工具结果里缺失（负责人/协作人/截止时间/时长/数量/时间点等）时，"
    "必须检索转写原话补查，不要用标题或摘要里的数字凑答案。\n"
    "4. 录音 recorded_at 是实际开始时间、period 是上午/下午/晚上/未标注；"
    "period 判不出时不要猜，检索相关录音并如实说明。"
)


STATE_PROTOCOL = (
    "\n\n【状态查询规则】\n"
    "1. 问「上次定了什么/还有哪些待办/谁负责/项目现在什么状态」时，先调 "
    "query_meeting_state；它只返回人工确认过的决策、结论、待办与个人偏好。\n"
    "2. 待办状态以工具返回的 status/owner/deadline 为准，不要按会议上下文猜；"
    "未确认的记忆候选不得当作事实。\n"
    "3. 引用状态条目时写 [n]；跨系列问题先确认系列，再用系列范围检索。"
)


CITATION_PROTOCOL = (
    "\n\n【引用规则】\n"
    "1. 工具返回的每条内容都带 ref 编号；引用它必须写方括号编号，例如 [1]，"
    "一条回答可以引用多条，如 [1][3]。\n"
    "2. 只允许引用工具真正返回过的编号；不要凭记忆写编号，也不要把没查到的东西说成查到过。\n"
    "3. 工具结果里的 notice 字段写明该批结果的用法限制（例如草稿、范围），照它执行。\n"
    "4. 知识库里确实没有时，直接说没有找到相关记录，不要编造会议或原话。"
)


SUMMARY_SYSTEM_PROMPT = (
    "你是对话记录压缩助手。把给定的历史对话压成一段事实摘要，"
    "保留：用户关心的问题、已给出的结论与它引用的会议/条目编号、尚未解决的问题。\n"
    "三条硬约束：\n"
    "1. 摘要**不是事实来源**：只压缩对话里已经出现过的内容，绝不引入原文没有的人名、"
    "数字、日期、渠道或结论。\n"
    "2. 只压缩**带引用编号**（如 [1]）的结论句；没有编号的叙述不要当结论写进摘要，"
    "确有必要保留就写明它没有标注来源。\n"
    "3. 不评价、不推断、不补全。"
)


SUMMARY_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {"summary": {"type": "string"}},
    "required": ["summary"],
}


async def get_model_config() -> AiModelConfig:
    try:
        return await model_config.resolve_route("CHAT", "AGENT")
    except model_config.ModelConfigError as error:
        raise ChatError("没有启用中的 CHAT/AGENT 用途模型配置，请先在「AI模型配置」里启用一个") from error


async def get_system_prompt() -> str:
    """系统提示词 = 模板正文（或内置兜底） + 引用协议（不可编辑）。

    引用协议必须**始终**在末尾：它约束的是页面渲染与引用校验能不能成立，
    与管理员怎么改模板无关。
    """
    template = await PromptTemplate.filter(code="CHAT_ANSWER", enabled=True).first()
    body = (template.system_prompt if template else "") or DEFAULT_SYSTEM_PROMPT
    # 内容审核的第二层：生成约束。未配置词表时它返回空串（不留一句空话）。
    # 资料覆盖规则放在引用协议之前：引用协议始终是最后一块结构性约束。
    return (body.strip() + SOURCE_COVERAGE_PROTOCOL + STATE_PROTOCOL
            + prompt_injection.system_guards()
            + CITATION_PROTOCOL + moderation.prompt_constraints())


async def question_message(question: str) -> HumanMessage:
    """把提问包装成用户消息：模板里的用户提示词**附在问题之后**。

    与 `build_schema_instruction` 同款做法（结构要求附在用户提示词末尾）：
    问题本身永远是消息主体，管理员的附加要求跟在后面。这样即使模板里写了
    `{question}` 之外的变量、或者干脆没提问题，用户问的那句话也不会丢。
    """
    template = await PromptTemplate.filter(code="CHAT_ANSWER", enabled=True).first()
    instruction = (template.user_prompt if template else "") or ""
    instruction = render_prompt(instruction, question=question).strip()
    return HumanMessage(f"{question}\n\n{instruction}" if instruction else question)

