import asyncio
import json
import logging
from typing import Literal

from pydantic import BaseModel, Field

from models import (
    AiModelConfig,
    Meeting,
    MeetingMaterial,
    MeetingMinutes,
    PromptTemplate,
    TranscriptionTask,
    TranscriptSegment,
    User,
)
from services.model_factory import invoke_json
from settings import MAX_CHUNK_CHARS

logger = logging.getLogger(__name__)

# 单段最多拼接多少字符由 settings.MAX_CHUNK_CHARS 决定（默认 12000），
# transcript_chunks 按这个长度把转写切成多段分别提取
# 纪要ID到后台协程的映射，schedule_minutes 用它判断同一份纪要是否已经在跑
_running_minutes: dict[int, asyncio.Task] = {}
# 纪要生成共用的信号量，第一次调用 get_semaphore 时创建
_minutes_semaphore: asyncio.Semaphore | None = None


class TopicItem(BaseModel):
    # 议题标题和这个议题的小结
    title: str
    summary: str


class ViewpointItem(BaseModel):
    # 说话人姓名来自第 9 章匹配好的 speaker_user_id，没匹配时是说话人标签
    speaker: str
    viewpoint: str


class DecisionItem(BaseModel):
    # 决策内容
    content: str
    # 这条决策的依据，来自转写里的哪段讨论
    basis: str
    # 建议负责人和建议完成时间，模型返回的人名和时间字符串，分别用于决策与待确认事项的负责人和期限展示
    owner_suggestion: str
    deadline_suggestion: str


class PendingItem(BaseModel):
    content: str
    owner_suggestion: str
    deadline_suggestion: str


class RiskItem(BaseModel):
    content: str
    # 风险等级三选一，页面上按它显示不同颜色的标签
    level: Literal["LOW", "MEDIUM", "HIGH"]
    suggestion: str


class MinutesResult(BaseModel):
    # 六个字段和 meeting_minutes 表里的六列一一对应
    summary: str
    topics: list[TopicItem] = Field(default_factory=list)
    viewpoints: list[ViewpointItem] = Field(default_factory=list)
    decisions: list[DecisionItem] = Field(default_factory=list)
    pending_items: list[PendingItem] = Field(default_factory=list)
    risks: list[RiskItem] = Field(default_factory=list)


MINUTES_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "summary": {"type": "string"},
        "topics": {"type": "array", "items": {"type": "object", "additionalProperties": False, "properties": {"title": {"type": "string"}, "summary": {"type": "string"}}, "required": ["title", "summary"]}},
        "viewpoints": {"type": "array", "items": {"type": "object", "additionalProperties": False, "properties": {"speaker": {"type": "string"}, "viewpoint": {"type": "string"}}, "required": ["speaker", "viewpoint"]}},
        "decisions": {"type": "array", "items": {"type": "object", "additionalProperties": False, "properties": {"content": {"type": "string"}, "basis": {"type": "string"}, "owner_suggestion": {"type": "string"}, "deadline_suggestion": {"type": "string"}}, "required": ["content", "basis", "owner_suggestion", "deadline_suggestion"]}},
        "pending_items": {"type": "array", "items": {"type": "object", "additionalProperties": False, "properties": {"content": {"type": "string"}, "owner_suggestion": {"type": "string"}, "deadline_suggestion": {"type": "string"}}, "required": ["content", "owner_suggestion", "deadline_suggestion"]}},
        "risks": {"type": "array", "items": {"type": "object", "additionalProperties": False, "properties": {"content": {"type": "string"}, "level": {"type": "string", "enum": ["LOW", "MEDIUM", "HIGH"]}, "suggestion": {"type": "string"}}, "required": ["content", "level", "suggestion"]}},
    },
    "required": ["summary", "topics", "viewpoints", "decisions", "pending_items", "risks"],
}


def get_semaphore() -> asyncio.Semaphore:
    global _minutes_semaphore
    # 第一次调用时创建容量为 2 的信号量，之后一直复用同一个
    if _minutes_semaphore is None:
        _minutes_semaphore = asyncio.Semaphore(2)
    return _minutes_semaphore


def render_prompt(template: str, **variables) -> str:
    result = template
    # 模板里出现的 {key} 换成传入的字符串，没有传值的变量原样保留
    for key, value in variables.items():
        result = result.replace("{" + key + "}", value)
    return result


async def call_minutes_model(minutes: MeetingMinutes, config: AiModelConfig, system_prompt: str, user_prompt: str, call_type: str) -> MinutesResult:
    """调一次纪要模型，返回按 MinutesResult 校验过的结构化结果。"""
    # biz_id 传纪要ID，第 17 章员工视角按纪要创建人归属；call_type 是 MINUTES_CHUNK 或 MINUTES_MERGE；
    # 传入 MINUTES_SCHEMA 作为 response_format 的结构约束
    return await invoke_json(
        config, system_prompt, user_prompt, MINUTES_SCHEMA, call_type, minutes.id, "纪要模型",
        # 字段缺失或类型不对时，这次调用在日志里记为失败
        validator=MinutesResult.model_validate,
    )


async def transcript_chunks(task_id: int) -> list[str]:
    # 按第 9 章写入的顺序号取全部片段
    segments = await TranscriptSegment.filter(task_id=task_id).order_by("segment_no")
    # 一次查出全部说话人的姓名，避免在循环里逐条查库
    user_ids = {segment.speaker_user_id for segment in segments if segment.speaker_user_id}
    users = {user.id: user.name for user in await User.filter(id__in=user_ids)} if user_ids else {}
    chunks, current = [], []
    current_size = 0
    for segment in segments:
        # 优先用第 9 章匹配好的真实姓名，没匹配就退回说话人标签
        speaker = users.get(segment.speaker_user_id) or segment.speaker_label or "未知说话人"
        # 每行带上秒数和说话人，模型才能归纳出谁在什么时候说了什么
        line = f"[{segment.start_ms // 1000}s][{speaker}] {segment.content}"
        # 加上这一行就超长时，先把攒着的这一段收进结果，再重新开一段
        if current and current_size + len(line) > MAX_CHUNK_CHARS:
            chunks.append("\n".join(current))
            current, current_size = [], 0
        current.append(line)
        current_size += len(line)
    # 循环结束后最后一段还没收进去，这里补上
    if current:
        chunks.append("\n".join(current))
    return chunks


async def execute_minutes(minutes_id: int):
    # 领取式抢占：重复调度或多进程时，只有一个执行者能把这一行从「等待」改成「已领取」
    claimed = await MeetingMinutes.filter(id=minutes_id, status="GENERATING").exclude(stage="已领取").update(stage="已领取")
    if not claimed:
        return
    # 只处理状态是 GENERATING 的记录，中途被删掉或者已经跑完的直接跳过
    minutes = await MeetingMinutes.get_or_none(id=minutes_id, status="GENERATING")
    if minutes is None:
        return
    logger.info("纪要生成开始", extra={"minutes_id": minutes.id, "source_task_id": minutes.source_task_id})
    try:
        meeting = await Meeting.get(id=minutes.meeting_id)
        config = await AiModelConfig.get(id=minutes.model_config_id)
        # 第 10 章的两条模板，分段提取和合并各一条
        chunk_prompt = await PromptTemplate.get(code="MEETING_MINUTES_CHUNK", enabled=True)
        merge_prompt = await PromptTemplate.get(code="MEETING_MINUTES_MERGE", enabled=True)
        chunks = await transcript_chunks(minutes.source_task_id)
        if not chunks:
            raise RuntimeError("该转写任务没有可用文本")
        partials = []
        for index, chunk in enumerate(chunks):
            # 每段开始前更新阶段和进度，页面上的进度条和阶段文字跟着变
            # 10 到 75 这段区间留给分段提取，后面合并再占一段
            await MeetingMinutes.filter(id=minutes.id).update(stage=f"正在分析第{index + 1}/{len(chunks)}段转写", progress=10 + int(index * 65 / len(chunks)))
            partials.append(await call_minutes_model(
                minutes, config, chunk_prompt.system_prompt,
                # 替换模板里的 {meeting_title} 和 {transcript}
                render_prompt(chunk_prompt.user_prompt, meeting_title=meeting.title, transcript=chunk),
                "MINUTES_CHUNK",
            ))
        # 只有一段时它就是最终结果，不用再调一次合并
        result = partials[0]
        if len(partials) > 1:
            await MeetingMinutes.filter(id=minutes.id).update(stage="正在合并分段结果", progress=80)
            # 把每段的结构化结果序列化成 JSON 数组交给合并模板
            partial_json = json.dumps([item.model_dump() for item in partials], ensure_ascii=False)
            result = await call_minutes_model(
                minutes, config, merge_prompt.system_prompt,
                render_prompt(merge_prompt.user_prompt, meeting_title=meeting.title, partials=partial_json),
                "MINUTES_MERGE",
            )
        data = result.model_dump()
        logger.info("纪要生成完成", extra={"minutes_id": minutes.id, "chunks": len(chunks)})
        # 六块内容一次性写回，状态转成 DRAFT，第 12 章从这里接手修订和确认
        await MeetingMinutes.filter(id=minutes.id).update(
            status="DRAFT", stage="纪要已生成", progress=100, summary=data["summary"],
            topics_json=data["topics"], viewpoints_json=data["viewpoints"], decisions_json=data["decisions"],
            pending_items_json=data["pending_items"], risks_json=data["risks"], error_message=None,
        )
    except Exception as error:
        logger.exception("纪要生成失败", extra={"minutes_id": minutes_id})
        # 任何一步失败都落到 FAILED，原因写进 error_message，页面上可以点重试
        await MeetingMinutes.filter(id=minutes.id).update(status="FAILED", stage="纪要生成失败", error_message=str(error)[:2000])


async def run_scheduled(minutes_id: int):
    # 拿到信号量才真正开始跑，拿不到就排队等
    async with get_semaphore():
        await execute_minutes(minutes_id)


def schedule_minutes(minutes_id: int):
    # 同一份纪要已经在跑就直接返回，重试和恢复都会走到这里
    running = _running_minutes.get(minutes_id)
    if running and not running.done():
        return
    # 创建后台协程，generate 和 retry 接口不等待它，请求立刻返回
    task = asyncio.create_task(run_scheduled(minutes_id))
    _running_minutes[minutes_id] = task
    # 任务结束后把自己从字典里摘掉，避免字典一直变大
    task.add_done_callback(lambda _: _running_minutes.pop(minutes_id, None))


async def recover_minutes_tasks() -> int:
    # 上次进程退出时被领取但没跑完的，先退回可领取状态，再统一重新调度
    await MeetingMinutes.filter(status="GENERATING", stage="已领取").update(stage="等待恢复执行")
    ids = list(await MeetingMinutes.filter(status="GENERATING").values_list("id", flat=True))
    for minutes_id in ids:
        schedule_minutes(minutes_id)
    if ids:
        logger.info("纪要生成任务已重新入队", extra={"count": len(ids)})
    return len(ids)


async def material_name_of_minutes(minutes_id: int | None) -> str | None:
    """顺着纪要找到来源转写任务，再找到当初上传的那个音视频文件名。

    一场会议可以传多份录音、生成多份纪要，页面上光有会议主题分不清是哪一份，
    纪要与自检运行列表用这个文件名区分同一会议的不同音视频资料。
    """
    if not minutes_id:
        return None
    minutes = await MeetingMinutes.get_or_none(id=minutes_id)
    if minutes is None:
        return None
    source_task = await TranscriptionTask.get_or_none(id=minutes.source_task_id)
    if source_task is None:
        return None
    material = await MeetingMaterial.get_or_none(id=source_task.material_id)
    return material.file_name if material else None
