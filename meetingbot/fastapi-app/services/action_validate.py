"""行动项槽位校验（确定性，零模型调用）。

从纪要条目（pending_items / decisions）抽出 owner/deadline/evidence 后，用**转写原文**
校验：负责人是否真的在原文出现、期限是否真的是时间表达、证据引句是否可定位。
不通过时降级（丢掉该槽位/证据），不伪造、不静默保留。

口径说明（与轴4历史指标对齐）：
- owner fabricated = 条目声称的负责人文本（拆分“与/、/和/及”后）不能全部在转写里找到；
- deadline field_misuse = 期限槽位里有内容但不是时间表达（把行动写进时间槽）；
- evidence unlocated = 引句与转写的最长公共子串 < 10 字。
"""
from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

from common import textsim, timex

MISSING_TEXTS = {"", "未提及", "无", "待定", "暂无", "未明确", "not_mentioned", "missing"}
_COLLECTIVE = ("全体", "参会人员", "与会人员", "大家", "所有人", "各位")
_UNRESOLVED = ("未明确", "待确认", "未确定", "不明确", "待定", "未形成")
_PAREN = re.compile(r"（[^）]*）|\([^)]*\)")
_SPLIT = re.compile(r"[与和及、/；;，,]")
_PUNCT = re.compile(r"[\s（）()【】\[\]・·:：\"'“”‘’]")
_ROLE_WORDS = ("负责人", "牵头人", "协作人", "与会人", "参与人", "团队", "部门", "同事",
               "主任", "专家", "及其他", "及其他")
_ORG_SUFFIX = ("机构", "公司", "集团", "研究院", "基金会", "大学", "中心", "区政府", "区")
# 时间表达拆成可核对的组件：今年8月份 → ["今年", "8月份"]
_TIME_PART = re.compile(
    r"今年|明年|去年|前年|本周|下周|上周|本月|下月|上月|月底|月初|季度末|年底|年前|"
    r"\d+年\d+月(?:\d+日)?|\d+月\d+日|\d+月份|\d+月|\d+日|\d+点|\d+:\d+|"
    r"工作日|下班前|上线前|发布前|会议前|节前|之前|以后|之内")
MIN_QUOTE_OVERLAP = 10


def _norm(value: Any) -> str:
    return _PUNCT.sub("", str(value or "")).strip()


def _names(text: str) -> list[str]:
    parts = [_norm(part) for part in _SPLIT.split(_norm(text))]
    cleaned = []
    for part in parts:
        for word in _ROLE_WORDS:
            part = part.replace(word, "")
        if len(part) >= 2:
            cleaned.append(part)
    return cleaned


def _name_found(name: str, haystack: str) -> bool:
    if name in haystack:
        return True
    # 机构名容错：去掉“机构/公司/集团/研究院/…”后缀再找（如“知根机构”→“知根”）
    for suffix in _ORG_SUFFIX:
        if name.endswith(suffix) and len(name) > len(suffix) + 1:
            core = name[:-len(suffix)]
            if core in haystack:
                return True
    return False


def owner_status(owner_text: Any, transcript: str) -> str:
    """missing / located / fabricated。多个负责人要求全部出现在转写里。

    集体/角色表述（全体参会人员、大家）不算“具体负责人”，记 missing，不污染 fabricated。
    """
    raw = str(owner_text or "").strip()
    if _norm(raw).lower() in {_norm(item).lower() for item in MISSING_TEXTS}:
        return "missing"
    if any(cue in raw for cue in _UNRESOLVED):
        return "missing"
    cleaned = _PAREN.sub("", raw).strip()
    if any(cue in cleaned for cue in _COLLECTIVE):
        return "missing"
    raw = cleaned
    names = _names(raw)
    if not names:
        return "missing"
    haystack = _norm(transcript)
    return "located" if all(_name_found(name, haystack) for name in names) else "fabricated"


def deadline_status(deadline_text: Any, transcript: str) -> str:
    """missing / misuse / located / unlocated。

    - 没有内容 → missing；
    - 有内容但不是时间表达 → misuse（历史 field_misuse）；
    - 是时间表达且在转写里出现 → located；否则 unlocated（可降级）。
    """
    text = str(deadline_text or "").strip()
    if text.lower() in {item.lower() for item in MISSING_TEXTS}:
        return "missing"
    if not timex.looks_like_a_time(text):
        return "misuse"
    haystack = _norm(transcript)
    parts = [_norm(part) for part in _TIME_PART.findall(text)]
    parts = [part for part in parts if part]
    if parts and all(part in haystack for part in parts):
        return "located"
    normalized = _norm(text)
    return "located" if normalized and normalized in haystack else "unlocated"


def evidence_status(quotes: Sequence[Any], transcript: str) -> str:
    """missing / located / unlocated（引句与转写最长公共子串 ≥10 字视为可定位）。"""
    texts = []
    for quote in quotes or []:
        if isinstance(quote, Mapping):
            texts.append(str(quote.get("quote") or ""))
        else:
            texts.append(str(quote or ""))
    texts = [text for text in texts if text.strip()]
    if not texts:
        return "missing"
    for text in texts:
        _, overlap = textsim.similarity(text, transcript)
        if overlap >= MIN_QUOTE_OVERLAP:
            return "located"
    return "unlocated"


def validate_item(item: Mapping[str, Any], transcript: str) -> dict[str, Any]:
    """返回校验结果；`drop_owner`/`drop_deadline` 表示该槽位应降级为空。"""
    owner_text = ""
    owner = item.get("owner")
    if isinstance(owner, Mapping):
        owner_text = str(owner.get("text") or owner.get("normalized") or "")
    else:
        owner_text = str(item.get("owner_name") or item.get("owner_suggestion") or "")
    deadline_text = ""
    deadline = item.get("deadline")
    if isinstance(deadline, Mapping):
        deadline_text = str(deadline.get("text") or deadline.get("normalized") or "")
    else:
        deadline_text = str(item.get("deadline_text") or item.get("deadline_suggestion") or "")
    quotes = item.get("evidence") or []
    owner = owner_status(owner_text, transcript)
    deadline = deadline_status(deadline_text, transcript)
    evidence = evidence_status(quotes, transcript)
    return {
        "owner_text": owner_text,
        "owner_status": owner,
        "deadline_text": deadline_text,
        "deadline_status": deadline,
        "evidence_status": evidence,
        "drop_owner": owner == "fabricated",
        "drop_deadline": deadline in {"misuse", "unlocated"},
        "drop_evidence": evidence == "unlocated",
    }
