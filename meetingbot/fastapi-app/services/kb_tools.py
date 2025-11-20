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
  唯一的差别是**呈现层编号**：聊天侧会由宿主给条目编号（`ref`，见
  `services/citations.py`）好让答案能写 `[n]`；MCP 侧不带 `ref`（那边没有"答案"，
  客户端直接用 `chunk_id` 或 `minutes_id + field + item_index` 定位即可）。
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
from datetime import datetime
from typing import Any, Awaitable, Callable, Literal

from common import access, qdrant
from common.times import LOCAL_TZ, format_datetime
from models import (
    Meeting,
    MeetingMinutes,
    TranscriptionTask,
    TranscriptSegment,
    User,
)
from pydantic import BaseModel, Field, ValidationError, field_validator

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

    `biz_id` 是"这次检索属于哪条业务记录"，只用于**记账归属**
    （`ai_call_log.biz_id` → 观测页能说出"这一轮问答花了多少 token"）。
    工具层不解释它、也不拿它判权，只透传给模型调用。
    """

    user: User
    allowed_meeting_ids: list[int] | None
    biz_id: int = 0

    @property
    def user_id(self) -> int:
        return int(self.user.id)

    @property
    def role(self) -> str:
        return str(self.user.role)

    @classmethod
    async def resolve(cls, user: User, *, requested: list[int] | None = None,
                      biz_id: int = 0) -> Actor:
        """构造 Actor。`requested` 是本次对话锁定的会议范围（可空＝不锁定）。"""
        allowed = await access.visible_meeting_ids(user, requested=requested)
        return cls(user=user, allowed_meeting_ids=allowed, biz_id=biz_id)


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


def _loose(value: Any, expected: type | tuple[type, ...]) -> Any:
    """把被写成 JSON 字符串的参数还原成真值。**宽松解析只做这一件事**。

    实测背景（2026-10-16，q26/q27 两轮）：模型把数组参数写成字符串
    （`meeting_ids="[2]"`），`StructuredTool` 用 args_schema 校验直接拒，而回给
    模型的错误文本不是 JSON（是一段 pydantic 报错），模型看不懂，用**同样的参数**
    重试到停滞闸掐断——整轮零引用、零字答案。27 轮里 2 轮如此，
    context recall 缺的 5 条金标事实全部来自这两轮。

    规则只有一条：**字符串先按 JSON 解析，成功且类型对得上就采用，否则原样交回
    pydantic 报常规错误**。不猜意图、不做字符串切分——边界必须清晰，否则"宽松"
    会变成"参数怎么错都能过"，而那比报错危险（静默的畸形参数会一路走到检索层）。
    """
    if not isinstance(value, str):
        return value
    try:
        parsed = json.loads(value.strip())
    except ValueError:
        return value
    # bool 是 int 的子类：`"true"` 不该被当成数字 1 混进数值参数
    if isinstance(parsed, bool):
        return value
    return parsed if isinstance(parsed, expected) else value


class SearchKnowledgeArgs(BaseModel):
    query: str = Field(description="自然语言检索词。例如「上次会上定的上线时间是什么」")
    limit: int = Field(default=8, ge=1, le=20, description="返回的切片条数上限")
    authority: list[Literal["RAW", "CONFIRMED"]] | None = Field(
        default=None,
        description="限定来源权威级：CONFIRMED 是人工确认过的纪要条目，"
                    "RAW 是转写原话；留空表示两者都搜",
    )
    meeting_ids: list[int] | None = Field(
        default=None,
        description="限定在这些会议里搜（会议 id 见 list_facts(kind=meeting)）；"
                    "超过权限范围的会被忽略",
    )
    date_from: str | None = Field(
        default=None,
        description="按**会议日期**限定起始（含当天），格式 YYYY-MM-DD。"
                    "**只在你知道确切会议日期时才传**（先 list_facts(kind=meeting) 看日期）；"
                    "不确定就留空——猜错的日期会把正确内容挡在过滤器外面。"
                    "注意它不是录音里的秒数",
    )
    date_to: str | None = Field(
        default=None,
        description="按会议日期限定截止（含当天），格式 YYYY-MM-DD；"
                    "**不确定就留空**（同 date_from）")
    chunk_type: list[Literal["TRANSCRIPT_WINDOW", "MINUTES_ITEM"]] | None = Field(
        default=None,
        description="限定切片类型：TRANSCRIPT_WINDOW 是转写窗口（原话），"
                    "MINUTES_ITEM 是纪要条目（人工确认的归纳）；留空表示都搜",
    )

    @field_validator("limit", mode="before")
    @classmethod
    def _loose_limit(cls, value: Any) -> Any:
        return _loose(value, int)

    @field_validator("authority", mode="before")
    @classmethod
    def _loose_authority(cls, value: Any) -> Any:
        return _loose(value, (list, type(None)))

    @field_validator("meeting_ids", "chunk_type", mode="before")
    @classmethod
    def _loose_containers(cls, value: Any) -> Any:
        return _loose(value, (list, type(None)))


class ListFactsArgs(BaseModel):
    kind: Literal["decision", "pending_item", "risk", "meeting", "recording"] = Field(
        description="要查的事实类别：decision 决策 / pending_item 待办 / "
                    "risk 风险 / meeting 会议清单 / recording 录音清单"
                    "（哪几段录音、分别是什么时候的，用于「下午那场会」这类指代）")
    meeting_ids: list[int] | None = Field(
        default=None, description="限定在这些会议里查；超过权限范围的会被忽略")
    owner: str | None = Field(
        default=None,
        description="按负责人过滤（只对 kind=decision / pending_item 有意义），"
                    "取值为人名，例如「张晨」",
    )
    limit: int = Field(default=20, ge=1, le=50, description="返回条数上限")

    @field_validator("meeting_ids", mode="before")
    @classmethod
    def _loose_meeting_ids(cls, value: Any) -> Any:
        # 这一条就是 q26/q27 的现场：模型发的是字符串 "[2]"
        return _loose(value, (list, type(None)))

    @field_validator("limit", mode="before")
    @classmethod
    def _loose_limit(cls, value: Any) -> Any:
        return _loose(value, int)


class GetMinutesArgs(BaseModel):
    minutes_id: int | None = Field(default=None, description="纪要 id，与 meeting_id 二选一")
    meeting_id: int | None = Field(default=None, description="会议 id，与 minutes_id 二选一")

    @field_validator("minutes_id", "meeting_id", mode="before")
    @classmethod
    def _loose_ids(cls, value: Any) -> Any:
        return _loose(value, (int, type(None)))


class TranscriptExcerptArgs(BaseModel):
    task_id: int = Field(description="转写任务 id（检索结果里的 task 字段会给到）")
    start_ms: int = Field(default=0, ge=0, description="起始毫秒（含）")
    end_ms: int | None = Field(default=None, ge=0, description="结束毫秒（含），留空取到末尾")

    @field_validator("task_id", "start_ms", "end_ms", mode="before")
    @classmethod
    def _loose_ms(cls, value: Any) -> Any:
        # `"null"` 会被还原成 None（模型确实会把"不填"写成字符串 null）
        return _loose(value, (int, type(None)))


# --------------------------------------------------------------------------- #
# 工具实现
# --------------------------------------------------------------------------- #


def _epoch_of(value: str, *, end_of_day: bool) -> int:
    """把模型给的时间解析成本地时区的 epoch 秒。

    接受 `YYYY-MM-DD` 与 ISO 时间两种写法。**只给日期时按整天解释**
    （起点 00:00:00、终点 23:59:59）：模型说"9月10日"指的是那一天，
    把终点当成 00:00:00 会把当天下午的会排除掉——这是最容易悄悄出错的一处。
    """
    text = (value or "").strip()
    date_only = len(text) == 10 and text[4] == "-" and text[7] == "-"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as error:
        raise ToolError(f"时间格式不认：{text}（要 YYYY-MM-DD 或 ISO 时间）") from error
    if date_only and end_of_day:
        parsed = parsed.replace(hour=23, minute=59, second=59)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=LOCAL_TZ)
    return int(parsed.timestamp())


def _applied_filters(args: SearchKnowledgeArgs, meeting_scope: list[int] | None) -> list[str]:
    """这次检索真正生效的限定，用于告诉模型（与用户）"结果是在什么范围内得到的"。"""
    applied: list[str] = []
    if meeting_scope is not None:
        applied.append(f"会议 {meeting_scope}")
    if args.date_from:
        applied.append(f"会议日期 ≥ {args.date_from}")
    if args.date_to:
        applied.append(f"会议日期 ≤ {args.date_to}")
    if args.chunk_type:
        applied.append(f"切片类型 {args.chunk_type}")
    if args.authority:
        applied.append(f"权威级 {args.authority}")
    return applied


async def _search_knowledge(actor: Actor, args: SearchKnowledgeArgs) -> dict[str, Any]:
    # 会议范围走与其余工具同一条收窄路径：参数只能缩小可见集合，绝不能扩大
    allowed = _narrow(actor.allowed_meeting_ids, args.meeting_ids)
    time_from = _epoch_of(args.date_from, end_of_day=False) if args.date_from else None
    time_to = _epoch_of(args.date_to, end_of_day=True) if args.date_to else None
    if time_from is not None and time_to is not None and time_to < time_from:
        raise ToolError("date_to 不能早于 date_from")
    try:
        hits = await kb_service.search_knowledge(
            args.query, allowed_meeting_ids=allowed, limit=args.limit,
            authority=args.authority, chunk_type=args.chunk_type,
            meeting_time_from=time_from, meeting_time_to=time_to, biz_id=actor.biz_id)
    except kb_service.KbError as error:
        raise ToolError(str(error)) from error
    except qdrant.QdrantError as error:
        raise ToolError(f"检索失败：{error}") from error

    titles = await _meeting_titles([hit.meeting_id for hit in hits])
    result: dict[str, Any] = {
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
    applied = _applied_filters(args, None if allowed is None else list(allowed))
    if applied:
        result["applied_filters"] = applied
    if not hits and (time_from is not None or time_to is not None):
        # **时间限定是模型猜出来的**（实测：它把 2026 年的会猜成 2024 年，
        # 于是过滤把正确内容全挡在外面，那一轮的 3 条金标事实一条都没检索到）。
        # 只靠一句"你可以放宽"不够——模型不会自己回头。这里自动去掉时间限定重试一次，
        # 并把"放宽后有什么"如实交出去：**结论仍由模型下**，它只是不再因为一次猜错而盲。
        relaxed = await kb_service.search_knowledge(
            args.query, allowed_meeting_ids=allowed, limit=args.limit,
            authority=args.authority, chunk_type=args.chunk_type, biz_id=actor.biz_id)
        titles.update(await _meeting_titles([hit.meeting_id for hit in relaxed]))
        result["hits"] = [
            {**hit.as_citation(), "meeting_title": titles.get(hit.meeting_id, ""),
             "score": round(hit.score, 6)}
            for hit in relaxed
        ]
        result["relaxed_time_filter"] = True
        result["notice"] = (
            "在给定的时间限定（" + "；".join(applied) + "）下没有命中任何切片；"
            f"**已去掉时间限定重试，下面是 {len(relaxed)} 条结果**。"
            "如果这些内容其实谈的正是你问的事，说明会议日期与你的限定不符——"
            "请按会议实际日期回答，并说明时间限定没有命中；"
            "若放宽后仍为空，才可以说这个范围内没有相关记录。")
    elif applied and not hits:
        result["notice"] = (
            "在给定的限定（" + "；".join(applied) + "）下没有命中任何切片。"
            "可以去掉其中一项再检索一次；如果放宽后仍没有，就如实告诉用户"
            "这个范围内没有相关记录，不要凭印象补内容。")
    return result


async def _list_facts(actor: Actor, args: ListFactsArgs) -> dict[str, Any]:
    allowed = _narrow(actor.allowed_meeting_ids, args.meeting_ids)
    if args.kind == "meeting":
        return await _list_meetings(allowed, args.limit)
    if args.kind == "recording":
        return await _list_recordings(allowed, args.limit)

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
    result: dict[str, Any] = {
        "kind": args.kind,
        "scope_meeting_count": None if allowed is None else len(allowed),
        "facts": facts,
        "notice": "这些条目来自人工确认过的纪要（authority=CONFIRMED），"
                  "引用时带上 meeting_id / minutes_id / item_index。",
    }
    # 资料覆盖与"查不到时怎么办"：这两件事必须是模型能读到的信息。
    # 实测（P1 复测的 q26）：事实类问题只读已确认纪要，而"下午那场会"往往还没确认
    # ——模型不知道还有草稿与转写可查时，会把"确认纪要里没有"讲成"没有"。
    coverage = await _source_coverage(allowed)
    result["source_coverage"] = coverage
    if coverage["draft_minutes"] or coverage["recordings"] > coverage["confirmed_minutes"]:
        result["notice"] = (
            f"资料覆盖：已确认纪要 {coverage['confirmed_minutes']} 份、"
            f"未确认纪要 {coverage['draft_minutes']} 份、转写录音 {coverage['recordings']} 段。"
            "未确认的纪要**不算事实**（这里的 facts 只来自已确认纪要）；"
            "所以「这里没有」不等于「没发生过」——可以 search_knowledge 到转写原话里核实，"
            "并说清依据来自转写而不是确认纪要。")
    return result


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
            raise ToolError(f"参数校验失败：{_readable_errors(error)}") from error

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


def _readable_errors(error: ValidationError) -> str:
    """把 pydantic 的错误列表压成"字段 需要什么"这样一句话。

    这段文本有**两个读者**：模型（据此改参数重试）与用户（停滞时它就是"卡在哪"的
    说明，见 `chat_graph.stop_detail`）。原始的 `error.errors()` 是一串字典，两者都
    读不动——实测 q26/q27 的模型正是对这种文本无从下手，只好原样重试。
    """
    parts = []
    for item in error.errors()[:3]:
        field = ".".join(str(part) for part in (item.get("loc") or ())) or "参数"
        parts.append(f"{field} {item.get('msg') or '不合法'}")
    return "；".join(parts)


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


async def _list_recordings(allowed: list[int] | None, limit: int) -> dict[str, Any]:
    """录音清单：一场会可能有多段录音（上午一段、下午一段），而待办与决策是按
    **会议**存的。所以"下午这场会"没法用会议 id 表达——先看这里，再决定查哪段转写。

    只列转写成功的任务：没转写完的录音搜不到任何东西，列出来只会误导模型。
    """
    query = TranscriptionTask.filter(status="SUCCEEDED")
    query = _filter_meetings(query, allowed)
    rows = await query.order_by("-create_time").limit(limit)
    titles = await _meeting_titles([row.meeting_id for row in rows])
    return {
        "kind": "recording",
        "scope_meeting_count": None if allowed is None else len(allowed),
        "facts": [{
            "task_id": row.id, "meeting_id": row.meeting_id,
            "meeting_title": titles.get(row.meeting_id, ""),
            "create_time": _fmt(row.create_time),
            "transcribed_at": _fmt(row.finished_at) or _fmt(row.started_at),
            "duration_minutes": round((row.total_duration_ms or 0) / 60_000, 1) or None,
        } for row in rows],
        "notice": "task_id 用于 get_transcript_excerpt（取这段录音的原话）。"
                  "引用时用 meeting_id + start_second，并说清是哪段录音。",
    }


async def _source_coverage(allowed: list[int] | None) -> dict[str, int]:
    """范围内"资料覆盖"的三个数：已确认纪要、未确认纪要、转写录音。

    它是"查不到时该怎么办"的判据：事实只来自已确认纪要，而待办/决策往往先出现在
    录音里。模型看不到这三个数就只能猜（实测 q26 把"确认纪要里只有 1 条"讲成了全部）。
    """
    confirmed = await _filter_meetings(
        MeetingMinutes.filter(status="CONFIRMED"), allowed).count()
    drafts = await _filter_meetings(
        MeetingMinutes.filter(status__not="CONFIRMED"), allowed).count()
    recordings = await _filter_meetings(
        TranscriptionTask.filter(status="SUCCEEDED"), allowed).count()
    return {"confirmed_minutes": confirmed, "draft_minutes": drafts,
            "recordings": recordings}


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
