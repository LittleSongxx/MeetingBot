import importlib
import os
import pkgutil

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from common.auth import create_token, get_current_user
from common.exception_handler import CustomException
from common import ratelimit, security
from common.result import Result
from models import Department, User


class Account(BaseModel):
    # 登录和注册页的账号输入框
    username: str | None = None
    # 登录和注册页的密码输入框，在修改密码接口里表示原密码
    password: str | None = None
    # 第 3 章修改密码页的新密码输入框
    newPassword: str | None = None
    # 注册页的确认密码输入框
    confirmPassword: str | None = None


api_router = APIRouter()


async def account_dict(user: User, token: str | None = None) -> dict:
    # 用户挂在某个部门下时才查部门记录，用于返回部门名称给页面展示
    department = await Department.get_or_none(id=user.department_id) if user.department_id else None
    data = {
        # 前端存进 xm-user 后，很多页面用它判断“这条数据是不是我的”
        "id": user.id,
        # 登录账号，第 3 章个人资料页展示
        "username": user.username,
        # 姓名，管理端右上角和各类列表展示
        "name": user.name,
        # 头像地址，管理端右上角展示
        "avatar": user.avatar,
        # 角色，前端按它显示或隐藏管理员菜单和按钮
        "role": user.role,
        "phone": user.phone,
        "email": user.email,
        # 部门ID，第 3 章个人资料页随用户信息一起保存
        "department_id": user.department_id,
        # 部门名称由上面查出的部门记录提供，没有部门时为 None；第 2 章部门成员页顶部标题读取它
        "department_name": department.name if department else None,
        "position": user.position,
        # 账号状态，登录成功时一定是 NORMAL
        "status": user.status,
    }
    # 只有登录接口会传 token，个人资料等接口复用这个函数时不重复下发 Token
    if token:
        data["token"] = token
    return data


@api_router.post("/login")
async def login(account: Account, request: Request):
    # 限流：按用户名与来源 IP 各自累计失败次数，任一超阈值即临时拒绝（防在线爆破）
    client_ip = request.client.host if request.client else "unknown"
    for kind, key in (("login-user", account.username or ""), ("login-ip", client_ip)):
        if ratelimit.is_blocked(kind, key):
            raise CustomException(
                f"尝试过于频繁，请约 {ratelimit.seconds_left(kind, key) // 60} 分钟后再试")
    # 按页面输入的账号查询 user 表，账号不存在时 user 为 None
    user = await User.get_or_none(username=account.username)
    # 账号不存在或密码不匹配时返回同一句提示，页面上看不出是账号错还是密码错
    if user is None or not security.verify_password(account.password, user.password):
        for kind, key in (("login-user", account.username or ""), ("login-ip", client_ip)):
            ratelimit.record_failure(kind, key)
        raise CustomException("账号或密码错误")
    # 迁移期：库中还是遗留明文时，登录成功即原位升级为 bcrypt 哈希
    if security.migrate_if_plaintext(user):
        await User.filter(id=user.id).update(password=user.password)
    # 账号被管理员停用后 status 不是 NORMAL，即使密码正确也不放行
    if user.status != "NORMAL":
        raise CustomException("账号已被停用")
    for kind, key in (("login-user", account.username or ""), ("login-ip", client_ip)):
        ratelimit.record_success(kind, key)
    # 校验通过后签发 Token（含密码指纹 pv），并把用户信息和 Token 一起返回给前端
    return Result.success(await account_dict(user, create_token(user)))


@api_router.post("/register")
async def register(account: Account, request: Request):
    # 自注册默认开放以保持既有部署行为；对外/生产部署应在 .env 里设
    # REGISTER_ENABLED=false，改为管理员建号（内部会议系统的推荐形态）。
    if (os.getenv("REGISTER_ENABLED", "true").strip().lower()) not in {"1", "true", "yes"}:
        raise CustomException("当前未开放自助注册，请联系管理员开通账号")
    client_ip = request.client.host if request.client else "unknown"
    if ratelimit.is_blocked("register-ip", client_ip):
        raise CustomException("注册尝试过于频繁，请稍后再试")
    ratelimit.record_failure("register-ip", client_ip)
    # 账号或密码为空时不建账号。前端虽然有非空校验，后端仍然独立校验一次
    if not account.username or not account.password:
        raise CustomException("账号和密码不能为空")
    if len(account.password) < 6:
        raise CustomException("密码至少需要6位")
    # 两次密码不一致时不建账号，和前端 validatePass 的判断保持同一个口径
    if account.password != account.confirmPassword:
        raise CustomException("两次输入的密码不一致")
    # 按账号查一次 user 表，已存在同名账号时直接拒绝，避免触发数据库唯一索引报错
    if await User.get_or_none(username=account.username):
        raise CustomException("账号重复")
    # 向 user 表插入一条新记录
    await User.create(
        # username 来自注册页账号输入框
        username=account.username,
        # password 只落哈希，明文不进库
        password=security.hash_password(account.password),
        # 注册页没有姓名输入框，用账号填充 name，保证 name 字段非空
        name=account.username,
        # 注册接口固定写 EMPLOYEE，管理员账号只能由已有管理员在第 2 章创建
        role="EMPLOYEE",
        # 新账号直接可用，登录时的状态校验会放行
        status="NORMAL",
    )
    ratelimit.record_success("register-ip", client_ip)
    # 注册不返回数据，前端只根据 code 判断成功与否
    return Result.success()


@api_router.put("/updatePassword")
async def update_password(account: Account, current_user: User = Depends(get_current_user)):
    # 用页面“原密码”输入框的值和数据库里的密码比对，不符时不允许改（兼容遗留明文）
    if not security.verify_password(account.password, current_user.password):
        raise CustomException("原密码错误")
    # 新密码不能为空。前端有非空校验，后端独立再校验一次
    if not account.newPassword:
        raise CustomException("请输入新密码")
    if len(account.newPassword) < 6:
        raise CustomException("新密码至少需要6位")
    # 新旧密码相同时没有修改意义，直接拦住（对旧值做一次真实校验即可覆盖明文/哈希两种形态）
    if security.verify_password(account.newPassword, current_user.password):
        raise CustomException("新密码不能与原密码相同")
    # 按当前登录用户的ID更新密码（只落哈希）。改密后密码指纹变化，
    # 本 token 与所有旧 token 立即失效，前端会引导重新登录。
    await User.filter(id=current_user.id).update(password=security.hash_password(account.newPassword))
    return Result.success()


@api_router.get("/currentUser")
async def current_user(user: User = Depends(get_current_user)):
    return Result.success(await account_dict(user))


for _, module_name, _ in pkgutil.iter_modules(__path__, __name__ + "."):
    module = importlib.import_module(module_name)
    if hasattr(module, "router"):
        api_router.include_router(module.router)
