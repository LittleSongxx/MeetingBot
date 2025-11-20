"""内容审核：输入 DFA 扫描 + 生成约束 + 输出复检。

## 三层防御各管什么

1. **输入扫描**（`check_input`）：用户提问进入模型之前先扫一遍。命中即拒，
   绝不把违规内容送进模型——送进去就已经产生了一次可能是违规的生成。
2. **生成约束**（`prompt_constraints`）：把审核要求写进系统提示词。
   这一层是**概率性**的，只能降低概率，不能当保证；前两层合起来是"少说错"，
   真正的门在第三层。
3. **输出复检**（`check_output`）：模型吐完再扫一遍。**这一层才是门**——
   输入扫描挡不住"用户用无害的词问出违规答案"（检索到的会议原文本身就可能违规），
   所以输出必须独立检查一次，而不是假定输入干净输出就干净。

命中的处置默认是 `flag`：**留痕并照常返回**，同时在消息上记录命中了什么。
为什么不默认直接拦：本项目是内部自托管系统，词表由部署方提供，误伤一次正常提问
的代价（用户问不出问题）往往大于收益；而 `flag` 让命中可统计、可复核，能先量出
误报率再决定是否升级为 `redact`/`block`。三种处置都可通过环境变量切换，
命中一律写日志——**审核动作必须可追溯，包括"这次没拦"**。

## 词表不是内置的

代码里**没有**内置敏感词表，也不该有：合规口径取决于部署地与业务（内部自托管
与对外服务的差别是数量级的），内置一份词表只会给出虚假的安全感。词表由
`MODERATION_WORDS`（逗号分隔）或 `MODERATION_WORDS_FILE`（每行一个，
`#` 开头是注释）提供；两者都为空时审核器处于 **disabled** 状态，
`status()` 会如实报告它没在工作——而不是让上层以为"扫过了，没命中"。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

logger = logging.getLogger("meetingbot.moderation")

ACTION_FLAG = "flag"
ACTION_REDACT = "redact"
ACTION_BLOCK = "block"
ACTIONS = (ACTION_FLAG, ACTION_REDACT, ACTION_BLOCK)

# 命中的词在正文里替换成它，避免"记录命中"本身又转述了一遍违规内容
REDACTION = "***"


@dataclass(frozen=True)
class Hit:
    word: str
    start: int
    end: int


@dataclass(frozen=True)
class Verdict:
    """一次扫描的结果。`action` 是**建议动作**，由调用方按场景决定是否采纳。"""

    enabled: bool
    hits: list[Hit] = field(default_factory=list)
    action: str = ACTION_FLAG

    @property
    def matched(self) -> bool:
        return bool(self.hits)

    def words(self) -> list[str]:
        return [hit.word for hit in self.hits]

    def as_dict(self) -> dict:
        """留痕用。**只记词与位置，不记上下文**——留痕不该复制一份违规原文。"""
        return {
            "enabled": self.enabled,
            "action": self.action,
            "matched": self.matched,
            "words": self.words(),
            "positions": [[hit.start, hit.end] for hit in self.hits],
        }


def _env_words() -> list[str]:
    raw = os.getenv("MODERATION_WORDS", "")
    return [item.strip() for item in raw.split(",") if item.strip()]


def _file_words() -> list[str]:
    path = os.getenv("MODERATION_WORDS_FILE", "").strip()
    if not path:
        return []
    file_path = Path(path)
    if not file_path.is_file():
        # 配了路径却读不到：这必须显式可见，否则会表现为"审核在跑但其实没词表"
        logger.error("审核词表文件不存在：%s（本次审核按未启用处理）", path)
        return []
    words: list[str] = []
    for line in file_path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if text and not text.startswith("#"):
            words.append(text)
    return words


class _Dfa:
    """Aho–Corasick 那样的多模式匹配的简化版：字典树 + 失配回退（仅一层）。

    为什么用 DFA 而不是对每个词做一次 `in`：词表上千时，逐词 `str.find` 是
    O(词数 × 文本长)，而每次问答都要扫两遍（输入、输出）。字典树一遍过文本，
    代价是 O(文本长 × 词长)，与词表大小无关。
    """

    def __init__(self, words: Iterable[str]):
        self._root: dict = {}
        self._max_len = 0
        for word in words:
            if not word:
                continue
            self._max_len = max(self._max_len, len(word))
            node = self._root
            for char in word:
                node = node.setdefault(char, {})
            node["\x00"] = word  # 终止标记，值是原词

    def __bool__(self) -> bool:
        return bool(self._root)

    def scan(self, text: str) -> list[Hit]:
        hits: list[Hit] = []
        index = 0
        length = len(text)
        while index < length:
            node = self._root
            cursor = index
            # 从 index 出发尽可能往右走，走到头回头再找终止标记
            while cursor < length and text[cursor] in node:
                node = node[text[cursor]]
                cursor += 1
                word = node.get("\x00")
                if word:
                    hits.append(Hit(word=str(word), start=index, end=cursor))
            index += 1
        return hits


_snapshot: tuple[str, _Dfa] | None = None


def _current_words() -> list[str]:
    return _env_words() + _file_words()


def dfa() -> _Dfa:
    """字典树单例。词表变了（测试或重载）自动重建，避免读到旧表。"""
    global _snapshot
    words = _current_words()
    signature = "\x1f".join(sorted(words))
    if _snapshot is None or _snapshot[0] != signature:
        _snapshot = (signature, _Dfa(words))
        if words:
            logger.info("内容审核词表已加载：%d 条", len(words))
    return _snapshot[1]


def enabled() -> bool:
    return bool(dfa())


def status() -> dict:
    """给接口与观测页看的状态：审核到底在不在工作、词表多大。"""
    words = _current_words()
    return {
        "enabled": enabled(),
        "word_count": len(set(words)),
        "output_action": output_action(),
        "note": ("未配置审核词表（MODERATION_WORDS / MODERATION_WORDS_FILE），"
                 "当前不做内容审核" if not words else ""),
    }


def output_action() -> str:
    action = os.getenv("MODERATION_OUTPUT_ACTION", ACTION_FLAG).strip().lower()
    if action not in ACTIONS:
        raise ValueError(f"MODERATION_OUTPUT_ACTION 只能是 {ACTIONS}，当前是 {action!r}")
    return action


def scan(text: str) -> list[Hit]:
    return dfa().scan(text or "")


def check_input(text: str) -> Verdict:
    """输入扫描：命中即建议拒绝（违规内容不进模型）。"""
    hits = scan(text)
    if hits:
        logger.warning("内容审核命中（输入）：%s", [hit.word for hit in hits])
    return Verdict(enabled=enabled(), hits=hits, action=ACTION_BLOCK if hits else ACTION_FLAG)


def check_output(text: str) -> Verdict:
    """输出复检：动作按 `MODERATION_OUTPUT_ACTION`（默认 flag=留痕但照常返回）。"""
    hits = scan(text)
    if hits:
        logger.warning("内容审核命中（输出）：%s", [hit.word for hit in hits])
    return Verdict(enabled=enabled(), hits=hits,
                   action=output_action() if hits else ACTION_FLAG)


def redact(text: str, hits: list[Hit]) -> str:
    """把命中的位置替换成占位符（从后往前替换，避免位置漂移）。"""
    result = text or ""
    for hit in sorted(hits, key=lambda item: item.start, reverse=True):
        result = result[:hit.start] + REDACTION + result[hit.end:]
    return result


def apply_output(text: str, verdict: Verdict) -> tuple[str, bool]:
    """按复检结果决定返回什么。返回 `(文本, 是否被拦截)`。

    - flag：原样返回（留痕已由调用方记录）
    - redact：命中位置替换成占位符
    - block：不返回正文，改由调用方给出提示
    """
    if not verdict.matched:
        return text, False
    if verdict.action == ACTION_REDACT:
        return redact(text, verdict.hits), False
    if verdict.action == ACTION_BLOCK:
        return "", True
    return text, False


# 第二层：生成约束。写在这一处，聊天与将来任何走模型的传输共用同一句
PROMPT_CONSTRAINT = ("\n\n【内容要求】回答不得包含违法违规、涉政敏感、色情暴力或"
                     "侵犯他人隐私的内容；会议原文里若出现此类表述，只做必要的概括，"
                     "不要逐字复述。")

BLOCKED_MESSAGE = "提问包含不被允许的内容，已被内容审核拦截。"


def prompt_constraints() -> str:
    """启用审核时才把约束拼进系统提示词（未启用时不留一句空话）。"""
    return PROMPT_CONSTRAINT if enabled() else ""


__all__ = [
    "Hit", "Verdict", "ACTION_FLAG", "ACTION_REDACT", "ACTION_BLOCK",
    "scan", "check_input", "check_output", "apply_output", "redact",
    "enabled", "status", "output_action", "prompt_constraints", "BLOCKED_MESSAGE",
]
