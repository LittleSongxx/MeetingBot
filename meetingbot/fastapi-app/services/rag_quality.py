"""RAG 质量闸的确定性部分：检查答案里的数字/日期与引语是否被**引用的上下文**支持。

只做可判定检查、只标注不拦截；真正的语义 faithful 仍归 judge/盲评。

2026-10-23 增强（零模型调用）：区分「引用归属错」与「无依据」。
- 数字/引语不在**被引**上下文、但在本轮**其它检索上下文**里出现 →
  `number_attribution_mismatch` / `quote_attribution_mismatch`，并给出 `suggested_refs`。
  这类问题本轮证据其实检索到了，只是没引对编号（40 条盲评里的主要剩余问题之一）。
- 哪里都找不到 → 原来的 `number_not_in_cited` / `quote_not_in_cited`（更可能是编造）。
区分两者的理由：把"证据在别的 ref 里"也报成编造会造成误报；实体核查同理，
**只标注不拦截**，真伪仍由 judge/人工裁决。
"""
from __future__ import annotations

import os
import re
from typing import Any, Iterable, Mapping

CITATION = re.compile(r"\[(\d{1,3})\]")
QUOTE = re.compile(r"[“‘\"]([^”’\"]{4,160})[”’\"]|「([^」]{4,160})」")
NUMBER = re.compile(r"\d+年\d+月(?:\d+日)?|\d+月\d+日|\d+(?:\.\d+)?(?:[:：/-]\d+)*(?:%|％)?")
SENTENCE = re.compile(r"[。！？\n]+")
_LOCATOR = re.compile(r"\d{1,6}s|第?\d{1,6}秒")
_MAX_FLAGS = 8
_MAX_SUGGESTED = 3


def enabled() -> bool:
    return os.getenv("RAG_QUALITY_GUARD", "1").strip().lower() not in {"0", "false", "no", "off"}


def _compact(text: str) -> str:
    return re.sub(r"[\s，,、。．:：/／\-—]", "", text or "")


def _source_text(item: Mapping[str, Any]) -> str:
    parts: list[str] = []
    for key in ("source_text", "quote", "text"):
        value = item.get(key)
        if isinstance(value, str) and value:
            parts.append(value)
    return "\n".join(parts)


def _by_ref(items: Iterable[Mapping[str, Any]]) -> dict[int, Mapping[str, Any]]:
    rows: dict[int, Mapping[str, Any]] = {}
    for item in items or []:
        ref = item.get("ref")
        if ref is None:
            continue
        try:
            rows[int(ref)] = item
        except (TypeError, ValueError):
            continue
    return rows


def _refs_containing(token: str, rows: Mapping[int, Mapping[str, Any]],
                     *, exclude: set[int] | None = None) -> list[int]:
    """在给定 ref 集合里找哪些上下文含该 token（归一后字面匹配），最多返回若干个。"""
    needle = _compact(token)
    if not needle:
        return []
    found: list[int] = []
    for ref in sorted(rows):
        if exclude and ref in exclude:
            continue
        if needle in _compact(_source_text(rows[ref])):
            found.append(ref)
            if len(found) >= _MAX_SUGGESTED:
                break
    return found


def _attribution_flags(answer: str, citations: Iterable[Mapping[str, Any]],
                       retrieved: Iterable[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    """核心审计：返回 flags。retrieved=None 时退化为旧行为（不区分归属错）。"""
    cited_by_ref = _by_ref(citations)
    all_by_ref = {**cited_by_ref, **_by_ref(retrieved or [])}
    classify = retrieved is not None
    flags: list[dict[str, Any]] = []
    for sentence in SENTENCE.split(answer or ""):
        sentence = sentence.strip()
        if not sentence:
            continue
        refs = [int(match.group(1)) for match in CITATION.finditer(sentence)]
        tokens = [token for token in dict.fromkeys(NUMBER.findall(_LOCATOR.sub("", sentence)))
                  if not (token.isdigit() and len(token) <= 2)]
        if not tokens and not QUOTE.search(sentence):
            continue
        if not refs:
            if tokens:
                if classify:
                    suggested = _refs_containing(tokens[0], all_by_ref)
                    if suggested:
                        flags.append({"kind": "number_attribution_mismatch",
                                      "text": tokens[0],
                                      "suggested_refs": suggested,
                                      "reason": "含数字/日期的断言没有引用，但证据在本轮其它上下文里"})
                        if len(flags) >= _MAX_FLAGS:
                            break
                        continue
                flags.append({"kind": "uncited_number", "text": tokens[0],
                              "reason": "含数字/日期的断言没有引用"})
            continue
        source_parts = []
        missing_refs = []
        for ref in refs:
            item = cited_by_ref.get(ref)
            if item is None:
                missing_refs.append(ref)
                continue
            source_parts.append(_source_text(item))
        if missing_refs:
            flags.append({"kind": "missing_citation", "text": ",".join(map(str, missing_refs)),
                          "reason": "引用了本轮未登记的编号"})
        cited_hay = _compact("\n".join(source_parts))
        for match in QUOTE.finditer(sentence):
            quote = (match.group(1) or match.group(2) or "").strip()
            if not quote or _compact(quote) in cited_hay:
                continue
            if classify:
                suggested = _refs_containing(quote, all_by_ref, exclude=set(refs))
                if suggested:
                    flags.append({"kind": "quote_attribution_mismatch", "text": quote[:40],
                                  "suggested_refs": suggested,
                                  "reason": "引语不在被引上下文里，但在本轮其它上下文里"})
                    if len(flags) >= _MAX_FLAGS:
                        break
                    continue
            flags.append({"kind": "quote_not_in_cited", "text": quote[:40],
                          "reason": "引语不在被引用上下文里"})
        for token in tokens:
            if _compact(token) in cited_hay:
                continue
            if classify:
                suggested = _refs_containing(token, all_by_ref, exclude=set(refs))
                if suggested:
                    flags.append({"kind": "number_attribution_mismatch", "text": token,
                                  "suggested_refs": suggested,
                                  "reason": "数字/日期不在被引上下文里，但在本轮其它上下文里"})
                    if len(flags) >= _MAX_FLAGS:
                        break
                    continue
            flags.append({"kind": "number_not_in_cited", "text": token,
                          "reason": "数字/日期不在被引用上下文里"})
        if len(flags) >= _MAX_FLAGS:
            break
    return flags


def audit_answer(answer: str, citations: Iterable[Mapping[str, Any]],
                 retrieved: Iterable[Mapping[str, Any]] | None = None) -> list[dict[str, Any]]:
    """检查答案里的数字/日期/引语是否被引用上下文支持。

    `citations` 是答案**实际引用**的条目（每个至少要能提供 source_text/quote/text 之一）；
    `retrieved` 是本轮**检索到的全部**条目（含未被引用的）。传了它才能区分
    「归属错（证据在别的 ref 里）」与「无依据（哪里都没有）」；不传则保持旧行为。
    """
    return _attribution_flags(answer, citations, retrieved)


def note(flags: Iterable[Mapping[str, Any]]) -> str:
    rows = list(flags or [])
    if not rows:
        return ""
    return "RAG质量提示：" + "；".join(
        f"{row.get('kind')} {row.get('text')}" for row in rows[:4])


__all__ = ["audit_answer", "enabled", "note"]
