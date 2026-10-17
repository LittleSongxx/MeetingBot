"""引用登记与解析：把工具返回的条目编号，并从答案里读出它引用了哪几条。

## 为什么要编号

工具返回的条目本来就有稳定标识（`chunk_id`，或 `minutes_id + field + item_index`），
但让模型在自由文本里逐字抄对这些标识是一种无谓的失误来源——抄错一位数字，
引用就指向了另一条。业界做法（RAG 引用范式的通行写法）是**检索结果编号、
答案里写 `[n]`**：编号由宿主分配，模型只写小整数，于是"答案引用了什么"
变成可解析、可校验的一件事。

这不只是为了点得动：**"答案里的每个引用都必须能在被引条目里定位"这条不变量
只有在引用可解析时才谈得上判定**（阶段 5 的 L0 零调用检查建立在它上面）。

## 登记表的两条规则

- **按身份去重**：同一片切片被检索两次只占一个编号，否则编号列表会随工具
  调用次数膨胀，模型也可能对同一件事写两个 `[n]`。
- **编号只增不改**：已发出去的编号在整轮对话里含义固定，答案里的 `[3]`
  永远指同一条。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Any, Callable

# 答案里的引用标记：`[1]`、`[12]`。上限三位数——编号超过 999 说明检索没收敛，
# 那种情况该由工具层的 limit 参数拦住，而不是靠解析器容忍。
CITATION_PATTERN = re.compile(r"\[(\d{1,3})\]")

KIND_CHUNK = "chunk"
KIND_FACT = "fact"
KIND_SEGMENT = "segment"


@dataclass(frozen=True)
class Citation:
    """一条可点回的引用。字段够前端画卡片，也够校验答案是否引用了它。"""

    ref: int
    kind: str
    meeting_id: int
    quote: str
    meeting_title: str = ""
    chunk_id: int | None = None
    minutes_id: int | None = None
    field: str | None = None
    item_index: int | None = None
    speaker: str | None = None
    start_second: int | None = None
    # 切片的时间窗终点。检索结果里本来就带它（Hit.as_citation），这里原样留下：
    # 评测侧要用"这条切片覆盖哪几段转写"算检索指标（recall@k/MRR/nDCG），
    # 只有起点算不出覆盖范围。
    end_second: int | None = None
    authority: str | None = None
    # 条目/切片的**完整正文**。与 `quote` 的区别是刻意的：quote 要能落回原文
    # （所以事实条目优先取证据原句），而这一份是"判定者拿来核对的上下文"——
    # 事实条目的类型化槽位（负责人/截止时间/风险等级）只在这里，缺了它判定者
    # 只能对"表格里的截止时间"这类断言判"不支撑"（P1 复测里 6 条不支撑就是这么来的）。
    source_text: str | None = None
    # 来源身份（转写任务/纪要的 id）：模型据此指认"哪段录音"，
    # 评测侧据此把切片映射回录音（跨录音问题的检索指标靠它）
    source_id: int | None = None
    task_id: int | None = None
    segment_no: int | None = None

    @property
    def key(self) -> tuple:
        """去重身份。同一条内容的两个来源编号相同。"""
        if self.kind == KIND_CHUNK:
            return (self.kind, self.chunk_id)
        if self.kind == KIND_FACT:
            # 纪要条目按 minutes_id + field + item_index；录音/会议清单没有 minutes_id，
            # 过去三者全为 None，导致所有清单行被合并成同一个空引用（q91 的 [1]）。
            # 这里用清单自己的身份做 key：录音按 task_id、会议按 meeting_id。
            if self.minutes_id is not None:
                return (self.kind, "minutes", self.minutes_id, self.field, self.item_index)
            if self.task_id is not None:
                return (self.kind, "recording", self.task_id)
            return (self.kind, "meeting", self.meeting_id)
        return (self.kind, self.task_id, self.segment_no)

    def as_dict(self) -> dict[str, Any]:
        data = {
            "ref": self.ref, "kind": self.kind, "meeting_id": self.meeting_id,
            "quote": self.quote, "meeting_title": self.meeting_title,
            "speaker": self.speaker, "start_second": self.start_second,
            "end_second": self.end_second, "authority": self.authority,
        }
        if self.source_text:
            data["source_text"] = self.source_text
        for name in ("chunk_id", "minutes_id", "field", "item_index", "task_id", "segment_no",
                     "source_id"):
            value = getattr(self, name)
            if value is not None:
                data[name] = value
        return data

    def label(self) -> str:
        """一行短标签，用于折叠面板与卡片标题。"""
        where = self.meeting_title or f"会议 {self.meeting_id}"
        when = f"{self.start_second // 60:02d}:{self.start_second % 60:02d}" if self.start_second is not None else ""
        who = self.speaker or ""
        parts = [part for part in (where, who, when) if part]
        return " · ".join(parts)


@dataclass
class CitationRegistry:
    """一次问答里的引用登记表。进程内对象，不进数据库（答案落库时随消息存快照）。"""

    _items: list[Citation] = field(default_factory=list)
    _by_key: dict[tuple, int] = field(default_factory=dict)

    def annotate(self, tool_name: str, result: dict[str, Any]) -> dict[str, Any]:
        """给工具结果里的条目补上 `ref` 字段，返回新字典（不改原对象）。

        这里同时决定"哪些工具结果能成为引用"——只有带出处信息的条目才会被登记，
        因此模型不可能引用一个工具从没返回过的东西。
        """
        extract = _EXTRACTORS.get(tool_name)
        if extract is None or not result.get("ok"):
            return result
        annotated = dict(result)
        pairs = extract(result)
        for item, candidate in pairs:
            item["ref"] = self._register(candidate).ref
        key = _ITEMS_KEY[tool_name]
        if key is not None:
            annotated[key] = [item for item, _ in pairs]
        return annotated

    def _register(self, candidate: Citation) -> Citation:
        existing = self._by_key.get(candidate.key)
        if existing is not None:
            return self._items[existing - 1]
        citation = replace(candidate, ref=len(self._items) + 1)
        self._items.append(citation)
        self._by_key[citation.key] = citation.ref
        return citation

    def since(self, ref: int) -> list[Citation]:
        """编号大于 `ref` 的引用（增量推给前端用）。"""
        return [item for item in self._items if item.ref > ref]

    def get(self, ref: int) -> Citation | None:
        if 1 <= ref <= len(self._items):
            return self._items[ref - 1]
        return None

    def all(self) -> list[Citation]:
        return list(self._items)

    def as_dicts(self) -> list[dict[str, Any]]:
        return [item.as_dict() for item in self._items]

    def __len__(self) -> int:
        return len(self._items)


def cited_refs(text: str) -> list[int]:
    """答案里出现过的引用编号，按出现顺序去重。"""
    seen: list[int] = []
    for match in CITATION_PATTERN.finditer(text or ""):
        ref = int(match.group(1))
        if ref not in seen:
            seen.append(ref)
    return seen


def unknown_refs(text: str, registry: CitationRegistry) -> list[int]:
    """答案里引用了、但登记表里没有的编号。

    它只可能来自模型的自由发挥（编一个不存在的编号）。**不静默丢弃**：
    调用方要把数量与编号如实记下来——这是"无证据不判定"在引用层的探针，
    也是阶段 5 零调用不变量要判定的对象。
    """
    return [ref for ref in cited_refs(text) if registry.get(ref) is None]


def used_citations(text: str, registry: CitationRegistry) -> list[Citation]:
    """答案实际引用到的条目（用于给前端只推真正用到的卡片）。"""
    return [citation for ref in cited_refs(text)
            if (citation := registry.get(ref)) is not None]


def strip_refs(text: str) -> str:
    """去掉引用标记后的纯文本（导出与朗读场景用；页面上保留标记以便点击）。"""
    return CITATION_PATTERN.sub("", text or "").strip()


# --------------------------------------------------------------------------- #
# 按工具形状取条目，并构造它的引用候选：一张表，新增工具只加一行
# --------------------------------------------------------------------------- #

# 工具 → 结果里"条目数组"的键名；None 表示条目嵌在结果内部（get_minutes 的六字段），
# 由抽取函数原位补 `ref` 而不是整体替换
_ITEMS_KEY: dict[str, str | None] = {
    "search_knowledge": "hits",
    "list_facts": "facts",
    "get_transcript_excerpt": "segments",
    "get_minutes": None,
}


def _from_search(result: dict[str, Any]) -> list[tuple[dict[str, Any], Citation]]:
    pairs: list[tuple[dict[str, Any], Citation]] = []
    for hit in result.get("hits") or []:
        pairs.append((hit, Citation(
            ref=0, kind=KIND_CHUNK, meeting_id=int(hit.get("meeting_id") or 0),
            quote=str(hit.get("quote") or ""), meeting_title=str(hit.get("meeting_title") or ""),
            chunk_id=hit.get("chunk_id"), speaker=hit.get("speaker"),
            start_second=hit.get("start_second"), end_second=hit.get("end_second"),
            authority=hit.get("authority"), source_id=hit.get("source_id"),
        )))
    return pairs


def _from_facts(result: dict[str, Any]) -> list[tuple[dict[str, Any], Citation]]:
    pairs: list[tuple[dict[str, Any], Citation]] = []
    for fact in result.get("facts") or []:
        if result.get("kind") == "meeting":
            title = str(fact.get("title") or "")
            start_time = fact.get("start_time")
            parts = [f"会议 {fact.get('meeting_id')}"]
            if title:
                parts.append(title)
            if start_time:
                parts.append(str(start_time))
            quote = " · ".join(parts)
            pairs.append((fact, Citation(
                ref=0, kind=KIND_FACT, meeting_id=int(fact.get("meeting_id") or 0),
                quote=quote, meeting_title=title,
                # 空标题/空时间不写 source_text：与“没有正文也没有槽位就不写空字段”
                # 的既有口径一致。
                source_text=quote if (title or start_time) else None,
            )))
            continue
        if result.get("kind") == "recording":
            source_id = fact.get("task_id")
            quote = (f"录音 task {source_id} · {fact.get('recorded_at') or '时间未标注'} · "
                     f"{fact.get('period') or '未标注'}")
            pairs.append((fact, Citation(
                ref=0, kind=KIND_FACT, meeting_id=int(fact.get("meeting_id") or 0),
                quote=quote, meeting_title=str(fact.get("meeting_title") or ""),
                source_text=quote, task_id=source_id, source_id=source_id,
            )))
            continue
        item = fact.get("item") or {}
        pairs.append((fact, Citation(
            ref=0, kind=KIND_FACT, meeting_id=int(fact.get("meeting_id") or 0),
            quote=_fact_quote(item), meeting_title=str(fact.get("meeting_title") or ""),
            minutes_id=fact.get("minutes_id"), field=fact.get("field"),
            item_index=fact.get("item_index"), authority=fact.get("authority"),
            source_text=_item_text(item),
        )))
    return pairs


def _from_segments(result: dict[str, Any]) -> list[tuple[dict[str, Any], Citation]]:
    meeting = result.get("meeting") or {}
    meeting_id = int(meeting.get("id") or 0)
    title = str(meeting.get("title") or "")
    pairs: list[tuple[dict[str, Any], Citation]] = []
    for segment in result.get("segments") or []:
        pairs.append((segment, Citation(
            ref=0, kind=KIND_SEGMENT, meeting_id=meeting_id,
            quote=str(segment.get("text") or ""), meeting_title=title,
            speaker=segment.get("speaker"), start_second=segment.get("start_second"),
            end_second=segment.get("end_second"),
            task_id=result.get("task_id"), segment_no=segment.get("segment_no"),
            # 来源身份与 task_id 同值：评测侧按 source_id 把片段映射回录音；
            # 缺了它，get_transcript_excerpt 的引用会被误判成"没有时间轴"（任务 5 扩展集暴露）
            source_id=result.get("task_id"),
        )))
    return pairs


def _from_minutes(result: dict[str, Any]) -> list[tuple[dict[str, Any], Citation]]:
    """整份纪要：逐个条目登记（六字段里的条目本身就是可引用的最小单位）。

    为什么要登记它：`get_minutes` 返回的条目原先不带 ref，模型想引用却无从下手——
    实测它就直接写了个不存在的编号（`[3]`，而当时登记表是空的）。答案里出现查不到的
    编号，页面上的引用就是死链；这条路径此前一直是这样。
    """
    meeting = result.get("meeting") or {}
    meeting_id = int(meeting.get("id") or 0)
    title = str(meeting.get("title") or "")
    minutes_id = result.get("minutes_id")
    authority = result.get("authority")
    pairs: list[tuple[dict[str, Any], Citation]] = []
    fields = result.get("fields") or {}
    if not isinstance(fields, dict):
        return pairs
    for field_name, items in fields.items():
        if not isinstance(items, list):
            continue  # summary 是整段文本，不是一个可引用条目
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            # get_minutes 过滤/截断后仍带原始 item_index；没有它时才退回列表位置。
            item_index = item.get("item_index", index)
            pairs.append((item, Citation(
                ref=0, kind=KIND_FACT, meeting_id=meeting_id,
                quote=_fact_quote(item), meeting_title=title,
                minutes_id=minutes_id, field=field_name, item_index=item_index,
                authority=authority,
                speaker=_evidence_speaker(item),
                source_text=_item_text(item),
            )))
    return pairs


def _evidence_speaker(item: dict[str, Any]) -> str | None:
    evidence = item.get("evidence")
    if isinstance(evidence, list):
        for entry in evidence:
            if isinstance(entry, dict) and str(entry.get("speaker") or "").strip():
                return str(entry["speaker"])
    return None


_EXTRACTORS: dict[str, Callable[[dict[str, Any]], list[tuple[dict[str, Any], Citation]]]] = {
    "search_knowledge": _from_search,
    "list_facts": _from_facts,
    "get_transcript_excerpt": _from_segments,
    "get_minutes": _from_minutes,
}


def _primary_text_of(item: dict[str, Any]) -> str:
    """条目自己的正文（不取证据）。字段名按槽位种类不同：content / title / viewpoint。"""
    for key in ("content", "title", "viewpoint"):
        value = item.get(key)
        text = str(value.get("text") or "").strip() if isinstance(value, dict) else str(value or "").strip()
        if text:
            return text
    return ""


def _fact_quote(item: dict[str, Any]) -> str:
    """事实条目的引用文本：优先取证据原句，没有证据才退回条目正文。

    引用要能落回原文，所以"证据原句"比"条目正文"更该当引用文本——
    条目正文可能是模型改写过的措辞。
    """
    evidence = item.get("evidence")
    if isinstance(evidence, list):
        for entry in evidence:
            if isinstance(entry, dict) and str(entry.get("quote") or "").strip():
                return str(entry["quote"])
    return _primary_text_of(item)


# 条目里除正文之外的槽位：它们在 presented 形态下是 `*_suggestion` 字符串
_ITEM_SLOT_LABELS = (("owner_suggestion", "负责人"), ("deadline_suggestion", "截止时间"),
                     ("raised_by_suggestion", "提出人"), ("level", "等级"))


def _item_text(item: dict[str, Any]) -> str:
    """条目的**完整可读文本**：正文 + 类型化槽位。

    与 `_fact_quote` 分工不同：quote 要能逐字落回原文（所以取证据原句），
    这一份是给判定者核对的上下文——"负责人王工、截止时间 8 月 30 日 18:00"
    这类断言只写在槽位里，不给判定者就只能被判"上下文没写"。
    """
    parts = [_primary_text_of(item)]
    for key, label in _ITEM_SLOT_LABELS:
        value = item.get(key)
        if isinstance(value, dict):
            value = value.get("text")
        text = str(value or "").strip()
        if text:
            parts.append(f"{label}：{text}")
    return "；".join(part for part in parts if part)


__all__ = [
    "Citation", "CitationRegistry", "cited_refs", "unknown_refs", "used_citations",
    "strip_refs", "CITATION_PATTERN", "KIND_CHUNK", "KIND_FACT", "KIND_SEGMENT",
]
