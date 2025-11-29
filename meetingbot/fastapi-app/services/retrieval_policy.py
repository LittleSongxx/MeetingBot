"""检索深度策略：多时间点/时间线问题用更宽的 rerank 截断口（通用机制，非提示词）。

关闭时行为与旧链路完全一致；开启时只对满足条件的这一轮把 top_k 从 3 提到 5。
"""
from __future__ import annotations

import os
import re

ENV = "CHAT_ADAPTIVE_DEPTH_ENABLED"
DEPTH = 5

# 时间表达（通用类别，不针对具体会议）：显式日期/相对日期/时刻/周期
_DATE = re.compile(
    r"\d{4}\s*年|\d{1,2}\s*月\s*\d{1,2}\s*日|\d{1,2}\s*月|"
    r"\d{1,2}\s*[：:]\s*\d{2}"
)
_RELATIVE = re.compile(
    r"今天|明天|昨天|前天|后天|本周|下周|上周|本月|下月|上月|"
    r"今年|明年|去年|本季度|下季度|上季度|季度末|月底|月初|年底|下周[一二三四五六日天]"
)
# 问“时间线/时间节点”类问题的通用线索
_TIMELINE_CUES = ("时间节点", "时间线", "关键时间", "先后", "顺序", "哪些时间",
                  "几个阶段", "里程碑")


def enabled() -> bool:
    value = (os.getenv(ENV) or "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def time_tokens(question: str) -> list[str]:
    """问题里出现的时间表达（去重保序）；用于判断“是否多时间点”。"""
    found: list[str] = []
    seen: set[str] = set()
    for pattern in (_DATE, _RELATIVE):
        for match in pattern.finditer(question or ""):
            surface = match.group(0)
            if surface not in seen:
                seen.add(surface)
                found.append(surface)
    return found


def adaptive_depth(question: str) -> int | None:
    """命中时返回本轮 top_k，否则 None（沿用环境配置）。"""
    if not enabled():
        return None
    tokens = time_tokens(question)
    if len(tokens) >= 2 or any(cue in (question or "") for cue in _TIMELINE_CUES):
        return DEPTH
    return None
