"""寒暄/闲聊识别：只用于**关掉工具**，不用于跳过大模型回答。

为什么需要确定性判据：系统提示已经写了"寒暄不要调用工具"，但实测 27 题里
smalltalk 2 题仍各自调了一次工具。规则只覆盖高置信形态（整句寒暄/天气），
并排除会议关键词，避免把"你好，帮我查一下上周发布会"误判为闲聊。
"""
from __future__ import annotations

import re

_PATTERN = re.compile(
    r"^(你好|您好|嗨|hi|hello|在吗|谢谢|多谢|早上好|中午好|下午好|晚上好)[！!，,。.？?\s]*$"
    r"|今天天气|天气怎么样|今天几号|讲个笑话|你叫什么|你能帮我做什么",
    re.I,
)
_MEETING_HINTS = ("会", "纪要", "待办", "任务", "发布", "项目", "决策", "转写",
                  "知识库", "负责人", "截止", "议程")


def is_smalltalk(question: str) -> bool:
    """高置信闲聊：命中寒暄/天气形态，且不含会议业务关键词。"""
    text = (question or "").strip()
    if not text or not _PATTERN.search(text):
        return False
    return not any(hint in text for hint in _MEETING_HINTS)


__all__ = ["is_smalltalk"]
