"""生成后引用绑定（citation binding）：把答案里的数字/日期重绑到真正包含它的上下文。

**纯函数、零模型调用。** 产品在最终答案落库前调用；评测侧用同一份函数离线回放
（`evaluation/analysis/evidence_bind_replay.py`），保证"回放口径 = 运行时口径"。

口径与 `evaluation/analysis/claim_citation_audit.py` 对齐：
- 只看**带引用**的句子（`[n]`）；
- 数字/日期 token 用同一个正则；字面匹配（不做语义归一）；
- token 在被引上下文里 → 视为已支撑，不动；
- token 不在被引上下文、但在其它检索上下文里 → 把该 ref 追加进这句话的引用；
- token 哪里都没有 → **v1 不改写**（不删内容），记为 unbound（交给人工/后续策略）。

这是"生成后校验"的一环：模型可以引用错，但落库前必须由确定性代码把证据对齐。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

# 与 claim_citation_audit.NUMBER 完全一致：数字/日期/百分比（含 9:00、2026-10-01 等）
NUMBER = re.compile(r"\d+年\d+月(?:\d+日)?|\d+月\d+日|\d+(?:\.\d+)?(?:[:：/-]\d+)*(?:%|％)?")
CITATION = re.compile(r"\[(\d{1,3})\]")
# 句子切分：保留分隔符，便于把引用插在标点前
_SENTENCE = re.compile(r"[^。！？\n]+[。！？\n]?")
_TEXT_KEYS = ("source_text", "quote", "text")


@dataclass
class BindResult:
    answer: str
    original: str = ""
    checked_numbers: int = 0
    already_supported: int = 0
    rebound: int = 0
    unbound: int = 0
    uncited_numbers: int = 0
    rebinds: list[dict[str, Any]] = field(default_factory=list)
    unbound_items: list[dict[str, Any]] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return self.answer != self.original

    def as_dict(self) -> dict[str, Any]:
        return {
            "checked_numbers": self.checked_numbers,
            "already_supported": self.already_supported,
            "rebound": self.rebound,
            "unbound": self.unbound,
            "uncited_numbers": self.uncited_numbers,
            "rebinds": self.rebinds,
            "unbound_items": self.unbound_items,
        }


def _context_text(item: Mapping[str, Any]) -> str:
    parts = []
    for key in _TEXT_KEYS:
        value = item.get(key)
        if isinstance(value, str) and value:
            parts.append(value)
    return "\n".join(parts)


def _context_map(contexts: Sequence[Mapping[str, Any]]) -> dict[int, str]:
    mapping: dict[int, str] = {}
    for item in contexts or []:
        ref = item.get("ref")
        if ref is None:
            continue
        try:
            ref = int(ref)
        except (TypeError, ValueError):
            continue
        text = _context_text(item)
        if text:
            mapping[ref] = text
    return mapping


def _bigrams(text: str) -> set[str]:
    compact = re.sub(r"\s+", "", text or "")
    return {compact[i:i + 2] for i in range(len(compact) - 1)}


def _overlap(sentence: str, text: str) -> float:
    """句子与上下文的字符二元组重叠率；用于在多个候选 ref 里选最相关的一个。"""
    left = _bigrams(sentence)
    if not left:
        return 0.0
    return len(left & _bigrams(text)) / len(left)


def _insert_citations(sentence: str, refs: Sequence[int]) -> str:
    """把 [k] 插在句末标点前；不改动原句内容。"""
    if not refs:
        return sentence
    cite = "".join(f"[{ref}]" for ref in sorted(set(refs)))
    index = len(sentence)
    while index > 0 and sentence[index - 1] in " \t\r\n。！？":
        index -= 1
    # 若句末是标点，标点留在最后；否则直接追加
    return sentence[:index] + cite + sentence[index:]


def bind_answer(answer: str, contexts: Sequence[Mapping[str, Any]]) -> BindResult:
    """对答案做确定性引用绑定，返回新答案与统计。"""
    result = BindResult(answer=answer or "", original=answer or "")
    context_texts = _context_map(contexts)
    if not context_texts or not answer:
        return result
    pieces = []
    for match in _SENTENCE.finditer(answer):
        sentence = match.group(0)
        refs = [int(value) for value in CITATION.findall(sentence)]
        if not refs:
            # 无引用句不在审计口径里；只统计有多少数字没被检查（诊断用）
            result.uncited_numbers += len(NUMBER.findall(sentence))
            pieces.append(sentence)
            continue
        cited_texts = [context_texts[ref] for ref in refs if ref in context_texts]
        cited_haystack = "\n".join(cited_texts)
        add_refs: list[int] = []
        for token in dict.fromkeys(NUMBER.findall(sentence)):
            if token.isdigit() and len(token) <= 2:
                continue  # 与审计口径一致：1–2 位裸数字不算（9:00 这类 token 不是纯数字）
            result.checked_numbers += 1
            if token in cited_haystack:
                result.already_supported += 1
                continue
            candidates = [(ref, text) for ref, text in context_texts.items()
                          if ref not in refs and token in text]
            if not candidates:
                result.unbound += 1
                result.unbound_items.append({"token": token, "refs": refs,
                                             "sentence": sentence[:160]})
                continue
            # 选与句子最相关的上下文；同分取较小 ref，保证确定性
            candidates.sort(key=lambda item: (-_overlap(sentence, item[1]), item[0]))
            add_refs.append(candidates[0][0])
            result.rebound += 1
            result.rebinds.append({"token": token, "from_refs": refs,
                                   "to_ref": candidates[0][0],
                                   "sentence": sentence[:160]})
        pieces.append(_insert_citations(sentence, add_refs) if add_refs else sentence)
    result.answer = "".join(pieces)
    return result
