"""寒暄/闲聊判据的零调用单测：高置信命中、会议关键词不误伤。"""

from __future__ import annotations

import unittest

from services.smalltalk import is_smalltalk


class SmalltalkTest(unittest.TestCase):
    def test_greeting_and_weather(self):
        for text in ("你好", "您好！", "hello", "谢谢", "今天天气怎么样", "你能帮我做什么"):
            self.assertTrue(is_smalltalk(text), text)

    def test_meeting_question_is_not_smalltalk(self):
        for text in ("你好，帮我查一下上周发布决策", "会议纪要说了什么",
                     "今天有几场会", "谢谢，帮我看看待办"):
            self.assertFalse(is_smalltalk(text), text)

    def test_empty(self):
        self.assertFalse(is_smalltalk(""))
