import asyncio
import hashlib
import json
from pathlib import Path

from common.prompts import render_prompt
from models import (AiModelConfig, Meeting, MeetingMaterial, MeetingMinutes,
                    PromptTemplate, TranscriptSegment,
                    TranscriptionTask, User)
from services import contracts
from services.model_factory import invoke_json

# 纪要生成的结果缓存（重试去重）：重试时同一模型 + 同一提示词的调用不再重复付费。
# 键 = 模型名 + system + 渲染后的 user prompt 的 sha256——提示词、转写内容任何一项
# 变化，哈希随之变化，不会读到过期结果。
# 单测进程禁用磁盘缓存：保证测试密闭（同一提示词跨测试运行命中缓存会让
# "调用次数"类断言失真）；生产进程不受影响。
import sys as _sys
_CACHE_ENABLED = "unittest" not in _sys.modules
MINUTES_CACHE_DIR = Path(__file__).resolve().parent.parent / "files" / "minutes_cache"

# 六个字段的结构、类型化槽位与来源标注全部由 contracts 的契约表派生。
# 模型约束写在 SCHEMA 里、校验写在 Pydantic 里、展示再写一份的做法会漂移；
# 现在只有一张表（contracts.SLOT_KINDS / BASE_ITEM_FIELDS），三者同源。
_, MinutesResult = contracts.minutes_pydantic_models()

MINUTES_SCHEMA = contracts.minutes_json_schema()


def _call_cache_key(model_name: str, system_prompt: str, user_prompt: str) -> str:
    digest = hashlib.sha256((model_name + "\n" + system_prompt + "\n" + user_prompt)
                            .encode("utf-8")).hexdigest()
    return digest


def _load_call_cache(model_name: str, system_prompt: str, user_prompt: str) -> dict | None:
    path = MINUTES_CACHE_DIR / f"{_call_cache_key(model_name, system_prompt, user_prompt)}.json"
    if not _CACHE_ENABLED or not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _store_call_cache(model_name: str, system_prompt: str, user_prompt: str, dump: dict) -> None:
    if not _CACHE_ENABLED:
        return
    try:
        MINUTES_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path = MINUTES_CACHE_DIR / f"{_call_cache_key(model_name, system_prompt, user_prompt)}.json"
        path.write_text(json.dumps(dump, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


async def call_minutes_model(minutes: MeetingMinutes, config: AiModelConfig, system_prompt: str, user_prompt: str) -> MinutesResult:
    """调一次纪要模型，返回按 MinutesResult 校验过的结构化结果。"""
    # biz_id 传纪要ID，第 17 章员工视角按纪要创建人归属；传入 MINUTES_SCHEMA
    # 作为 response_format 的结构约束
    return await invoke_json(
        config, system_prompt, user_prompt, MINUTES_SCHEMA, "MINUTES", minutes.id, "纪要模型",
        # 字段缺失或者类型不对，这次调用在日志里记为失败
        validator=MinutesResult.model_validate,
    )


async def transcript_text(task_id: int) -> str:
    """整场转写拼成带说话人与时间戳的文本。单遍长上下文：不再切分。"""
    # 按写入的顺序号取全部片段
    segments = await TranscriptSegment.filter(task_id=task_id).order_by("segment_no")
    # 一次查出全部说话人的姓名，避免在循环里逐条查库
    user_ids = {segment.speaker_user_id for segment in segments if segment.speaker_user_id}
    users = {user.id: user.name for user in await User.filter(id__in=user_ids)} if user_ids else {}
    lines = []
    for segment in segments:
        # 优先用匹配好的真实姓名，没匹配就退回说话人标签
        speaker = users.get(segment.speaker_user_id) or segment.speaker_label or "未知说话人"
        # 每行带上秒数和说话人，模型才能归纳出谁在什么时候说了什么
        lines.append(f"[{segment.start_ms // 1000}s][{speaker}] {segment.content}")
    return "\n".join(lines)


async def execute_minutes(minutes_id: int):
    """单遍长上下文生成：整场转写一次调用生成六字段纪要。

    不再 Map-Reduce：当前模型上下文足以容纳 2 小时会议的转写（约 3 万字），
    归因诊断（LEDGER W4-R2）实测合并步无损、损失集中在分段抽取的碎片上，
    单遍从根上消除该损失，且省去 N-1 次调用。
    """
    # 只处理状态是 GENERATING 的记录，中途被删掉或者已经跑完的直接跳过
    minutes = await MeetingMinutes.get_or_none(id=minutes_id, status="GENERATING")
    if minutes is None:
        return
    try:
        meeting = await Meeting.get(id=minutes.meeting_id)
        config = await AiModelConfig.get(id=minutes.model_config_id)
        prompt = await PromptTemplate.get(code="MEETING_MINUTES", enabled=True)
        transcript = await transcript_text(minutes.source_task_id)
        if not transcript.strip():
            raise RuntimeError("该转写任务没有可用文本")
        await MeetingMinutes.filter(id=minutes.id).update(stage="正在生成纪要", progress=30)
        user_prompt = render_prompt(prompt.user_prompt, meeting_title=meeting.title, transcript=transcript)
        # 重试去重：同一模型 + 同一提示词（含整场转写）命缓存则不再调用
        cached = _load_call_cache(config.model_name, prompt.system_prompt, user_prompt)
        if cached is not None:
            result = MinutesResult.model_validate(cached)
        else:
            result = await call_minutes_model(minutes, config, prompt.system_prompt, user_prompt)
            # 先过根因护栏再落缓存：is_unprovided_content_generation 依赖
            # model_fields_set 判"模型原始输出里是否真的给了五个列表字段"，
            # 缓存存 model_dump() 会把默认值补成全字段，让护栏在重试时静默失效
            if contracts.is_unprovided_content_generation(result):
                missing = "、".join(contracts.unprovided_content_fields(result))
                raise ValueError(
                    f"模型返回的不是一份完整纪要：{missing} 在原始输出里全部未提供，"
                    "被默认值补成了空。按失败处理，不写入纪要（可点重试）。"
                )
            _store_call_cache(config.model_name, prompt.system_prompt,
                              user_prompt, result.model_dump())
        data = result.model_dump()
        # 六块内容一次性写回，状态转成 DRAFT，第 12 章从这里接手修订和确认
        await MeetingMinutes.filter(id=minutes.id).update(
            status="DRAFT", stage="纪要已生成", progress=100, summary=data["summary"],
            topics_json=data["topics"], viewpoints_json=data["viewpoints"], decisions_json=data["decisions"],
            pending_items_json=data["pending_items"], risks_json=data["risks"], error_message=None,
        )
    except Exception as error:
        # 任何一步失败都落到 FAILED，原因写进 error_message，页面上可以点重试
        await MeetingMinutes.filter(id=minutes.id).update(status="FAILED", stage="纪要生成失败", error_message=str(error)[:2000])


def schedule_minutes(minutes_id: int):
    """把纪要生成交给 Temporal 执行（同步启动，不等待完成）。"""
    from services import task_queue
    asyncio.get_running_loop().create_task(
        task_queue.enqueue(task_queue.KIND_MINUTES, minutes_id, (minutes_id,)))


async def recover_minutes_tasks():
    """兜底扫尾：重新入队非终态纪要行（崩溃续跑由 Temporal 原生完成）。"""
    from services import task_queue
    ids = await MeetingMinutes.filter(status="GENERATING").values_list("id", flat=True)
    for minutes_id in ids:
        await task_queue.enqueue(task_queue.KIND_MINUTES, minutes_id, (minutes_id,))


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
