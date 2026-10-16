"""给**历史产物**里的纪要条目引用补上条目正文（`source_text`）——一次性补字段工具。

## 为什么需要它

判定侧（`judges/rag.py`）的上下文取自引用快照的 `source_text`，优先于 `quote`；
2026-10-16 之前抓的产物没有这个字段（那时快照只留证据原句），于是"表格里的截止时间 /
负责人"这类断言的来源（类型化槽位）根本进不了判定者眼前，只能被判"上下文没写"。
**信息本来就在产品的纪要里**，只是旧快照把它丢了——这个工具把它按引用一条条取回来，
使"同一批答案、只换仪器"这种干净配对成为可能（否则只能重跑问答，答案也一起变了）。

## 边界

- 只补 `kind=fact`（纪要条目）且带 `minutes_id/field/item_index` 的引用；
  切片引用的 `quote` 就是切片正文，不需要补。
- 取数走**产品的 HTTP 接口**（`/meetingMinutes/selectById/{id}`），不 import 产品内部实现
  （评测宪法第 5 条）；渲染只用 `services.contracts` 已登记的那份呈现出口
  （`present_output`）——槽位名与产品一致，不然补出来的上下文会与线上不同形。
- 补不上的（纪要已被删、字段/下标对不上）**如实计数并跳过**，不编内容。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Mapping

# 条目里除正文之外的槽位：presented 形态下是 `*_suggestion` 字符串
_SLOT_LABELS = (("owner_suggestion", "负责人"), ("deadline_suggestion", "截止时间"),
                ("raised_by_suggestion", "提出人"), ("level", "等级"))


def _http_json(url: str, *, token: str, timeout: float = 60.0) -> dict:
    request = urllib.request.Request(url, method="GET")
    request.add_header("token", token)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - 地址来自命令行
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")[:200]
        raise RuntimeError(f"GET {url} → HTTP {error.code}: {detail}") from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"GET {url} → {error.reason}") from error


def _presented(item: Any) -> str:
    """渲染一条条目的完整文本：正文 + 槽位（与产品 `citations._item_text` 同形）。"""
    if not isinstance(item, Mapping):
        return ""
    parts: list[str] = []
    body = item.get("content") or item.get("title") or item.get("viewpoint")
    if isinstance(body, Mapping):
        body = body.get("text")
    text = str(body or "").strip()
    if text:
        parts.append(text)
    for key, label in _SLOT_LABELS:
        value = item.get(key)
        if isinstance(value, Mapping):
            value = value.get("text")
        text = str(value or "").strip()
        if text:
            parts.append(f"{label}：{text}")
    return "；".join(parts)


def enrich_artifact(artifact: Mapping[str, Any], *, base_url: str, token: str,
                    progress: Callable[..., None] = print) -> tuple[dict[str, Any], dict[str, int]]:
    """返回（补好的产物副本，统计）。原产物不动。"""
    enriched = json.loads(json.dumps(artifact))          # 深拷贝：不碰调用方的对象
    cache: dict[int, dict[str, Any]] = {}
    stats = {"citations_seen": 0, "already_present": 0, "enriched": 0, "skipped": 0}
    for turn in enriched.get("turns") or []:
        for citation in turn.get("citations") or []:
            stats["citations_seen"] += 1
            if str(citation.get("kind")) != "fact" or citation.get("source_text"):
                stats["already_present"] += 1
                continue
            minutes_id = citation.get("minutes_id")
            field = citation.get("field")
            index = citation.get("item_index")
            if minutes_id is None or not field or index is None:
                stats["skipped"] += 1
                continue
            if minutes_id not in cache:
                cache[minutes_id] = _http_json(
                    f"{base_url}/meetingMinutes/selectById/{int(minutes_id)}", token=token)
            items = ((cache[minutes_id].get("data") or {}).get(field) or [])
            item = items[int(index)] if 0 <= int(index) < len(items) else None
            text = _presented(item)
            if not text:
                stats["skipped"] += 1
                continue
            citation["source_text"] = text
            stats["enriched"] += 1
    progress(f"补字段：{stats['enriched']} 条；已带/不适用 {stats['already_present']}；"
             f"补不上 {stats['skipped']}")
    return enriched, stats


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description="给历史产物的纪要条目引用补 source_text")
    parser.add_argument("--answers", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:19091")
    parser.add_argument("--token", required=True)
    args = parser.parse_args(argv)
    artifact = json.loads(args.answers.read_text(encoding="utf-8"))
    enriched, stats = enrich_artifact(artifact, base_url=args.base_url, token=args.token)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(enriched, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {args.output}：{json.dumps(stats, ensure_ascii=False)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
