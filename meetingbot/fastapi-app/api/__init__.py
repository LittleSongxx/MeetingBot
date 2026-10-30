import importlib
import logging
import pkgutil

from common.auth import create_token, get_current_user
from common.exception_handler import CustomException
from common.login_guard import check_locked, record_failure
from common.login_guard import clear as clear_login_failures
from common.result import Result
from common.security import hash_password, is_hashed, password_rule_error, verify_password
from common.urls import to_relative_url
from fastapi import APIRouter, Depends, Request
from models import Department, User
from pydantic import BaseModel
from settings import REGISTER_ENABLED

logger = logging.getLogger(__name__)


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
        "avatar": to_relative_url(user.avatar),
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
    # 同一账号/来源连续失败 5 次锁 5 分钟，避免被无限次试密码
    client_host = request.client.host if request is not None and request.client else None
    locked = check_locked(account.username, client_host)
    if locked:
        raise CustomException(f"登录失败次数过多，请{locked}秒后再试")
    # 按页面输入的账号查询 user 表，账号不存在时 user 为 None
    user = await User.get_or_none(username=account.username)
    # 账号不存在或密码不匹配时返回同一句提示，页面上看不出是账号错还是密码错
    if user is None or not verify_password(account.password or "", user.password):
        record_failure(account.username, client_host)
        raise CustomException("账号或密码错误")
    clear_login_failures(account.username, client_host)
    # 迁移窗口：历史明文密码校验通过后立即原位升级为 bcrypt
    if not is_hashed(user.password):
        upgraded = hash_password(account.password or "")
        await User.filter(id=user.id).update(password=upgraded)
        user.password = upgraded
        logger.info("登录时把遗留明文密码升级为 bcrypt", extra={"user_id": user.id})
    # 账号被管理员停用后 status 不是 NORMAL，即使密码正确也不放行
    if user.status != "NORMAL":
        raise CustomException("账号已被停用")
    # 校验通过后签发 Token，并把用户信息和 Token 一起返回给前端
    return Result.success(await account_dict(user, create_token(user)))


@api_router.post("/register")
async def register(account: Account):
    # 注册开关：企业内部系统默认关闭，账号只能由管理员创建
    if not REGISTER_ENABLED:
        raise CustomException("注册功能未开启，请联系管理员创建账号", "403")
    # 账号或密码为空时不建账号。前端虽然有非空校验，后端仍然独立校验一次
    if not account.username or not account.password:
        raise CustomException("账号和密码不能为空")
    # 两次密码不一致时不建账号，和前端 validatePass 的判断保持同一个口径
    if account.password != account.confirmPassword:
        raise CustomException("两次输入的密码不一致")
    password_error = password_rule_error(account.password)
    if password_error:
        raise CustomException(password_error)
    # 按账号查一次 user 表，已存在同名账号时直接拒绝，避免触发数据库唯一索引报错
    if await User.get_or_none(username=account.username):
        raise CustomException("账号重复")
    # 向 user 表插入一条新记录
    await User.create(
        # username 来自注册页账号输入框
        username=account.username,
        # password 来自注册页密码输入框，落库前先做 bcrypt 哈希
        password=hash_password(account.password),
        # 注册页没有姓名输入框，用账号填充 name，保证 name 字段非空
        name=account.username,
        # 注册接口固定写 EMPLOYEE，管理员账号只能由已有管理员在第 2 章创建
        role="EMPLOYEE",
        # 新账号直接可用，登录时的状态校验会放行
        status="NORMAL",
    )
    # 注册不返回数据，前端只根据 code 判断成功与否
    return Result.success()


@api_router.put("/updatePassword")
async def update_password(account: Account, current_user: User = Depends(get_current_user)):
    # 用页面“原密码”输入框的值和数据库里的密码比对，不符时不允许改
    if not verify_password(account.password or "", current_user.password):
        raise CustomException("原密码错误")
    # 新密码不能为空。前端有非空校验，后端独立再校验一次
    if not account.newPassword:
        raise CustomException("请输入新密码")
    password_error = password_rule_error(account.newPassword)
    if password_error:
        raise CustomException(password_error)
    # 新旧密码相同时没有修改意义，直接拦住
    if verify_password(account.newPassword, current_user.password):
        raise CustomException("新密码不能与原密码相同")
    # 按当前登录用户的ID更新密码，用户ID来自 Token，不接受前端传
    await User.filter(id=current_user.id).update(password=hash_password(account.newPassword))
    return Result.success()


@api_router.get("/currentUser")
async def current_user(user: User = Depends(get_current_user)):
    return Result.success(await account_dict(user))


for _, module_name, _ in pkgutil.iter_modules(__path__, __name__ + "."):
    module = importlib.import_module(module_name)
    if hasattr(module, "router"):
        api_router.include_router(module.router)
