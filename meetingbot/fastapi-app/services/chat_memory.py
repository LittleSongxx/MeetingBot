"""问答记忆：滑窗、历史压缩、滚动摘要。"""

from __future__ import annotations

import logging
import os
from typing import Any

from common import access
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from models import ChatMessage, ChatSession, Meeting, User
from tortoise.expressions import Q

from services import (
    model_factory,
)
from services.chat_prompts import (
    SUMMARY_CALL_TYPE,
    SUMMARY_SCHEMA,
    SUMMARY_SYSTEM_PROMPT,
    get_model_config,
    get_system_prompt,
)
from services.chat_sessions import MESSAGE_ASSISTANT, MESSAGE_USER

DEFAULT_HISTORY_WINDOW = 10


DEFAULT_SUMMARY_THRESHOLD = 20


MIN_SUMMARY_BATCH = 4


logger = logging.getLogger("meetingbot.chat")

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
    config = await get_model_config()
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

