"""问答落库：引用解析、token 汇总、最终消息持久化。"""

from __future__ import annotations

import logging
from typing import Any

from common.times import format_datetime
from models import AiCallLog, ChatMessage

from services import (
    chat_graph,
    citations,
)
from services.chat_prompts import CHAT_CALL_TYPE
from services.chat_sessions import MESSAGE_ASSISTANT

logger = logging.getLogger("meetingbot.chat")

async def _history_citations(session_id: int, before_id: int) -> dict[int, dict[str, Any]]:
    """本会话里此前各轮用过的引用，按编号索引（最近的轮次优先）。

    引用登记表是**按轮**新建的，上一轮的 [3] 在本轮不在登记表里；但模型会在
    历史回答里看到它并复用。落库时用这张表把旧编号补回引用快照，页面才能渲染
    卡片；补不到的才记 `unknown_refs`。不是"改历史编号"，只是把旧编号解析回
    它当时指向的条目。
    """
    rows = await ChatMessage.filter(
        session_id=session_id, role=MESSAGE_ASSISTANT, id__lt=before_id,
    ).order_by("-id")
    mapping: dict[int, dict[str, Any]] = {}
    for row in rows:
        for item in (row.citations_json or {}).get("used") or []:
            if not isinstance(item, dict):
                continue
            ref = item.get("ref")
            if ref is not None and int(ref) not in mapping:
                mapping[int(ref)] = item
    return mapping


def _resolve_unknown_refs(answer: str, registry: citations.CitationRegistry,
                          history: dict[int, dict[str, Any]]) -> tuple[list[dict[str, Any]],
                                                                     list[int]]:
    """本轮用到的引用 + 能从历史解析的旧编号；返回 (引用快照, 仍未知的编号)。

    只解析**能对上号的**旧编号；对不上号的保持 unknown，不猜、不静默丢。
    """
    used = [item.as_dict() for item in citations.used_citations(answer, registry)]
    known = {int(item.get("ref")) for item in used}
    remaining: list[int] = []
    for ref in citations.unknown_refs(answer, registry):
        item = history.get(int(ref))
        if item is not None and int(ref) not in known:
            used.append(dict(item))
            known.add(int(ref))
        else:
            remaining.append(int(ref))
    return used, remaining


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
                        entity_note: str = "",
                        moderation_verdict: Any = None,
                        plan: dict[str, Any] | None = None,
                        security: dict[str, Any] | None = None,
                        citation_binding: dict[str, Any] | None = None,
                        run_status: str = "FINISHED",
                        thread_id: str | None = None,
                        checkpoint_id: str | None = None,
                        trace_id: str | None = None,
                        timing: dict[str, Any] | None = None) -> dict[str, Any]:
    """把这一轮的结果落库：正文、停止原因、引用快照、工具轨迹、用量。"""
    # 旧编号解析：引用登记表按轮新建，模型可能复用历史回答里的 [n]。
    # 能从本会话此前的引用快照对上号的补回 used（页面卡片可渲染）；
    # 对不上号的才记 unknown_refs。**不在生成前剥历史编号**——实测那会让模型
    # 干脆不写引用（27 题回归 q27 变成 0 引用）。
    history = await _history_citations(assistant_row.session_id, assistant_row.id)
    used, unknown = _resolve_unknown_refs(answer, registry, history)
    moderation_snapshot = (moderation_verdict.as_dict()
                          if moderation_verdict is not None else None)
    if security:
        # 注入防护留痕与审核共用一列：两者都是"这一轮的安全扫描结果"，
        # 页面/排障读同一个出口；不新增列，避免又一份会漂移的真相。
        moderation_snapshot = dict(moderation_snapshot or {})
        moderation_snapshot["security"] = security
    if citation_binding:
        # 生成后引用绑定的留痕：绑了几条/哪些数字仍无依据。与安全扫描同列，
        # 但放在独立 key 下，互不覆盖（复盘时能看出"这轮做过绑定"）。
        moderation_snapshot = dict(moderation_snapshot or {})
        moderation_snapshot["citation_binding"] = citation_binding
    if timing:
        # 阶段耗时：图节点（agent/tools/coverage/nudge/finalize）+ 工具调用。
        # 与安全/绑定同列但独立 key，页面与排障读同一个出口。
        moderation_snapshot = dict(moderation_snapshot or {})
        moderation_snapshot["timing"] = timing
    stored = {
        "citations": {
            "used": used,
            # 检索到但答案没引用的也留着：页面要展示"查到了什么"，
            # 复盘时要能回答"明明查到了为什么没引用"
            "all": registry.as_dicts(),
            # 补不回来的编号 = 真未知；如实记录、不静默丢弃
            "unknown_refs": unknown,
        },
        "tools": chat_graph.tool_trace(state.get("messages") or []),
    }
    await ChatMessage.filter(id=assistant_row.id).update(
        content=answer, stop_reason=stop_reason or None,
        citations_json=stored["citations"], tools_json=stored["tools"],
        # 审核留痕：没命中时也写一行（enabled/matched=false），
        # 这样"这次扫过了没命中"与"这次根本没扫"在库里能分开
        moderation_json=moderation_snapshot,
        token_count=tokens["total_tokens"],
        # 失败原因优先；没有失败时，实体核查的标注也留在这里（页面按警告渲染），
        # 但 stop_reason 仍是 finished——标注不是失败
        error_message=failure or entity_note or None,
        plan_json=plan,
        run_status=run_status,
        thread_id=thread_id,
        checkpoint_id=checkpoint_id,
        trace_id=trace_id,
    )
    return stored


def _fmt(value: Any) -> Any:
    return format_datetime(value)

