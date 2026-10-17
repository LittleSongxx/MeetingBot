"""知识库切块：把转写与纪要变成可检索、可引用的切片。

设计取舍（都写成理由，便于日后复核）：

- **按片段边界切，不按字符硬切转写**：转写已经是"一句一段"，一句话被切成两半
  会让检索命中一半、引用读起来没头没尾。所以窗口在片段边界上聚合。
- **窗口目标 300 字、重叠 2 句**：中文约 1.5 token/字，300 字 ≈ 450 token，
  落在业界常用的 300–500 token 区间中段。重叠 2 句是为了让"跨句的问答"在任一
  窗口里都完整。**预算按渲染后的行算**（含 `[123s][说话人] ` 前缀），
  否则前缀会让窗口实际超出目标三成。
- **切片带时间戳与说话人**：检索结果要能指回"哪场会、谁、什么时候说的"，
  这也是引用卡片能跳回原文的前提。
- **纪要按条目切，不按字段整段切**：一条决策就是一个检索单位——
  整段塞进一个切片会让引用模糊（"这段里哪句支撑了答案？"）。
- **草稿纪要不进知识库**：只有 `CONFIRMED` 的纪要才作为 `authority=CONFIRMED`
  入库。这是把产品已有的"人拍板才算数"不变量延伸到知识库：答案更该引权威条目。

本模块是纯函数：不碰数据库、不调模型、不发网络请求，所以能脱离环境单测。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

# 切片类型与权威级别（与 kb_chunk.chunk_type / kb_document.authority 取值一致）
CHUNK_TRANSCRIPT = "TRANSCRIPT_WINDOW"
CHUNK_MINUTES_ITEM = "MINUTES_ITEM"
AUTHORITY_RAW = "RAW"
AUTHORITY_CONFIRMED = "CONFIRMED"

# 窗口目标字数与重叠句数。默认 500 字 + 2 行重叠（约 15%），落在中文 RAG 的常用区间
# （500–1000 字、10–20% 重叠）。**这是可配参数**：语料是会议转写（连续口语、没有文档
# 章节边界），窗口该多大要用检索指标在本文料上定——用 `KB_CHUNK_TARGET_CHARS` 覆盖。
DEFAULT_TARGET_CHARS = 500
DEFAULT_OVERLAP_LINES = 2


def target_chars() -> int:
    """本部署生效的窗口字数（环境变量优先，非法值退回默认并抛错）。"""
    raw = os.getenv("KB_CHUNK_TARGET_CHARS", "").strip()
    if not raw:
        return DEFAULT_TARGET_CHARS
    try:
        value = int(raw)
    except ValueError as error:
        raise ValueError("KB_CHUNK_TARGET_CHARS 必须为正整数") from error
    if value < 100:
        raise ValueError("KB_CHUNK_TARGET_CHARS 太小（<100 字），切块会碎到无法检索")
    return value
# 单句上限：超过就按句读切，再不行硬切。防止一句超长（ASR 偶尔不给标点）撑爆窗口
MAX_SINGLE_LINE_CHARS = 800

_CLAUSE_SPLIT = re.compile(r"(?<=[。！？；!?;])")


@dataclass(frozen=True)
class Chunk:
    """一个待入库的切片。字段与 models.KbChunk 一一对应（除 document_id 等归属字段）。"""

    chunk_no: int
    chunk_type: str
    text: str
    speaker: str | None = None
    start_ms: int | None = None
    end_ms: int | None = None
    field: str | None = None
    item_index: int | None = None
    origin: str | None = None
    evidence: list[dict[str, Any]] | None = None


def transcript_line(start_ms: int, speaker: str, content: str) -> str:
    """与 minutes_service.transcript_text 完全同格式的一行。

    同格式不是审美问题：引用卡片会把切片原文展示给人，两处格式不一致会让人
    以为是两份不同的内容。
    """
    return f"[{start_ms // 1000}s][{speaker}] {content}"


def _render_window(lines: Sequence[tuple[int, int, str, str]]) -> str:
    return "\n".join(transcript_line(start, speaker, content) for start, _end, speaker, content in lines)


def _split_long_line(start_ms: int, end_ms: int, speaker: str, content: str) -> list[tuple[int, int, str, str]]:
    """超长单句拆成若干段；时间戳按字数比例线性分摊（只用于定位，不追求精确）。"""
    pieces = [p for p in _CLAUSE_SPLIT.split(content) if p]
    if not pieces:
        pieces = [content]
    # 仍在上限之上的（ASR 没给标点）再硬切
    bounded: list[str] = []
    for piece in pieces:
        while len(piece) > MAX_SINGLE_LINE_CHARS:
            bounded.append(piece[:MAX_SINGLE_LINE_CHARS])
            piece = piece[MAX_SINGLE_LINE_CHARS:]
        if piece:
            bounded.append(piece)

    total = sum(len(p) for p in bounded) or 1
    span = max(0, end_ms - start_ms)
    out: list[tuple[int, int, str, str]] = []
    cursor = start_ms
    for piece in bounded:
        piece_end = cursor + int(span * len(piece) / total)
        out.append((cursor, piece_end, speaker, piece))
        cursor = piece_end
    return out


def chunk_transcript(
    segments: Iterable[tuple[int, int, str, str]],
    *,
    target_chars: int = DEFAULT_TARGET_CHARS,
    overlap_lines: int = DEFAULT_OVERLAP_LINES,
) -> list[Chunk]:
    """把转写片段聚合成重叠窗口。

    `segments` 是 `(start_ms, end_ms, speaker, content)` 序列，按 segment_no 顺序传入。
    返回的切片按 chunk_no 连续编号。
    """
    if target_chars <= 0:
        raise ValueError("target_chars 必须为正数")
    if overlap_lines < 0:
        raise ValueError("overlap_lines 不能为负")

    # 先规范化：空内容丢弃；超长单句拆开
    lines: list[tuple[int, int, str, str]] = []
    for start_ms, end_ms, speaker, content in segments:
        text = (content or "").strip()
        if not text:
            continue
        if len(text) > MAX_SINGLE_LINE_CHARS:
            lines.extend(_split_long_line(start_ms, end_ms, speaker, text))
        else:
            lines.append((start_ms, end_ms, speaker, text))
    if not lines:
        return []

    chunks: list[Chunk] = []
    cursor = 0
    while cursor < len(lines):
        window: list[tuple[int, int, str, str]] = []
        chars = 0
        probe = cursor
        while probe < len(lines):
            # 按渲染后的整行计费（含时间戳与说话人前缀），否则窗口会超出目标
            line_cost = len(transcript_line(lines[probe][0], lines[probe][2], lines[probe][3]))
            # 窗口非空且已达标就收口；单行也必须至少占一个窗口，否则会死循环
            if window and chars + line_cost > target_chars:
                break
            window.append(lines[probe])
            chars += line_cost
            probe += 1

        chunks.append(Chunk(
            chunk_no=len(chunks),
            chunk_type=CHUNK_TRANSCRIPT,
            text=_render_window(window),
            speaker=window[0][2],
            start_ms=window[0][0],
            end_ms=window[-1][1],
        ))

        if probe >= len(lines):
            break
        # 重叠：下一窗口从本窗口末尾回退若干句，保证跨句语境不被切断。
        # 取 max 保证游标必然前进（否则重叠会把循环卡死）
        step_back = min(overlap_lines, len(window) - 1)
        cursor = max(probe - step_back, cursor + 1)
    return chunks


def _slot_text(value: Any) -> str:
    """类型化槽位的可读文本：`{text}` 或 `{text}（{normalized}）`。

    把归一化后的值也写进切片正文，检索才能用"9月1日"命中原文里的"下月一号"这类表述。
    """
    if isinstance(value, Mapping):
        text = str(value.get("text") or "").strip()
        normalized = str(value.get("normalized") or "").strip()
        if text and normalized and normalized != text:
            return f"{text}（{normalized}）"
        return text or normalized
    return str(value or "").strip()


def minutes_item_text(field_name: str, item: Mapping[str, Any]) -> str:
    """把一条纪要条目渲染成可检索的一行（含类型化槽位与来源标注）。"""
    parts: list[str] = []
    if field_name == "topics":
        parts = [_slot_text(item.get("title")), _slot_text(item.get("summary"))]
    elif field_name == "viewpoints":
        parts = [_slot_text(item.get("speaker")), _slot_text(item.get("viewpoint"))]
    elif field_name == "risks":
        level = str(item.get("level") or "").strip()
        parts = [_slot_text(item.get("content")), f"风险等级 {level}" if level else "",
                 _slot_text(item.get("suggestion"))]
    else:  # decisions / pending_items
        parts = [_slot_text(item.get("content")), _slot_text(item.get("owner")),
                 _slot_text(item.get("deadline"))]

    body = " ".join(p for p in parts if p)
    if not body:
        # 只有字段头、没有正文的条目（例如 content 是空白）不该进知识库：
        # 否则会得到一条内容为 "[decisions]" 的切片，检索命中它等于命中噪声
        return ""
    return f"[{field_name}] {body}"


def chunk_minutes(
    summary: Any,
    items_by_field: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    field_order: Sequence[str],
) -> list[Chunk]:
    """把一份纪要切成条目级切片。

    `field_order` 传契约层的 FIELD_ORDER（summary 在首位），保持字段顺序稳定——
    切片编号稳定，引用卡片上的"第几条"才跟着稳定。
    """
    chunks: list[Chunk] = []
    for field_name in field_order:
        if field_name == "summary":
            text = _slot_text(summary).strip()
            if text:
                chunks.append(Chunk(
                    chunk_no=len(chunks), chunk_type=CHUNK_MINUTES_ITEM, text=text,
                    field="summary", item_index=None,
                ))
            continue
        for index, item in enumerate(items_by_field.get(field_name) or []):
            if not isinstance(item, Mapping):
                continue
            text = minutes_item_text(field_name, item).strip()
            if not text:
                continue
            evidence = item.get("evidence")
            chunks.append(Chunk(
                chunk_no=len(chunks),
                chunk_type=CHUNK_MINUTES_ITEM,
                text=text,
                field=field_name,
                item_index=index,
                origin=str(item.get("origin") or "") or None,
                evidence=list(evidence) if isinstance(evidence, list) else None,
            ))
    return chunks
