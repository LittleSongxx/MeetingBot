"""知识库工具层：**唯一**的工具实现，HTTP 聊天与 MCP 都只调这里。

## 为什么是唯一一份

同一组工具会出现两种传输（浏览器里的聊天、MCP 客户端），这是行业里最常见的
分叉点：各写一份，权限判断与参数校验就会在其中一份上悄悄缺失，而"缺失"的表现
是越权或幻觉，不是报错。因此这里把四件东西各收成一处：

- **参数 schema**：pydantic 模型一处定义，派生出给模型看的 JSON Schema；
  参数校验用的是同一个模型，不存在"模型看到的约束"与"后端检查的约束"两套。
- **权限判断**：`Actor` 在构造时解析一次可见会议集合，之后每个工具只做收窄，
  工具内部**再判一次** `has_meeting_access`（拿具体会议行的场景），不依赖提示词。
- **结果形状**：一律返回 JSON 可序列化的 dict；聊天侧序列化成 tool message 文本，
  MCP 侧序列化成 text content，两边看到的是同一份数据。
- **工具数量**：四个。业界（Google / Anthropic / Microsoft 的工具设计建议）收敛在
  3–5 个：每多一个工具，模型选错的概率就上升一档。事实类问题走 `list_facts`
  具名参数，**不做自由文本转 SQL**——那同时带来注入面与幻觉 SQL。

## 权限模型

`Actor.allowed_meeting_ids` 与 `common.access.visible_meeting_ids` 同义：
`None` 表示不过滤（管理员），列表（可能为空）表示允许的集合。工具的
`meeting_ids` 参数只能**收窄**这个集合，绝不能扩大——收窄逻辑只有 `_narrow` 一处。

## 与产品不变量的关系

- **人拍板才算数**：`list_facts` 只读 `CONFIRMED` 的纪要，草稿永远不进事实层
  （与 `kb_service.minutes_chunks` 拒绝草稿同款理由）。`get_minutes` 允许读到草稿，
  但一定带上 `authority=DRAFT` 标注——用户本来就能在自己页面上看到草稿，藏起来
  只会让"总结一下上周那场会"答不出来；但工具把它标成草稿，模型不许当权威引用。
- **无证据不判定**：工具返回的纪要条目带 `evidence`（契约层 `upgrade_output`
  保留它），检索切片带 `start_second` 与原文，模型被要求引用它们。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Literal

from common import access, qdrant
from common.times import format_datetime
from models import (
    Meeting,
    MeetingMinutes,
    TranscriptionTask,
    TranscriptSegment,
    User,
)
from pydantic import BaseModel, Field, ValidationError

from services import contracts, kb_service

logger = logging.getLogger("meetingbot.kb_tools")

# 工具返回的时间窗口上限：模型最多一次看这么多转写片段，防止把整场转写拉进来
# 撑爆下一轮上下文（要看更多就再取一段，这是"引用能落到具体句子"的正常代价）。
MAX_EXCERPT_SEGMENTS = 60
# list_facts 扫描的纪要行上限。JSON 列没法在库侧按条目过滤，只能取一批再在
# Python 里筛；上限存在的意义是让最坏耗时可估，而不是正确性。
MAX_FACT_MINUTES_SCANNED = 200


class ToolError(RuntimeError):
    """工具的可读失败。会被包成 `{"ok": false, "error": ...}` 交回模型重试。"""


# --------------------------------------------------------------------------- #
# Actor：调用者身份 + 解析好的权限
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Actor:
    """一次工具调用里的"谁"。

    `allowed_meeting_ids` 在构造时算一次（一次查询），此后所有工具共用；
    它的语义与 `access.visible_meeting_ids` 完全一致，不要在任何地方另作解释。
    """

    user: User
    allowed_meeting_ids: list[int] | None

    @property
    def user_id(self) -> int:
        return int(self.user.id)

    @property
    def role(self) -> str:
        return str(self.user.role)

    @classmethod
    async def resolve(cls, user: User, *, requested: list[int] | None = None) -> Actor:
        """构造 Actor。`requested` 是本次对话锁定的会议范围（可空＝不锁定）。"""
        allowed = await access.visible_meeting_ids(user, requested=requested)
        return cls(user=user, allowed_meeting_ids=allowed)


def _narrow(allowed: list[int] | None, requested: list[int] | None) -> list[int] | None:
    """把工具参数里的会议范围收窄到权限范围内。**唯一**的范围计算点。

    返回 None 表示不限制（仅当调用者是管理员且参数也没限制时）；空列表表示
    一个都不许看——它与 None 是两种语义，混用就是把"没权限"变成"没限制"。
    """
    if requested is None:
        return allowed
    wanted = {int(i) for i in requested}
    if allowed is None:
        return sorted(wanted)
    return sorted(wanted & {int(i) for i in allowed})


# --------------------------------------------------------------------------- #
# 工具参数：pydantic 一处定义，派生出给模型看的 JSON Schema
# --------------------------------------------------------------------------- #


class SearchKnowledgeArgs(BaseModel):
    query: str = Field(description="自然语言检索词。例如「上次会上定的上线时间是什么」")
    limit: int = Field(default=8, ge=1, le=20, description="返回的切片条数上限")
    authority: list[Literal["RAW", "CONFIRMED"]] | None = Field(
        default=None,
        description="限定来源权威级：CONFIRMED 是人工确认过的纪要条目，"
                    "RAW 是转写原话；留空表示两者都搜",
    )


class ListFactsArgs(BaseModel):
    kind: Literal["decision", "pending_item", "risk", "meeting"] = Field(
        description="要查的事实类别：decision 决策 / pending_item 待办 / "
                    "risk 风险 / meeting 会议清单")
    meeting_ids: list[int] | None = Field(
        default=None, description="限定在这些会议里查；超过权限范围的会被忽略")
    owner: str | None = Field(
        default=None,
        description="按负责人过滤（只对 kind=decision / pending_item 有意义），"
                    "取值为人名，例如「张晨」",
    )
    limit: int = Field(default=20, ge=1, le=50, description="返回条数上限")


class GetMinutesArgs(BaseModel):
    minutes_id: int | None = Field(default=None, description="纪要 id，与 meeting_id 二选一")
    meeting_id: int | None = Field(default=None, description="会议 id，与 minutes_id 二选一")


class TranscriptExcerptArgs(BaseModel):
    task_id: int = Field(description="转写任务 id（检索结果里的 task 字段会给到）")
    start_ms: int = Field(default=0, ge=0, description="起始毫秒（含）")
    end_ms: int | None = Field(default=None, ge=0, description="结束毫秒（含），留空取到末尾")


# --------------------------------------------------------------------------- #
# 工具实现
# --------------------------------------------------------------------------- #


async def _search_knowledge(actor: Actor, args: SearchKnowledgeArgs) -> dict[str, Any]:
    allowed = _narrow(actor.allowed_meeting_ids, None)
    try:
        hits = await kb_service.search_knowledge(
            args.query, allowed_meeting_ids=allowed, limit=args.limit,
            authority=args.authority)
    except kb_service.KbError as error:
        raise ToolError(str(error)) from error
    except qdrant.QdrantError as error:
        raise ToolError(f"检索失败：{error}") from error

    titles = await _meeting_titles([hit.meeting_id for hit in hits])
    return {
        "query": args.query,
        "scope_meeting_count": None if allowed is None else len(allowed),
        "hits": [
            {**hit.as_citation(), "meeting_title": titles.get(hit.meeting_id, ""),
             "score": round(hit.score, 6)}
            for hit in hits
        ],
        "notice": "引用时请用 meeting_id + start_second 指回原句；"
                  "所有条目都必须来自上面的 hits，不要凭记忆补充。",
    }


async def _list_facts(actor: Actor, args: ListFactsArgs) -> dict[str, Any]:
    allowed = _narrow(actor.allowed_meeting_ids, args.meeting_ids)
    if args.kind == "meeting":
        return await _list_meetings(allowed, args.limit)

    field = {"decision": "decisions", "pending_item": "pending_items",
             "risk": "risks"}[args.kind]
    # 只查已确认纪要：草稿是模型产物，人还没拍板，不能当事实回答（同 kb_service 口径）
    query = MeetingMinutes.filter(status="CONFIRMED")
    query = _filter_meetings(query, allowed)
    rows = await query.order_by("-id").limit(MAX_FACT_MINUTES_SCANNED)
    titles = await _meeting_titles([row.meeting_id for row in rows])

    facts: list[dict[str, Any]] = []
    for minutes in rows:
        items = contracts.upgrade_output({field: getattr(minutes, f"{field}_json") or []})
        for index, item in enumerate(items.get(field) or []):
            if args.owner and not _owner_matches(item, args.owner):
                continue
            facts.append(_fact(minutes, field, index, item, titles))
            if len(facts) >= args.limit:
                break
        if len(facts) >= args.limit:
            break
    return {
        "kind": args.kind,
        "scope_meeting_count": None if allowed is None else len(allowed),
        "facts": facts,
        "notice": "这些条目来自人工确认过的纪要（authority=CONFIRMED），"
                  "引用时带上 meeting_id / minutes_id / item_index。",
    }


async def _get_minutes(actor: Actor, args: GetMinutesArgs) -> dict[str, Any]:
    if args.minutes_id is None and args.meeting_id is None:
        raise ToolError("minutes_id 与 meeting_id 至少要给一个")
    minutes = None
    if args.minutes_id is not None:
        minutes = await MeetingMinutes.get_or_none(id=args.minutes_id)
    else:
        # 同一场会可能有多版纪要：优先取人工确认过的，没有确认版才退回最新草稿
        minutes = (await MeetingMinutes.filter(meeting_id=args.meeting_id, status="CONFIRMED")
                   .order_by("-id").first()
                   or await MeetingMinutes.filter(meeting_id=args.meeting_id)
                   .order_by("-id").first())
    if minutes is None:
        raise ToolError("找不到对应的纪要")

    meeting = await Meeting.get_or_none(id=minutes.meeting_id)
    if meeting is None:
        raise ToolError("纪要对应的会议不存在")
    await _require_access(actor, meeting)

    stored = {name: getattr(minutes, f"{name}_json") or [] for name in contracts.LIST_FIELDS}
    stored["summary"] = {"text": minutes.summary or ""}
    # 与详情页、导出走同一个适配出口：这里不自己拼字符串
    presented = contracts.present_minutes_fields(
        contracts.upgrade_output(stored))
    draft = minutes.status != "CONFIRMED"
    return {
        "meeting": {"id": meeting.id, "title": meeting.title,
                    "start_time": _fmt(meeting.start_time)},
        "minutes_id": minutes.id,
        "authority": "DRAFT" if draft else "CONFIRMED",
        # 草稿的说明直接写进结果，模型引用时必须如实标注（"人拍板才算数"）
        "notice": ("这是尚未确认的草稿，引用时必须说明是草稿。" if draft
                   else "这是人工确认过的纪要，可以直接作为会议事实引用。"),
        "fields": presented,
    }


async def _transcript_excerpt(actor: Actor, args: TranscriptExcerptArgs) -> dict[str, Any]:
    task = await TranscriptionTask.get_or_none(id=args.task_id)
    if task is None:
        raise ToolError("转写任务不存在")
    meeting = await Meeting.get_or_none(id=task.meeting_id)
    if meeting is None:
        raise ToolError("转写对应的会议不存在")
    await _require_access(actor, meeting)

    query = TranscriptSegment.filter(task_id=task.id, start_ms__gte=args.start_ms)
    if args.end_ms is not None:
        if args.end_ms < args.start_ms:
            raise ToolError("end_ms 不能小于 start_ms")
        query = query.filter(start_ms__lte=args.end_ms)
    segments = await query.order_by("segment_no").limit(MAX_EXCERPT_SEGMENTS + 1)
    truncated = len(segments) > MAX_EXCERPT_SEGMENTS
    segments = segments[:MAX_EXCERPT_SEGMENTS]

    names = await _speaker_names(segments)
    return {
        "meeting": {"id": meeting.id, "title": meeting.title},
        "task_id": task.id,
        "window": {"start_ms": args.start_ms, "end_ms": args.end_ms},
        "truncated": truncated,
        "segments": [{
            "segment_no": segment.segment_no,
            "speaker": names.get(segment.speaker_user_id) or segment.speaker_label or "未知说话人",
            "start_second": segment.start_ms // 1000,
            "end_second": segment.end_ms // 1000,
            "text": segment.content,
        } for segment in segments],
        "notice": "引用原文时写清说话人与秒数；想要更早或更晚的原话就调大 start_ms 再取。",
    }


# --------------------------------------------------------------------------- #
# 工具表：唯一真源，派生给模型看的 schema、聊天侧的分发、MCP 侧的工具注册
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    arguments: type[BaseModel]
    handler: Callable[[Actor, Any], Awaitable[dict[str, Any]]]

    def json_schema(self) -> dict[str, Any]:
        """给模型看的参数 schema。与校验用的是同一个模型，不可能漂移。"""
        return self.arguments.model_json_schema()

    def validate(self, arguments: Any) -> BaseModel:
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments or "{}")
            except ValueError as error:
                raise ToolError(f"参数不是合法 JSON：{error}") from error
        if not isinstance(arguments, dict):
            raise ToolError("参数必须是一个 JSON 对象")
        try:
            return self.arguments.model_validate(arguments)
        except ValidationError as error:
            raise ToolError(f"参数校验失败：{error.errors()[:3]}") from error

    def openai_tool(self) -> dict[str, Any]:
        """OpenAI 兼容的 function calling 声明（ChatOpenAI.bind_tools 直接吃这个形状）。"""
        return {"type": "function", "function": {
            "name": self.name, "description": self.description,
            "parameters": self.json_schema(),
        }}


TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="search_knowledge",
        description="在会议知识库里做混合检索，返回带出处的切片。"
                    "适合「上次会上说了什么」这类需要原文出处的问题。",
        arguments=SearchKnowledgeArgs,
        handler=_search_knowledge,
    ),
    ToolSpec(
        name="list_facts",
        description="按类别查已确认纪要里的结构化事实：决策 / 待办 / 风险 / 会议清单。"
                    "适合「有哪些待办」「谁负责什么」「这周开了哪些会」这类枚举问题。",
        arguments=ListFactsArgs,
        handler=_list_facts,
    ),
    ToolSpec(
        name="get_minutes",
        description="取某场会的完整六字段纪要（摘要、议题、观点、决策、待办、风险，含证据）。",
        arguments=GetMinutesArgs,
        handler=_get_minutes,
    ),
    ToolSpec(
        name="get_transcript_excerpt",
        description="按时间窗口取转写原文片段，用于把引用落到具体句子。",
        arguments=TranscriptExcerptArgs,
        handler=_transcript_excerpt,
    ),
)

TOOLS_BY_NAME: dict[str, ToolSpec] = {spec.name: spec for spec in TOOL_SPECS}


def openai_tools() -> list[dict[str, Any]]:
    """绑定给模型用的工具声明列表。"""
    return [spec.openai_tool() for spec in TOOL_SPECS]


async def execute_tool(actor: Actor, name: str, arguments: Any) -> dict[str, Any]:
    """执行一个工具，**永不抛异常**：失败包装成 `{"ok": false, "error": ...}`。

    为什么不抛：工具失败是模型可自纠的正常事件（换个参数再试）。抛出去的异常
    要么被传输层变成 500（聊天中断），要么被模型当成系统故障乱道歉；
    而返回结构化错误意味着模型看得见"参数错了"并重试一次正确的调用。
    """
    spec = TOOLS_BY_NAME.get(str(name))
    if spec is None:
        return {"ok": False, "error": f"没有这个工具：{name}",
                "available": sorted(TOOLS_BY_NAME)}
    try:
        parsed = spec.validate(arguments)
        data = await spec.handler(actor, parsed)
        return {"ok": True, **data}
    except ToolError as error:
        return {"ok": False, "error": str(error)}
    except Exception as error:  # noqa: BLE001 - 工具边界：任何异常都不得打断对话主流程
        logger.exception("工具 %s 执行失败", name)
        return {"ok": False, "error": f"工具执行失败：{(str(error) or type(error).__name__)[:300]}"}


def tool_result_text(result: dict[str, Any]) -> str:
    """工具结果 → tool message 文本。聊天与 MCP 共用同一种序列化。"""
    return json.dumps(result, ensure_ascii=False, default=str)


# --------------------------------------------------------------------------- #
# 共用零件
# --------------------------------------------------------------------------- #


async def _require_access(actor: Actor, meeting: Meeting) -> None:
    """拿到具体会议行时再判一次权限（Actor 的列表是构造时的快照，这里用行级判据）。"""
    if not await access.has_meeting_access(meeting, actor.user):
        raise ToolError("没有这场会议的访问权限")


def _filter_meetings(query, allowed: list[int] | None):
    """把可见会议集合塞进查询。空集合用 [-1] 兜底：`in []` 在部分后端上等于不过滤。"""
    if allowed is None:
        return query
    return query.filter(meeting_id__in=list(allowed) or [-1])


async def _list_meetings(allowed: list[int] | None, limit: int) -> dict[str, Any]:
    query = Meeting.all() if allowed is None else Meeting.filter(id__in=list(allowed) or [-1])
    rows = await query.order_by("-start_time").limit(limit)
    return {
        "kind": "meeting",
        "scope_meeting_count": None if allowed is None else len(allowed),
        "facts": [{
            "meeting_id": row.id, "title": row.title,
            "start_time": _fmt(row.start_time), "status": row.status,
        } for row in rows],
        "notice": "只列当前用户可见的会议。",
    }


def _owner_matches(item: Any, owner: str) -> bool:
    """按 owner 槽位的文本判断"这条是某人的"。空槽位（未提及）不匹配任何名字。"""
    if not isinstance(item, dict):
        return False
    slot = item.get("owner")
    text = ""
    if isinstance(slot, dict):
        text = str(slot.get("text") or "")
    elif isinstance(slot, str):
        text = slot
    return owner.strip() in text


def _fact(minutes: MeetingMinutes, field: str, index: int, item: Any,
          titles: dict[int, str]) -> dict[str, Any]:
    """一条事实：能回答问题，也能被引用回条目。"""
    presented = contracts.present_output({field: [item]}).get(field, [item])[0]
    return {
        "meeting_id": minutes.meeting_id,
        "meeting_title": titles.get(minutes.meeting_id, ""),
        "minutes_id": minutes.id,
        "field": field,
        "item_index": index,
        "authority": "CONFIRMED",
        "item": presented,
    }


async def _meeting_titles(meeting_ids: list[int]) -> dict[int, str]:
    ids = {int(i) for i in meeting_ids if i}
    if not ids:
        return {}
    rows = await Meeting.filter(id__in=list(ids)).values_list("id", "title")
    return {int(row[0]): str(row[1]) for row in rows}


async def _speaker_names(segments: list[TranscriptSegment]) -> dict[int, str]:
    user_ids = {segment.speaker_user_id for segment in segments if segment.speaker_user_id}
    if not user_ids:
        return {}
    rows = await User.filter(id__in=list(user_ids)).values_list("id", "name")
    return {int(row[0]): str(row[1]) for row in rows}


def _fmt(value: Any) -> str | None:
    return format_datetime(value)


__all__ = [
    "Actor", "ToolError", "ToolSpec", "TOOL_SPECS", "TOOLS_BY_NAME",
    "execute_tool", "openai_tools", "tool_result_text",
    "SearchKnowledgeArgs", "ListFactsArgs", "GetMinutesArgs", "TranscriptExcerptArgs",
]
