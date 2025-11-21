"""问答编排：会话记忆、工具装配、状态图驱动的**唯一**装配点。

这一层负责把四块拼起来，四块各自都有单测，因此这里的逻辑刻意写得很薄：

    kb_tools（唯一工具实现） + model_factory（唯一模型出入口）
        + chat_graph（唯一的循环与硬闸） + citations（引用编号）

## 会话记忆：滑窗 + 滚动摘要（两层，不再多）

- **滑窗**：最近 N 条消息原样进上下文（`CHAT_HISTORY_WINDOW`，默认 10 条）。
- **滚动摘要**：更早的消息压成一段摘要，与**水位线**一起存在会话行上。

为什么不做第三层（长期记忆 / 用户画像）：会话本身很短，两层已经覆盖"上一句问的是谁"
与"开头说过什么"这两类真实需求；而长期记忆要引入独立的写入策略与过期策略，
那是另一个子系统，不是这一段代码该顺手带上的东西。

摘要**不放在回答之前**：它要额外调一次模型，放在回答前会把首字延迟推高。
但也不做后台任务（进程重启会丢），而是挂在"下一轮开始前"——只在真的攒够
未摘要消息时才发生，代价可见、可测。

## 模型配置从哪来

聊天复用 `AGENT` 用途的配置（它与纪要自检同属对话式模型用途）。不为聊天新开
一个模型用途：那要在四处登记（接口白名单、bootstrap、启动断言、前端下拉），
而它换来的能力（给聊天单独换一个更便宜的模型）当前没有真实需求。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, AsyncIterator

from common.prompts import render_prompt
from common.times import format_datetime, now
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.tools import StructuredTool
from langgraph.prebuilt import ToolNode
from models import AiCallLog, AiModelConfig, ChatMessage, ChatSession, Meeting, PromptTemplate, TranscriptionTask, User
from tortoise.expressions import F

from services import chat_graph, citations, entity_check, kb_tools, model_factory, moderation, query_plan

logger = logging.getLogger("meetingbot.chat")

CHAT_CALL_TYPE = "CHAT"
SUMMARY_CALL_TYPE = "CHAT_SUMMARY"

# 会话记忆的窗口：进上下文的最近消息条数
DEFAULT_HISTORY_WINDOW = 10
# 未摘要消息超过这个数量就把最旧的那批压成摘要
DEFAULT_SUMMARY_THRESHOLD = 20
# 摘要最少要覆盖几条消息才值得压缩（压两条没有意义）
MIN_SUMMARY_BATCH = 4

MESSAGE_USER = "USER"
MESSAGE_ASSISTANT = "ASSISTANT"

SESSION_TITLE_CHARS = 30


class ChatError(RuntimeError):
    """问答的可读失败（接口层转成页面提示）。"""


# 内置系统提示词：老库升级后没有这条 Prompt 模板也能用。聊天是新能力，
# 不该因为少一条种子数据就整页不可用；管理员想要定制时，在 Prompt 模板里
# 建一条 code=CHAT_ANSWER 的记录即可（启用后它优先）。
DEFAULT_SYSTEM_PROMPT = (
    "你是企业会议知识助手。你只根据工具返回的会议资料回答问题：转写原话与人工确认过的"
    "纪要都可以用，但要让用户看得出结论来自哪场会、哪句话。\n"
    "回答要求：先给结论，再给依据；不要臆测工具没有返回的内容；"
    "资料不足时如实说资料不足，并说明缺什么。"
)

# 资料覆盖规则：**这是正确性约束，不交给可编辑模板**。
# 实测（q26）：list_facts 只读已确认纪要，返回 1 条待办 + source_coverage 显示还有
# 2 段录音与 1 份草稿；模型看懂了"资料不足"，却直接答"无法列出"，没有回头检索转写。
# 把"确认纪要里没有"说成"没有发生"是错误结论，所以这条规则与引用协议一样由代码持有，
# 管理员改模板不会把它弄丢。它只约束**有资料缺口时**的行为，不要求每个问题都多检索。
SOURCE_COVERAGE_PROTOCOL = (
    "\n\n【资料覆盖规则】\n"
    "1. list_facts 只读已确认纪要。它的 source_coverage 显示还有未确认纪要或转写录音时，"
    "「确认纪要里没有」不等于「没有发生过」。\n"
    "2. **只有当问题明确在问「有哪些行动项/待办」或「行动项清单」**，"
    "而 list_facts 没有给出完整清单时，才必须再用 search_knowledge 检索转写原话；"
    "会议有多段录音时，对每段各检索一次（或一次不限来源），再下结论。"
    "问单一事实、流程步骤、时间点、原因或一般决策复述时，不要为了「覆盖」额外检索。\n"
    "3. 问题里的「上午/下午/那场会」在元数据里判不出来时，检索相关录音并如实说明，"
    "不要猜一段录音；猜出来的日期/来源限定空了会自动放宽。"
)

# 引用协议：**这不是可编辑的提示词**，而是前后端之间的协议——页面按 `[n]`
# 渲染可点击的引用卡片，答案里的编号必须与工具返回的条目对得上。
# 与 `build_schema_instruction` 同款理由：结构约束只写在代码常量里，
# 管理员改 Prompt 模板不影响它。
CITATION_PROTOCOL = (
    "\n\n【引用规则】\n"
    "1. 工具返回的每条内容都带 ref 编号；引用它必须写方括号编号，例如 [1]，"
    "一条回答可以引用多条，如 [1][3]。\n"
    "2. 只允许引用工具真正返回过的编号；不要凭记忆写编号，也不要把没查到的东西说成查到过。\n"
    "3. 工具结果里的 notice 字段写明该批结果的用法限制（例如草稿、范围），照它执行。\n"
    "4. 知识库里确实没有时，直接说没有找到相关记录，不要编造会议或原话。"
)

# 摘要提示词：内部压缩步骤，不是面向用户的产品提示词，因此不进 Prompt 模板表。
#
# 为什么写得这么防守：实测（2026-10-16）会话 1 的滚动摘要里**至今留着修复前的编造内容**
# （"新版搜索响应慢""优惠券叠加规则"——这两件事会议里根本不存在，是修复前那次
# "不检索直接答"的产物）。它们被摘要压缩之后，就成了后续轮次眼里的"事实"：
# 记忆层的失效形态不是"忘了"，是**把错的东西记牢**——编造 → 答案 → 摘要 → 后续回答的依据。
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


def history_window() -> int:
    return _positive_env("CHAT_HISTORY_WINDOW", DEFAULT_HISTORY_WINDOW)


def summary_threshold() -> int:
    return _positive_env("CHAT_SUMMARY_THRESHOLD", DEFAULT_SUMMARY_THRESHOLD)


def _positive_env(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as error:
        raise ValueError(f"{name} 必须为正整数") from error
    if value <= 0:
        raise ValueError(f"{name} 必须为正整数")
    return value


# --------------------------------------------------------------------------- #
# 配置与提示词
# --------------------------------------------------------------------------- #


async def get_model_config() -> AiModelConfig:
    config = await AiModelConfig.filter(model_type="AGENT", enabled=True).order_by("-id").first()
    if config is None:
        raise ChatError("没有启用中的 AGENT 用途模型配置，请先在「AI模型配置」里启用一个")
    return config


async def get_system_prompt() -> str:
    """系统提示词 = 模板正文（或内置兜底） + 引用协议（不可编辑）。

    引用协议必须**始终**在末尾：它约束的是页面渲染与引用校验能不能成立，
    与管理员怎么改模板无关。
    """
    template = await PromptTemplate.filter(code="CHAT_ANSWER", enabled=True).first()
    body = (template.system_prompt if template else "") or DEFAULT_SYSTEM_PROMPT
    # 内容审核的第二层：生成约束。未配置词表时它返回空串（不留一句空话）。
    # 资料覆盖规则放在引用协议之前：引用协议始终是最后一块结构性约束。
    return (body.strip() + SOURCE_COVERAGE_PROTOCOL + CITATION_PROTOCOL
            + moderation.prompt_constraints())


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


# --------------------------------------------------------------------------- #
# 会话
# --------------------------------------------------------------------------- #


async def create_session(user: User, *, meeting_ids: list[int] | None = None) -> ChatSession:
    """建会话。范围先过一遍权限：用户无权看的会议不会进范围（静默取交集）。"""
    from common import access
    scoped = await access.visible_meeting_ids(user, requested=meeting_ids or None)
    return await ChatSession.create(
        user_id=user.id, title="新对话",
        scope_meeting_ids=[] if scoped is None else sorted(scoped),
        last_active_time=now(),
    )


async def get_session(session_id: int, user: User) -> ChatSession:
    session = await ChatSession.get_or_none(id=session_id, user_id=user.id, status="ACTIVE")
    if session is None:
        raise ChatError("会话不存在或无权访问")
    return session


async def list_sessions(user: User, *, page_num: int = 1, page_size: int = 20) -> dict[str, Any]:
    query = ChatSession.filter(user_id=user.id, status="ACTIVE")
    total = await query.count()
    rows = await query.order_by("-last_active_time", "-id").offset(
        (page_num - 1) * page_size).limit(page_size)
    return {"total": total, "list": [{
        "id": row.id, "title": row.title, "scope_meeting_ids": row.scope_meeting_ids or [],
        "message_count": row.message_count,
        "last_active_time": _fmt(row.last_active_time), "create_time": _fmt(row.create_time),
    } for row in rows], "pageNum": page_num, "pageSize": page_size}


async def delete_session(session_id: int, user: User) -> None:
    """软删：消息保留（问答是有引用与追责价值的记录，不随会话删除一起消失）。"""
    session = await get_session(session_id, user)
    await ChatSession.filter(id=session.id).update(status="DELETED")


async def list_messages(session_id: int, user: User, *, limit: int = 100) -> list[dict[str, Any]]:
    await get_session(session_id, user)
    rows = await ChatMessage.filter(session_id=session_id).order_by("-id").limit(limit)
    return [_message_dict(row) for row in reversed(rows)]


def _message_dict(row: ChatMessage) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": row.id, "role": row.role, "content": row.content,
        "stop_reason": row.stop_reason, "error_message": row.error_message,
        "token_count": row.token_count, "create_time": _fmt(row.create_time),
        "citations": (row.citations_json or {}).get("used", []),
        # 检索到但答案没引用的也一并给出：它既是页面"查到了什么"的数据源，
        # 也是评测侧算检索指标（recall@k / MRR）的分母——只给被引用的会把召回算高
        "retrieved": (row.citations_json or {}).get("all", []),
        "tools": row.tools_json or [],
        # 查询规划留痕（没有规划过就是 None）：页面不渲染它，但复盘时要能对上
        "plan": row.plan_json,
    }
    if row.stop_reason and row.stop_reason != chat_graph.STOP_FINISHED:
        payload["notice"] = chat_graph.STOP_NOTICE.get(row.stop_reason, "")
    return payload


# --------------------------------------------------------------------------- #
# 记忆
# --------------------------------------------------------------------------- #


async def build_memory(session: ChatSession, user: User) -> list[Any]:
    """系统提示词 + 滚动摘要 + 最近窗口的消息，组成下一次调用的输入。

    历史里的助手消息**进上下文时压成"结论 + 引用编号"**（原文完整留在库里）。
    理由：单轮 token 实测随会话从 5.7k 涨到 26.5k，涨的主要是上一轮那份 20–30 条断言的
    全文；而依据段逐条抄的就是检索原文——同一场会反复问时，它们下一轮还会被重新检索到，
    留在上下文里等于把同一批切片付第二遍钱。编号留着，要细节就再检索一次（工具的用途）。
    """
    system = await get_system_prompt()
    messages: list[Any] = [SystemMessage(system)]
    scope = list(session.scope_meeting_ids or [])
    if scope:
        titles = await Meeting.filter(id__in=scope).values_list("id", "title")
        listed = "、".join(f"{title}（id={mid}）" for mid, title in titles)
        messages.append(SystemMessage(f"本次对话限定在这些会议内：{listed}。"))
    if session.summary:
        messages.append(SystemMessage(f"更早对话的摘要：\n{session.summary}"))
    rows = await ChatMessage.filter(
        session_id=session.id, id__gt=session.summary_upto_message_id,
    ).order_by("-id").limit(history_window())
    for row in reversed(rows):
        # 失败的、没内容的助手行不进上下文：它们没有可用的信息，
        # 留在历史里只会让模型模仿"空回答"
        if row.role == MESSAGE_ASSISTANT and not (row.content or "").strip():
            continue
        if row.role == MESSAGE_USER:
            messages.append(HumanMessage(row.content))
        else:
            messages.append(AIMessage(compress_answer(row.content or "",
                                                      citations=_used_refs(row))))
    return messages


def _used_refs(row: ChatMessage) -> list[int]:
    """这条助手消息引用过的编号（落库的引用快照，与页面渲染同源）。"""
    used = (row.citations_json or {}).get("used") or []
    return [int(item.get("ref")) for item in used if item.get("ref")]


# 回答里"依据段"的常见小标题。压掉的是**从这个小标题到结尾**的整段：
# 那一段逐条抄检索原文，是上下文里最贵、也最容易在下一轮被重新检索到的部分。
_EVIDENCE_HEADINGS = ("依据", "出处", "来源", "证据")
# 没有依据段时的兜底：保留结论的前 N 字，并**显式标注被截断**（不假装完整）
_COMPRESSED_KEEP_CHARS = 200


def _heading_of(line: str) -> str:
    """从一行里取出"小标题"：剥掉 markdown 装饰（`**`、`#`）与冒号，再去空白。"""
    return line.strip().strip("*# ").strip("：: ").strip()


def compress_answer(text: str, *, citations: list[int] | None = None) -> str:
    """把一条历史回答压成"结论 + 引用编号"。原文不丢，它完整留在库里。

    压缩只做两件事：砍掉依据段、附上引用编号。**不做摘要改写**——改写会让历史回答
    带上模型的第二次概括，而"模型可以建议，不能成交"这条同样适用于记忆层：
    进上下文的应该是上一轮的原结论，不是对这一轮的再加工。
    """
    body = (text or "").strip()
    lines = body.splitlines()
    for index, line in enumerate(lines):
        if _heading_of(line) in _EVIDENCE_HEADINGS:
            body = "\n".join(lines[:index]).strip()
            break
    else:
        if len(body) > _COMPRESSED_KEEP_CHARS:
            body = body[:_COMPRESSED_KEEP_CHARS] + "……（后文略）"
    refs = sorted({int(ref) for ref in (citations or [])})
    suffix = ("（本回答的引用编号：" + "".join(f"[{ref}]" for ref in refs)
              + "；需要细节请重新检索原文）") if refs else "（本回答未引用条目）"
    return f"{body}\n{suffix}" if body else suffix


def _summary_transcript(rows: list[ChatMessage]) -> str:
    """把一批消息渲染成摘要的输入。助手行**附上它引用过的编号**：
    "只压缩带引用的结论"这条要求要能被满足，模型得先看得见哪些句子有引用。
    """
    lines = []
    for row in rows:
        if row.role == MESSAGE_USER:
            lines.append(f"用户：{row.content}")
            continue
        refs = _used_refs(row)
        tag = "（引用 " + " ".join(f"[{ref}]" for ref in refs) + "）" if refs else "（无引用）"
        lines.append(f"助手{tag}：{row.content}")
    return "\n".join(lines)


async def maybe_summarize(session: ChatSession, *, model: Any = None) -> bool:
    """攒够未摘要消息就把最旧的一批压成摘要。返回是否真的压了。

    水位线（`summary_upto_message_id`）保证每批消息只进一次摘要；
    没有它就只能整段重算（重复付费）或猜"哪些已进过"（重复计入）。
    """
    rows = await ChatMessage.filter(
        session_id=session.id, id__gt=session.summary_upto_message_id,
    ).order_by("id")
    keep = history_window()
    overflow = len(rows) - keep
    if overflow < max(MIN_SUMMARY_BATCH, summary_threshold() - keep):
        return False
    batch = rows[:overflow]
    config = await get_model_config()
    transcript = _summary_transcript(batch)
    prompt = (f"已有摘要：\n{session.summary or '（无）'}\n\n新增对话：\n{transcript}\n\n"
              "请输出合并后的摘要。")
    try:
        result = await model_factory.invoke_json(
            config, SUMMARY_SYSTEM_PROMPT, prompt, SUMMARY_SCHEMA,
            SUMMARY_CALL_TYPE, session.id, "摘要模型",
            validator=lambda data: str((data or {}).get("summary") or "").strip())
    except Exception:  # noqa: BLE001 - 摘要失败不该挡住这一轮问答
        logger.exception("会话 %s 的滚动摘要失败，本轮照常回答", session.id)
        return False
    if not result:
        return False
    await ChatSession.filter(id=session.id).update(
        summary=result, summary_upto_message_id=batch[-1].id)
    session.summary = result
    session.summary_upto_message_id = batch[-1].id
    return True


# --------------------------------------------------------------------------- #
# 工具装配
# --------------------------------------------------------------------------- #


def build_tools(actor: kb_tools.Actor, registry: citations.CitationRegistry) -> list[Any]:
    """把工具表包成 ToolNode 认的工具，同时挂上引用登记。

    每个工具的实现在 `kb_tools` 里只有一份；这里只做三件事：把 Actor 绑进去、
    把结果登记成引用、把结果序列化成 tool message。MCP 侧走的是同一条
    `kb_tools.execute_tool`，只是没有引用登记（那边不产回答）。
    """
    tools: list[Any] = []
    for spec in kb_tools.TOOL_SPECS:
        tools.append(StructuredTool(
            name=spec.name,
            description=spec.description,
            args_schema=spec.arguments,
            coroutine=_tool_runner(spec, actor, registry),
        ))
    return tools


def _tool_runner(spec: kb_tools.ToolSpec, actor: kb_tools.Actor,
                 registry: citations.CitationRegistry):
    async def run(**kwargs: Any) -> str:
        result = await kb_tools.execute_tool(actor, spec.name, kwargs)
        return kb_tools.tool_result_text(registry.annotate(spec.name, result))

    return run


# --------------------------------------------------------------------------- #
# 一次问答的事件流
# --------------------------------------------------------------------------- #


async def stream_answer(session: ChatSession, question: str, *, user: User,
                        model: Any = None) -> AsyncIterator[tuple[str, dict[str, Any]]]:
    """跑一次问答，产出协议事件：meta / tool_start / tool_end / citation / token / done / error。

    事件的**语义**在这里确定（传输无关）；api/chat.py 只负责把它包成 SSE 帧、
    加心跳与禁缓冲头，MCP 侧则完全不经过这里。
    """
    text = (question or "").strip()
    if not text:
        yield ("error", {"message": "问题不能为空"})
        return
    # 内容审核第一层（输入）：违规内容不进模型——送进去就已经产生了一次生成
    input_verdict = moderation.check_input(text)
    if input_verdict.action == moderation.ACTION_BLOCK and input_verdict.matched:
        yield ("error", {"message": moderation.BLOCKED_MESSAGE})
        return

    config = await get_model_config()
    await maybe_summarize(session, model=None)

    # 助手行**先建后用**：它的 id 就是本轮所有模型调用的 biz_id（ai_call_log），
    # 于是"这轮花了多少 token"是一次聚合查询，而不是靠时间窗猜；
    # 工具层发起的检索调用也带同一个 biz_id，所以那一部分也归到这条回答上。
    user_row = await ChatMessage.create(session_id=session.id, role=MESSAGE_USER, content=text)
    assistant_row = await ChatMessage.create(session_id=session.id, role=MESSAGE_ASSISTANT, content="")

    actor = await kb_tools.Actor.resolve(
        user, requested=list(session.scope_meeting_ids or []) or None,
        biz_id=assistant_row.id)
    registry = citations.CitationRegistry()
    tools = build_tools(actor, registry)
    graph = chat_graph.build_chat_graph()
    budget = chat_graph.budget_from_env()
    recursion_limit = chat_graph.recursion_limit(budget)
    values: dict[str, Any] = {"message_count": F("message_count") + 2, "last_active_time": now()}
    if session.title == "新对话":
        # 首次提问的前若干字当标题（列表页要能认出来这是哪次对话）
        values["title"] = text[:SESSION_TITLE_CHARS]
    await ChatSession.filter(id=session.id).update(**values)

    # 查询规划（默认关）：把相对时间/指代落到已知事实上，并给出意图与建议工具。
    # **提示式注入**——它只多出一条系统消息，不改工具集、不替换问题、也不强制 tool_choice。
    # 失败一律降级为"不注入"（规划是增强，不该有能力把主链路弄挂）。
    plan: dict[str, Any] | None = None
    plan_message: SystemMessage | None = None
    if query_plan.plan_enabled():
        # 整段包住：`plan_query` 内部已经吞掉模型调用失败，但**取范围**那一步也会碰库——
        # 增强能力不该有任何一条路径能把主链路弄挂
        try:
            plan = await query_plan.plan_query(
                text, scope=await _plan_scope(actor), biz_id=assistant_row.id)
        except Exception:  # noqa: BLE001 - 规划是增强，失败就退回"不注入"
            logger.exception("查询规划失败，本轮不注入规划提示")
            plan = {"enabled": True, "used": False, "triggers": [], "reason": "规划失败"}
        if plan.get("used"):
            plan_message = SystemMessage(query_plan.plan_hint(plan))

    yield ("meta", {
        "session_id": session.id, "message_id": assistant_row.id,
        "user_message_id": user_row.id, "model": config.model_name,
        "scope_meeting_ids": list(session.scope_meeting_ids or []),
        "question": text,
        # 规划结果也进 meta：抓取侧据此能判断"这一轮到底有没有开规划、改写成什么"
        "plan": plan,
    })

    history = await build_memory(session, user=user)
    messages = history + ([plan_message] if plan_message else []) + [await question_message(text)]
    state_in: dict[str, Any] = {
        "messages": messages,
        "call_model": model_factory.make_model_caller(
            config, tools=kb_tools.openai_tools(), call_type=CHAT_CALL_TYPE,
            biz_id=assistant_row.id, label="问答模型", model=model,
            # 首轮强制检索：能力允许时用 tool_choice="required"（供应商 × 推理开关
            # 的判定在 model_factory 里），判不了就退回图里的"提醒"路径。
            # 实测背景：修复前 27 轮里 24 轮首轮不检索，多花约 26% token。
            first_round_tool_choice=model_factory.first_round_tool_choice(config, CHAT_CALL_TYPE)),
        "tools": _tool_node(tools),
        "budget": budget,
    }

    chunks: list[str] = []
    emitted_refs = 0
    final_state: dict[str, Any] = {}
    failure: str | None = None
    interrupted: BaseException | None = None
    # **首轮的文本先不发**：首轮要么应该先检索（模型通常会直接给工具调用、没有正文），
    # 要么没检索、会被图里的"提醒"重问一次。若把首轮正文直接推给用户，那段没依据的
    # 内容就已经出现在屏幕上了（实测它还可能是编造的），事后再补一句"未检索"也来不及。
    # 所以首轮正文先缓着：这一轮确实去调工具了才当作开场白补发，否则丢弃。
    # 第二轮起照常逐 token 实时推——那时"有没有依据"这件事已经确定了。
    model_round = 0
    first_round_buffer = ""
    try:
        async for event in graph.astream_events(
                state_in, config={"recursion_limit": recursion_limit}, version="v2"):
            name = event.get("event")
            node = (event.get("metadata") or {}).get("langgraph_node")
            if name == "on_chat_model_end" and node == "agent":
                model_round += 1
                message = event.get("data", {}).get("output")
                if model_round == 1 and getattr(message, "tool_calls", None) and first_round_buffer:
                    # 首轮确实发起了检索：把它前面的开场白补发给用户
                    chunks.append(first_round_buffer)
                    yield ("token", {"text": first_round_buffer})
                first_round_buffer = ""
                continue
            if name == "on_chat_model_stream" and node == "agent":
                piece = _stream_text(event.get("data", {}).get("chunk"))
                if not piece:
                    continue
                if model_round == 0:
                    first_round_buffer += piece
                else:
                    chunks.append(piece)
                    yield ("token", {"text": piece})
            elif name == "on_tool_start" and node == "tools":
                yield ("tool_start", {
                    "name": event.get("name") or "", "call_id": event.get("run_id", ""),
                    "arguments": _tool_arguments(event),
                })
            elif name == "on_tool_end" and node == "tools":
                yield ("tool_end", {
                    "name": event.get("name") or "", "call_id": event.get("run_id", ""),
                    "summary": chat_graph.summarize_tool_result(
                        event.get("name") or "", _tool_output_text(event)),
                })
                fresh = registry.since(emitted_refs)
                for citation in fresh:
                    yield ("citation", citation.as_dict())
                emitted_refs += len(fresh)
            elif name == "on_chain_end" and event.get("name") == "LangGraph":
                output = event.get("data", {}).get("output")
                if isinstance(output, dict):
                    final_state = output
    except (asyncio.CancelledError, GeneratorExit) as error:
        # 客户端断开（页面关了、网络断了、上游取消了这次请求）。这一轮真的调过模型、
        # 真的花过钱，所以必须落库；但**要把中断原样传出去**——吞掉它会让传输层
        # 以为"一切正常结束"，那是另一种不诚实。
        interrupted = error
        failure = "客户端断开，本轮回答未完成"
    except Exception as error:  # noqa: BLE001 - 失败也要落库并如实告知，不能只断流
        failure = (str(error) or type(error).__name__)[:1000]
        logger.exception("会话 %s 的问答失败", session.id)

    # 首轮缓冲若还留着（模型既没检索、也没被提醒后重问就直接结束），它已经被
    # 上面的规则丢弃：它没有依据，不该出现在答案里
    first_round_buffer = ""
    answer = "".join(chunks)
    stop_reason = str(final_state.get("stop_reason") or "")
    detail = chat_graph.stop_detail(final_state)
    # **有没有依据**：这一轮一次工具都没调用（提醒之后仍未调用），说明答案没有任何
    # 知识库依据。实测过模型在这种情况下的行为：它会直接编造会议里不存在的内容并
    # 声称来自某场会（同一条问题连问四次，两次如此）。处置是"如实标注"而不是静默
    # 放行——页面以错误级提示告知这一轮的回答不作为依据（"无证据不判定"在问答侧
    # 的落点），原文仍留库供审计与复盘。
    #
    # **两个前提缺一不可**（实测踩过：一条中间失败的轮次被扣上了"没检索"的帽子）：
    # 1. 图**正常跑完**才谈得上"这一轮有没有调工具"——中途抛异常时 `final_state` 是空的，
    #    `tool_calls` 读出来是 0，那是"没有状态"而不是"没有检索"；
    # 2. 已经有失败原因时不许覆盖——那会把真实原因（模型调用失败）换成一句错的诊断，
    #    排查的人会照着错的方向查（"为什么没检索"）（这一步是本条缺陷的根因）。
    if (final_state and failure is None
            and int(final_state.get("tool_calls", 0) or 0) == 0 and answer):
        stop_reason = chat_graph.STOP_NO_RETRIEVAL
        failure = chat_graph.STOP_NOTICE[chat_graph.STOP_NO_RETRIEVAL]
        logger.warning("会话 %s 的这一轮没有调用任何工具（跑了 %d 轮）",
                       session.id, int(final_state.get("round_no", 0) or 0))
    elif failure is None and not answer and stop_reason == chat_graph.STOP_STALLED:
        # 停滞闸掐断，而且这一轮什么都没给出来（q26/q27 就是这么结束的）：
        # 把"卡在哪"提成**错误级**结局——页面用红色告警渲染 error_message，
        # 而不是一句蓝色提示配一个空气泡。停在闸门上是正确行为，说不清原因才是缺陷。
        failure = chat_graph.stop_reason_text(stop_reason, detail)
        logger.warning("会话 %s 的这一轮停在 %s：%s", session.id, stop_reason, detail)
    # 内容审核第三层（输出复检）：输入干净不代表输出干净——检索到的会议原文本身
    # 就可能含违规表述。这一层才是门，处置动作（flag/redact/block）由配置决定
    output_verdict = moderation.check_output(answer)
    if output_verdict.matched:
        answer, blocked_by_moderation = moderation.apply_output(answer, output_verdict)
        if blocked_by_moderation:
            # 拦下整段回答：正文不返回，改由下面的 error 事件给提示
            failure = moderation.BLOCKED_MESSAGE
    # 实体核查（生成后、落库前）：答案里的人名/数字如果在本轮检索结果里找不到，
    # **如实标注、不拦截**（与 no_retrieval 同款处置）。实测背景 q14：答案里出现了
    # 种子里不存在的人名"李婷"与通知渠道，判定侧逮到了、产品侧没有任何环节会报警。
    entity_flags = entity_check.check_entities(answer, registry.as_dicts()) if answer else []
    entity_note = entity_check.describe(entity_flags)
    if entity_note and failure is None:      # 已经失败的那一轮不叠加标注（原因只说一次）
        failure = entity_note
        logger.warning("会话 %s 的这一轮出现检索结果外的实体：%s",
                       session.id, [flag["text"] for flag in entity_flags])
    tokens = await turn_tokens(assistant_row.id)
    stored = await _persist_turn(assistant_row, answer=answer, stop_reason=stop_reason,
                                 registry=registry, state=final_state, failure=failure,
                                 tokens=tokens, moderation_verdict=output_verdict, plan=plan)
    if interrupted is not None:
        raise interrupted
    if failure:
        yield ("error", {"message": failure, "message_id": assistant_row.id})
        return
    yield ("done", {
        "message_id": assistant_row.id, "stop_reason": stop_reason,
        "notice": chat_graph.STOP_NOTICE.get(stop_reason, ""),
        # 实体核查的标注（检索结果里找不到的人名/数字）。它同时进了 error_message，
        # 页面与库里是同一句话
        "entity_flags": entity_flags,
        # 非正常结束时说明"卡在哪"（正常结束是空串）。它同时进 error_message 与
        # done 事件：页面看得到，库里也查得到，两处是同一句话。
        "detail": detail,
        "tokens": tokens, "citations": stored["citations"]["used"],
        "tool_calls": len(stored["tools"]),
        "unknown_refs": stored["citations"]["unknown_refs"],
        "plan": plan,
    })


async def _plan_scope(actor: kb_tools.Actor) -> str:
    """给规划模型的"已知事实"：今天 + 可见会议（含日期）+ 各会议录音段数与时间。

    只给这些——改写要落到**系统确实知道**的实体上；给多了它就会去猜
    （实测它把 2026 年的会猜成 2024 年，一次猜错丢了一轮的全部金标事实）。
    """
    allowed = actor.allowed_meeting_ids
    query = Meeting.all() if allowed is None else Meeting.filter(id__in=list(allowed) or [-1])
    rows = await query.order_by("-start_time").limit(20)
    meetings = [{"id": row.id, "title": row.title, "start_time": _fmt(row.start_time)}
                for row in rows]
    tasks = (TranscriptionTask.filter(status="SUCCEEDED", meeting_id__in=[row["id"] for row in meetings])
             if meetings else None)
    recordings: list[dict[str, Any]] = []
    if tasks is not None:
        scanned = await tasks.order_by("meeting_id", "started_at").limit(40)
        grouped: dict[int, list[Any]] = {}
        for task in scanned:
            grouped.setdefault(int(task.meeting_id), []).append(task)
        for meeting_id, items in grouped.items():
            times = [_fmt(item.started_at or item.create_time) for item in items]
            recordings.append({"meeting_id": meeting_id, "count": len(items),
                               "first": times[0], "last": times[-1]})
    return query_plan.scope_lines(meetings, recordings, _fmt(now()) or "")


def _tool_node(tools: list[Any]) -> Any:
    node = ToolNode(tools)

    async def run(state: dict[str, Any]) -> dict[str, Any]:
        return await node.ainvoke(state)

    return run


def _stream_text(chunk: Any) -> str:
    content = getattr(chunk, "content", "")
    if isinstance(content, list):
        return "".join(str(part.get("text") or "") for part in content
                       if isinstance(part, dict))
    return str(content or "")


def _tool_arguments(event: dict[str, Any]) -> dict[str, Any]:
    data = event.get("data") or {}
    raw = data.get("input")
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(str(raw or "{}"))
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _tool_output_text(event: dict[str, Any]) -> str:
    output = (event.get("data") or {}).get("output")
    for attribute in ("content", "text"):
        value = getattr(output, attribute, None)
        if isinstance(value, str):
            return value
    return str(output or "")


async def turn_tokens(message_id: int, *, call_types: tuple[str, ...] = (CHAT_CALL_TYPE,)
                      ) -> dict[str, int]:
    """本轮 token 用量：直接聚合 ai_call_log（记账的唯一真源，不再另记一份）。

    接口层也用它——**按账本记账而不是按 done 事件记账**：客户端在答案生成完之前
    断开时拿不到 done 事件，若按它记账，反复"提问→立刻断开"就能绕开每日预算。
    账本里的行是每次调用当场写的，与客户端在不在无关。

    `call_types` 默认只算问答模型（CHAT）。**配额要传全部相关类型**（问答 + 检索重排 +
    查询规划）：那些调用同样花钱，不记进配额等于给了一条免费额度。
    """
    totals = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    rows = await AiCallLog.filter(biz_id=message_id, call_type__in=list(call_types))
    for row in rows:
        usage = row.usage_json or {}
        totals["input_tokens"] += int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
        totals["output_tokens"] += int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
        totals["total_tokens"] += int(usage.get("total_tokens") or 0)
    return totals


async def _persist_turn(assistant_row: ChatMessage, *, answer: str, stop_reason: str,
                        registry: citations.CitationRegistry, state: dict[str, Any],
                        failure: str | None, tokens: dict[str, int],
                        moderation_verdict: Any = None,
                        plan: dict[str, Any] | None = None) -> dict[str, Any]:
    """把这一轮的结果落库：正文、停止原因、引用快照、工具轨迹、用量。"""
    used = citations.used_citations(answer, registry)
    stored = {
        "citations": {
            "used": [item.as_dict() for item in used],
            # 检索到但答案没引用的也留着：页面要展示"查到了什么"，
            # 复盘时要能回答"明明查到了为什么没引用"
            "all": registry.as_dicts(),
            # 引用了登记表里没有的编号 = 模型的自由发挥。如实记录、不静默丢弃
            "unknown_refs": citations.unknown_refs(answer, registry),
        },
        "tools": chat_graph.tool_trace(state.get("messages") or []),
    }
    await ChatMessage.filter(id=assistant_row.id).update(
        content=answer, stop_reason=stop_reason or None,
        citations_json=stored["citations"], tools_json=stored["tools"],
        # 审核留痕：没命中时也写一行（enabled/matched=false），
        # 这样"这次扫过了没命中"与"这次根本没扫"在库里能分开
        moderation_json=(moderation_verdict.as_dict() if moderation_verdict is not None
                         else None),
        token_count=tokens["total_tokens"],
        error_message=failure,
        plan_json=plan,
    )
    return stored


def _fmt(value: Any) -> Any:
    return format_datetime(value)


__all__ = [
    "ChatError", "CHAT_CALL_TYPE", "SUMMARY_CALL_TYPE", "DEFAULT_SYSTEM_PROMPT",
    "CITATION_PROTOCOL", "create_session", "get_session", "list_sessions",
    "delete_session", "list_messages", "build_memory", "maybe_summarize", "question_message",
    "build_tools", "stream_answer", "get_model_config", "get_system_prompt", "turn_tokens",
    "history_window", "summary_threshold", "MESSAGE_USER", "MESSAGE_ASSISTANT",
    "compress_answer", "SUMMARY_SYSTEM_PROMPT",
]
