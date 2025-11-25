"""意图路由：用强规则识别 5 类会议问答意图，并给出工具策略。

默认不接入；`INTENT_ROUTER_ENABLED=0`。规则只做高置信路由，低置信不硬判。
"""
from __future__ import annotations

import os
import re

INTENTS = ("single_fact", "aggregation", "cross_meeting", "state", "smalltalk")

_SMALL = re.compile(r"(你好|您好|嗨|hi|hello|天气|谢谢|再见|上午好|下午好|晚上好|大家好)")
_CROSS = re.compile(
    r"(两段|两场|两条|两个录音|分别.*(录音|会议|上午|下午|由谁|什么时候|怎么说|是什么)"
    r"|上午.*下午|下午.*上午|跨会议|不同录音)")
_STATE_STRONG = re.compile(r"(行动项|待办|项目.*状态|复述.*结论|最后.*决定|最终决定|决定.*怎么|怎么处理|现在.*状态|什么状态)")
_AGG = re.compile(
    r"(哪几|哪些|清单|全部|所有|汇总|多少项|几组|几项|哪[一二三四五六七八九十两\d]+[组项类个]"
    r"|[两二三1-9]+个问题|分几步|成功率.*平均|平均.*成功率)")
_STATE_WEAK = re.compile(r"(负责人|谁负责|截止|偏好|协作人|已确认.*决策|未确认.*候选)")

POLICY = {
    "smalltalk": {"first_tool": None, "max_tool_calls": 0,
                  "hint": "这是寒暄/常识问题，不要调用任何工具。"},
    "cross_meeting": {"first_tool": "search_knowledge", "max_tool_calls": 4,
                      "hint": "这是跨录音/跨会议比较：先调用 search_knowledge，"
                              "分别检索上午/下午或两段录音，不要先调 list_facts。"},
    "aggregation": {"first_tool": "list_facts", "max_tool_calls": 3,
                    "hint": "这是清单/汇总问题：先调用 list_facts 拉结构化事实，"
                            "缺槽位再用 search_knowledge。"},
    "state": {"first_tool": "query_meeting_state", "max_tool_calls": 3,
              "hint": "这是状态/待办/负责人问题：先调用 query_meeting_state，"
                      "不要先用 list_facts 代替。"},
    "single_fact": {"first_tool": "search_knowledge", "max_tool_calls": 4,
                    "hint": "这是单事实问题：先调用 search_knowledge 检索相关片段。"},
}


def enabled() -> bool:
    return os.getenv("INTENT_ROUTER_ENABLED", "0").strip().lower() not in {"0", "false", "no", "off"}


def selective() -> bool:
    """只对 cross_meeting/state 注入路由，避免影响 single/aggregation 的答案忠实度。"""
    return os.getenv("INTENT_ROUTER_SELECTIVE", "1").strip().lower() not in {"0", "false", "no", "off"}


def classify(question: str) -> str:
    q = (question or "").strip()
    if not q:
        return "single_fact"
    if _SMALL.search(q.lower()):
        return "smalltalk"
    if _CROSS.search(q):
        return "cross_meeting"
    if _STATE_STRONG.search(q):
        return "state"
    if _AGG.search(q):
        return "aggregation"
    if _STATE_WEAK.search(q):
        return "state"
    return "single_fact"


def policy_for(question: str) -> dict:
    intent = classify(question)
    return {"intent": intent, **POLICY[intent]}


__all__ = ["INTENTS", "POLICY", "classify", "enabled", "selective", "policy_for"]
