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
- **工具数量**：五个（3–5 区间）。业界（Google / Anthropic / Microsoft 的工具设计建议）
  收敛在 3–5 个：每多一个工具，模型选错的概率就上升一档。事实类问题走 `list_facts`
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
import os
from dataclasses import dataclass, field
from datetime import datetime
from time import perf_counter
from typing import Any, Awaitable, Callable, Literal

from common import access, tracing
from common.times import LOCAL_TZ, format_datetime
from models import (
    Meeting,
    MeetingMinutes,
    MeetingSeries,
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


@dataclass
class ToolBudget:
    """一轮问答内的工具输出预算（目前只有摘录片段总数）。

    为什么需要它：单次 `get_transcript_excerpt` 的上限（60 段）管不住**多次调用**——
    扩展集 q90 两次调用拉回 112 条片段，只有 2 条有用，context_precision 被拖到 1.8%。
    预算是**每轮**的：同一 Actor 在整轮里共享，跨轮自动重置。
    """

    excerpt_segments: int = 0


def excerpt_segments_per_turn() -> int:
    """每轮允许通过 `get_transcript_excerpt` 拉回的总片段数。"""
    raw = os.getenv("EXCERPT_SEGMENTS_PER_TURN", "30").strip()
    try:
        return max(1, int(raw))
    except ValueError as error:
        raise ValueError("EXCERPT_SEGMENTS_PER_TURN 必须为正整数") from error


@dataclass(frozen=True)
class Actor:
    """一次工具调用里的"谁"。

    `allowed_meeting_ids` 在构造时算一次（一次查询），此后所有工具共用；
    它的语义与 `access.visible_meeting_ids` 完全一致，不要在任何地方另作解释。

    `biz_id` 是"这次检索属于哪条业务记录"，只用于**记账归属**
    （`ai_call_log.biz_id` → 观测页能说出"这一轮问答花了多少 token"）。
    工具层不解释它、也不拿它判权，只透传给模型调用。

    `budget` 是**可变**的：同一轮里的多个工具调用共享它。Actor 本身仍然冻结，
    但预算对象按轮递增；跨轮由 `Actor.resolve` 新建。
    """

    user: User
    allowed_meeting_ids: list[int] | None
    biz_id: int = 0
    budget: ToolBudget = field(default_factory=ToolBudget)

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
    limit: int = Field(default=5, ge=1, le=20,
                       description="返回的切片条数上限（默认 5：一次问一两个事实够用，"
                                   "取太多会混进同主题但答不了问题的窗口）")
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
    series_id: int | None = Field(
        default=None,
        description="限定在某个系列（周会/项目例会）的会议里搜；"
                    "系列 id 可先用 query_meeting_state(kind=decision) 或会议列表确认；"
                    "超过权限范围的系列会被拒绝",
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
    source_ids: list[int] | None = Field(
        default=None,
        description="限定来源：转写任务 id（list_facts(kind=recording) 会给到）。"
                    "一场会可能有多段录音，用它可以只搜指定的那一段（例如「下午那场」）",
    )

    @field_validator("limit", mode="before")
    @classmethod
    def _loose_limit(cls, value: Any) -> Any:
        return _loose(value, int)

    @field_validator("authority", mode="before")
    @classmethod
    def _loose_authority(cls, value: Any) -> Any:
        return _loose(value, (list, type(None)))

    @field_validator("series_id", mode="before")
    @classmethod
    def _loose_series_id(cls, value: Any) -> Any:
        return _loose(value, (int, type(None)))

    @field_validator("meeting_ids", "chunk_type", "source_ids", mode="before")
    @classmethod
    def _loose_containers(cls, value: Any) -> Any:
        return _loose(value, (list, type(None)))


class ListFactsArgs(BaseModel):
    kind: Literal["decision", "pending_item", "risk", "task", "meeting", "recording"] = Field(
        description="要查的事实类别：decision 决策 / pending_item 待办 / "
                    "risk 风险 / **task 决策+待办（只用于问某一个人负责的任务，"
                    "例如「张晨负责什么」；问「有哪些行动项/待办/决策清单」仍用 "
                    "pending_item/decision，不要用 task 拉整类清单）** / "
                    "meeting 会议清单 / recording 录音清单"
                    "（哪几段录音、分别是什么时候的，用于「下午那场会」这类指代）")
    meeting_ids: list[int] | None = Field(
        default=None, description="限定在这些会议里查；超过权限范围的会被忽略")
    owner: str | None = Field(
        default=None,
        description="按负责人过滤（只对 kind=decision / pending_item 有意义），"
                    "取值为人名，例如「张晨」",
    )
    keywords: list[str] | None = Field(
        default=None,
        description="按关键词缩小事实（任一命中即可）。先把问题里的关键名词放进来"
                    "（例如「报表导出」「回滚」），不要一次把整类事实都当上下文。",
    )
    limit: int = Field(default=20, ge=1, le=50, description="返回条数上限")

    @field_validator("meeting_ids", "keywords", mode="before")
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
    fields: list[Literal["summary", "topics", "viewpoints", "decisions",
                         "pending_items", "risks"]] | None = Field(
        default=None,
        description="只取这些字段（可按需缩小）。问会议时长/议题只取 summary/topics；"
                    "问待办只取 pending_items；不确定才留空取全部六字段。"
                    "一次把六字段全部当上下文会让无关条目占满窗口。",
    )
    keywords: list[str] | None = Field(
        default=None,
        description="按关键词缩小列表字段（任一命中即可）。问「发布范围/某个主题/某功能」时"
                    "**必须**把问题里的名词放进来，例如 [\"发布范围\", \"功能\"]；"
                    "不要整字段返回。summary 不受 keywords 影响。",
    )
    limit: int | None = Field(
        default=None, ge=1, le=50,
        description="每个列表字段最多返回前几条（默认不截断）。这只是**纪要数组顺序**，"
                    "不保证会议时间顺序；问「会议最后总结的几条」不要用它猜，"
                    "改用 search_knowledge 检索总结段。field_meta 会标 total/returned/truncated。",
    )
    include_draft: bool = Field(
        default=False,
        description="是否允许返回草稿纪要。默认优先人工确认版；问「最近一次会议的结论」"
                    "但 state 查不到已确认结论时，用 true 读最近草稿，并必须在回答里标注草稿。",
    )

    @field_validator("fields", "keywords", mode="before")
    @classmethod
    def _loose_fields(cls, value: Any) -> Any:
        return _loose(value, (list, type(None)))

    @field_validator("limit", mode="before")
    @classmethod
    def _loose_limit(cls, value: Any) -> Any:
        return _loose(value, (int, type(None)))

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


class QueryMeetingStateArgs(BaseModel):
    kind: Literal["decision", "conclusion", "action_item", "preference"] = Field(
        description="要查的状态：decision 先前决策 / conclusion 结论 / "
                    "action_item 待办 / preference 个人偏好（仅本人）")
    query: str | None = Field(
        default=None,
        description="可选的检索词（如项目名、事项名）；留空取最近的条目")
    meeting_ids: list[int] | None = Field(
        default=None,
        description="限定在这些会议里查（问「下午这场会/上次那场会」时用；"
                    "会议 id 可从 list_facts(kind=meeting) 或上下文拿到）")
    series_id: int | None = Field(
        default=None,
        description="限定系列主键。**系列 id 与会议 id 是不同命名空间**："
                    "例如系列1包含会议2时，series_id=1、meeting_id=2；"
                    "不确定就先不传范围或先 list_facts(kind=meeting)")
    status: Literal["open", "done", "all"] = Field(
        default="open",
        description="仅对 action_item 有意义：open 未完成 / done 已完成 / all 全部")
    owner: str | None = Field(
        default=None,
        description="仅对 action_item 有意义：按负责人姓名过滤")
    limit: int = Field(default=10, ge=1, le=30, description="返回条数上限")
    include_proposed: bool = Field(
        default=False,
        description="是否包含 PROPOSED 候选记忆。仅在问题问「是已确认还是未确认候选」"
                    "时设为 true；候选只能陈述状态，不得当作事实。")

    @field_validator("series_id", "limit", mode="before")
    @classmethod
    def _loose_ids(cls, value: Any) -> Any:
        return _loose(value, (int, type(None)))

    @field_validator("meeting_ids", mode="before")
    @classmethod
    def _loose_meeting_ids(cls, value: Any) -> Any:
        return _loose(value, (list, type(None)))


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
    if args.series_id is not None:
        applied.append(f"系列 {args.series_id}")
    if meeting_scope is not None:
        applied.append(f"会议 {meeting_scope}")
    if args.date_from:
        applied.append(f"会议日期 ≥ {args.date_from}")
    if args.date_to:
        applied.append(f"会议日期 ≤ {args.date_to}")
    if args.chunk_type:
        applied.append(f"切片类型 {args.chunk_type}")
    if args.source_ids:
        applied.append(f"来源（录音/文档）{args.source_ids}")
    if args.authority:
        applied.append(f"权威级 {args.authority}")
    return applied


async def _search_knowledge(actor: Actor, args: SearchKnowledgeArgs) -> dict[str, Any]:
    # 会议范围走与其余工具同一条收窄路径：参数只能缩小可见集合，绝不能扩大
    allowed = _narrow(actor.allowed_meeting_ids, args.meeting_ids)
    if args.series_id is not None:
        series = await MeetingSeries.get_or_none(id=args.series_id)
        if series is None:
            raise ToolError("系列不存在")
        if not await access.has_series_access(series, actor.user):
            raise ToolError("没有该系列的访问权限")
        series_meetings = {
            int(item) for item in await Meeting.filter(series_id=series.id).values_list("id", flat=True)
        }
        allowed = sorted(series_meetings if allowed is None else (set(allowed) & series_meetings))
    time_from = _epoch_of(args.date_from, end_of_day=False) if args.date_from else None
    time_to = _epoch_of(args.date_to, end_of_day=True) if args.date_to else None
    if time_from is not None and time_to is not None and time_to < time_from:
        raise ToolError("date_to 不能早于 date_from")
    try:
        hits = await kb_service.search_knowledge(
            args.query, allowed_meeting_ids=allowed, limit=args.limit,
            authority=args.authority, chunk_type=args.chunk_type,
            source_ids=args.source_ids,
            # source_id 在纪要表与转写任务表里各自独立编号；工具语义明确是
            # “录音任务 id”，必须同时限定来源类型，否则同号纪要会被误纳入。
            source_types=("TRANSCRIPT",) if args.source_ids else None,
            meeting_time_from=time_from, meeting_time_to=time_to, biz_id=actor.biz_id)
    except kb_service.KbError as error:
        raise ToolError(str(error)) from error
    except Exception as error:  # noqa: BLE001 - PG 检索失败转成模型可读的工具错误
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
    guessed = time_from is not None or time_to is not None or bool(args.source_ids)
    if not hits and guessed:
        # **时间与来源限定都是模型从语料布局里猜出来的**，而它看不到布局：
        # 实测两次——① 它把 2026 年的会猜成 2024 年（时间限定），那一轮 3 条金标事实
        # 全丢；② 它把"下午这场会"猜成某一段录音（来源限定），而那场会的两段录音
        # 时间戳都是上午，元数据根本判不出"下午"——猜错的方向把正确内容挡在外面。
        # 只靠一句"你可以放宽"不够（模型不会自己回头），这里自动去掉**猜出来的**
        # 限定重试一次，并把"放宽后有什么"如实交出去：结论仍由模型下，
        # 它只是不再因为一次猜错而盲。会议范围/权威级/切片类型**不放宽**——那些是
        # 用户或权限给的明确条件，不是猜的。
        relaxed = await kb_service.search_knowledge(
            args.query, allowed_meeting_ids=allowed, limit=args.limit,
            authority=args.authority, chunk_type=args.chunk_type, biz_id=actor.biz_id)
        titles.update(await _meeting_titles([hit.meeting_id for hit in relaxed]))
        result["hits"] = [
            {**hit.as_citation(), "meeting_title": titles.get(hit.meeting_id, ""),
             "score": round(hit.score, 6)}
            for hit in relaxed
        ]
        result["relaxed_filters"] = True
        result["notice"] = (
            "在给定的限定（" + "；".join(applied) + "）下没有命中任何切片；"
            f"**已去掉时间与来源限定重试，下面是 {len(relaxed)} 条结果**。"
            "如果这些内容其实谈的正是你问的事，说明你的限定（日期/录音）与实际情况不符——"
            "请按检索结果如实回答，并说明限定没有命中；"
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

    # `task` 把决策与待办一起查：问「某人负责的任务」时只查 pending_item 会漏掉
    # 写在 decisions 里的任务（q42 的专项评审触发项就是 pending_item 的干扰项），
    # 但工具不该猜用户指的是哪一类，所以给一个显式的合并类别。
    fields = {
        "decision": ("decisions",),
        "pending_item": ("pending_items",),
        "risk": ("risks",),
        "task": ("decisions", "pending_items"),
    }[args.kind]
    # 只查已确认纪要：草稿是模型产物，人还没拍板，不能当事实回答（同 kb_service 口径）
    query = MeetingMinutes.filter(status="CONFIRMED")
    query = _filter_meetings(query, allowed)
    rows = await query.order_by("-id").limit(MAX_FACT_MINUTES_SCANNED)
    titles = await _meeting_titles([row.meeting_id for row in rows])

    facts: list[dict[str, Any]] = []
    for minutes in rows:
        for field_name in fields:
            items = contracts.upgrade_output(
                {field_name: getattr(minutes, f"{field_name}_json") or []})
            for index, item in enumerate(items.get(field_name) or []):
                if args.owner and not _owner_matches(item, args.owner):
                    continue
                if args.keywords and not _keywords_match(item, args.keywords):
                    continue
                facts.append(_fact(minutes, field_name, index, item, titles))
                if len(facts) >= args.limit:
                    break
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
        notice = (
            f"资料覆盖：已确认纪要 {coverage['confirmed_minutes']} 份、"
            f"未确认纪要 {coverage['draft_minutes']} 份、转写录音 {coverage['recordings']} 段。"
            "未确认的纪要**不算事实**（这里的 facts 只来自已确认纪要）；"
            "所以「这里没有」不等于「没发生过」。")
        if args.kind == "pending_item":
            # **只对行动项给"必须回退检索"的明确提示**：实测（任务 4a）
            # 决策复述题也被这句话带去做额外检索，precision 掉了 5pp。
            notice += (
                "行动项/待办清单不完整时，必须再用 search_knowledge 检索转写原话；"
                "先做一次不限来源的检索，只有 list_facts(kind=recording) 的 "
                "recorded_at/period 明确指向某段录音时才用 source_ids 收窄。")
        else:
            notice += "如果问题需要原文依据，可以按需检索转写；不要仅仅为了覆盖而额外检索。"
        notice += ("如果问题问到的槽位（负责人/协作人/截止时间）在条目里缺失，"
                   "用 search_knowledge 到转写原话补查——这是补缺，不是为覆盖而检索。")
        notice += (f" 本次返回 {len(facts)} 条事实；问完整清单时必须逐条覆盖，"
                   "不得省略或只列前几项。")
        result["notice"] = notice
    return result


async def _get_minutes(actor: Actor, args: GetMinutesArgs) -> dict[str, Any]:
    if args.minutes_id is None and args.meeting_id is None:
        raise ToolError("minutes_id 与 meeting_id 至少要给一个")
    minutes = None
    if args.minutes_id is not None:
        minutes = await MeetingMinutes.get_or_none(id=args.minutes_id)
    elif args.include_draft:
        # 明确要草稿：取最新一版（即使有确认版），用于「最近一次会议结论」这类
        # 只在草稿里的信息；authority=DRAFT 会在结果里如实标注。
        minutes = await MeetingMinutes.filter(meeting_id=args.meeting_id).order_by("-id").first()
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
    requested = args.fields or list(contracts.FIELD_ORDER)
    selected = {name: presented[name] for name in requested if name in presented}
    field_meta: dict[str, dict[str, int | bool]] = {}
    for name, value in list(selected.items()):
        if not isinstance(value, list):
            continue
        pairs = list(enumerate(value))
        if args.keywords:
            pairs = [(index, item) for index, item in pairs
                     if _keywords_match(item, args.keywords)]
        total = len(pairs)
        if args.limit is not None:
            pairs = pairs[:args.limit]
        rendered: list[Any] = []
        for index, item in pairs:
            # **引用身份必须保留原始下标**：过滤/截断后如果重新 enumerate，
            # ref 会指向另一条纪要条目（静默错位比丢条目更危险）。
            if isinstance(item, dict):
                item = {**item, "item_index": index}
            rendered.append(item)
        selected[name] = rendered
        field_meta[name] = {"total": total, "returned": len(rendered),
                            "truncated": total > len(rendered)}
    draft = minutes.status != "CONFIRMED"
    notice = ("这是尚未确认的草稿，引用时必须说明是草稿。" if draft
              else "这是人工确认过的纪要，可以直接作为会议事实引用。")
    notice += (" 列表字段是**纪要数组顺序**，不保证会议时间顺序；问某主题用 keywords 缩小，"
               "limit 只截前 N 条。summary/topics 不保证包含会议时长、数量、具体时间点；"
               "问这些元信息时必须用 search_knowledge 检索转写；问「会议最后总结的几条」"
               "也要检索总结段，不要用字段顺序猜。")
    if any(meta["truncated"] for meta in field_meta.values()):
        notice += " 还有更多条目未返回，需要时缩小 keywords 或调整 limit。"
    notice += (" 若用户问完整清单（所有风险/全部待办/全部决策），逐条覆盖字段内全部条目，"
               "不要只列前几项；不要用关键词截断完整清单。")
    if args.keywords and field_meta and not any(meta["total"] for meta in field_meta.values()):
        notice += " keywords 没有命中任何列表条目；可换关键词或去掉 keywords。"
    return {
        "meeting": {"id": meeting.id, "title": meeting.title,
                    "start_time": _fmt(meeting.start_time)},
        "minutes_id": minutes.id,
        "authority": "DRAFT" if draft else "CONFIRMED",
        "fields_requested": args.fields,
        "field_meta": field_meta,
        # 草稿的说明直接写进结果，模型引用时必须如实标注（"人拍板才算数"）
        "notice": notice,
        "fields": selected,
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
    # **每轮总量预算**：单次 60 段管不住多次调用（扩展集 q90 两次拉回 112 段）。
    # 预算用尽时返回空 + 可读说明，让模型改用 search_knowledge 或缩小窗口，
    # 而不是继续把整段转写塞进上下文。
    total_budget = excerpt_segments_per_turn()
    remaining = total_budget - actor.budget.excerpt_segments
    if remaining <= 0:
        return {
            "meeting": {"id": meeting.id, "title": meeting.title},
            "task_id": task.id,
            "window": {"start_ms": args.start_ms, "end_ms": args.end_ms},
            "truncated": True,
            "segments": [],
            "notice": (f"本轮摘录预算已用完（上限 {total_budget} 段）。"
                       "请改用 search_knowledge 命中具体窗口，或缩小 start_ms/end_ms 后再取；"
                       "不要把整段转写重复拉进上下文。"),
        }
    take = min(MAX_EXCERPT_SEGMENTS, remaining)
    segments = await query.order_by("segment_no").limit(take + 1)
    truncated = len(segments) > take
    segments = segments[:take]
    actor.budget.excerpt_segments += len(segments)

    names = await _speaker_names(segments)
    left = max(0, total_budget - actor.budget.excerpt_segments)
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
        "notice": (f"引用原文时写清说话人与秒数；本轮剩余摘录预算 {left} 段。"
                   "想要更早或更晚的原话就调大 start_ms 再取。"),
    }


async def _query_meeting_state(actor: Actor, args: QueryMeetingStateArgs) -> dict[str, Any]:
    """查询系列/个人的已确认状态（决策、结论、待办、偏好）。

    只返回人工确认过的记忆与有效待办；待办状态以 action_item 为准，不另存一份。
    """
    # 延迟导入：工具模块先于领域服务存在时也能被 import（MCP/能力探测不依赖它）
    from services import state_service

    if args.series_id is not None:
        visible = await access.visible_series_ids(actor.user, requested=[args.series_id])
        if not visible:
            all_visible = await access.visible_series_ids(actor.user) or []
            names = await MeetingSeries.filter(id__in=all_visible or [-1]).values("id", "name")
            hint = "、".join(f"{row['name']}(id={row['id']})" for row in names) or "无"
            raise ToolError(
                f"系列 id={args.series_id} 不存在或无权访问；系列 id 与会议 id 不同。"
                f"当前可见系列：{hint}")
    items = await state_service.query_state(
        actor, kind=args.kind, query=args.query, series_id=args.series_id,
        status=args.status, owner=args.owner, limit=args.limit,
        meeting_ids=args.meeting_ids, include_proposed=args.include_proposed)
    notice = ("只返回 CONFIRMED 记忆与有效待办；未确认候选不得当作事实。"
              "空结果表示当前没有已确认条目，不等于会议没发生过；需要原文或草稿时"
              "继续用 search_knowledge 或 get_minutes(include_draft=true) 查，并在回答里说明。"
              "引用时写清来源会议/纪要；待办状态以工具返回为准，不要按上下文猜。")
    return {"kind": args.kind, "items": items, "notice": notice}


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
        description="按类别查已确认纪要里的结构化事实：决策 / 待办 / 风险 / task（决策+待办）"
                    "/ 会议清单 / 录音清单。适合「有哪些待办/决策/风险」「这周开了哪些会」"
                    "这类**清单**；**kind=task 只用于问某一个人负责的任务**（例如「张晨"
                    "负责什么」），问「有哪些行动项/待办/决策清单」仍用 pending_item/"
                    "decision，不要用 task 拉整类清单；**查单条事实（某个负责人/截止时间/"
                    "某句话的原文出处）优先用 search_knowledge**，不要用本工具代替检索。"
                    "需要时可用 keywords/owner/limit 缩小，避免一次把整类事实都当上下文。",
        arguments=ListFactsArgs,
        handler=_list_facts,
    ),
    ToolSpec(
        name="get_minutes",
        description="取某场会的纪要字段（摘要、议题、观点、决策、待办、风险，含证据）。"
                    "问「最近一次会议的结论」而 query_meeting_state 没有已确认结论时，"
                    "用 include_draft=true 读最近草稿并标注草稿。"
                    "**按需传 fields**：问时长/议题只取 summary/topics，问待办只取 "
                    "pending_items；不确定才取全部。一次取六字段会把无关条目塞满上下文。"
                    "**问某主题/发布范围/功能时传 keywords**（从问题取名词）。"
                    "列表字段是纪要数组顺序，**不保证会议时间顺序**；问「会议最后总结的几条」"
                    "请用 search_knowledge 检索总结段，不要用字段顺序猜。"
                    "summary/topics 不保证包含时长/数量/具体时间点；问这些元信息必须检索转写。",
        arguments=GetMinutesArgs,
        handler=_get_minutes,
    ),
    ToolSpec(
        name="get_transcript_excerpt",
        description="按时间窗口取转写原文片段，用于把引用落到具体句子。"
                    "**每轮有总片段预算（默认 30 段）**；先用 search_knowledge 命中窗口，"
                    "再用本工具取窄窗口，不要把整段转写重复拉取。",
        arguments=TranscriptExcerptArgs,
        handler=_transcript_excerpt,
    ),
    ToolSpec(
        name="query_meeting_state",
        description="查询系列/个人的已确认状态：先前决策（decision）、结论（conclusion）、"
                    "待办及完成情况（action_item）、个人偏好（preference）。"
                    "**状态类问题的第一步必须调用本工具**：问「上次定了什么」「最后复述了哪些结论」"
                    "「还有哪些待办」「谁负责」「协作人是谁」「截止时间」「项目现在什么状态」"
                    "时都先用它；"
                    "问某场会时传 meeting_ids，问某系列时传 series_id；"
                    "两者是不同 id，不要混用。问「是已确认还是未确认候选」时传 include_proposed=true，"
                    "候选只能陈述状态、不得当事实。"
                    "再用 search_knowledge 补原文；只返回人工确认过的内容，未确认候选不会返回。",
        arguments=QueryMeetingStateArgs,
        handler=_query_meeting_state,
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
    started = perf_counter()
    with tracing.span("chat.tool", {
        "gen_ai.operation.name": "execute_tool",
        "gen_ai.tool.name": str(name),
        "meetingbot.tool.meeting_scope": (
            None if actor.allowed_meeting_ids is None else len(actor.allowed_meeting_ids)),
    }) as current:
        try:
            parsed = spec.validate(arguments)
            data = await spec.handler(actor, parsed)
            result = {"ok": True, **data}
        except ToolError as error:
            result = {"ok": False, "error": str(error)}
        except Exception as error:  # noqa: BLE001 - 工具边界：任何异常都不得打断对话主流程
            logger.exception("工具 %s 执行失败", name)
            result = {"ok": False, "error": f"工具执行失败：{(str(error) or type(error).__name__)[:300]}"}
        current.set_attribute("meetingbot.tool.ok", bool(result.get("ok")))
        current.set_attribute("meetingbot.tool.elapsed_ms", int((perf_counter() - started) * 1000))
        return result


def tool_result_text(result: dict[str, Any]) -> str:
    """工具结果 → tool message 文本。聊天与 MCP 共用同一种序列化。

    序列化前统一标记 `trust=untrusted` 并扫描指令样式文本：会议原文是数据，
    模型不得把它当指令执行（见 services/prompt_injection）。
    """
    from services import prompt_injection

    return json.dumps(prompt_injection.annotate_result(result), ensure_ascii=False, default=str)


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
            # **录音实际开始时间（墙钟）**：不是上传时间，也不是会议名义时间。
            # 一场会两段录音时，这是"上午/下午"唯一的依据；未标注就是 None。
            "recorded_at": _fmt(row.recorded_at),
            "period": _period_of(row.recorded_at),
            "create_time": _fmt(row.create_time),
            "transcribed_at": _fmt(row.finished_at) or _fmt(row.started_at),
            "duration_minutes": round((row.total_duration_ms or 0) / 60_000, 1) or None,
        } for row in rows],
        "notice": "task_id 可用于 search_knowledge 的 source_ids（只搜这一段录音）、"
                  "也用于 get_transcript_excerpt（取这段录音的原话）。"
                  "recorded_at 是录音实际开始时间；period 是上午/下午/晚上/未标注。"
                  "问「下午那场」时按 period 选 source_ids；period 未标注时不要猜，"
                  "检索所有相关录音并如实说明。引用时用 meeting_id + start_second。",
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


def _keywords_match(item: Any, keywords: list[str]) -> bool:
    """任一关键词出现在条目的渲染文本里就算命中（OR，大小写不敏感）。

    只做**缩小**，不做语义匹配：模型给的关键词来自问题本身，命中与否可解释。
    """
    blob = json.dumps(item, ensure_ascii=False).lower()
    return any(str(keyword).strip().lower() in blob
               for keyword in keywords if str(keyword).strip())


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


def _slot_status(item: Any) -> dict[str, dict[str, Any]]:
    """把类型化槽位的 basis 保留下来，供覆盖/充分性判断。

    `present_output` 只留展示文本；这里在展示之外附一份机器可读状态。
    """
    if not isinstance(item, dict):
        return {}
    status: dict[str, dict[str, Any]] = {}
    for name, value in item.items():
        if isinstance(value, dict) and ("basis" in value or "text" in value):
            status[str(name)] = {
                "basis": value.get("basis"),
                "text": value.get("text"),
            }
    return status


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
        # additive：不改变展示/引用，只给覆盖判断一个可读的槽位状态
        "slot_status": _slot_status(item),
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


def _period_of(value: Any) -> str | None:
    """把录音开始时间归到上午/下午/晚上；未标注返回 None。

    只做**展示与选择依据**，不参与权限或事实判断。用格式化后的本地时间取小时，
    与页面看到的时间一致（避免 UTC/本地时区在边界上差 8 小时）。
    """
    text = format_datetime(value)
    if not text or len(text) < 13:
        return None
    try:
        hour = int(text[11:13])
    except ValueError:
        return None
    if hour < 12:
        return "上午"
    if hour < 18:
        return "下午"
    return "晚上"


__all__ = [
    "Actor", "ToolError", "ToolSpec", "TOOL_SPECS", "TOOLS_BY_NAME",
    "execute_tool", "openai_tools", "tool_result_text",
    "SearchKnowledgeArgs", "ListFactsArgs", "GetMinutesArgs", "TranscriptExcerptArgs",
]
