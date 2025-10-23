from datetime import datetime, timedelta
from pathlib import Path
from typing import List
from uuid import uuid4

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from tortoise.expressions import Q

from common.auth import get_current_user, require_admin
from common.exception_handler import CustomException
from common.result import PageInfo, Result
from common.times import format_datetime, now, to_local
from tortoise.transactions import in_transaction
from models import (
    AgentRun,
    AgentStep,
    Department,
    Meeting,
    MeetingMaterial,
    MeetingMinutes,
    MeetingParticipant,
    TranscriptSegment,
    TranscriptionTask,
    User,
)

# 会议资料的存放目录，删除会议时要把这些文件一并从磁盘上清掉
MATERIAL_DIR = Path(__file__).resolve().parent.parent / "files" / "meeting_material"

router = APIRouter(prefix="/meeting")


class MeetingPayload(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    # 编辑时回填，新增时不存在
    id: int | None = None
    # 弹窗会议主题输入框
    title: str | None = None
    # 弹窗开始时间选择器，字符串会被 Pydantic 解析成 datetime
    start_time: datetime | None = None
    # 弹窗结束时间选择器
    end_time: datetime | None = None
    # 弹窗会议地点输入框
    location: str | None = None
    # 弹窗会议议程文本域
    agenda: str | None = None
    # 弹窗主持人下拉框
    host_id: int | None = None
    # 弹窗参会人员多选框，默认空列表，避免未传时为 None
    participant_ids: List[int] = Field(default_factory=list)


class MeetingStatusPayload(BaseModel):
    # 目标状态，取值 IN_PROGRESS、FINISHED 或 CANCELLED
    status: str
    # 取消原因，只有取消时才需要
    cancel_reason: str | None = None


def generate_meeting_no() -> str:
    # MTG 前缀 + 东八区当前时间到秒 + 6位随机串，同一秒内创建多场会议也不会撞号
    return "MTG" + now().strftime("%Y%m%d%H%M%S") + uuid4().hex[:6].upper()


async def validate_meeting(payload: MeetingPayload):
    # 主题是页面必填项，后端独立再校验一次
    if not payload.title:
        raise CustomException("请输入会议主题")
    # 起止时间都必须有值
    if payload.start_time is None or payload.end_time is None:
        raise CustomException("请选择会议开始和结束时间")
    # 结束时间必须严格晚于开始时间，相等也不允许
    if payload.end_time <= payload.start_time:
        raise CustomException("会议结束时间必须晚于开始时间")
    # 主持人必选
    if payload.host_id is None:
        raise CustomException("请选择主持人")
    # 主持人必须是存在且未停用的账号
    host = await User.get_or_none(id=payload.host_id, status="NORMAL")
    if host is None:
        raise CustomException("主持人不存在或账号已停用")
    # 参会人员可能被重复提交，先去重
    participant_ids = set(payload.participant_ids)
    if participant_ids:
        # 一次查出这批ID里有多少个是正常账号
        participant_count = await User.filter(id__in=participant_ids, status="NORMAL").count()
        # 数量对不上说明其中有不存在或已停用的账号
        if participant_count != len(participant_ids):
            raise CustomException("参会人员中包含不存在或已停用的账号")


async def has_meeting_access(meeting: Meeting, current_user: User) -> bool:
    # 管理员能访问任何会议
    if current_user.role == "ADMIN":
        return True
    # 创建人和主持人能访问自己的会议
    if meeting.creator_id == current_user.id or meeting.host_id == current_user.id:
        return True
    # 其余情况看这个人在不在参会名单里
    return await MeetingParticipant.filter(meeting_id=meeting.id, user_id=current_user.id).exists()


async def get_accessible_meeting(meeting_id: int, current_user: User) -> Meeting:
    meeting = await Meeting.get_or_none(id=meeting_id)
    # 会议不存在时按业务失败返回，页面弹出提示
    if meeting is None:
        raise CustomException("会议不存在")
    # 没有权限时返回 403，页面同样弹出提示
    if not await has_meeting_access(meeting, current_user):
        raise CustomException("没有会议访问权限", "403")
    return meeting


async def save_participants(meeting: Meeting, participant_ids: List[int]):
    # 页面选中的人员先去重
    selected_ids = set(participant_ids)
    # 创建人和主持人一定在名单里，即使页面上没勾选也补进去
    selected_ids.add(meeting.creator_id)
    selected_ids.add(meeting.host_id)
    # 先读出旧名单，把每个人当前的参会状态记下来
    old_rows = await MeetingParticipant.filter(meeting_id=meeting.id)
    old_status = {row.user_id: row.response_status for row in old_rows}
    # 删旧+插新包在同一事务里：中途失败时旧名单不丢（此前裸 autocommit，
    # 删完旧名单后 bulk_create 一失败，这场会议的参会人就整体消失）
    transaction = in_transaction()
    rows = []
    for user_id in selected_ids:
        # 会议角色由身份自动判定，页面上没有这个选项
        # 一个人同时是创建人和主持人时记为主持人
        if user_id == meeting.host_id:
            participant_role = "HOST"
        elif user_id == meeting.creator_id:
            participant_role = "CREATOR"
        else:
            participant_role = "PARTICIPANT"
        # 老名单里已有的人保留原参会状态，新加进来的人从待确认开始
        response_status = old_status.get(user_id, "PENDING")
        # 创建人和主持人直接记为参加
        if user_id in {meeting.creator_id, meeting.host_id}:
            response_status = "ACCEPTED"
        rows.append(
            MeetingParticipant(
                meeting_id=meeting.id,
                user_id=user_id,
                participant_role=participant_role,
                response_status=response_status,
            )
        )
    # bulk_create 一次插入整份新名单，详情弹窗的参会人员表格读的就是这些记录
    async with transaction:
        await MeetingParticipant.filter(meeting_id=meeting.id).delete()
        await MeetingParticipant.bulk_create(rows)


async def participant_dict(row: MeetingParticipant) -> dict:
    # 查参会人的用户记录
    user = await User.get_or_none(id=row.user_id)
    # 用户挂在部门下时才查部门，用于返回部门名称
    department = await Department.get_or_none(id=user.department_id) if user and user.department_id else None
    return {
        "id": row.id,
        # 前端拿它和当前登录用户比对，也是编辑弹窗回填多选框的值
        "user_id": row.user_id,
        # 详情表格“姓名”列
        "name": user.name if user else None,
        "avatar": user.avatar if user else None,
        # 详情表格“部门”列，来自第 2 章分配的部门
        "department_name": department.name if department else None,
        # 详情表格“职位”列，来自第 2 章维护的职位
        "position": user.position if user else None,
        # 详情表格“会议角色”列
        "participant_role": row.participant_role,
        # 详情表格“参会状态”列
        "response_status": row.response_status,
    }


async def prefetch_meeting_context(meetings: list[Meeting]) -> tuple[dict, dict, dict]:
    """列表页的关联预取：用户、部门、参会记录各查一次，消掉逐行 N+1。"""
    meeting_ids = [m.id for m in meetings]
    user_ids = {m.creator_id for m in meetings} | {m.host_id for m in meetings}
    participant_rows = await MeetingParticipant.filter(
        meeting_id__in=meeting_ids).order_by("id")
    for row in participant_rows:
        user_ids.add(row.user_id)
    users = {u.id: u for u in await User.filter(id__in=user_ids)}
    department_ids = {u.department_id for u in users.values() if u.department_id}
    departments = ({d.id: d for d in await Department.filter(id__in=department_ids)}
                   if department_ids else {})
    by_meeting: dict[int, list] = {}
    for row in participant_rows:
        by_meeting.setdefault(row.meeting_id, []).append(row)
    return users, departments, by_meeting


async def meeting_dict(meeting: Meeting, current_user: User,
                       context: tuple[dict, dict, dict] | None = None) -> dict:
    if context is not None:
        users, departments, by_meeting = context
        creator = users.get(meeting.creator_id)
        host = users.get(meeting.host_id)

        def _participant_dict(row):
            user = users.get(row.user_id)
            department = departments.get(user.department_id) if user and user.department_id else None
            return {
                "id": row.id, "user_id": row.user_id,
                "name": user.name if user else None,
                "avatar": user.avatar if user else None,
                "department_name": department.name if department else None,
                "position": user.position if user else None,
                "participant_role": row.participant_role,
                "response_status": row.response_status,
            }
        participants = [_participant_dict(row) for row in by_meeting.get(meeting.id, [])]
    else:
        # 查创建人和主持人，用于返回姓名给列表和详情展示
        creator = await User.get_or_none(id=meeting.creator_id)
        host = await User.get_or_none(id=meeting.host_id)
        # 按ID升序取全部参会记录，保证详情里的人员顺序稳定
        participant_rows = await MeetingParticipant.filter(meeting_id=meeting.id).order_by("id")
        participants = [await participant_dict(row) for row in participant_rows]
    # 从名单里找出当前登录用户自己那条，用于告诉前端“我在这场会议里是什么角色”
    my_participant = next((item for item in participants if item["user_id"] == current_user.id), None)
    return {
        # 编辑、详情、状态变更、跳资料页都用这个ID
        "id": meeting.id,
        # 列表“会议编号”列
        "meeting_no": meeting.meeting_no,
        # 列表“会议主题”列
        "title": meeting.title,
        # 列表“开始时间”列，统一格式化成东八区的字符串
        "start_time": format_datetime(meeting.start_time),
        # 列表“结束时间”列
        "end_time": format_datetime(meeting.end_time),
        # 列表“地点”列
        "location": meeting.location,
        # 详情弹窗“会议议程”
        "agenda": meeting.agenda,
        # 前端 canEdit 和 canManage 用它判断当前用户是不是创建人
        "creator_id": meeting.creator_id,
        # 详情弹窗“创建人”
        "creator_name": creator.name if creator else None,
        # 前端 canManage 用它判断当前用户是不是主持人；编辑弹窗回填主持人下拉框
        "host_id": meeting.host_id,
        # 列表“主持人”列
        "host_name": host.name if host else None,
        # 列表“状态”列，也决定“开始”“结束”“取消”按钮是否出现
        "status": meeting.status,
        # 详情弹窗的“取消原因”，非取消状态时为 None，那一行不渲染
        "cancel_reason": meeting.cancel_reason,
        # 编辑弹窗回填参会人员多选框
        "participant_ids": [item["user_id"] for item in participants],
        # 详情弹窗的参会人员表格
        "participants": participants,
        # 列表“参会人数”列
        "participant_count": len(participants),
        # 当前用户在这场会议里的角色，没参与时为 None
        "my_meeting_role": my_participant["participant_role"] if my_participant else None,
        "create_time": format_datetime(meeting.create_time),
        "update_time": format_datetime(meeting.update_time),
    }


@router.post("/add")
async def add(payload: MeetingPayload, current_user: User = Depends(get_current_user)):
    # 页面上主持人是必填项；请求里没带 host_id 时，把当前登录的创建人设为主持人
    if payload.host_id is None:
        payload.host_id = current_user.id
    await validate_meeting(payload)
    meeting = await Meeting.create(
        # 会议编号由后端生成，页面上没有这个输入框
        meeting_no=generate_meeting_no(),
        title=payload.title,
        start_time=payload.start_time,
        end_time=payload.end_time,
        location=payload.location,
        agenda=payload.agenda,
        # 创建人取自 Token 里的当前用户，不接受前端传
        creator_id=current_user.id,
        host_id=payload.host_id,
        # 新建会议一律是待开始状态，后续通过状态流转接口推进
        status="SCHEDULED",
    )
    # 会议记录建好后再写参会名单，因为要用到 meeting.id
    await save_participants(meeting, payload.participant_ids)
    # 直接把完整会议对象返回，前端可以拿到生成的编号和整理好的参会名单
    return Result.success(await meeting_dict(meeting, current_user))


@router.put("/update")
async def update(payload: MeetingPayload, current_user: User = Depends(get_current_user)):
    # 编辑必须带上会议ID
    if payload.id is None:
        raise CustomException("会议ID不能为空")
    # 先按数据权限取会议，看不到的会议直接被挡在这里
    meeting = await get_accessible_meeting(payload.id, current_user)
    # 能看到不等于能改：主持人和参会人看得到，但只有管理员和创建人能改
    if current_user.role != "ADMIN" and meeting.creator_id != current_user.id:
        raise CustomException("只有管理员或会议创建人可以编辑会议", "403")
    # 只有 SCHEDULED（待开始）的会议能编辑，进行中、已结束、已取消的会议返回这句提示
    if meeting.status != "SCHEDULED":
        raise CustomException("只有待开始会议可以编辑")
    # 和新增共用同一套字段校验
    await validate_meeting(payload)
    # 只更新页面能改的六个字段，会议编号、创建人和状态都不在其中
    await Meeting.filter(id=meeting.id).update(
        title=payload.title,
        start_time=payload.start_time,
        end_time=payload.end_time,
        location=payload.location,
        agenda=payload.agenda,
        host_id=payload.host_id,
    )
    # 重新读一次，拿到更新后的 host_id，下面重建名单时要用
    meeting = await Meeting.get(id=meeting.id)
    # 按新的参会人员整体重写名单，原有人员的参会状态被保留
    await save_participants(meeting, payload.participant_ids)
    return Result.success(await meeting_dict(meeting, current_user))


@router.put("/changeStatus/{meeting_id}")
async def change_status(
    # meeting_id 来自地址里的路径参数
    meeting_id: int,
    payload: MeetingStatusPayload,
    current_user: User = Depends(get_current_user),
):
    # 先按数据权限取会议
    meeting = await get_accessible_meeting(meeting_id, current_user)
    # 和页面的 canManage 口径一致：管理员、创建人、主持人可以变更状态
    if current_user.role != "ADMIN" and current_user.id not in {meeting.creator_id, meeting.host_id}:
        raise CustomException("只有管理员、创建人或主持人可以变更会议状态", "403")
    # 状态机写成一张表：当前状态可以流转到哪些目标状态
    # FINISHED 和 CANCELLED 不在键里，取不到时得到空集合，任何操作都会被拒绝
    transitions = {
        "SCHEDULED": {"IN_PROGRESS", "CANCELLED"},
        "IN_PROGRESS": {"FINISHED"},
    }
    # 目标状态不在当前状态允许的集合里就拒绝，例如把已结束的会议改回进行中
    if payload.status not in transitions.get(meeting.status, set()):
        raise CustomException("会议状态不能执行该操作")
    if payload.status == "IN_PROGRESS":
        # 开始会议要落在计划时间里，避免会议列表上的起止时间和实际情况脱节
        current = now()
        # 允许比计划开始时间早 10 分钟进场，提前太多说明选错了会议
        if current < to_local(meeting.start_time) - timedelta(minutes=10):
            raise CustomException("还没到会议开始时间，最早可提前 10 分钟开始")
        # 计划结束时间都过了还没开始，说明这场会议需要先改期
        if current > to_local(meeting.end_time):
            raise CustomException("已超过会议计划结束时间，请先修改会议时间再开始")
    # 取消必须带原因，和页面的 inputPattern 校验口径一致
    if payload.status == "CANCELLED" and not payload.cancel_reason:
        raise CustomException("请输入会议取消原因")
    await Meeting.filter(id=meeting.id).update(
        status=payload.status,
        # 只有取消时写入原因；开始和结束时写 None，清掉可能存在的旧值
        cancel_reason=payload.cancel_reason if payload.status == "CANCELLED" else None,
    )
    return Result.success()


@router.get("/deletePreview/{meeting_id}")
async def delete_preview(meeting_id: int, _: User = Depends(require_admin)):
    """删除确认框里展示的连带数据条数，让管理员点确定之前知道会删掉什么。"""
    # meeting_id 来自请求地址；require_admin 只放行管理员，员工调用返回 403
    meeting = await Meeting.get_or_none(id=meeting_id)
    if meeting is None:
        raise CustomException("会议不存在")
    # 转写分段挂在转写任务下，任务被删时分段也要跟着删，这里一并统计出来
    task_ids = await TranscriptionTask.filter(meeting_id=meeting_id).values_list("id", flat=True)
    # Agent 自检运行下面还有一张步骤表
    run_ids = await AgentRun.filter(meeting_id=meeting_id).values_list("id", flat=True)
    # 返回的键名和前端 items 数组里读取的 preview.xxx 一一对应
    return Result.success({
        # 确认框第一行“确定删除会议“……”吗？”里的会议主题
        "title": meeting.title,
        # meeting_participant 表里这场会议的参会记录条数
        "participant_count": await MeetingParticipant.filter(meeting_id=meeting_id).count(),
        # meeting_material 表里这场会议的资料条数，每条对应磁盘上一个文件
        "material_count": await MeetingMaterial.filter(meeting_id=meeting_id).count(),
        # transcription_task 表的条数，直接取上面查出的ID个数
        "transcription_count": len(task_ids),
        # transcript_segment 表同样带 meeting_id，按会议直接计数
        "segment_count": await TranscriptSegment.filter(meeting_id=meeting_id).count(),
        # meeting_minutes 表里这场会议的纪要份数
        "minutes_count": await MeetingMinutes.filter(meeting_id=meeting_id).count(),
        # agent_run 表的条数，直接取上面查出的ID个数
        "agent_run_count": len(run_ids),
    })


@router.delete("/delete/{meeting_id}")
async def delete(meeting_id: int, _: User = Depends(require_admin)):
    """删除会议，同时清掉挂在这场会议下面的全部业务数据和磁盘文件。"""
    # 确认框点“确定删除”后才会请求到这里，require_admin 再次校验管理员身份
    meeting = await Meeting.get_or_none(id=meeting_id)
    if meeting is None:
        raise CustomException("会议不存在")

    # 七张表级联删除包进一个事务：任一步失败整体回滚，不再留"有 step 无 run"式半删状态。
    # 磁盘文件挪到事务成功之后清理——文件删早了而事务回滚，记录会指向不存在的文件。
    materials = await MeetingMaterial.filter(meeting_id=meeting_id)

    async with in_transaction():
        # Agent 的步骤按 run_id 关联，同样先取运行ID
        run_ids = await AgentRun.filter(meeting_id=meeting_id).values_list("id", flat=True)
        if run_ids:
            # agent_step 表没有 meeting_id 字段，只能按 run_id 集合删除
            await AgentStep.filter(run_id__in=run_ids).delete()

        # 其余表都直接带 meeting_id，按会议删一遍
        await AgentRun.filter(meeting_id=meeting_id).delete()
        await MeetingMinutes.filter(meeting_id=meeting_id).delete()
        await TranscriptSegment.filter(meeting_id=meeting_id).delete()
        await TranscriptionTask.filter(meeting_id=meeting_id).delete()
        await MeetingMaterial.filter(meeting_id=meeting_id).delete()
        await MeetingParticipant.filter(meeting_id=meeting_id).delete()
        # 关联数据清干净之后再删会议本身
        await Meeting.filter(id=meeting_id).delete()

    # 事务提交成功后才清磁盘（storage_name 是第 6 章上传时生成的随机文件名；
    # missing_ok=True 让文件已不在磁盘上时不报错）
    for material in materials:
        (MATERIAL_DIR / material.storage_name).unlink(missing_ok=True)
    # 前端收到 code '200' 后提示“删除成功”并调用 load 刷新列表
    return Result.success()


@router.get("/selectById/{meeting_id}")
async def select_by_id(meeting_id: int, current_user: User = Depends(get_current_user)):
    # “详情”和“编辑”两个按钮都调用这个接口；先按会议数据权限取记录，看不到的会议返回 403
    meeting = await get_accessible_meeting(meeting_id, current_user)
    # 详情按钮把返回值赋给 data.detail，编辑按钮把它深拷贝进 data.form
    return Result.success(await meeting_dict(meeting, current_user))


@router.get("/selectOptions")
async def select_options(current_user: User = Depends(get_current_user)):
    # 管理员从全部会议开始取
    query = Meeting.all()
    # 员工只取与自己相关的会议，规则和第 5 章 has_meeting_access 一致
    if current_user.role != "ADMIN":
        # 先查出当前用户在 meeting_participant 里出现过的会议ID
        participant_meeting_ids = await MeetingParticipant.filter(
            user_id=current_user.id
        ).values_list("meeting_id", flat=True)
        # 自己创建、自己主持、自己参会，满足任一条件的会议都放进下拉框
        query = query.filter(
            Q(creator_id=current_user.id)
            | Q(host_id=current_user.id)
            | Q(id__in=participant_meeting_ids)
        )
    # 开始时间倒序，最近的会议排在下拉框最上面；不分页，一次返回全部
    meetings = await query.order_by("-start_time", "-id")
    return Result.success(
        [
            {
                # 下拉框选项的 value，写入 data.meetingId
                "id": meeting.id,
                # meetingLabel 拼接“编号 / 主题”作为选项文字
                "meeting_no": meeting.meeting_no,
                "title": meeting.title,
                "status": meeting.status,
                "start_time": format_datetime(meeting.start_time),
            }
            for meeting in meetings
        ]
    )


@router.get("/selectPage")
async def select_page(
    # title 来自页面会议主题输入框
    title: str = "",
    # status 来自页面状态下拉框
    status: str = "",
    # view_type 来自页面视图下拉框，默认 MY
    view_type: str = "MY",
    page_num: int = 1,
    page_size: int = 10,
    # 会议列表对所有登录用户开放，数据范围在下面按当前用户收窄
    current_user: User = Depends(get_current_user),
):
    # 主题模糊匹配，空串时相当于不加条件
    query = Meeting.filter(title__contains=title)
    # 先取出当前用户参与的全部会议ID，后面三个分支都可能用到
    participant_meeting_ids = await MeetingParticipant.filter(
        user_id=current_user.id
    ).values_list("meeting_id", flat=True)
    # “我创建的”：只看 creator_id 是自己的会议
    if view_type == "CREATED":
        query = query.filter(creator_id=current_user.id)
    # “我参与的”：只看在 meeting_participant 里有自己记录的会议
    elif view_type == "PARTICIPATED":
        query = query.filter(id__in=participant_meeting_ids)
    # 员工无论选什么视图，以及管理员主动选“我的会议”，都收窄到与自己相关的会议
    # 管理员选“全部会议”时两个条件都不成立，不加任何范围限制
    elif current_user.role != "ADMIN" or view_type == "MY":
        query = query.filter(
            Q(creator_id=current_user.id)
            | Q(host_id=current_user.id)
            | Q(id__in=participant_meeting_ids)
        )
    # 状态下拉框有值时追加等值条件
    if status:
        query = query.filter(status=status)
    # 按开始时间倒序，时间相同时按ID倒序，最新的会议排在最前面
    meetings = await query.order_by("-start_time", "-id").offset(
        (page_num - 1) * page_size
    ).limit(page_size)
    total = await query.count()
    context = await prefetch_meeting_context(meetings)
    return Result.success(
        PageInfo(
            total=total,
            # 逐条转换成页面需要的字段结构（关联已批量预取）
            list=[await meeting_dict(meeting, current_user, context) for meeting in meetings],
        )
    )
