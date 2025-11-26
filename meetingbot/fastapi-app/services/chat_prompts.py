"""问答提示词与模型配置读取。"""

from __future__ import annotations

import logging
import os

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
    "period 判不出时不要猜，检索相关录音并如实说明。\n"
    "5. 问完整清单（所有风险/全部待办/全部决策/哪几项）时，逐条列出工具返回的全部条目，"
    "不要省略、不要只列前几项；list_facts 不要用 keywords 截断，只有用户明确限定主题时才过滤。"
)


AGGREGATION_PROTOCOL = (
    "\n\n【聚合类问题的工具选择】\n"
    "问「有哪些/分别是什么/清单/汇总/数量/分组/入口等」时，第一步用 list_facts "
    "拉结构化事实；只有 list_facts 缺槽位或需要原文依据时，再用 search_knowledge。"
    "不要先用 search_knowledge 拉清单。"
)


STATE_PROTOCOL = (
    "\n\n【状态查询规则】\n"
    "1. 问「上次定了什么/最后复述了哪些结论/还有哪些待办/谁负责/协作人是谁/"
    "截止时间/项目现在什么状态」时，**第一步必须调用 query_meeting_state**；"
    "它只返回人工确认过的决策、结论、待办与个人偏好。"
    "只有该工具缺少原文依据时，才继续用 search_knowledge 补查；不要先用 list_facts 代替。\n"
    "2. 待办状态以工具返回的 status/owner/deadline 为准，不要按会议上下文猜；"
    "未确认的记忆候选不得当作事实。\n"
    "3. 引用状态条目时写 [n]；跨系列问题先确认系列，再用系列范围检索。\n"
    "4. 与会议无关的寒暄、天气、常识问题（例如「你好」「今天天气怎么样」）"
    "**不要调用任何工具**，直接简短回答。\n"
    "5. 问某事项/任务现在什么状态时，decision 与 action_item 都要查；只查一种会把"
    "“没有该类型条目”误判成“这件事不存在”。\n"
    "6. 问某场会的结论/某项决定是否已确认时，state 没有已确认条目就用 "
    "get_minutes(meeting_id=..., include_draft=true) 读最近草稿，并在回答里标注草稿；"
    "不要把草稿说成已确认。\n"
    "7. 同一事件可能有多个时间点（准备/内部执行/面向不同受众等），"
    "不要把时点不同当成口径矛盾；"
    "以工具返回的字段和来源为准。资料没给年份时不要编造年份。\n"
    "8. series_id 与 meeting_id 是不同命名空间；问系列时传系列主键，不要拿会议 id 当系列 id。"
    "问「是已确认决策还是未确认候选」时，query_meeting_state 传 include_proposed=true，"
    "候选只能陈述状态、不得当事实。"
)


EVIDENCE_PROTOCOL = (
    "\n\n【证据边界规则】\n"
    "1. 每个具体事实/数字/日期/引语，必须能在其引用 [n] 对应的工具结果原文中找到；"
    "找不到就不要写，或明确标为“我的推断”。\n"
    "2. 引语必须逐字来自工具结果；不要把改写后的句子放进引号，也不要把一般结论说成某人原话。\n"
    "3. 时间点、序号、复述顺序不等于时间先后；材料没有明确顺序时不要推断“先/后”。\n"
    "4. 同一事实在材料中冲突时，保留原文并说明冲突，不要替材料裁决。\n"
    "5. 回答补充解释时，区分“材料原文”和“我的推断”。"
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
    return (body.strip() + SOURCE_COVERAGE_PROTOCOL + AGGREGATION_PROTOCOL
            + STATE_PROTOCOL
            + (EVIDENCE_PROTOCOL if os.getenv("EVIDENCE_CONSTRAINT_ENABLED", "0").strip().lower()
               not in {"0", "false", "no", "off"} else "")
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

