import asyncio
import json
from uuid import uuid4

from common.auth import get_current_user
from common.exception_handler import CustomException
from common.result import PageInfo, Result
from common.times import format_datetime, now
from fastapi import APIRouter, Depends
from models import (
    AgentRun,
    AgentStep,
    AiModelConfig,
    Meeting,
    MeetingMinutes,
    PromptTemplate,
    User,
)
from pydantic import BaseModel
from services.agent_service import cancel_agent_run, schedule_agent_run
from services.minutes_service import material_name_of_minutes
from starlette.responses import StreamingResponse

from api.meeting_minutes import get_accessible_minutes

router = APIRouter(prefix="/agent")

class RunPayload(BaseModel):
    # 纪要页面「Agent自检」按钮所在行的纪要主键
    minutes_id: int
    # 一轮等于「审查一次 + 按问题改一版」。默认两轮：第一轮改完之后，
    # 第二轮的审查负责复核这一版改得对不对，模型判定通过就当场结束
    max_rounds: int = 2


class ApplyPayload(BaseModel):
    # 详情弹窗里点「用修订稿覆盖原纪要」传 true，点「放弃这次修订」传 false
    approved: bool
    # 放弃时输入框里填的原因；覆盖时前端传 null
    reason: str | None = None

async def get_accessible_run(run_id: int, current_user: User) -> AgentRun:
    """详情、确认、中断、恢复、删除和事件流接口都先经过这里取运行记录。"""
    run = await AgentRun.get_or_none(id=run_id)
    if run is None:
        raise CustomException("自检运行不存在")
    # 管理员可以操作全部运行，员工只能操作自己发起的运行
    if current_user.role != "ADMIN" and run.user_id != current_user.id:
        raise CustomException("没有自检运行访问权限", "403")
    return run


def step_dict(step: AgentStep) -> dict:
    """把一条 agent_step 转成详情弹窗时间线的一个节点。"""
    return {
        "id": step.id,
        "step_no": step.step_no,
        "round_no": step.round_no,
        # REVIEW、REFINE、FINAL，前端 stepTypeText 翻成中文
        "step_type": step.step_type,
        # 开步骤时写入的描述，例如「第1轮：审查是否可落实」
        "decision_summary": step.decision_summary,
        # 步骤结束时写入的结论文本
        "observation_summary": step.observation_summary,
        # 审查步里是 passed、score 和 issues，前端按它渲染问题卡片
        "payload": step.payload_json,
        # RUNNING、SUCCEEDED、FAILED，决定时间线节点颜色
        "status": step.status,
        "elapsed_ms": step.elapsed_ms,
        "error_message": step.error_message,
        "create_time": format_datetime(step.create_time),
        "finish_time": format_datetime(step.finish_time),
    }


async def run_dict(run: AgentRun, with_steps: bool = False) -> dict:
    """列表只要运行本身的字段；详情、发起和事件流传 with_steps=True，额外带上步骤和修订稿。"""
    # 发起人姓名、会议主题和模型名都不在 agent_run 表里，按外键各查一次
    user = await User.get_or_none(id=run.user_id)
    meeting = await Meeting.get_or_none(id=run.meeting_id)
    model = await AiModelConfig.get_or_none(id=run.model_config_id)
    # 确认或放弃过修订稿才有 applied_by
    applier = await User.get_or_none(id=run.applied_by) if run.applied_by else None
    data = {
        "id": run.id,
        "run_no": run.run_no,
        "minutes_id": run.minutes_id,
        "meeting_id": run.meeting_id,
        "meeting_title": meeting.title if meeting else None,
        # 这次自检的是哪份录音，光有会议主题分不清同一场会的两个素材
        "material_name": await material_name_of_minutes(run.minutes_id),
        "user_id": run.user_id,
        "user_name": user.name if user else None,
        "model_name": model.model_name if model else None,
        "status": run.status,
        # 页面列表的进度条和详情弹窗的当前阶段读这两个字段
        "stage": run.stage,
        "progress": run.progress,
        # 列表「轮次」列和详情里的「轮次」都显示成 current_round / max_rounds
        "current_round": run.current_round,
        "max_rounds": run.max_rounds,
        # 最后一轮审查的判定、得分和问题，列表「得分」「发现问题」两列读 score 和 issue_count
        "passed": run.passed,
        "score": run.score,
        "issue_count": run.issue_count,
        "issues": run.review_json or [],
        # 只返回有没有修订稿，修订稿内容在 with_steps 为真时通过 revision 返回
        "has_revision": run.revised_json is not None,
        # 人工确认或放弃修订稿时写入的处理人、处理时间和放弃原因
        "applied_by": run.applied_by,
        "applier_name": applier.name if applier else None,
        "applied_time": format_datetime(run.applied_time),
        "reject_reason": run.reject_reason,
        # 汇总结论和失败原因，详情弹窗按条件展示
        "final_result": run.final_result,
        "error_message": run.error_message,
        # 三个时间字段格式化成字符串，列表「开始时间」列读 started_at
        "started_at": format_datetime(run.started_at),
        "finished_at": format_datetime(run.finished_at),
        "update_time": format_datetime(run.update_time),
    }
    if with_steps:
        # 时间线按步骤号从小到大排列
        steps = await AgentStep.filter(run_id=run.id).order_by("step_no")
        data["steps"] = [step_dict(step) for step in steps]
        # 修订稿预览区读 revision，为 None 时不显示
        data["revision"] = run.revised_json
    return data


@router.post("/start")
async def start(payload: RunPayload, current_user: User = Depends(get_current_user)):
    # 复用纪要模块的权限函数：纪要不存在会抛异常，没有这场会议的访问权限也会被挡住
    minutes = await get_accessible_minutes(payload.minutes_id, current_user)
    if minutes.status not in {"DRAFT", "CONFIRMED"}:
        raise CustomException("只有已生成的会议纪要可以发起自检")
    if payload.max_rounds < 1 or payload.max_rounds > 5:
        raise CustomException("最大自检轮数必须在1至5之间")
    # 同一份纪要同时只允许跑一次自检，否则两次运行会各自改出一版修订稿
    if await AgentRun.filter(
        minutes_id=minutes.id, status__in=["RUNNING", "WAITING_CONFIRMATION"]
    ).exists():
        raise CustomException("该纪要已有自检在运行或等待确认，请先处理")
    # 取当前启用的自检模型，整次运行都用这一条配置
    config = await AiModelConfig.filter(model_type="AGENT", enabled=True).order_by("-id").first()
    if config is None or not config.api_key:
        raise CustomException("请先由管理员启用纪要自检模型配置")
    # 审查和重写两条模板缺一不可，缺了就别开始跑，免得跑到一半才失败
    for code in ("MINUTES_REVIEW", "MINUTES_REFINE"):
        if not await PromptTemplate.filter(code=code, enabled=True).exists():
            raise CustomException("纪要自检Prompt未启用：" + code)
    run = await AgentRun.create(
        # 运行编号用 AGR 前缀加一段十六进制随机串，页面表格第一列展示它
        run_no="AGR" + uuid4().hex.upper(),
        minutes_id=minutes.id,
        # meeting_id 从纪要记录上带过来，列表页靠它查会议主题
        meeting_id=minutes.meeting_id,
        # 发起人取自 Token 解析出的当前用户，员工列表按它过滤
        user_id=current_user.id,
        model_config_id=config.id,
        status="RUNNING",
        max_rounds=payload.max_rounds,
    )
    # 反思环放到后台任务里跑，接口不等它跑完就返回，页面靠 SSE 看进度
    schedule_agent_run(run.id)
    return Result.success(await run_dict(run, True))

@router.get("/selectPage")
async def select_page(
    # 运行状态下拉框的值，清空时为空字符串
    status: str = "",
    # 会议主题输入框的值
    meeting_title: str = "",
    pageNum: int = 1,
    pageSize: int = 10,
    current_user: User = Depends(get_current_user),
):
    query = AgentRun.all()
    # 员工只能看自己发起的自检，管理员看全部
    if current_user.role != "ADMIN":
        query = query.filter(user_id=current_user.id)
    if status:
        query = query.filter(status=status)
    if meeting_title:
        # agent_run 上只有 meeting_id，先按标题查出会议 ID 集合，再用它过滤运行记录
        meeting_ids = await Meeting.filter(title__contains=meeting_title).values_list("id", flat=True)
        query = query.filter(meeting_id__in=meeting_ids)
    # 最新发起的排在最前面，开始时间相同时按主键倒序保证顺序稳定
    rows = await query.order_by("-started_at", "-id").offset((pageNum - 1) * pageSize).limit(pageSize)
    # total 交给分页控件，list 填进表格
    return Result.success(PageInfo(total=await query.count(), list=[await run_dict(row) for row in rows]))

@router.get("/selectById/{run_id}")
async def select_by_id(run_id: int, current_user: User = Depends(get_current_user)):
    # 详情按钮传运行主键，先过权限，再带上步骤时间线和修订稿返回
    return Result.success(await run_dict(await get_accessible_run(run_id, current_user), True))

@router.get("/selectByMinutes/{minutes_id}")
async def select_by_minutes(minutes_id: int, current_user: User = Depends(get_current_user)):
    """纪要页面用它查最近一次自检，用来决定按钮显示成发起还是查看。"""
    await get_accessible_minutes(minutes_id, current_user)
    run = await AgentRun.filter(minutes_id=minutes_id).order_by("-id").first()
    return Result.success(await run_dict(run, True) if run else None)

@router.put("/apply/{run_id}")
async def apply(run_id: int, payload: ApplyPayload, current_user: User = Depends(get_current_user)):
    """人工确认环节：修订稿写不写回纪要，由人决定，模型自己不动纪要。"""
    run = await get_accessible_run(run_id, current_user)
    # 只有停在等待确认的运行才能处理，已采纳、已放弃的运行再点会被挡下
    if run.status != "WAITING_CONFIRMATION":
        raise CustomException("该自检运行不在等待确认状态")
    if run.revised_json is None:
        raise CustomException("该自检运行没有修订稿")
    minutes = await MeetingMinutes.get_or_none(id=run.minutes_id)
    if minutes is None:
        raise CustomException("会议纪要不存在")
    # 写回改的是纪要内容，所以按纪要创建人判断，不按运行发起人判断
    if current_user.role != "ADMIN" and minutes.created_by != current_user.id:
        raise CustomException("只有管理员或纪要创建人可以确认修订", "403")
    if not payload.approved:
        # 更新条件带上 WAITING_CONFIRMATION，返回受影响行数；两人同时处理时后一个人拿到 0
        claimed = await AgentRun.filter(id=run.id, status="WAITING_CONFIRMATION").update(
            status="REJECTED",
            # 没填原因时写默认文案，最多保存 500 字
            reject_reason=(payload.reason or "人工放弃本次修订").strip()[:500],
            applied_by=current_user.id,
            applied_time=now(),
        )
        if not claimed:
            raise CustomException("自检运行状态已经变化")
        # 放弃时纪要一个字都不动，修订稿留在 revised_json 里可以回看
        return Result.success()
    # 已确认归档的纪要不允许被覆盖，要改先去纪要页取消确认退回草稿
    if minutes.status == "CONFIRMED":
        raise CustomException("纪要已确认归档，不能再覆盖，请先取消确认")
    revision = run.revised_json
    # 先把运行抢占成 SUCCEEDED，抢占成功才写纪要，避免同一份修订稿被写两次
    claimed = await AgentRun.filter(id=run.id, status="WAITING_CONFIRMATION").update(
        status="SUCCEEDED", applied_by=current_user.id, applied_time=now()
    )
    if not claimed:
        raise CustomException("自检运行状态已经变化")
    # 修订稿的六个键和 meeting_minutes 的六个内容字段一一对应，整体覆盖
    await MeetingMinutes.filter(id=minutes.id).update(
        summary=revision.get("summary") or "",
        topics_json=revision.get("topics") or [],
        viewpoints_json=revision.get("viewpoints") or [],
        decisions_json=revision.get("decisions") or [],
        pending_items_json=revision.get("pending_items") or [],
        risks_json=revision.get("risks") or [],
    )
    return Result.success()

@router.put("/interrupt/{run_id}")
async def interrupt(run_id: int, current_user: User = Depends(get_current_user)):
    run = await get_accessible_run(run_id, current_user)
    if run.status != "RUNNING":
        raise CustomException("只有运行中的自检可以中断")
    # 带上 RUNNING 条件更新，后台任务恰好在这一刻跑完时返回 0，接口报状态已变化
    updated = await AgentRun.filter(id=run.id, status="RUNNING").update(status="INTERRUPTED", stage="已中断")
    if not updated:
        raise CustomException("自检运行状态已经变化")
    # 状态改完再取消后台任务，任务收到 CancelledError 时库里已经是 INTERRUPTED
    cancel_agent_run(run.id)
    return Result.success()

@router.put("/resume/{run_id}")
async def resume(run_id: int, current_user: User = Depends(get_current_user)):
    run = await get_accessible_run(run_id, current_user)
    if run.status != "INTERRUPTED":
        raise CustomException("只有已中断的自检可以恢复")
    # 改回 RUNNING，清掉上次的失败原因和结束时间
    await AgentRun.filter(id=run.id).update(
        status="RUNNING", stage="等待继续", error_message=None, finished_at=None
    )
    # 重新调度后台任务，execute_agent_run 从 current_round 和 revised_json 接着跑
    schedule_agent_run(run.id)
    return Result.success()

@router.delete("/delete/{run_id}")
async def delete(run_id: int, current_user: User = Depends(get_current_user)):
    run = await get_accessible_run(run_id, current_user)
    # 运行中的后台任务还在写 agent_run 和 agent_step，要求先中断
    if run.status == "RUNNING":
        raise CustomException("运行中的自检不能删除，请先中断")
    # 步骤挂在运行下面，先删子表再删主表
    await AgentStep.filter(run_id=run.id).delete()
    await AgentRun.filter(id=run.id).delete()
    return Result.success()

@router.get("/events/{run_id}")
async def events(run_id: int, current_user: User = Depends(get_current_user)):
    """把运行状态和步骤时间线推给页面，省掉前端定时轮询。"""
    # 先过权限，员工连不上别人的运行事件流
    await get_accessible_run(run_id, current_user)

    async def stream():
        # 记住上一次推过去的内容，内容没变就不重复推
        last_payload = ""
        while True:
            run = await AgentRun.get(id=run_id)
            # 每次推完整的运行数据，包含 steps 时间线，前端直接整体替换 data.detail
            payload = json.dumps(await run_dict(run, True), ensure_ascii=False)
            if payload != last_payload:
                # SSE 报文格式：data: 开头，两个换行结尾
                yield "data: " + payload + "\n\n"
                last_payload = payload
            # 进入这五种状态后反思环不会再写库，结束循环，连接随之关闭
            if run.status in {"SUCCEEDED", "FAILED", "INTERRUPTED", "REJECTED", "WAITING_CONFIRMATION"}:
                break
            # 每秒查一次运行记录
            await asyncio.sleep(1)

    return StreamingResponse(
        stream(),
        # 前端 startStream 检查响应头里的这个类型，确认连接的是事件流
        media_type="text/event-stream",
        # 不缓存响应，反向代理也不缓冲，每条报文写出后立即到达浏览器
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
