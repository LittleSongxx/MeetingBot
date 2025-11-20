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

from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

# 停止原因：值是**给页面看的**，不是内部的枚举缩写
STOP_FINISHED = "finished"
STOP_MAX_ROUNDS = "max_rounds"
STOP_BUDGET_TOKENS = "budget_tokens"
STOP_BUDGET_COST = "budget_cost"
STOP_BUDGET_TOOLS = "budget_tools"
STOP_STALLED = "stalled"

STOP_NOTICE = {
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


async def finalize_node(state: ChatState) -> ChatState:
    """结束节点：只补一个停止原因，不再发起任何调用。"""
    if state.get("stop_reason"):
        return {}
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
    """模型回复之后：硬闸 → 工具（模型要求且还有额度）→ 结束。"""
    reason = gate_reason(state)
    if reason:
        return "stop"
    return "tools" if wants_tools(state) else "finished"


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
    graph.add_node("stop", stop_node)
    graph.add_node("finalize", finalize_node)

    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", route_after_agent,
                               {"tools": "tools", "stop": "stop",
                                "finished": "finalize"})
    graph.add_conditional_edges("tools", route_after_tools,
                                {"again": "agent", "stop": "stop"})
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
    if key and isinstance(result, dict):
        items = result.get(key)
        if isinstance(items, list):
            return f"{len(items)} {unit}"
    if name == "get_minutes" and isinstance(result, dict):
        return "六字段纪要"
    return "完成"


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
    "STOP_NOTICE", "STOP_FINISHED", "STOP_STALLED", "STOP_MAX_ROUNDS",
    "STOP_BUDGET_TOKENS", "STOP_BUDGET_COST", "STOP_BUDGET_TOOLS",
]
