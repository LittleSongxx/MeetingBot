from html import escape
from io import BytesIO
from pathlib import Path
from urllib.parse import quote

from common.auth import get_current_user
from common.cooldown import ensure_interval
from common.exception_handler import CustomException
from common.result import PageInfo, Result
from common.times import format_datetime, now
from docx import Document
from fastapi import APIRouter, Depends, Query
from models import (
    ActionItem,
    AgentRun,
    AgentStep,
    AiModelConfig,
    Meeting,
    MeetingMinutes,
    MeetingParticipant,
    PromptTemplate,
    TranscriptionTask,
    User,
)
from pydantic import BaseModel, Field
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from services.evidence_service import merge_evidence
from services.minutes_qa_service import ask as ask_minutes
from services.minutes_service import MinutesResult, material_name_of_minutes, schedule_minutes
from starlette.responses import StreamingResponse
from tortoise.exceptions import IntegrityError
from tortoise.expressions import Q

from api.meeting import get_accessible_meeting

router = APIRouter(prefix="/meetingMinutes")


class EditPayload(MinutesResult):
    # MinutesResult 已经定义了六块内容的结构，这里只补一个要更新哪条记录
    # 页面提交的内容和模型产出走同一套 Pydantic 校验，例如风险级别只能是 LOW、MEDIUM、HIGH
    id: int


class AskMessage(BaseModel):
    role: str
    content: str


class AskPayload(BaseModel):
    question: str = Field(min_length=1, max_length=500)
    history: list[AskMessage] = Field(default_factory=list)


async def get_accessible_minutes(minutes_id: int, current_user: User) -> MeetingMinutes:
    minutes = await MeetingMinutes.get_or_none(id=minutes_id)
    if minutes is None:
        raise CustomException("会议纪要不存在")
    # 纪要本身不带权限规则，跟着它所属的会议走：管理员看全部，员工看自己创建、主持或参与的会议
    await get_accessible_meeting(minutes.meeting_id, current_user)
    return minutes


async def assert_minutes_prompts_enabled():
    """纪要生成要用分段提取和合并两条模板，缺一条就不建生成任务，页面直接提示是哪一条。"""
    # 两条模板逐条检查 prompt_template 表里是否存在已启用的记录
    for code in ("MEETING_MINUTES_CHUNK", "MEETING_MINUTES_MERGE"):
        if not await PromptTemplate.filter(code=code, enabled=True).exists():
            # 例如“会议纪要Prompt未启用：MEETING_MINUTES_MERGE”，前端直接弹出
            raise CustomException("会议纪要Prompt未启用：" + code)


async def minutes_dict(minutes: MeetingMinutes) -> dict:
    # 会议、创建人、确认人、模型配置各查一次，补齐页面要显示的名称
    meeting = await Meeting.get_or_none(id=minutes.meeting_id)
    creator = await User.get_or_none(id=minutes.created_by)
    confirmer = await User.get_or_none(id=minutes.confirmed_by) if minutes.confirmed_by else None
    config = await AiModelConfig.get_or_none(id=minutes.model_config_id)
    return {
        # id 供详情、修订、确认、导出、删除使用；meeting_title 是表格“会议主题”列
        "id": minutes.id, "meeting_id": minutes.meeting_id, "meeting_title": meeting.title if meeting else None,
        # 这份纪要是从哪个音视频文件来的，一场会传了两份录音时靠它区分
        "source_task_id": minutes.source_task_id, "material_name": await material_name_of_minutes(minutes.id),
        # 表格“生成模型”列
        "model_name": config.model_name if config else None,
        # 表格“状态”列和“生成进度”列读 status、progress
        "status": minutes.status, "stage": minutes.stage,
        # summary 和五个 JSON 字段是第 12 章详情弹窗、修订弹窗和导出的内容；JSON 字段为空时返回空数组
        "progress": minutes.progress, "summary": minutes.summary, "topics": minutes.topics_json or [],
        "viewpoints": minutes.viewpoints_json or [], "decisions": minutes.decisions_json or [],
        "pending_items": minutes.pending_items_json or [], "risks": minutes.risks_json or [],
        # created_by 在第 12 章用于删除、取消确认的权限判断，creator_name 是表格“创建人”列
        "created_by": minutes.created_by, "creator_name": creator.name if creator else None,
        # 确认人和确认时间，详情弹窗“确认人”读 confirmer_name
        "confirmed_by": minutes.confirmed_by, "confirmer_name": confirmer.name if confirmer else None,
        # error_message 只有生成失败时有值，详情弹窗显示“失败原因”
        "confirmed_time": format_datetime(minutes.confirmed_time), "error_message": minutes.error_message,
        # 表格“创建时间”列
        "create_time": format_datetime(minutes.create_time), "update_time": format_datetime(minutes.update_time),
    }


@router.post("/generate/{task_id}")
async def generate(task_id: int, current_user: User = Depends(get_current_user)):
    # 路径参数是转写任务ID，只有转写成功的任务才有可用文本
    task = await TranscriptionTask.get_or_none(id=task_id)
    if task is None or task.status != "SUCCEEDED":
        raise CustomException("请选择已完成的转写任务")
    # 顺着转写任务的会议判权限，没有这场会议访问权限的人发起不了
    await get_accessible_meeting(task.meeting_id, current_user)
    ensure_interval(f"minutes-generate:{current_user.id}", 5.0)
    # 取第 7 章配好的 MINUTES 用途模型
    config = await AiModelConfig.filter(model_type="MINUTES", enabled=True).order_by("-id").first()
    if config is None or not config.api_key:
        raise CustomException("请先由管理员启用会议纪要模型配置")
    # 第 10 章的分段提取和合并两条模板必须都已启用，否则提示是哪一条
    await assert_minutes_prompts_enabled()
    # 同一个转写任务已经生成过纪要时直接覆盖那一条，不再累积版本
    minutes = await MeetingMinutes.get_or_none(source_task_id=task.id)
    if minutes is not None:
        # 覆盖已有纪要属于“写”操作：只有管理员或纪要创建人可以重新生成
        if current_user.role != "ADMIN" and minutes.created_by != current_user.id:
            raise CustomException("只有管理员或纪要创建人可以重新生成", "403")
        # 已确认归档的内容不能被静默清空，先取消确认退回草稿再重生成
        if minutes.status == "CONFIRMED":
            raise CustomException("纪要已确认归档，请先取消确认再重新生成")
        # 自检运行还没处理完时不允许重生成，否则旧修订稿会覆盖新内容
        if await AgentRun.filter(
            minutes_id=minutes.id, status__in=["RUNNING", "WAITING_CONFIRMATION"]
        ).exists():
            raise CustomException("该纪要有自检运行未处理，请先完成或放弃自检")
    # 同一场会议同时只允许有一份在生成
    if await MeetingMinutes.filter(meeting_id=task.meeting_id, status="GENERATING").exists():
        raise CustomException("该会议已有正在生成的纪要")
    if minutes is None:
        try:
            minutes = await MeetingMinutes.create(
                meeting_id=task.meeting_id, source_task_id=task.id, model_config_id=config.id,
                status="GENERATING", stage="等待生成", progress=0, created_by=current_user.id,
            )
        except IntegrityError:
            # 两个请求同时首次生成时唯一索引只放行一个，另一个提示刷新即可
            raise CustomException("该转写任务已有纪要，请刷新页面后重试") from None
    else:
        # 旧内容整体作废：删掉未确认的草稿行动项；已生效/已完成的承诺保留
        await ActionItem.filter(minutes_id=minutes.id, status="DRAFT").delete()
        # 旧自检运行与步骤一并清掉（ai_call_log 保留用于审计）
        run_ids = await AgentRun.filter(minutes_id=minutes.id).values_list("id", flat=True)
        if run_ids:
            await AgentStep.filter(run_id__in=run_ids).delete()
            await AgentRun.filter(id__in=run_ids).delete()
        # 旧内容连同确认状态一起清空，重新生成后又是一份待修订的草稿
        await MeetingMinutes.filter(id=minutes.id).update(
            model_config_id=config.id, status="GENERATING", stage="等待生成", progress=0,
            summary=None, topics_json=[], viewpoints_json=[], decisions_json=[],
            pending_items_json=[], risks_json=[], error_message=None,
            confirmed_by=None, confirmed_time=None, created_by=current_user.id,
        )
        minutes = await MeetingMinutes.get(id=minutes.id)
    # 生成放到后台任务里跑，接口不等它跑完，页面靠轮询看进度
    schedule_minutes(minutes.id)
    return Result.success(await minutes_dict(minutes))


async def delete_minutes(minutes: MeetingMinutes, current_user: User):
    """删除一份纪要。"""
    # 权限比查看更严：能看到这场会议不代表能删纪要，只有管理员或发起生成的人可以
    if current_user.role != "ADMIN" and minutes.created_by != current_user.id:
        raise CustomException("只有管理员或纪要创建人可以删除", "403")
    # 自检正在跑时删纪要会让后台任务写到不存在的行上，先拦住
    if await AgentRun.filter(minutes_id=minutes.id, status="RUNNING").exists():
        raise CustomException("该纪要的自检正在运行，请先中断后再删除")
    # 纪要直接关联的行动项一并删除；自检运行与步骤也删，ai_call_log 保留作为审计记录
    await ActionItem.filter(minutes_id=minutes.id).delete()
    run_ids = await AgentRun.filter(minutes_id=minutes.id).values_list("id", flat=True)
    if run_ids:
        await AgentStep.filter(run_id__in=run_ids).delete()
        await AgentRun.filter(id__in=run_ids).delete()
    # 按主键删除这份纪要
    await MeetingMinutes.filter(id=minutes.id).delete()


@router.delete("/delete/{minutes_id}")
async def delete(minutes_id: int, current_user: User = Depends(get_current_user)):
    # 按纪要所属会议校验访问权限
    minutes = await get_accessible_minutes(minutes_id, current_user)
    # GENERATING 状态的纪要后台协程还在执行并更新这条记录，不允许删除
    if minutes.status == "GENERATING":
        raise CustomException("纪要正在生成中，请等生成结束后再删除")
    await delete_minutes(minutes, current_user)
    return Result.success()


@router.put("/retry/{minutes_id}")
async def retry(minutes_id: int, current_user: User = Depends(get_current_user)):
    # 按纪要所属会议校验访问权限
    minutes = await get_accessible_minutes(minutes_id, current_user)
    ensure_interval(f"minutes-retry:{current_user.id}", 3.0)
    # 只有失败的可以重试，GENERATING 状态的纪要后台协程还在执行
    if minutes.status != "FAILED":
        raise CustomException("只有生成失败的纪要可以重试")
    # 重试属于“写”操作：只有管理员或纪要创建人可以重试
    if current_user.role != "ADMIN" and minutes.created_by != current_user.id:
        raise CustomException("只有管理员或纪要创建人可以重试", "403")
    # 模板可能在失败之后被停用，重试前同样检查两条纪要模板
    await assert_minutes_prompts_enabled()
    # 重新取一次当前启用的纪要模型，管理员失败后可能已经换过配置
    config = await AiModelConfig.filter(model_type="MINUTES", enabled=True).order_by("-id").first()
    if config is None or not config.api_key:
        raise CustomException("请先由管理员启用会议纪要模型配置")
    # 状态改回 GENERATING、进度归零、清掉上次的失败原因，然后重新调度
    # 记录本身不变，重试沿用原来的 id 和来源转写任务
    updated = await MeetingMinutes.filter(id=minutes.id, status="FAILED").update(
        status="GENERATING", stage="等待重试", progress=0, error_message=None,
        model_config_id=config.id,
    )
    if not updated:
        raise CustomException("纪要状态已经变化")
    schedule_minutes(minutes.id)
    return Result.success()


@router.get("/selectPage")
async def select_page(meetingId: int | None = None, status: str = "", pageNum: int = Query(1, ge=1), pageSize: int = Query(10, ge=1, le=100), current_user: User = Depends(get_current_user)):
    query = MeetingMinutes.all()
    if meetingId is not None:
        # 指定了会议就先过这场会议的权限，过不了直接抛异常
        await get_accessible_meeting(meetingId, current_user)
        query = query.filter(meeting_id=meetingId)
    elif current_user.role != "ADMIN":
        # 没指定会议时，员工只能看自己创建、主持或参与的会议下的纪要
        participant_meeting_ids = await MeetingParticipant.filter(
            user_id=current_user.id
        ).values_list("meeting_id", flat=True)
        accessible_ids = await Meeting.filter(
            Q(creator_id=current_user.id)
            | Q(host_id=current_user.id)
            | Q(id__in=participant_meeting_ids)
        ).values_list("id", flat=True)
        query = query.filter(meeting_id__in=accessible_ids)
    # 状态下拉框有值时追加等值条件
    if status:
        query = query.filter(status=status)
    # 最新创建的排前面，创建时间相同时按主键倒序保证顺序稳定
    rows = await query.order_by("-create_time", "-id").offset((pageNum - 1) * pageSize).limit(pageSize)
    return Result.success(PageInfo(total=await query.count(), list=[await minutes_dict(row) for row in rows]))


@router.get("/selectById/{minutes_id}")
async def select_by_id(minutes_id: int, current_user: User = Depends(get_current_user)):
    # get_accessible_minutes 先查纪要存不存在，再顺着 meeting_id 走第 5 章的会议权限
    return Result.success(await minutes_dict(await get_accessible_minutes(minutes_id, current_user)))


@router.put("/update")
async def update(payload: EditPayload, current_user: User = Depends(get_current_user)):
    # 按纪要所属会议校验访问权限
    minutes = await get_accessible_minutes(payload.id, current_user)
    # 只有 DRAFT 状态允许写入页面修订结果，已确认的纪要要先取消确认
    if minutes.status != "DRAFT":
        raise CustomException("只有草稿纪要可以编辑")
    # 编辑属于“写”操作：只有管理员或纪要创建人可以改，参会人只能看
    if current_user.role != "ADMIN" and minutes.created_by != current_user.id:
        raise CustomException("只有管理员或纪要创建人可以编辑", "403")
    # exclude id 之后剩下的就是六块内容，字段名和表里的六列一一对应
    data = payload.model_dump(exclude={"id"})
    # 兼容旧前端：某条没带 evidence 但内容没变时，继承数据库里已有的引用
    old_data = {
        "summary": minutes.summary or "",
        "topics": minutes.topics_json or [],
        "viewpoints": minutes.viewpoints_json or [],
        "decisions": minutes.decisions_json or [],
        "pending_items": minutes.pending_items_json or [],
        "risks": minutes.risks_json or [],
    }
    data = merge_evidence(old_data, data)
    await MeetingMinutes.filter(id=minutes.id).update(
        summary=data["summary"], topics_json=data["topics"], viewpoints_json=data["viewpoints"],
        decisions_json=data["decisions"], pending_items_json=data["pending_items"], risks_json=data["risks"],
    )
    return Result.success()


@router.put("/confirm/{minutes_id}")
async def confirm(minutes_id: int, current_user: User = Depends(get_current_user)):
    # 按纪要所属会议校验访问权限
    minutes = await get_accessible_minutes(minutes_id, current_user)
    # 只有草稿可以确认，重复点击第二次会被这条挡住
    if minutes.status != "DRAFT":
        raise CustomException("只有草稿纪要可以确认")
    # 确认属于“写”操作：只有管理员或纪要创建人可以确认
    if current_user.role != "ADMIN" and minutes.created_by != current_user.id:
        raise CustomException("只有管理员或纪要创建人可以确认", "403")
    # 记下是谁在什么时候确认的，详情弹窗的“确认人”读这两个字段
    await MeetingMinutes.filter(id=minutes.id).update(status="CONFIRMED", confirmed_by=current_user.id, confirmed_time=now())
    return Result.success()


async def assert_can_unconfirm(minutes_id: int, current_user: User) -> MeetingMinutes:
    """取消确认的前置校验：纪要可访问、状态是 CONFIRMED、当前用户是管理员或纪要创建人。"""
    # 先查纪要并校验所属会议的访问权限
    minutes = await get_accessible_minutes(minutes_id, current_user)
    # 草稿、生成中、失败的纪要都没有可以撤销的确认
    if minutes.status != "CONFIRMED":
        raise CustomException("只有已确认的纪要可以取消确认")
    # 权限和删除一致：能看到这场会议不代表能动已经归档的纪要
    if current_user.role != "ADMIN" and minutes.created_by != current_user.id:
        raise CustomException("只有管理员或纪要创建人可以取消确认", "403")
    return minutes


@router.put("/unconfirm/{minutes_id}")
async def unconfirm(minutes_id: int, current_user: User = Depends(get_current_user)):
    """把已确认的纪要退回草稿，内容才能继续修订或者被自检的修订稿覆盖。"""
    minutes = await assert_can_unconfirm(minutes_id, current_user)
    # 状态退回 DRAFT，确认人和确认时间一并清掉，等于这次确认从没发生过
    await MeetingMinutes.filter(id=minutes.id).update(status="DRAFT", confirmed_by=None, confirmed_time=None)
    return Result.success()


def section_rows(title: str, items: list, columns: list[str]) -> list[list[str]]:
    # 第一行是标题行，后面每条内容一行
    rows = [[title]]
    for index, item in enumerate(items, 1):
        # 一条内容里的几个字段用竖线拼在一行，前面标上序号
        rows.append([f"{index}. " + " | ".join(str(item.get(column, "")) for column in columns)])
    # 这一块没有任何内容时补一行“无”，避免导出的文档里出现空标题
    if len(rows) == 1:
        rows.append(["无"])
    return rows


def build_docx(data: dict) -> BytesIO:
    document = Document()
    # 0 级标题就是文档标题，用会议主题拼出来
    document.add_heading(data["meeting_title"] + " 会议纪要", 0)
    document.add_paragraph(f"状态：{data['status']}")
    document.add_heading("会议摘要", level=1)
    document.add_paragraph(data["summary"] or "")
    # 五块内容的标题、数据和要导出的字段，顺序和页面详情一致
    sections = [
        ("议题总结", data["topics"], ["title", "summary"]),
        ("发言观点", data["viewpoints"], ["speaker", "viewpoint"]),
        ("会议决策", data["decisions"], ["content", "basis", "owner_suggestion", "deadline_suggestion"]),
        ("待确认事项", data["pending_items"], ["content", "owner_suggestion", "deadline_suggestion"]),
        ("风险与争议点", data["risks"], ["content", "level", "suggestion"]),
    ]
    for title, items, columns in sections:
        document.add_heading(title, level=1)
        # section_rows 的第一行是标题行，标题已经用 add_heading 写过了，所以从第二行开始取
        for row in section_rows(title, items, columns)[1:]:
            document.add_paragraph(row[0])
    # 写进内存而不是磁盘，直接交给 StreamingResponse 返回
    output = BytesIO()
    document.save(output)
    # 指针挪回开头，否则读出来是空的
    output.seek(0)
    return output


def find_chinese_font() -> str:
    # 按顺序找系统里可能存在的中文字体，Windows 上是黑体和微软雅黑，Linux 上是思源黑体
    candidates = [
        # Windows 本地开发
        Path("C:/Windows/Fonts/simhei.ttf"),
        Path("C:/Windows/Fonts/msyh.ttc"),
        # Docker 镜像（Dockerfile 安装 fonts-wqy-zenhei）
        Path("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"),
        Path("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc"),
        # 其它发行版常见的 Noto CJK
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK.ttc"),
    ]
    for path in candidates:
        if path.is_file():
            return str(path)
    # 一个都没找到时导出会变成方块，所以直接抛业务异常告诉用户
    raise CustomException("未找到可用的中文PDF字体")


def build_pdf(data: dict) -> BytesIO:
    font_name = "MinutesChinese"
    # 字体只需要注册一次，第二次导出时直接复用已经注册的
    if font_name not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(font_name, find_chinese_font()))
    output = BytesIO()
    doc = SimpleDocTemplate(output, pagesize=A4, rightMargin=18 * mm, leftMargin=18 * mm, topMargin=16 * mm, bottomMargin=16 * mm)
    styles = getSampleStyleSheet()
    # 标题居中，正文行距 18，两个样式都要指定上面注册的中文字体
    title_style = ParagraphStyle("MinutesTitle", parent=styles["Title"], fontName=font_name, alignment=TA_CENTER)
    body_style = ParagraphStyle("MinutesBody", parent=styles["BodyText"], fontName=font_name, leading=18)
    # escape 把内容里的尖括号转义掉，reportlab 的 Paragraph 会把它当成标签解析
    story = [Paragraph(escape(data["meeting_title"] + " 会议纪要"), title_style), Spacer(1, 8), Paragraph(f"状态：{data['status']}", body_style)]
    sections = [("会议摘要", [[data["summary"] or ""]]), ("议题总结", section_rows("", data["topics"], ["title", "summary"])[1:]), ("发言观点", section_rows("", data["viewpoints"], ["speaker", "viewpoint"])[1:]), ("会议决策", section_rows("", data["decisions"], ["content", "basis", "owner_suggestion", "deadline_suggestion"])[1:]), ("待确认事项", section_rows("", data["pending_items"], ["content", "owner_suggestion", "deadline_suggestion"])[1:]), ("风险与争议点", section_rows("", data["risks"], ["content", "level", "suggestion"])[1:])]
    for title, rows in sections:
        story.extend([Spacer(1, 10), Paragraph(title, body_style)])
        # 每一块用一个单列表格装，内容长了会自动换行和跨页
        table = Table([[Paragraph(escape(row[0]), body_style)] for row in rows], colWidths=[170 * mm])
        table.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.3, colors.grey), ("VALIGN", (0, 0), (-1, -1), "TOP"), ("BACKGROUND", (0, 0), (-1, -1), colors.whitesmoke)]))
        story.append(table)
    doc.build(story)
    output.seek(0)
    return output


@router.post("/ask/{minutes_id}")
async def ask(
    minutes_id: int,
    payload: AskPayload,
    current_user: User = Depends(get_current_user),
):
    """单场会议问答：只读检索本场转写 + 纪要，返回带引用的答案。"""
    minutes = await get_accessible_minutes(minutes_id, current_user)
    if minutes.status not in {"DRAFT", "CONFIRMED"}:
        raise CustomException("纪要尚未生成，不能问答")
    ensure_interval(f"minutes-ask:{current_user.id}", 3.0)
    prompt = await PromptTemplate.get_or_none(code="MINUTES_QA", enabled=True)
    if prompt is None:
        raise CustomException("问答 Prompt 未启用，请检查 MINUTES_QA 模板")
    config = await AiModelConfig.filter(model_type="MINUTES", enabled=True).order_by("-id").first()
    if config is None or not config.api_key:
        raise CustomException("请先由管理员启用会议纪要模型配置")
    history = [message.model_dump() for message in payload.history[-6:]]
    result = await ask_minutes(minutes, config, prompt, payload.question.strip(), history)
    return Result.success(result)


@router.get("/export/{minutes_id}/{file_type}")
async def export_file(minutes_id: int, file_type: str, current_user: User = Depends(get_current_user)):
    # 按纪要所属会议校验访问权限
    minutes = await get_accessible_minutes(minutes_id, current_user)
    # 生成中和失败的纪要没有内容，直接调接口也导不出来
    if minutes.status not in {"DRAFT", "CONFIRMED"}:
        raise CustomException("纪要尚未生成，不能导出")
    # 复用列表和详情用的那个转换函数，导出的内容和页面上看到的完全一致
    data = await minutes_dict(minutes)
    if file_type == "word":
        content, extension, media_type = build_docx(data), "docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    elif file_type == "pdf":
        content, extension, media_type = build_pdf(data), "pdf", "application/pdf"
    else:
        raise CustomException("导出文件类型不正确")
    filename = f"{data['meeting_title']}-会议纪要.{extension}"
    # 文件名里有中文，用 filename*=UTF-8'' 加百分号编码，浏览器才能正确还原
    return StreamingResponse(content, media_type=media_type, headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"})
