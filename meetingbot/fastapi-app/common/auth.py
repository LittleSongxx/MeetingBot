import logging
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Header

from common.exception_handler import CustomException
from common.security import password_fingerprint
from models import User
from settings import JWT_ALGORITHM, JWT_EXPIRE_HOURS, JWT_SECRET

logger = logging.getLogger(__name__)


def create_token(user: User) -> str:
    # 过期时间按 JWT_EXPIRE_HOURS 计算，超过后 PyJWT 解析时直接判定过期
    expire_time = datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRE_HOURS)
    # sub 保存用户ID；pv 是当前密码哈希的指纹，改密后旧 token 立即失效
    payload = {
        "sub": str(user.id),
        "role": user.role,
        "pv": password_fingerprint(user.password),
        "exp": expire_time,
    }
    # 用配置里的密钥和算法签名，结果就是返回给前端的 Token 字符串
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


async def get_current_user(token: str | None = Header(default=None)) -> User:
    # 参数名 token 对应请求头 token，Header 让 FastAPI 从请求头而不是查询串取值
    # 没登录时前端补的是空串，这里直接判定登录失效
    if not token:
        raise CustomException("登录状态已失效，请重新登录", "401")
    try:
        # 用同一个密钥和算法验签，签名不对或 exp 已过期都会抛 InvalidTokenError
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        # 签发时 sub 存的是字符串形式的用户ID，这里转回整数
        user_id = int(payload.get("sub"))
        password_version = payload.get("pv")
    except (jwt.InvalidTokenError, TypeError, ValueError) as error:
        # Token 被篡改、已过期、sub 缺失或不是数字，统一按登录失效处理
        raise CustomException("登录状态已失效，请重新登录", "401") from error
    # 拿 Token 里的用户ID回查数据库，而不是直接信任 Token 里的其他字段
    user = await User.get_or_none(id=user_id)
    # 账号已被删除，或被管理员停用，即使 Token 没过期也不放行
    if user is None or user.status != "NORMAL":
        raise CustomException("账号不存在或已被停用", "401")
    # 改过密码后旧 token 的 pv 与当前库中哈希指纹不一致，要求重新登录
    if password_version != password_fingerprint(user.password):
        raise CustomException("密码已变更，请重新登录", "401")
    return user


async def require_admin(token: str | None = Header(default=None)) -> User:
    # 先完成登录识别，拿到数据库里最新的用户记录
    user = await get_current_user(token)
    # 角色以数据库里的 role 为准，不看 Token 里写的 role
    if user.role != "ADMIN":
        raise CustomException("没有操作权限", "403")
    return user
