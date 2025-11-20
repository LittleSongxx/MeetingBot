"""密码与令牌指纹。

历史：本项目曾全程明文存取密码（models.User.password 直接比对）。本模块是
修复后的唯一入口——哈希、校验、旧明文的识别与迁移、以及随密码变更使
已签发 JWT 失效的指纹（`pv`），全部收敛在这里，别处不得自行实现。
"""

from __future__ import annotations

import hashlib

import bcrypt

# bcrypt 输出固定以 $2 开头（$2a$/$2b$/$2y$），用它区分"已是哈希"与"遗留明文"
_BCRYPT_PREFIX = "$2"


def hash_password(plain: str) -> str:
    """明文 → bcrypt 哈希（含盐）。截断到 72 字节是 bcrypt 的算法上限。"""
    return bcrypt.hashpw(plain.encode("utf-8")[:72], bcrypt.gensalt(rounds=10)).decode("utf-8")


def verify_password(plain: str, stored: str) -> bool:
    """校验明文与库中值。库中值可能是哈希（新）也可能是明文（遗留），
    两种都接受，迁移期平滑过渡。恒定时间比较由 bcrypt 内部保证。"""
    if not stored:
        return False
    if stored.startswith(_BCRYPT_PREFIX):
        try:
            return bcrypt.checkpw(plain.encode("utf-8")[:72], stored.encode("utf-8"))
        except ValueError:
            return False
    # 遗留明文：直接比较（下一行迁移逻辑会尽快把它换成哈希）
    return plain == stored


def is_hashed(stored: str | None) -> bool:
    return bool(stored) and bool(stored.startswith(_BCRYPT_PREFIX))


def migrate_if_plaintext(user) -> bool:
    """把遗留明文密码原位升级为哈希。返回是否发生了迁移。

    只在能读到明文（即库中存的还是明文）时执行；哈希无法再哈希。
    """
    if is_hashed(user.password):
        return False
    user.password = hash_password(user.password)
    return True


def password_fingerprint(stored_password: str) -> str:
    """签进 JWT 的密码版本指纹：改密后旧 token 全部失效，无需建黑名单。

    取哈希值的 sha256 前 16 位——不含任何可逆信息；同一密码的两次哈希
    （盐不同）指纹不同，因此指纹只在"当前库中值"上计算。
    """
    return hashlib.sha256(stored_password.encode("utf-8")).hexdigest()[:16]
