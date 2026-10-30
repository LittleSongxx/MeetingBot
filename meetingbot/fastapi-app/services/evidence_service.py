"""纪要证据：把模型给出的逐字引用绑定到转写片段，得到可核对的 segment_no/时间/说话人。"""

from __future__ import annotations

import json
import logging
import re

from models import TranscriptSegment, User

logger = logging.getLogger(__name__)

# 证据引用最短长度（归一化后），太短（“是的”“好的”）没有核对价值
MIN_QUOTE_CHARS = 6
# 每条内容最多保留几条证据
MAX_EVIDENCE_PER_ITEM = 2
# 参与证据绑定的五个数组字段（summary 是纯字符串，v1 不绑定）
EVIDENCE_FIELDS = ("topics", "viewpoints", "decisions", "pending_items", "risks")

_SPACE_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[，。！？、；：,.!?;:\"'“”‘’()（）\[\]【】<>《》—\-…·]")


def normalize(text: str | None) -> str:
    """归一化文本：去掉标点/空白并转小写，用于逐字引用匹配。"""
    if not text:
        return ""
    text = str(text).lower()
    text = _SPACE_RE.sub("", text)
    return _PUNCT_RE.sub("", text)


def _speaker_name(segment: TranscriptSegment, user_names: dict[int, str]) -> str:
    if segment.speaker_user_id and segment.speaker_user_id in user_names:
        return user_names[segment.speaker_user_id]
    return segment.speaker_label or "未知说话人"


async def load_task_evidence_context(task_id: int) -> list[dict]:
    """一次读出一个转写任务的全部片段，附带说话人姓名与归一化文本。"""
    segments = await TranscriptSegment.filter(task_id=task_id).order_by("segment_no")
    if not segments:
        return []
    user_ids = {segment.speaker_user_id for segment in segments if segment.speaker_user_id}
    users = {user.id: user.name for user in await User.filter(id__in=user_ids)} if user_ids else {}
    return [
        {
            "segment_no": segment.segment_no,
            "start_ms": segment.start_ms,
            "end_ms": segment.end_ms,
            "speaker": _speaker_name(segment, users),
            "content": segment.content,
            "norm": normalize(segment.content),
        }
        for segment in segments
    ]


def _match_segment(quote: str, segments: list[dict]) -> dict | None:
    """把一条逐字 quote 匹配到转写片段；找不到返回 None。"""
    norm_quote = normalize(quote)
    if len(norm_quote) < MIN_QUOTE_CHARS:
        return None
    candidates = [segment for segment in segments if norm_quote in segment["norm"]]
    if not candidates:
        return None
    # 命中多处时取时间最早的那一段
    return min(candidates, key=lambda segment: segment["segment_no"])


def _canonical_evidence(quote: str, segments: list[dict]) -> dict | None:
    segment = _match_segment(quote, segments)
    if segment is None:
        return None
    return {
        "segment_no": segment["segment_no"],
        "start_ms": segment["start_ms"],
        "end_ms": segment["end_ms"],
        "speaker": segment["speaker"],
        "quote": str(quote).strip(),
    }


def _raw_quotes(item: dict) -> list[str]:
    evidence = item.get("evidence")
    if not isinstance(evidence, list):
        return []
    quotes = []
    for entry in evidence:
        if isinstance(entry, str):
            quotes.append(entry)
        elif isinstance(entry, dict):
            quote = entry.get("quote")
            if isinstance(quote, str):
                quotes.append(quote)
    return quotes


def _raw_entries(item: dict) -> list[dict]:
    """原样返回 evidence 条目（保留 segment_no/时间/说话人），用于页面编辑保存。"""
    evidence = item.get("evidence")
    if not isinstance(evidence, list):
        return []
    entries = []
    for entry in evidence:
        if isinstance(entry, str) and entry.strip():
            entries.append({"quote": entry.strip()})
        elif isinstance(entry, dict) and isinstance(entry.get("quote"), str):
            entries.append(dict(entry))
    return entries


def bind_evidence(result: dict, segments: list[dict]) -> dict:
    """校验模型返回的 evidence：只保留能在转写里逐字匹配上的引用。"""
    if not isinstance(result, dict) or not segments:
        return result
    for field in EVIDENCE_FIELDS:
        items = result.get(field)
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            canonical = []
            for quote in _raw_quotes(item):
                evidence = _canonical_evidence(quote, segments)
                if evidence is not None:
                    canonical.append(evidence)
                if len(canonical) >= MAX_EVIDENCE_PER_ITEM:
                    break
            item["evidence"] = canonical
    return result


def _item_fingerprint(item: dict) -> str:
    """条目内容指纹（排除 evidence 本身），用来判断改稿时是否原样保留。"""
    copy = {key: value for key, value in item.items() if key != "evidence"}
    return normalize(json.dumps(copy, ensure_ascii=False, sort_keys=True))


def merge_evidence(old_result: dict, new_result: dict, segments: list[dict] | None = None) -> dict:
    """把旧稿的 evidence 合并到新稿：未改动条目继承、新 evidence 校验、改动条目重匹配。

    segments 为空时不做逐字校验（用于页面编辑场景：前端回传的 evidence 已来自数据库）。
    """
    if not isinstance(old_result, dict) or not isinstance(new_result, dict):
        return new_result
    for field in EVIDENCE_FIELDS:
        old_items = old_result.get(field) or []
        new_items = new_result.get(field) or []
        if not isinstance(old_items, list) or not isinstance(new_items, list):
            continue
        for index, item in enumerate(new_items):
            if not isinstance(item, dict):
                continue
            canonical = []
            if segments:
                # 生成/自检场景：必须能在转写里逐字匹配，并回填时间/说话人
                for quote in _raw_quotes(item):
                    evidence = _canonical_evidence(quote, segments)
                    if evidence is not None:
                        canonical.append(evidence)
            else:
                # 页面编辑场景：保留前端回传的完整 evidence（quote + 时间/说话人/片段号）
                canonical = _raw_entries(item)
            if not canonical and index < len(old_items) and isinstance(old_items[index], dict):
                old_item = old_items[index]
                old_evidence = old_item.get("evidence") if isinstance(old_item.get("evidence"), list) else []
                # 内容没变（或没有内容可比较）时直接继承旧证据
                old_fingerprint = _item_fingerprint(old_item)
                new_fingerprint = _item_fingerprint(item)
                if old_fingerprint == new_fingerprint or not new_fingerprint:
                    canonical = [dict(entry) for entry in old_evidence if isinstance(entry, dict)]
            item["evidence"] = canonical[:MAX_EVIDENCE_PER_ITEM]
    return new_result


def collect_evidence_segment_nos(result: dict) -> set[int]:
    """收集纪要里所有 evidence 的 segment_no，供问答时优先纳入上下文。"""
    numbers: set[int] = set()
    if not isinstance(result, dict):
        return numbers
    for field in EVIDENCE_FIELDS:
        for item in result.get(field) or []:
            if not isinstance(item, dict):
                continue
            for evidence in item.get("evidence") or []:
                if isinstance(evidence, dict) and isinstance(evidence.get("segment_no"), int):
                    numbers.add(evidence["segment_no"])
    return numbers
