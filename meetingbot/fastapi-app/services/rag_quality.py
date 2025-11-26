"""RAG 质量闸的确定性部分：检查答案里的数字/日期是否被**所引用的上下文**支持。

只做可判定检查、只标注不拦截；真正的语义 faithful 仍归 judge/盲评。
"""
from __future__ import annotations

import os
import re
from typing import Any, Iterable, Mapping

CITATION = re.compile(r"\[(\d{1,3})\]")
NUMBER = re.compile(r"\d+年\d+月(?:\d+日)?|\d+月\d+日|\d+(?:\.\d+)?(?:[:：/-]\d+)*(?:%|％)?")
SENTENCE = re.compile(r"[。！？\n]+")
_LOCATOR = re.compile(r"\d{1,6}s|第?\d{1,6}秒")


def enabled() -> bool:
    return os.getenv("RAG_QUALITY_GUARD", "1").strip().lower() not in {"0", "false", "no", "off"}


def _compact(text: str) -> str:
    return re.sub(r"[\s，,、。．:：/／\-—]", "", text or "")


def audit_answer(answer: str, citations: Iterable[Mapping[str, Any]]) -> list[dict[str, str]]:
    by_ref = {int(item.get("ref")): item for item in citations or []
              if item.get("ref") is not None}
    flags: list[dict[str, str]] = []
    for sentence in SENTENCE.split(answer or ""):
        sentence = sentence.strip()
        if not sentence:
            continue
        refs = [int(match.group(1)) for match in CITATION.finditer(sentence)]
        tokens = [token for token in dict.fromkeys(NUMBER.findall(_LOCATOR.sub("", sentence)))
                  if not (token.isdigit() and len(token) <= 2)]
        if not tokens:
            continue
        if not refs:
            flags.append({"kind": "uncited_number", "text": tokens[0],
                          "reason": "含数字/日期的断言没有引用"})
            continue
        source_parts = []
        missing_refs = []
        for ref in refs:
            item = by_ref.get(ref)
            if item is None:
                missing_refs.append(ref)
                continue
            for key in ("source_text", "quote", "text"):
                value = item.get(key)
                if isinstance(value, str) and value:
                    source_parts.append(value)
        if missing_refs:
            flags.append({"kind": "missing_citation", "text": ",".join(map(str, missing_refs)),
                          "reason": "引用了本轮未登记的编号"})
        haystack = _compact("\n".join(source_parts))
        for token in tokens:
            if _compact(token) not in haystack:
                flags.append({"kind": "number_not_in_cited", "text": token,
                              "reason": "数字/日期不在被引用上下文里"})
        if len(flags) >= 8:
            break
    return flags


def note(flags: Iterable[Mapping[str, str]]) -> str:
    rows = list(flags or [])
    if not rows:
        return ""
    return "RAG质量提示：" + "；".join(
        f"{row.get('kind')} {row.get('text')}" for row in rows[:4])


__all__ = ["audit_answer", "enabled", "note"]
