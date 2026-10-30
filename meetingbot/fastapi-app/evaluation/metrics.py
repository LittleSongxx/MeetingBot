"""评测指标（纯函数，可在无数据库/无模型调用下单测）。"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

MINUTES_FIELDS = ("summary", "topics", "viewpoints", "decisions", "pending_items", "risks")
ARRAY_FIELDS = ("topics", "viewpoints", "decisions", "pending_items", "risks")
ITEM_TEXT_KEYS = ("content", "summary", "title", "viewpoint")

_PUNCT_RE = re.compile(r"[，。！？、；：,.!?;:\"'“”‘’()（）\[\]【】<>《》—\-…·\s]+")


def normalize_text(text: str | None) -> str:
    return _PUNCT_RE.sub("", str(text or "").lower())


def text_similarity(left: str | None, right: str | None) -> float:
    """两段文本的相似度（0~1），用于修改率和人工标注匹配。"""
    a, b = normalize_text(left), normalize_text(right)
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def _item_text(item: dict) -> str:
    for key in ITEM_TEXT_KEYS:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def field_coverage(minutes_payload: dict) -> dict:
    """六字段非空比例。"""
    filled = {}
    for field in MINUTES_FIELDS:
        value = minutes_payload.get(field)
        filled[field] = bool(value)
    total = len(filled)
    covered = sum(1 for value in filled.values() if value)
    return {"total": total, "covered": covered, "ratio": round(covered / total, 4), "fields": filled}


def evidence_stats(minutes_payload: dict, segments: dict[int, dict] | None = None) -> dict:
    """统计有证据的条目占比和引用合法率；segments 提供时校验逐字引用。"""
    total_items = 0
    with_evidence = 0
    total_evidence = 0
    valid_evidence = 0
    for field in ARRAY_FIELDS:
        for item in minutes_payload.get(field) or []:
            if not isinstance(item, dict):
                continue
            total_items += 1
            evidences = [entry for entry in item.get("evidence") or [] if isinstance(entry, dict)]
            if evidences:
                with_evidence += 1
            for entry in evidences:
                total_evidence += 1
                if segments is not None:
                    segment = segments.get(entry.get("segment_no"))
                    quote = normalize_text(entry.get("quote"))
                    if segment is not None and quote and quote in normalize_text(segment.get("content")):
                        valid_evidence += 1
                elif isinstance(entry.get("segment_no"), int) and normalize_text(entry.get("quote")):
                    valid_evidence += 1
    return {
        "total_items": total_items,
        "items_with_evidence": with_evidence,
        "item_coverage": round(with_evidence / total_items, 4) if total_items else 0.0,
        "total_evidence": total_evidence,
        "valid_evidence": valid_evidence,
        "valid_ratio": round(valid_evidence / total_evidence, 4) if total_evidence else 0.0,
    }


def _item_list(payload: dict, field: str) -> list[dict]:
    return [item for item in payload.get(field) or [] if isinstance(item, dict)]


def edit_stats(ai_draft: dict | None, final: dict, threshold: float = 0.8) -> dict:
    """比较 AI 草稿和人工终稿：摘要是否改、各数组条目改/增/删比例。"""
    if not ai_draft:
        return {"available": False}
    changed = added = deleted = unchanged = 0
    for field in ARRAY_FIELDS:
        old_items = _item_list(ai_draft, field)
        new_items = _item_list(final, field)
        for index, new_item in enumerate(new_items):
            if index >= len(old_items):
                added += 1
                continue
            if text_similarity(_item_text(old_items[index]), _item_text(new_item)) >= threshold:
                unchanged += 1
            else:
                changed += 1
        deleted += max(0, len(old_items) - len(new_items))
    total = changed + added + deleted + unchanged
    summary_changed = text_similarity(ai_draft.get("summary"), final.get("summary")) < threshold
    return {
        "available": True,
        "changed": changed,
        "added": added,
        "deleted": deleted,
        "unchanged": unchanged,
        "item_edit_rate": round((changed + added + deleted) / total, 4) if total else 0.0,
        "summary_changed": summary_changed,
    }


def action_match(expected: list[str], actual: list[str], threshold: float = 0.5) -> dict:
    """把人工标注的行动项与系统生成的行动项做贪心匹配，算召回/精确。"""
    expected_norm = [normalize_text(text) for text in expected if normalize_text(text)]
    actual_norm = [normalize_text(text) for text in actual if normalize_text(text)]
    matched_expected = set()
    matched_actual = set()
    for exp_index, exp_text in enumerate(expected_norm):
        best_index = None
        best_score = threshold
        for act_index, act_text in enumerate(actual_norm):
            if act_index in matched_actual:
                continue
            score = SequenceMatcher(None, exp_text, act_text).ratio()
            if score >= best_score:
                best_score = score
                best_index = act_index
        if best_index is not None:
            matched_expected.add(exp_index)
            matched_actual.add(best_index)
    recall = len(matched_expected) / len(expected_norm) if expected_norm else 0.0
    precision = len(matched_actual) / len(actual_norm) if actual_norm else 0.0
    return {
        "expected": len(expected_norm),
        "actual": len(actual_norm),
        "matched": len(matched_expected),
        "recall": round(recall, 4),
        "precision": round(precision, 4),
    }
