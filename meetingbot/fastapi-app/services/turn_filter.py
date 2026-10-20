"""Turn 级上下文过滤：把一轮内所有工具结果合并后只过滤一次。

与 `kb_service` 的 per-search 过滤不同：这里看到的是本轮已经累积的完整证据，
不会因为某次子查询只看到一半候选而误删后续要用的上下文。
默认关闭；评估器由调用方注入（chat_turn 用现有 AGENT 模型），便于测试。
"""
from __future__ import annotations

import json
import os
from typing import Any, Awaitable, Callable

from services import citations

TURN_FILTER_CALL_TYPE = "KB_TURN_FILTER"
_ITEM_KEYS = {"hits": "hits", "facts": "facts", "items": "items"}


def enabled() -> bool:
    return os.getenv("KB_TURN_FILTER_ENABLED", "0").strip().lower() not in {"0", "false", "no", "off"}


def _text_of(message: Any) -> str:
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(str(part.get("text", "")) if isinstance(part, dict) else str(part)
                       for part in content)
    return str(content or "")


def collect_entries(messages: list[Any]) -> list[dict[str, Any]]:
    """收集工具结果里可过滤的条目；每条带 message_index/list_key/item_index/ref/text。"""
    entries = []
    for message_index, message in enumerate(messages):
        if getattr(message, "type", "") != "tool":
            continue
        try:
            data = json.loads(_text_of(message))
        except (TypeError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        for list_key in _ITEM_KEYS:
            items = data.get(list_key)
            if not isinstance(items, list):
                continue
            for item_index, item in enumerate(items):
                if not isinstance(item, dict) or not isinstance(item.get("ref"), int):
                    continue
                text = (item.get("quote") or item.get("text") or item.get("source_text")
                        or item.get("fact_text") or json.dumps(item, ensure_ascii=False))
                entries.append({"message_index": message_index, "list_key": list_key,
                                "item_index": item_index, "ref": int(item["ref"]),
                                "text": str(text)})
    return entries


def apply_keep(messages: list[Any], entries: list[dict[str, Any]],
               keep: set[int], registry: citations.CitationRegistry) -> list[Any]:
    """按全局序号 keep（1-based）重建工具消息；未保留的 ref 从登记表删除。"""
    keep_refs = {entry["ref"] for index, entry in enumerate(entries, 1) if index in keep}
    dropped_refs = {entry["ref"] for entry in entries} - keep_refs
    if not dropped_refs:
        return messages
    kept_keys = {(entry["message_index"], entry["list_key"], entry["item_index"])
                 for index, entry in enumerate(entries, 1) if index in keep}
    result = list(messages)
    touched = {entry["message_index"] for entry in entries if entry["ref"] in dropped_refs}
    for message_index in touched:
        message = messages[message_index]
        try:
            data = json.loads(_text_of(message))
        except (TypeError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        for list_key in _ITEM_KEYS:
            items = data.get(list_key)
            if not isinstance(items, list):
                continue
            data[list_key] = [item for item_index, item in enumerate(items)
                              if (message_index, list_key, item_index) in kept_keys]
        content = json.dumps(data, ensure_ascii=False)
        try:
            result[message_index] = message.model_copy(update={"content": content})
        except Exception:  # noqa: BLE001 - 非 Pydantic 消息对象时只保留原消息
            result[message_index] = message
    registry.prune(dropped_refs)
    return result


async def filter_messages(messages: list[Any], question: str,
                          evaluator: Callable[[str, list[str]], Awaitable[set[int]]],
                          registry: citations.CitationRegistry) -> list[Any]:
    entries = collect_entries(messages)
    if len(entries) <= 1:
        return messages
    keep = await evaluator(question, [entry["text"] for entry in entries])
    if not keep:
        keep = {1}
    return apply_keep(messages, entries, keep, registry)


__all__ = ["TURN_FILTER_CALL_TYPE", "enabled", "collect_entries", "apply_keep",
           "filter_messages"]
