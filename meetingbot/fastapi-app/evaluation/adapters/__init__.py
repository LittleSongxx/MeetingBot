"""公开评测集 adapter 的公共约定。

每个 adapter 把原始数据集样本转换为统一 case：

    {
      "dataset": "mug|vcsum|qmsum",
      "id": "唯一 ID",
      "language": "zh|en",
      "transcript": [{"segment_no": int, "speaker": str, "content": str, "start_ms": int|None}],
      "gold": {... 数据集相关标注 ...},
      "metadata": {... 原始附加字段 ...}
    }

不下载、不修改原始数据；数据许可由调用方自行确认。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

EvalCase = dict[str, Any]


def read_jsonl(path: Path) -> Iterable[dict]:
    """逐行读取 JSON/JSONL；空行跳过。"""
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                yield __import__("json").loads(line)


def load_json_or_jsonl(path: Path) -> list[dict]:
    """兼容单对象、JSON 数组和 JSONL 三种常见格式。"""
    import json

    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return []
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return list(read_jsonl(path))
    if isinstance(payload, list):
        return payload
    return [payload]
