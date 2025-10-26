
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from tortoise.exceptions import IntegrityError
from tortoise.expressions import Q

from api.meeting import get_accessible_meeting
from api.meeting_material import get_accessible_material, MATERIAL_DIR
from common.auth import get_current_user
from common.exception_handler import CustomException
from common.result import PageInfo, Result
from common.times import format_datetime, now
from models import (
    AiModelConfig,
    Department,
    Meeting,
    MeetingMaterial,
    MeetingMinutes,
    MeetingParticipant,
    TranscriptSegment,
    TranscriptionTask,
    User,
)
from services.speaker_service import suggest_speakers
from services.transcription_service import cancel_transcription, schedule_transcription, reject_overlong_audio

router = APIRouter(prefix="/transcription")


class StartPayload(BaseModel):
    # 弹窗下拉框选择的音频语言代码，不传时按中文处理
    language: str = "zh"


class SegmentPayload(BaseModel):
    # 要修订的片段ID
    id: int
    # 修订后的文本
    content: str


class SpeakerMatchPayload(BaseModel):
    # 当前转写任务ID
    task_id: int
    # 要匹配的说话人标签
    speaker_label: str
    # 匹配到的参会人员ID，为 None 表示解除匹配
    user_id: int | None = None


def format_time_ms(value: int) -> str:
    # 负数按 0 处理，毫秒转成整秒
    total_seconds = max(value, 0) // 1000
    # 拆成时、分、秒，统一补零成两位
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


async def get_accessible_task(task_id: int, current_user: User) -> TranscriptionTask:
    task = await TranscriptionTask.get_or_none(id=task_id)
    if task is None:
        raise CustomException("转写任务不存在")
    # 任务本身不单独管权限，按它所属的会议走第 5 章的权限判断
    await get_accessible_meeting(task.meeting_id, current_user)
    return task


async def task_dict(task: TranscriptionTask) -> dict:
    # 会议、资料、发起人、模型配置四张表各查一次，补齐列表要显示的名称
    meeting = await Meeting.get_or_none(id=task.meeting_id)
    material = await MeetingMaterial.get_or_none(id=task.material_id)
    initiator = await User.get_or_none(id=task.initiator_id)
    config = await AiModelConfig.get_or_none(id=task.model_config_id)
    # 统计这个任务已经写入多少个片段，列表“片段数”列
    segment_count = await TranscriptSegment.filter(task_id=task.id).count()
    return {
        # 详情、重试、生成纪要都用这个ID
        "id": task.id,
        # “生成纪要”跳转时要带上会议ID
        "meeting_id": task.meeting_id,
        # 列表“会议主题”列
        "meeting_title": meeting.title if meeting else None,
        "material_id": task.material_id,
        # 列表“音视频资料”列
        "material_name": material.file_name if material else None,
        "material_type": material.file_type if material else None,
        "model_config_id": task.model_config_id,
        # 详情弹窗“转写模型”
        "model_name": config.model_name if config else None,
        "initiator_id": task.initiator_id,
        # 列表“发起人”列
        "initiator_name": initiator.name if initiator else None,
        "language": task.language,
        # 列表“状态”列，也决定重试和生成纪要按钮是否出现
        "status": task.status,
        # 详情弹窗“进度”一栏的阶段文字
        "stage": task.stage,
        # 列表进度条的百分比
        "progress": task.progress,
        # 列表“重试”列
        "retry_count": task.retry_count,
        # 前端用它和 retry_count 比较，决定重试按钮是否显示
        "max_retry": task.max_retry,
        "total_duration_ms": task.total_duration_ms,
        # 列表“时长”列，由毫秒换算成时分秒
        "total_duration_text": format_time_ms(task.total_duration_ms),
        "full_text": task.full_text,
        # 列表“片段数”列
        "segment_count": segment_count,
        # 详情弹窗“失败原因”
        "error_message": task.error_message,
        "started_at": format_datetime(task.started_at),
        "finished_at": format_datetime(task.finished_at),
        # 列表“提交时间”列
        "create_time": format_datetime(task.create_time),
        "update_time": format_datetime(task.update_time),
    }


async def segment_dict(segment: TranscriptSegment) -> dict:
    # 匹配过参会人员时才查用户，用于返回姓名
    speaker = await User.get_or_none(id=segment.speaker_user_id) if segment.speaker_user_id else None
    # 修订过才查修订人
    reviser = await User.get_or_none(id=segment.revised_by) if segment.revised_by else None
    return {
        # 修订接口按它定位片段
        "id": segment.id,
        "task_id": segment.task_id,
        # 表格第一列的“#”
        "segment_no": segment.segment_no,
        "start_ms": segment.start_ms,
        "end_ms": segment.end_ms,
        # 表格“时间”列，由起止毫秒拼成时分秒区间
        "time_range": f"{format_time_ms(segment.start_ms)} - {format_time_ms(segment.end_ms)}",
        # 模型给的标签，未匹配用户时表格“说话人”列显示它
        "speaker_label": segment.speaker_label,
        "speaker_user_id": segment.speaker_user_id,
        # 匹配过用户时表格“说话人”列优先显示姓名
        "speaker_name": speaker.name if speaker else None,
        # 模型原始文本，修订后仍保留原值
        "original_text": segment.original_text,
        # 表格“转写文本”列，也是修订弹窗的初始值
        "content": segment.content,
        "revised_by": segment.revised_by,
        "reviser_name": reviser.name if reviser else None,
        "revised_time": format_datetime(segment.revised_time),
    }


async def participant_options(meeting_id: int) -> list[dict]:
    # 按第 5 章写入的参会记录取人，顺序和会议详情里的名单一致
    rows = await MeetingParticipant.filter(meeting_id=meeting_id).order_by("id")
    result = []
    for row in rows:
        # 只取仍处于正常状态的账号，停用账号不出现在候选里
        user = await User.get_or_none(id=row.user_id, status="NORMAL")
        if user is None:
            continue
        # 补上部门名称，让下拉框能显示“姓名 / 部门 / 职位”
        department = await Department.get_or_none(id=user.department_id) if user.department_id else None
        result.append(
            {
                # 下拉框选项的 value，最终写进 transcript_segment.speaker_user_id
                "id": user.id,
                "name": user.name,
                "department_name": department.name if department else None,
                "position": user.position,
            }
        )
    return result


async def refresh_full_text(task_id: int):
    # 按时间轴顺序取出全部片段
    segments = await TranscriptSegment.filter(task_id=task_id).order_by("segment_no")
    await TranscriptionTask.filter(id=task_id).update(
        # 用修订后的 content 逐行拼接，覆盖第 8 章转写完成时写入的 full_text
        full_text="\n".join(segment.content for segment in segments)
    )


@router.post("/start/{material_id}")
async def start(
    # material_id 来自地址里的路径参数，就是第 6 章那一行资料的ID
    material_id: int,
    payload: StartPayload,
    current_user: User = Depends(get_current_user),
):
    # 复用第 6 章的资料权限入口，内部再走一次第 5 章的会议权限判断
    material = await get_accessible_material(material_id, current_user)
    # 只有录音和视频能转写，和页面上按钮的显示条件一致
    if material.file_type not in {"AUDIO", "VIDEO"}:
        raise CustomException("只有录音和视频资料可以发起转写")
    # 页面下拉给出的取值；空字符串表示交给模型自动识别，不往转写请求里放 language 参数
    if payload.language and payload.language.lower() not in {"zh", "en", "ja", "ko"}:
        raise CustomException("语言代码不正确")
    # 取第 7 章配置的、当前启用的转写模型；多条时取ID最大的那条
    config = await AiModelConfig.filter(
        model_type="TRANSCRIPTION", enabled=True
    ).order_by("-id").first()
    # 没有启用配置或 Key 为空时不建任务，避免建了立刻失败
    if config is None or not config.api_key:
        raise CustomException("请先由管理员启用转写模型配置")
    # 时长预检（fail-open：ffprobe 读不到时长时放行，由任务内同一守卫兜底）：
    # 超限音频在建任务前拒绝，用户立即得到可读原因，不产生失败任务行和重试按钮
    try:
        reject_overlong_audio(MATERIAL_DIR / material.storage_name)
    except RuntimeError as error:
        raise CustomException(str(error))
    # 一份资料只能有一个任务，先看有没有建过
    existed = await TranscriptionTask.get_or_none(material_id=material.id)
    if existed:
        # 失败的任务要走重试入口，那里会累加重试次数
        if existed.status == "FAILED":
            raise CustomException("该资料转写失败，请在转写任务页重试")
        # 已完成的不重复转写
        if existed.status == "SUCCEEDED":
            raise CustomException("该资料已完成转写")
        # 剩下的是 PENDING 或 PROCESSING，重新排一次队，防止服务重启后漏掉
        schedule_transcription(existed.id)
        return Result.success(await task_dict(existed))
    try:
        task = await TranscriptionTask.create(
            # 会议ID从资料记录上带过来，不接受前端传
            meeting_id=material.meeting_id,
            material_id=material.id,
            # 记下本次用的模型配置，execute_transcription 执行时按它读取地址和 Key，第 7 章删除配置时也按它检查占用
            model_config_id=config.id,
            # 发起人取自 Token 里的当前用户
            initiator_id=current_user.id,
            language=payload.language.lower() if payload.language else "",
            status="PENDING",
            stage="等待执行",
            progress=0,
        )
    except IntegrityError:
        # 两个人同时点“发起转写”时，唯一索引挡住第二条，这里把已存在的那条取出来
        task = await TranscriptionTask.get(material_id=material.id)
    # 建好任务立刻排队，接口本身不等转写完成
    schedule_transcription(task.id)
    return Result.success(await task_dict(task))


@router.put("/forceFail/{task_id}")
async def force_fail(task_id: int, current_user: User = Depends(get_current_user)):
    """卡死任务的出路：后台协程已丢失（进程崩溃且恢复失效）时，把停在
    PENDING/PROCESSING 的任务强制置为 FAILED，让重试/删除按钮重新可用。
    条件更新保证真在跑的任务不受影响（在跑协程会继续写进度，此处抢占失败）。"""
    task = await get_accessible_task(task_id, current_user)
    if current_user.role != "ADMIN" and task.initiator_id != current_user.id:
        raise CustomException("只有管理员或发起人可以强制结束", "403")
    claimed = await TranscriptionTask.filter(
        id=task.id, status__in=["PENDING", "PROCESSING"],
    ).update(status="FAILED", stage="已被强制结束",
             error_message="任务长时间无进展，被强制结束；可点击重试重新排队",
             finished_at=now())
    if not claimed:
        raise CustomException("任务状态已经变化，请刷新查看")
    # 若协程其实还活着，把它停掉，避免它稍后把状态写回去
    cancel_transcription(task.id)
    return Result.success()


@router.put("/retry/{task_id}")
async def retry(task_id: int, current_user: User = Depends(get_current_user)):
    # 按任务ID取任务，内部走会议权限判断
    task = await get_accessible_task(task_id, current_user)
    # 只有失败任务能重试，和页面按钮的显示条件一致
    if task.status != "FAILED":
        raise CustomException("只有失败的转写任务可以重试")
    # 达到上限后不再重试，避免反复消耗模型调用
    if task.retry_count >= task.max_retry:
        raise CustomException("该转写任务已达到最大重试次数")
    # 重新取一次当前启用的转写配置。上次失败可能就是因为配置有问题，管理员改过之后这里取到新的
    config = await AiModelConfig.filter(
        model_type="TRANSCRIPTION", enabled=True
    ).order_by("-id").first()
    if config is None or not config.api_key:
        raise CustomException("请先由管理员启用转写模型配置")
    # 带 status="FAILED" 条件更新，防止两个人同时点重试时重复累加次数
    await TranscriptionTask.filter(id=task.id, status="FAILED").update(
        # 换成当前启用的配置
        model_config_id=config.id,
        # 回到等待执行状态，重新排队
        status="PENDING",
        stage="等待重试",
        progress=0,
        # 重试次数加一，达到 max_retry 后按钮消失
        retry_count=task.retry_count + 1,
        # 清掉上次的失败原因和时间
        error_message=None,
        started_at=None,
        finished_at=None,
    )
    schedule_transcription(task.id)
    return Result.success()


@router.delete("/delete/{task_id}")
async def delete(task_id: int, current_user: User = Depends(get_current_user)):
    """删除转写任务。由它生成的纪要一并删除，正在执行的任务不允许删。"""
    # task_id 来自请求地址；先按任务所属会议校验访问权限
    task = await get_accessible_task(task_id, current_user)
    # 能看到这场会议还不够，只有管理员或发起这次转写的人才能删
    if current_user.role != "ADMIN" and task.initiator_id != current_user.id:
        raise CustomException("只有管理员或转写发起人可以删除", "403")
    # 和页面按钮的显示条件一致：等待执行或转写中的任务不能删
    if task.status in {"PENDING", "PROCESSING"}:
        raise CustomException("转写正在执行中，请等执行结束后再删除")

    # 在函数内部导入纪要模块的删除函数
    from api.meeting_minutes import delete_minutes
    # 这个转写生成的纪要要跟着删掉，否则纪要会指向一个不存在的转写任务；
    # delete_minutes 先检查管理员或纪要创建人权限，再删除 meeting_minutes 记录
    minutes = await MeetingMinutes.get_or_none(source_task_id=task.id)
    if minutes is not None:
        # 纪要还在生成中时，后台协程正在写这条记录，不允许删除
        if minutes.status == "GENERATING":
            raise CustomException("该转写的纪要正在生成中，请等生成结束后再删除")
        await delete_minutes(minutes, current_user)

    # 删除这个任务写入的全部时间轴片段
    await TranscriptSegment.filter(task_id=task.id).delete()
    # 最后删除 transcription_task 记录，前端收到 code '200' 后刷新列表
    await TranscriptionTask.filter(id=task.id).delete()
    return Result.success()


@router.get("/selectPage")
async def select_page(
    # meetingId 来自会议下拉框，None 表示不限会议
    meetingId: int | None = None,
    # status 来自任务状态下拉框
    status: str = "",
    # fileName 来自资料文件名输入框
    fileName: str = "",
    pageNum: int = 1,
    pageSize: int = 10,
    current_user: User = Depends(get_current_user),
):
    query = TranscriptionTask.all()
    # 员工先把范围收窄到自己能看到的会议，管理员不加这层限制
    if current_user.role != "ADMIN":
        participant_meeting_ids = await MeetingParticipant.filter(
            user_id=current_user.id
        ).values_list("meeting_id", flat=True)
        # 口径和第 5 章的会议列表一致：自己创建、自己主持或自己参与
        accessible_ids = await Meeting.filter(
            Q(creator_id=current_user.id)
            | Q(host_id=current_user.id)
            | Q(id__in=participant_meeting_ids)
        ).values_list("id", flat=True)
        query = query.filter(meeting_id__in=accessible_ids)
    # 指定了会议时再单独校验一次这场会议的访问权限
    if meetingId is not None:
        await get_accessible_meeting(meetingId, current_user)
        query = query.filter(meeting_id=meetingId)
    if status:
        query = query.filter(status=status)
    # 文件名存在 meeting_material 表里，先按名字查出资料ID，再用它过滤任务
    if fileName:
        material_ids = await MeetingMaterial.filter(
            file_name__contains=fileName
        ).values_list("id", flat=True)
        query = query.filter(material_id__in=material_ids)
    # 按创建时间倒序，最新提交的任务排最前面
    tasks = await query.order_by("-create_time", "-id").offset(
        (pageNum - 1) * pageSize
    ).limit(pageSize)
    total = await query.count()
    return Result.success(
        PageInfo(total=total, list=[await task_dict(task) for task in tasks])
    )


@router.get("/selectById/{task_id}")
async def select_by_id(task_id: int, current_user: User = Depends(get_current_user)):
    # 按任务ID取任务，内部走第 5 章的会议权限判断
    task = await get_accessible_task(task_id, current_user)
    # 先放第 8 章讲过的任务基本信息
    data = await task_dict(task)
    # 按时间轴顺序号升序取全部片段，页面表格直接按这个顺序渲染
    segments = await TranscriptSegment.filter(task_id=task.id).order_by("segment_no")
    data["segments"] = [await segment_dict(segment) for segment in segments]
    # 该会议的参会人员，作为说话人匹配下拉框的候选
    data["participants"] = await participant_options(task.meeting_id)
    # 把片段按说话人标签归并成一张汇总表
    speakers = {}
    for segment in segments:
        # 标签为空的片段统一归到 A 这个标签下
        label = segment.speaker_label or "A"
        # 每个标签只在第一次遇到时建一条汇总记录
        if label not in speakers:
            # 这个标签已经匹配过用户时，把姓名一并带出来
            speaker = await User.get_or_none(id=segment.speaker_user_id) if segment.speaker_user_id else None
            speakers[label] = {
                # 汇总表“说话人标签”列
                "speaker_label": label,
                # 汇总表下拉框的当前值，没匹配时是 None
                "speaker_user_id": segment.speaker_user_id,
                "speaker_name": speaker.name if speaker else None,
                "segment_count": 0,
            }
        # 同一标签每出现一个片段就加一，得到“发言片段数”
        speakers[label]["segment_count"] += 1
    data["speakers"] = list(speakers.values())
    return Result.success(data)


@router.put("/updateSegment")
async def update_segment(
    payload: SegmentPayload,
    current_user: User = Depends(get_current_user),
):
    segment = await TranscriptSegment.get_or_none(id=payload.id)
    if segment is None:
        raise CustomException("转写片段不存在")
    # 片段本身不单独管权限，按它所属的任务走会议权限判断
    task = await get_accessible_task(segment.task_id, current_user)
    # 只有完成的转写才允许修订，执行中改会被后续写入覆盖
    if task.status != "SUCCEEDED":
        raise CustomException("只有已完成的转写可以修订")
    # 去掉首尾空白后不能为空，和页面 inputPattern 的口径一致
    content = payload.content.strip()
    if not content:
        raise CustomException("转写文本不能为空")
    await TranscriptSegment.filter(id=segment.id).update(
        # 只改 content，original_text 保持模型的原始输出不变
        content=content,
        # 记下是谁改的，取自 Token 里的当前用户
        revised_by=current_user.id,
        # 记下改的时间，用东八区当前时间
        revised_time=now(),
    )
    # 片段变了，任务上的整段文本也要跟着重拼
    await refresh_full_text(task.id)
    return Result.success()


@router.put("/matchSpeaker")
async def match_speaker(
    payload: SpeakerMatchPayload,
    current_user: User = Depends(get_current_user),
):
    # 按任务ID取任务，内部走会议权限判断
    task = await get_accessible_task(payload.task_id, current_user)
    # 只有转写完成的任务才有片段可匹配，和页面上这块内容的渲染条件一致
    if task.status != "SUCCEEDED":
        raise CustomException("只有已完成的转写可以匹配说话人")
    # 圈定这个任务下、这个标签的全部片段
    label_query = TranscriptSegment.filter(
        task_id=task.id, speaker_label=payload.speaker_label
    )
    # 标签对不上说明前端传了不存在的值，直接拒绝
    if not await label_query.exists():
        raise CustomException("说话人标签不存在")
    # 传了用户时校验他确实是这场会议的参会人员，不能匹配到无关员工
    if payload.user_id is not None:
        if not await MeetingParticipant.filter(
            meeting_id=task.meeting_id, user_id=payload.user_id
        ).exists():
            raise CustomException("只能匹配该会议的参会人员")
    # 整批更新：同一标签下的所有片段一次性写入同一个 speaker_user_id
    # user_id 为 None 时相当于把这批片段的匹配清空
    await label_query.update(speaker_user_id=payload.user_id)
    return Result.success()


@router.post("/suggestSpeakers/{task_id}")
async def suggest_speaker_match(task_id: int, current_user: User = Depends(get_current_user)):
    """让模型读一段转写，猜每个说话人标签对应哪位参会人员。

    模型只给建议，不写库。人在页面上看过理由再决定采不采纳，采纳后走的还是 matchSpeaker。
    """
    # 复用本章的权限函数，看不到这个任务的人也拿不到建议
    task = await get_accessible_task(task_id, current_user)
    if task.status != "SUCCEEDED":
        raise CustomException("只有已完成的转写可以做说话人建议匹配")
    try:
        suggestions = await suggest_speakers(task.id, task.meeting_id, current_user.id)
    except Exception as error:
        # 服务层抛的是普通异常，转成 CustomException 让前端拿到中文提示而不是 500
        raise CustomException(str(error) or type(error).__name__)
    return Result.success(suggestions)
