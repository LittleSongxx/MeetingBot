from typing import List

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict

from common.auth import get_current_user, require_admin
from common import security
from common.exception_handler import CustomException
from common.result import PageInfo, Result
from common.times import format_datetime
from models import (
    AgentRun,
    Department,
    Meeting,
    MeetingMaterial,
    MeetingMinutes,
    MeetingParticipant,
    TranscriptSegment,
    TranscriptionTask,
    User,
)

router = APIRouter(prefix="/user")


class UserPayload(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    # 编辑时回填，新增时不存在
    id: int | None = None
    # 弹窗账号输入框，只有新增时可编辑
    username: str | None = None
    # 弹窗密码输入框，只有新增时出现
    password: str | None = None
    # 弹窗姓名输入框
    name: str | None = None
    # 头像上传成功后由 handleFileUpload 写入
    avatar: str | None = None
    # 弹窗角色下拉框
    role: str | None = None
    # 弹窗电话输入框
    phone: str | None = None
    # 弹窗邮箱输入框
    email: str | None = None
    # 弹窗所属部门下拉框
    department_id: int | None = None
    # 弹窗职位输入框
    position: str | None = None
    # 弹窗状态单选框
    status: str | None = None


async def user_dict(user: User) -> dict:
    # department_id 有值时才查部门记录，用于返回部门名称
    department = await Department.get_or_none(id=user.department_id) if user.department_id else None
    return {
        # 编辑弹窗回填和删除接口都用这个ID
        "id": user.id,
        # 表格“账号”列，编辑时这个输入框被禁用
        "username": user.username,
        # 表格“姓名”列
        "name": user.name,
        # 表格“头像”列的图片地址
        "avatar": user.avatar,
        # 表格“角色”列，页面翻译成“管理员”或“员工”
        "role": user.role,
        # 表格“电话”列
        "phone": user.phone,
        # 表格“邮箱”列
        "email": user.email,
        # 编辑弹窗回填所属部门下拉框
        "department_id": user.department_id,
        # 表格“所属部门”列
        "department_name": department.name if department else None,
        # 表格“职位”列
        "position": user.position,
        # 表格“状态”列，页面按它显示绿色或红色标签
        "status": user.status,
        # 两个时间字段统一格式化成 2026-09-09 15:04:05 这样的字符串再返回
        "create_time": format_datetime(user.create_time),
        "update_time": format_datetime(user.update_time),
    }


async def validate_department(department_id: int | None):
    # 没选部门时直接放行；选了部门就必须是存在且未停用的部门
    if department_id is not None and not await Department.filter(id=department_id, status="NORMAL").exists():
        raise CustomException("所选部门不存在或已停用")


async def has_business_reference(user_id: int) -> bool:
    # 列出这个用户可能出现在哪些业务表的哪些字段上，每一项都是一个待执行的存在性查询
    checks = [
        # 第 5 章：他创建的会议、他主持的会议、他参加的会议
        Meeting.filter(creator_id=user_id).exists(),
        Meeting.filter(host_id=user_id).exists(),
        MeetingParticipant.filter(user_id=user_id).exists(),
        # 第 6 章：他上传的会议资料
        MeetingMaterial.filter(uploader_id=user_id).exists(),
        # 第 8 章：他发起的转写任务
        TranscriptionTask.filter(initiator_id=user_id).exists(),
        # 第 9 章：被匹配成他的转写片段、由他修订过的片段
        TranscriptSegment.filter(speaker_user_id=user_id).exists(),
        TranscriptSegment.filter(revised_by=user_id).exists(),
        # 第 11、12 章：他发起生成的纪要、他确认的纪要
        MeetingMinutes.filter(created_by=user_id).exists(),
        MeetingMinutes.filter(confirmed_by=user_id).exists(),
        # 第 13、15 章：他发起的纪要自检运行、他确认过的修订稿
        AgentRun.filter(user_id=user_id).exists(),
        AgentRun.filter(applied_by=user_id).exists(),
    ]
    # 逐个执行，命中任意一项就说明有引用，后面的查询不必再跑
    for check in checks:
        if await check:
            return True
    return False

async def validate_user_delete(ids: list[int]):
    # 前端已经拦了空选，后端再确认一次
    if not ids:
        raise CustomException("请选择要删除的用户")
    # set 去重，避免同一个ID被检查多次
    for user_id in set(ids):
        # 用户可能已被别人删掉
        if not await User.filter(id=user_id).exists():
            raise CustomException("用户不存在")
        # 逐张业务表检查引用
        if await has_business_reference(user_id):
            raise CustomException("该用户已关联会议、转写、纪要或AI运行数据，不能删除")


@router.post("/add")
async def add(payload: UserPayload, _: User = Depends(require_admin)):
    # 账号是唯一必填项
    if not payload.username:
        raise CustomException("请输入账号")
    # 和第 1 章注册接口一样，先按账号查重，避免触发唯一索引报错
    if await User.get_or_none(username=payload.username):
        raise CustomException("账号重复")
    # 前端没选角色时按员工处理
    role = payload.role or "EMPLOYEE"
    # 只接受下拉框里的两个取值，防止绕过页面提交别的角色
    if role not in {"ADMIN", "EMPLOYEE"}:
        raise CustomException("角色参数错误")
    # 选中的部门必须存在且处于正常状态
    await validate_department(payload.department_id)
    if payload.password and len(payload.password) < 6:
        raise CustomException("密码至少需要6位")
    await User.create(
        # 来自弹窗账号输入框
        username=payload.username,
        # 密码留空时用固定初始密码，员工登录后可在第 3 章自行修改；库里只存哈希
        password=security.hash_password(payload.password or "123456"),
        # 姓名留空时用账号填充，保证 name 字段非空
        name=payload.name or payload.username,
        # 头像地址由上传回调写入
        avatar=_validated_avatar(payload.avatar),
        role=role,
        phone=payload.phone,
        email=payload.email,
        # 部门ID写入后，这个员工就出现在对应部门的成员列表里
        department_id=payload.department_id,
        position=payload.position,
        # 未指定状态时按正常账号创建
        status=payload.status or "NORMAL",
    )
    return Result.success()


@router.put("/update")
def _validated_avatar(value: str | None) -> str | None:
    """头像只接受本站 /files/download/ 相对地址，防外链钓鱼与 javascript: 注入。"""
    if not value:
        return None
    if not value.startswith("/files/download/"):
        raise CustomException("头像地址不正确")
    return value


async def update(payload: UserPayload, current_user: User = Depends(get_current_user)):
    # 编辑必须带上用户ID，个人资料页的 data.user.id 来自登录缓存
    if payload.id is None:
        raise CustomException("用户ID不能为空")
    user = await User.get_or_none(id=payload.id)
    if user is None:
        raise CustomException("未找到用户")
    # 员工只能改自己那一条；管理员可以改任何人
    if current_user.role != "ADMIN" and current_user.id != user.id:
        raise CustomException("没有操作权限", "403")

    # 只取前端提交过的字段；账号和密码不在这个接口里修改，直接排除
    update_data = payload.model_dump(
        exclude_unset=True,
        exclude={"id", "username", "password"},
    )
    # 员工提交上来的角色、状态、部门、职位一律丢掉，这四项只能由管理员在第 2 章改
    if current_user.role != "ADMIN":
        update_data.pop("role", None)
        update_data.pop("status", None)
        update_data.pop("department_id", None)
        update_data.pop("position", None)
    # 角色只接受两个取值，None 表示这次没提交角色
    if update_data.get("role") not in {None, "ADMIN", "EMPLOYEE"}:
        raise CustomException("角色参数错误")
    # 状态只接受两个取值
    if update_data.get("status") not in {None, "NORMAL", "DISABLED"}:
        raise CustomException("账号状态参数错误")
    # 提交了部门时校验部门存在且未停用
    await validate_department(update_data.get("department_id"))
    # 头像地址只允许本站相对地址（add 接口同样经过 _validated_avatar）
    if "avatar" in update_data:
        update_data["avatar"] = _validated_avatar(update_data["avatar"])
    # 按主键更新这一条用户记录
    await User.filter(id=user.id).update(**update_data)
    return Result.success()


@router.delete("/delete/{user_id}")
async def delete(user_id: int, current_user: User = Depends(require_admin)):
    # 管理员删掉自己会立刻失去登录态，这里直接拦住
    if current_user.id == user_id:
        raise CustomException("不能删除当前登录账号")
    await validate_user_delete([user_id])
    await User.filter(id=user_id).delete()
    return Result.success()


@router.delete("/deleteBatch")
async def delete_batch(ids: List[int], current_user: User = Depends(require_admin)):
    # 批量勾选里含当前登录账号时同样拦住
    if current_user.id in ids:
        raise CustomException("不能删除当前登录账号")
    await validate_user_delete(ids)
    await User.filter(id__in=ids).delete()
    return Result.success()


@router.get("/selectDepartmentMembers")
async def select_department_members(current_user: User = Depends(get_current_user)):
    # 当前用户还没被分配部门时返回空列表，页面表格显示无数据
    if current_user.department_id is None:
        return Result.success([])
    # 部门取自 Token 解析出的当前用户，不接受前端传部门ID，员工无法查看别的部门
    users = await User.filter(
        department_id=current_user.department_id,
        # 只显示在职员工，停用账号不出现在成员列表里
        status="NORMAL",
    ).order_by("name", "id")
    return Result.success([await user_dict(user) for user in users])


@router.get("/selectMeetingCandidates")
async def select_meeting_candidates(current_user: User = Depends(get_current_user)):
    # 只取正常状态账号，停用账号不能被选为主持人或参会人
    users = await User.filter(status="NORMAL").order_by("name", "id")
    result = []
    for user in users:
        # 逐个补上部门名称，供下拉框显示“姓名 / 部门 / 职位”
        department = await Department.get_or_none(id=user.department_id) if user.department_id else None
        result.append(
            {
                # 下拉框选项的 value，最终写进 meeting.host_id 或 meeting_participant.user_id
                "id": user.id,
                "name": user.name,
                "avatar": user.avatar,
                "department_name": department.name if department else None,
                "position": user.position,
            }
        )
    return Result.success(result)


@router.get("/selectPage")
async def select_page(
    # name 来自页面姓名输入框
    name: str = "",
    # role 来自页面角色下拉框
    role: str = "",
    # department_id 来自页面部门下拉框，None 表示不限
    department_id: int | None = None,
    page_num: int = 1,
    page_size: int = 10,
    # 员工账号管理只对管理员开放
    _: User = Depends(require_admin),
):
    # 姓名模糊匹配，空串时相当于不加条件
    query = User.filter(name__contains=name)
    # 角色下拉框有值时追加等值条件
    if role:
        query = query.filter(role=role)
    # 部门下拉框有值时追加等值条件
    if department_id is not None:
        query = query.filter(department_id=department_id)
    # 按ID倒序，最新创建的账号排在最前面
    users = await query.order_by("-id").offset((page_num - 1) * page_size).limit(page_size)
    total = await query.count()
    return Result.success(PageInfo(total=total, list=[await user_dict(user) for user in users]))
