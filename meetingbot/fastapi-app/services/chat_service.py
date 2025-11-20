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
from models import AiCallLog, AiModelConfig, ChatMessage, ChatSession, Meeting, PromptTemplate, User
from tortoise.expressions import F

from services import chat_graph, citations, kb_tools, model_factory, moderation

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

# 摘要提示词：内部压缩步骤，不是面向用户的产品提示词，因此不进 Prompt 模板表
SUMMARY_SYSTEM_PROMPT = (
    "你是对话记录压缩助手。把给定的历史对话压成一段事实摘要，"
    "保留：用户关心的问题、已给出的结论与它引用的会议/条目编号、尚未解决的问题。"
    "不要补充原文没有的信息，不要评价。"
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
    # 内容审核的第二层：生成约束。未配置词表时它返回空串（不留一句空话）
    return body.strip() + CITATION_PROTOCOL + moderation.prompt_constraints()


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
        "tools": row.tools_json or [],
    }
    if row.stop_reason and row.stop_reason != chat_graph.STOP_FINISHED:
        payload["notice"] = chat_graph.STOP_NOTICE.get(row.stop_reason, "")
    return payload


# --------------------------------------------------------------------------- #
# 记忆
# --------------------------------------------------------------------------- #


async def build_memory(session: ChatSession, user: User) -> list[Any]:
    """系统提示词 + 滚动摘要 + 最近窗口的消息，组成下一次调用的输入。"""
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
            messages.append(AIMessage(row.content))
    return messages


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
    transcript = "\n".join(
        f"{'用户' if row.role == MESSAGE_USER else '助手'}：{row.content}" for row in batch)
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

    yield ("meta", {
        "session_id": session.id, "message_id": assistant_row.id,
        "user_message_id": user_row.id, "model": config.model_name,
        "scope_meeting_ids": list(session.scope_meeting_ids or []),
        "question": text,
    })

    history = await build_memory(session, user=user)
    state_in: dict[str, Any] = {
        "messages": history + [await question_message(text)],
        "call_model": model_factory.make_model_caller(
            config, tools=kb_tools.openai_tools(), call_type=CHAT_CALL_TYPE,
            biz_id=assistant_row.id, label="问答模型", model=model),
        "tools": _tool_node(tools),
        "budget": budget,
    }

    chunks: list[str] = []
    emitted_refs = 0
    final_state: dict[str, Any] = {}
    failure: str | None = None
    interrupted: BaseException | None = None
    try:
        async for event in graph.astream_events(
                state_in, config={"recursion_limit": recursion_limit}, version="v2"):
            name = event.get("event")
            node = (event.get("metadata") or {}).get("langgraph_node")
            if name == "on_chat_model_stream" and node == "agent":
                piece = _stream_text(event.get("data", {}).get("chunk"))
                if piece:
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

    answer = "".join(chunks)
    # 内容审核第三层（输出复检）：输入干净不代表输出干净——检索到的会议原文本身
    # 就可能含违规表述。这一层才是门，处置动作（flag/redact/block）由配置决定
    output_verdict = moderation.check_output(answer)
    blocked_by_moderation = False
    if output_verdict.matched:
        answer, blocked_by_moderation = moderation.apply_output(answer, output_verdict)
        if blocked_by_moderation:
            # 拦下整段回答：正文不返回，改由下面的 error 事件给提示
            failure = moderation.BLOCKED_MESSAGE
    stop_reason = str(final_state.get("stop_reason") or "")
    tokens = await turn_tokens(assistant_row.id)
    stored = await _persist_turn(assistant_row, answer=answer, stop_reason=stop_reason,
                                 registry=registry, state=final_state, failure=failure,
                                 tokens=tokens, moderation_verdict=output_verdict)
    if interrupted is not None:
        raise interrupted
    if failure:
        yield ("error", {"message": failure, "message_id": assistant_row.id})
        return
    yield ("done", {
        "message_id": assistant_row.id, "stop_reason": stop_reason,
        "notice": chat_graph.STOP_NOTICE.get(stop_reason, ""),
        "tokens": tokens, "citations": stored["citations"]["used"],
        "tool_calls": len(stored["tools"]),
        "unknown_refs": stored["citations"]["unknown_refs"],
    })


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


async def turn_tokens(message_id: int) -> dict[str, int]:
    """本轮 token 用量：直接聚合 ai_call_log（记账的唯一真源，不再另记一份）。

    接口层也用它——**按账本记账而不是按 done 事件记账**：客户端在答案生成完之前
    断开时拿不到 done 事件，若按它记账，反复"提问→立刻断开"就能绕开每日预算。
    账本里的行是每次调用当场写的，与客户端在不在无关。
    """
    totals = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    rows = await AiCallLog.filter(biz_id=message_id, call_type=CHAT_CALL_TYPE)
    for row in rows:
        usage = row.usage_json or {}
        totals["input_tokens"] += int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
        totals["output_tokens"] += int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
        totals["total_tokens"] += int(usage.get("total_tokens") or 0)
    return totals


async def _persist_turn(assistant_row: ChatMessage, *, answer: str, stop_reason: str,
                        registry: citations.CitationRegistry, state: dict[str, Any],
                        failure: str | None, tokens: dict[str, int],
                        moderation_verdict: Any = None) -> dict[str, Any]:
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
]
