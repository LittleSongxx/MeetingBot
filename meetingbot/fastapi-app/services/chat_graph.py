"""会议知识问答的状态图：**单 Agent + 工具循环 + 三道硬闸**。

    ┌─────────── agent（调模型，可能带工具调用） ──────────┐
    │        ▲                                            │
    │        │  没有工具调用 → 这就是答案 ─────────────────→ 结束
    │        │  触发硬闸 ──────────────────────────────────→ 结束（带停止原因）
    │        │                                            │
    └─→ tools（执行工具，结果回到消息里） ─────────────────┘

## 为什么是单 Agent

Google / Anthropic / Microsoft 三家的公开工程结论一致：**多 Agent 只在四种情况下
划算**——任务可并行、单上下文装不下、存在信任边界、工具域大到单 Agent 选不准。
这里四个条件一个都不成立：问答是一条串行的"查 → 读 → 答"链，工具只有 4 个，
没有需要隔离的不可信输入。多 Agent 会带来两个新问题（子 Agent 之间的信息丢失、
调试时要在三份上下文里找错误）而没有任何收益。

## 三道硬闸（缺一不可）

1. **最大轮数**：模型可以一直调工具。轮数是唯一在代码里兜住的终止条件——
   反思环那张图（`agent_graph`）的教训：终止由模型判定，但上限必须由代码守。
2. **token / 成本预算**：一次问答烧掉多少钱必须是可设上限的。token 数取自
   `ai_call_log` 里那一行的真实用量（`usage_metadata`），成本由注入的换算函数算，
   两者都在状态里累加，超了就在**下一次模型调用之前**停。
3. **停滞检测**：同一个工具、同一组参数连续调用多次说明它不会给出新信息
   （检索的词表没变、参数没变），再转一轮只会把 token 烧在同一个空结果上。
   连续 3 次（可配）即停——这是"模型说还要查，但事实是查不动了"的兜底。

## 不碰库、不调模型

与 `agent_graph` 同款：模型调用（`call_model`）与工具执行（`tools`）都由
`chat_service` 注入，图只决定"什么时候调哪个、什么时候必须停"。
因此这张图可以脱库、脱模型单测（`tests/test_chat_graph.py`）。
"""

from __future__ import annotations

import json
import os
from typing import Annotated, Any, TypedDict

from langchain_core.messages import HumanMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

# 停止原因：值是**给页面看的**，不是内部的枚举缩写
STOP_FINISHED = "finished"
# 首轮（含一次提醒后）仍没有调用任何工具：这一轮的回答没有任何知识库依据。
# 实测背景：同一条问题连问四次，两次模型直接作答且**编造**了会议里不存在的内容
# （例如"新版搜索响应慢"），并声称来自该会议。提示词里已经写明"只根据工具返回的
# 资料回答"，但那是概率性的；因此这里把它变成结构性的：不检索 → 这一轮标为异常。
STOP_NO_RETRIEVAL = "no_retrieval"
STOP_MAX_ROUNDS = "max_rounds"
STOP_BUDGET_TOKENS = "budget_tokens"
STOP_BUDGET_COST = "budget_cost"
STOP_BUDGET_TOOLS = "budget_tools"
STOP_STALLED = "stalled"

STOP_NOTICE = {
    STOP_NO_RETRIEVAL: "本轮没有检索知识库，回答内容未经会议资料验证，不能作为依据。",
    STOP_MAX_ROUNDS: "已达到本次问答的最大推理轮数，回答可能不完整。",
    STOP_BUDGET_TOKENS: "本次问答已达到 token 预算上限，回答可能不完整。",
    STOP_BUDGET_COST: "本次问答已达到成本预算上限，回答可能不完整。",
    STOP_BUDGET_TOOLS: "本次问答调用的工具次数已达上限，回答可能不完整。",
    STOP_STALLED: "检测到工具调用在原地重复，已停止继续检索。",
}


class ChatState(TypedDict, total=False):
    """状态从头传到尾，节点只返回自己改了的字段。"""

    # 消息流：用户提问、模型回复、工具结果都在这里，add_messages 负责追加与去重
    messages: Annotated[list[Any], add_messages]
    # 模型已经被调用了几轮（一个"轮"= 一次模型调用，不是一次工具调用）
    round_no: int
    tool_calls: int
    tokens_used: int
    cost_micro: int
    # 上一次工具调用的签名与它已经连续重复的次数（停滞检测用）
    last_signature: str
    stall_streak: int
    # 结束原因，由路由或 finalize 写入
    stop_reason: str
    # 这次为什么停得"什么都没有"的一句话（可读，给用户看；见 stop_detail）
    stop_detail: str
    # 是否已经为"没调工具"提醒过一次（只提醒一次，避免与模型的拉锯）
    nudged: bool

    # 下面两个由 chat_service 注入，图只管调用时机
    # async (messages) -> AIMessage（内部已完成记账）
    call_model: Any
    # 可 await 的工具节点（聊天侧注入 LangGraph 的 ToolNode）
    tools: Any
    # (usage: dict) -> int，把 token 用量换算成"微元"成本；缺省按 0 算
    cost_micro_of: Any
    # 预算，见 budget_from_env
    budget: dict


def budget_from_env() -> dict[str, int]:
    """一次问答的硬闸阈值。全部可配，因为它们取决于部署的用量与成本策略。"""

    def read(name: str, default: int) -> int:
        raw = os.getenv(name, str(default)).strip()
        try:
            value = int(raw)
        except ValueError as error:
            raise ValueError(f"{name} 必须为非负整数") from error
        if value < 0:
            raise ValueError(f"{name} 必须为非负整数")
        return value

    return {
        # 一轮 = 一次模型调用；6 轮足够"检索→补充原文→回答"的常见形态
        "max_rounds": read("CHAT_MAX_ROUNDS", 6),
        # 工具调用总数上限（一次回复里可以并行请求多个工具）
        "max_tool_calls": read("CHAT_MAX_TOOL_CALLS", 12),
        # 累计 token 上限：一次问答的输入会随轮次增长，这个值同时兜住上下文膨胀
        "max_tokens": read("CHAT_MAX_TOKENS", 120_000),
        # 累计成本上限（单位与 cost_micro_of 一致，默认是"微元"= 1e-6 元）；
        # 0 表示不按成本设限（只按 token 设限）
        "max_cost_micro": read("CHAT_MAX_COST_MICRO", 0),
        # 同一组工具参数连续重复多少次就判停滞
        "max_stall": read("CHAT_MAX_STALL", 3),
    }


# --------------------------------------------------------------------------- #
# 节点
# --------------------------------------------------------------------------- #


def tool_signature(message: Any) -> str:
    """一条模型回复里工具调用的签名。没有工具调用时返回空串。"""
    calls = getattr(message, "tool_calls", None) or []
    if not calls:
        return ""
    parts = []
    for call in calls:
        if isinstance(call, dict):
            name, args = call.get("name"), call.get("args")
        else:  # 理论上不会发生，防御式取值
            name, args = getattr(call, "name", ""), getattr(call, "args", None)
        parts.append({"name": name, "args": args})
    return json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str)


async def agent_node(state: ChatState) -> ChatState:
    """调一次模型。模型回复带不带工具调用决定下一站去哪（路由函数判断）。"""
    message = await state["call_model"](list(state.get("messages") or []))
    usage = getattr(message, "usage_metadata", None) or {}
    tokens = int(usage.get("total_tokens") or 0)
    cost = int(state["cost_micro_of"](usage)) if state.get("cost_micro_of") else 0
    calls = len(getattr(message, "tool_calls", None) or [])
    signature = tool_signature(message)
    # 停滞累计：签名与上次相同才算一次重复；换了参数或不再调工具就归零
    if signature and signature == state.get("last_signature"):
        streak = int(state.get("stall_streak", 0)) + 1
    else:
        streak = 1 if signature else 0
    return {
        "messages": [message],
        "round_no": int(state.get("round_no", 0)) + 1,
        "tool_calls": int(state.get("tool_calls", 0)) + calls,
        "tokens_used": int(state.get("tokens_used", 0)) + tokens,
        "cost_micro": int(state.get("cost_micro", 0)) + cost,
        "last_signature": signature or str(state.get("last_signature") or ""),
        "stall_streak": streak,
    }


async def tools_node(state: ChatState) -> ChatState:
    """执行工具。注入的是 `async (state) -> 部分状态`——聊天侧用 LangGraph 的
    ToolNode 包一层传进来（`ToolNode.ainvoke` 正好是同一个形状）。"""
    return await state["tools"](state)


# 提醒模型去检索：写在**用户位置**而不是系统位置——实测同一条问题，
# 系统提示词里写"只根据工具返回的资料回答"时模型仍会直接作答，而在最后一轮
# 用户消息里点名要求"先调用工具"，它就会调用。
NUDGE_MESSAGE = (
    "你上一条回复没有调用任何工具，而会议资料只能通过工具读到。"
    "请先调用 search_knowledge（或 list_facts / get_minutes / get_transcript_excerpt）"
    "检索，再依据检索结果回答；不要凭记忆描述会议内容。"
)


async def nudge_node(state: ChatState) -> ChatState:
    """首轮没检索时补一条提醒，让模型带着明确的动作要求再跑一轮。"""
    return {"messages": [HumanMessage(content=NUDGE_MESSAGE)], "nudged": True}


async def finalize_node(state: ChatState) -> ChatState:
    """结束节点：补上停止原因与它的可读说明，不再发起任何调用。"""
    if state.get("stop_reason"):
        return {"stop_detail": stop_detail(state)}
    return {"stop_reason": STOP_FINISHED}


# --------------------------------------------------------------------------- #
# 路由：三道硬闸都在这里
# --------------------------------------------------------------------------- #


def gate_reason(state: ChatState) -> str:
    """该不该停、为什么停。返回空串表示可以继续。

    `max_stall` 为 0 表示**关掉**停滞检测（不是"一有重复就停"）——
    阈值类参数在这里统一按"0 = 不设限"解释，与 `max_cost_micro` 一致。
    """
    budget = state.get("budget") or {}
    if int(state.get("round_no", 0)) >= int(budget.get("max_rounds", 0) or 0):
        return STOP_MAX_ROUNDS
    if int(state.get("tokens_used", 0)) >= int(budget.get("max_tokens", 0) or 0):
        return STOP_BUDGET_TOKENS
    limit_cost = int(budget.get("max_cost_micro", 0) or 0)
    if limit_cost and int(state.get("cost_micro", 0)) >= limit_cost:
        return STOP_BUDGET_COST
    if int(state.get("tool_calls", 0)) >= int(budget.get("max_tool_calls", 0) or 0):
        return STOP_BUDGET_TOOLS
    limit_stall = int(budget.get("max_stall", 0) or 0)
    if limit_stall and int(state.get("stall_streak", 0)) >= limit_stall:
        return STOP_STALLED
    return ""


def wants_tools(state: ChatState) -> bool:
    """最后一条模型回复里有没有工具调用。"""
    messages = state.get("messages") or []
    if not messages:
        return False
    return bool(getattr(messages[-1], "tool_calls", None))


def route_after_agent(state: ChatState) -> str:
    """模型回复之后：硬闸 → 工具（模型要求且还有额度）→ 没检索就提醒 → 结束。

    "提醒"这一步是**结构性要求**而不是提示词：这一轮回答若没有任何工具调用，
    就没有任何知识库依据，允许它直接结束等于让模型用记忆编造会议内容。
    """
    reason = gate_reason(state)
    if reason:
        return "stop"
    if wants_tools(state):
        return "tools"
    if not state.get("nudged") and int(state.get("tool_calls", 0)) == 0:
        return "nudge"
    return "finished"


def route_after_tools(state: ChatState) -> str:
    """工具执行之后：还有额度就回到模型，否则结束。"""
    return "stop" if gate_reason(state) else "again"


def stop_node(state: ChatState) -> ChatState:
    """把停止原因写进状态。路由只能读状态，写要靠节点——这是同一个动作的两个入口。

    注意**不是所有停在 finalize 的情况都是硬闸**：模型自己不再调工具时也走这里，
    那种情况不带停止原因（finalize 会补 `finished`）。
    """
    reason = gate_reason(state)
    return {"stop_reason": reason} if reason else {}


def recursion_limit(budget: dict[str, int]) -> int:
    """LangGraph 的步数上限。每轮最多「模型 + 工具」两个节点，外加收尾与余量。"""
    return max(2, int(budget.get("max_rounds", 1)) * 2 + 3)


def build_chat_graph():
    """组装状态图。流程写在边上、业务写在注入的动作里。"""
    graph = StateGraph(ChatState)
    graph.add_node("agent", agent_node)
    graph.add_node("tools", tools_node)
    graph.add_node("nudge", nudge_node)
    graph.add_node("stop", stop_node)
    graph.add_node("finalize", finalize_node)

    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", route_after_agent,
                               {"tools": "tools", "stop": "stop",
                                "nudge": "nudge", "finished": "finalize"})
    graph.add_conditional_edges("tools", route_after_tools,
                                {"again": "agent", "stop": "stop"})
    # 提醒之后回到模型：这一轮要么调工具，要么给出极短回复（寒暄）
    graph.add_edge("nudge", "agent")
    graph.add_edge("stop", "finalize")
    graph.add_edge("finalize", END)
    return graph.compile()


# --------------------------------------------------------------------------- #
# 读结果：纯函数，从消息流里取出答案、工具轨迹与用到的引用
# --------------------------------------------------------------------------- #


def final_answer(state: ChatState) -> str:
    """最后一条**有内容**的模型回复。停在硬闸上时，最后一条消息可能是工具结果，
    所以要往前找——找不到就返回空串，由调用方如实告诉用户"没有得到回答"，
    而不是把工具结果的 JSON 当答案吐出来。"""
    for message in reversed(state.get("messages") or []):
        if getattr(message, "type", "") != "ai":
            continue
        text = _text_of(message)
        if text:
            return text
    return ""


def _text_of(message: Any) -> str:
    content = getattr(message, "content", "")
    if isinstance(content, list):
        return "".join(str(part.get("text") or "") for part in content
                       if isinstance(part, dict))
    return str(content or "").strip()


# --------------------------------------------------------------------------- #
# 结局可读：停了以后要说清"卡在哪"
# --------------------------------------------------------------------------- #


def last_tool_failure(messages: list[Any]) -> dict[str, str] | None:
    """最后一次失败的工具调用（名字 + 错误原文）。没失败过就返回 None。

    工具结果本来就是 JSON（`{"ok": false, "error": ...}`），所以"卡在哪"不用另记一份——
    从消息流里读回来即可。另记一份的代价是两处真相会漂移。
    """
    for message in reversed(messages or []):
        if getattr(message, "type", "") != "tool":
            continue
        try:
            result = json.loads(_text_of(message))
        except (TypeError, ValueError):
            continue
        if isinstance(result, dict) and result.get("ok") is False:
            name = str(getattr(message, "name", "") or "").strip() or "未知工具"
            reason = str(result.get("error") or "").strip()[:200]
            return {"name": name, "error": reason or "没有给出原因"}
    return None


def stop_detail(state: ChatState) -> str:
    """这次为什么停在这儿：一句话说清，能落在哪条证据上就落在哪条上。

    实测背景（q26/q27 两轮）：停滞闸掐断后答案为空，页面上只有一句
    "检测到工具调用在原地重复，已停止继续检索"，**用户看不到工具其实一直卡在参数上**
    （模型把 meeting_ids 写成了字符串）。停是对的，说不清"卡在哪"才是缺陷。
    """
    reason = str(state.get("stop_reason") or "")
    streak = int(state.get("stall_streak", 0) or 0)
    if reason == STOP_STALLED:
        failure = last_tool_failure(state.get("messages") or [])
        if failure:
            return (f"工具「{failure['name']}」连续 {streak} 次以同一组参数调用都失败，"
                    f"最后一次返回：{failure['error']}")
        return f"工具连续 {streak} 次以同一组参数调用，没有取到新的信息"
    if reason == STOP_MAX_ROUNDS:
        return f"已用满 {int(state.get('round_no', 0) or 0)} 轮推理"
    if reason == STOP_BUDGET_TOKENS:
        return f"本轮已用 {int(state.get('tokens_used', 0) or 0)} token"
    if reason == STOP_BUDGET_COST:
        return "本次问答达到成本上限"
    if reason == STOP_BUDGET_TOOLS:
        return f"本轮已调用 {int(state.get('tool_calls', 0) or 0)} 次工具"
    if reason == STOP_NO_RETRIEVAL:
        return "本轮没有调用任何工具"
    return ""


def stop_reason_text(stop_reason: str, detail: str = "") -> str:
    """给用户看的一句话：停止原因 +（有的话）具体卡在哪。

    两个读者共用一句：`error_message` 列（页面的红色告警）与 `done` 事件的 notice。
    分成两句写就会出现"页面上说了原因、库里没说"的不一致。
    """
    notice = STOP_NOTICE.get(stop_reason, "")
    if detail and notice:
        return f"{notice}原因：{detail}。"
    return notice or detail


# 工具结果里能数出来的"这条结果给了多少东西"，用来说清一次工具调用干了什么
_COUNT_KEYS = {
    "search_knowledge": ("hits", "条切片"),
    "list_facts": ("facts", "条事实"),
    "get_transcript_excerpt": ("segments", "句原文"),
}


def summarize_tool_result(name: str, result_text: str) -> str:
    """一句话说明这次工具调用拿到了什么。失败时如实说失败。"""
    try:
        result = json.loads(result_text)
    except (TypeError, ValueError):
        return "结果无法解析"
    if isinstance(result, dict) and result.get("ok") is False:
        return f"失败：{str(result.get('error') or '')[:60]}"
    key, unit = _COUNT_KEYS.get(name, ("", ""))
    summary = "完成"
    if key and isinstance(result, dict):
        items = result.get(key)
        if isinstance(items, list):
            summary = f"{len(items)} {unit}"
    elif name == "get_minutes" and isinstance(result, dict):
        summary = "六字段纪要"
    # 条件补搜的条目不在主结果数组里；不把这一笔写进摘要，页面会以为工具只做了
    # 主检索（q91 的补搜正是回答依据）。
    supplemental = result.get("coverage_hits") if isinstance(result, dict) else None
    if isinstance(supplemental, list) and supplemental:
        summary += f" + 补搜 {len(supplemental)} 条"
    return summary


def tool_trace(messages: list[Any]) -> list[dict[str, Any]]:
    """工具调用轨迹：给页面折叠显示"这次回答查了什么"。"""
    trace: list[dict[str, Any]] = []
    for message in messages or []:
        for call in getattr(message, "tool_calls", None) or []:
            if not isinstance(call, dict):
                continue
            trace.append({
                "call_id": str(call.get("id") or ""),
                "name": str(call.get("name") or ""),
                "arguments": call.get("args") or {},
                "summary": "",
                "ok": None,
            })
        if getattr(message, "type", "") != "tool":
            continue
        name = str(getattr(message, "name", "") or "")
        text = _text_of(message)
        entry = next((item for item in reversed(trace)
                      if item["call_id"] and item["call_id"] == str(getattr(message, "tool_call_id", "") or "")), None)
        if entry is None:
            entry = {"call_id": str(getattr(message, "tool_call_id", "") or ""),
                     "name": name, "arguments": {}, "summary": "", "ok": None}
            trace.append(entry)
        entry["name"] = name or entry["name"]
        entry["summary"] = summarize_tool_result(name, text)
        try:
            entry["ok"] = bool(json.loads(text).get("ok"))
        except (TypeError, ValueError):
            entry["ok"] = None
    return trace


__all__ = [
    "ChatState", "build_chat_graph", "budget_from_env", "recursion_limit",
    "final_answer", "tool_trace", "summarize_tool_result", "gate_reason",
    "last_tool_failure", "stop_detail", "stop_reason_text",
    "STOP_NOTICE", "STOP_FINISHED", "STOP_STALLED", "STOP_MAX_ROUNDS", "STOP_NO_RETRIEVAL",
    "STOP_BUDGET_TOKENS", "STOP_BUDGET_COST", "STOP_BUDGET_TOOLS",
]
