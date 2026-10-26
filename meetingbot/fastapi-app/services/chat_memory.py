"""问答记忆：滑窗、历史压缩、滚动摘要。"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

from common import access
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from models import ActionItem, ChatMessage, ChatSession, Meeting, MeetingSeries, User
from tortoise.expressions import Q

from services import (
    model_config,
    model_factory,
)
from services.chat_prompts import (
    SUMMARY_CALL_TYPE,
    SUMMARY_SCHEMA,
    SUMMARY_SYSTEM_PROMPT,
    get_system_prompt,
)
from services.chat_sessions import MESSAGE_ASSISTANT, MESSAGE_USER

DEFAULT_HISTORY_WINDOW = 10


DEFAULT_SUMMARY_THRESHOLD = 20


MIN_SUMMARY_BATCH = 4


logger = logging.getLogger("meetingbot.chat")

# 滚动摘要默认同步执行；置 CHAT_SUMMARY_ASYNC=1 后改为回答完成后的后台任务，
# 避免长会话每 5 轮把 16s+ 的摘要调用挡在首 token 之前。
_SUMMARY_TASKS: set[asyncio.Task] = set()
_SUMMARY_LOCKS: dict[int, asyncio.Lock] = {}


def summary_async_enabled() -> bool:
    value = (os.getenv("CHAT_SUMMARY_ASYNC") or "0").strip().lower()
    return value in {"1", "true", "yes", "on"}

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


async def build_memory(session: ChatSession, user: User, *,
                       before_id: int | None = None) -> list[Any]:
    """系统提示词 + 滚动摘要 + 最近窗口的消息，组成下一次调用的输入。

    历史里的助手消息**进上下文时压成"结论 + 引用编号"**（原文完整留在库里）。
    理由：单轮 token 实测随会话从 5.7k 涨到 26.5k，涨的主要是上一轮那份 20–30 条断言的
    全文；而依据段逐条抄的就是检索原文——同一场会反复问时，它们下一轮还会被重新检索到，
    留在上下文里等于把同一批切片付第二遍钱。编号留着，要细节就再检索一次（工具的用途）。

    `before_id` 排除本轮刚创建的用户消息：调用方随后会把当前问题作为
    `question` 追加，两处都放会让模型把同一个问题看到两遍。
    """
    system = await get_system_prompt()
    messages: list[Any] = [SystemMessage(system)]
    # 系列范围：只给"系列 + 会议数 + 未完成待办数"这种高信号小上下文；
    # 具体条目仍走 query_meeting_state 工具按需取，避免每轮把记忆全塞进来。
    series_scope = list(session.scope_series_ids or [])
    if series_scope:
        visible_series = await access.visible_series_ids(user, requested=series_scope)
        visible_ids = series_scope if visible_series is None else visible_series
        if visible_ids:
            series_rows = await MeetingSeries.filter(id__in=visible_ids)
            names = "、".join(f"{row.name}（id={row.id}）" for row in series_rows)
            meeting_count = await Meeting.filter(series_id__in=visible_ids).count()
            open_actions = await ActionItem.filter(
                series_id__in=visible_ids, status="OPEN").count()
            messages.append(SystemMessage(
                f"本次对话限定在这些系列内：{names}；共 {meeting_count} 场会，"
                f"其中未完成待办 {open_actions} 条。跨会议问题优先用 "
                "query_meeting_state 查已确认决策/待办，再检索原文。"))
    scope = list(session.scope_meeting_ids or [])
    if scope:
        # 范围是建会话时的快照；权限可能后来被收回，标题也不能再进上下文
        visible = await access.visible_meeting_ids(user, requested=scope)
        valid = scope if visible is None else visible
        titles = await Meeting.filter(id__in=valid or [-1]).values_list("id", "title") if valid else []
        listed = "、".join(f"{title}（id={mid}）" for mid, title in titles)
        if listed:
            messages.append(SystemMessage(f"本次对话限定在这些会议内：{listed}。"))
    if session.summary:
        messages.append(SystemMessage(f"更早对话的摘要：\n{session.summary}"))
    query = ChatMessage.filter(
        session_id=session.id, id__gt=session.summary_upto_message_id,
    ).filter(Q(role=MESSAGE_USER) | Q(role=MESSAGE_ASSISTANT, run_status="FINISHED"))
    if before_id is not None:
        query = query.filter(id__lt=int(before_id))
    rows = await query.order_by("-id").limit(history_window())
    for row in reversed(rows):
        # 失败的、没内容的助手行不进上下文：它们没有可用的信息，
        # 留在历史里只会让模型模仿"空回答"
        if row.role == MESSAGE_ASSISTANT and not (row.content or "").strip():
            continue
        if row.role == MESSAGE_USER:
            messages.append(HumanMessage(row.content))
        else:
            # 历史里**保留**引用编号：剥掉它们会让模型下一轮干脆不写引用
            #（实测 27 题回归里 q27 变成 0 引用）。旧编号的死链在落库时解析，
            # 见 `_resolve_unknown_refs`。
            messages.append(AIMessage(compress_answer(row.content or "",
                                                      citations=_used_refs(row))))
    return messages


def _used_refs(row: ChatMessage) -> list[int]:
    """这条助手消息引用过的编号（落库的引用快照，与页面渲染同源）。"""
    used = (row.citations_json or {}).get("used") or []
    return [int(item.get("ref")) for item in used if item.get("ref")]


_EVIDENCE_HEADINGS = ("依据", "出处", "来源", "证据")


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


async def maybe_summarize(session: ChatSession, *, biz_id: int | None = None) -> bool:
    """攒够未摘要消息就把最旧的一批压成摘要。返回是否真的压了。

    水位线（`summary_upto_message_id`）保证每批消息只进一次摘要；
    没有它就只能整段重算（重复付费）或猜"哪些已进过"（重复计入）。
    `biz_id` 传当前助手消息 id 时，摘要调用会计入这一轮的 token 预算。
    """
    rows = await ChatMessage.filter(
        session_id=session.id, id__gt=session.summary_upto_message_id,
    ).order_by("id")
    keep = history_window()
    overflow = len(rows) - keep
    if overflow < max(MIN_SUMMARY_BATCH, summary_threshold() - keep):
        return False
    batch = rows[:overflow]
    try:
        config = await model_config.resolve_route("CHAT_SUMMARY", "AGENT")
    except model_config.ModelConfigError:
        logger.exception("会话 %s 的摘要模型不可用，本轮跳过摘要", session.id)
        return False
    transcript = _summary_transcript(batch)
    prompt = (f"已有摘要：\n{session.summary or '（无）'}\n\n新增对话：\n{transcript}\n\n"
              "请输出合并后的摘要。")
    try:
        result = await model_factory.invoke_json(
            config, SUMMARY_SYSTEM_PROMPT, prompt, SUMMARY_SCHEMA,
            SUMMARY_CALL_TYPE, biz_id or session.id, "摘要模型",
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


def _summary_lock(session_id: int) -> asyncio.Lock:
    """同一会话的摘要串行化：两个请求同时触发时，后一个只会在水位线后重算。"""
    lock = _SUMMARY_LOCKS.get(session_id)
    if lock is None:
        lock = asyncio.Lock()
        _SUMMARY_LOCKS[session_id] = lock
    return lock


async def _record_summary_timing(message_id: int, elapsed_ms: int) -> None:
    """把后台摘要耗时合并进本轮 timing；消息已被删除/还未落库时静默跳过。"""
    row = await ChatMessage.get_or_none(id=int(message_id))
    if row is None:
        return
    moderation = dict(row.moderation_json or {})
    timing = dict(moderation.get("timing") or {})
    timing["summary_ms"] = int(elapsed_ms)
    moderation["timing"] = timing
    await ChatMessage.filter(id=row.id).update(moderation_json=moderation)


async def _summarize_in_background(session_id: int, biz_id: int) -> None:
    started = time.perf_counter()
    try:
        async with _summary_lock(session_id):
            session = await ChatSession.get_or_none(id=int(session_id))
            if session is None:
                return
            await maybe_summarize(session, biz_id=biz_id)
    except Exception:  # noqa: BLE001 - 后台摘要失败不能影响问答
        logger.exception("会话 %s 的后台滚动摘要失败", session_id)
    finally:
        await _record_summary_timing(biz_id, int((time.perf_counter() - started) * 1000))


def schedule_summary(session_id: int, biz_id: int) -> None:
    """把滚动摘要放到回答完成后的后台任务里；关闭异步时由调用方走同步路径。"""
    if not summary_async_enabled():
        return
    task = asyncio.create_task(_summarize_in_background(int(session_id), int(biz_id)))
    _SUMMARY_TASKS.add(task)
    task.add_done_callback(_SUMMARY_TASKS.discard)


async def wait_summary_tasks() -> None:
    """测试/优雅退出用：等待已排队的后台摘要全部结束。"""
    tasks = list(_SUMMARY_TASKS)
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)

