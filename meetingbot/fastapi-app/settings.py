import os

TORTOISE_ORM = {
    "connections": {
        "default": {
            "engine": "tortoise.backends.mysql",
            "credentials": {
                "host": os.getenv("MYSQL_HOST", "127.0.0.1"),
                "port": int(os.getenv("MYSQL_PORT", "3306")),
                # 数据库名称
                "database": os.getenv("MYSQL_DATABASE", "ai_meeting"),
                "user": os.getenv("MYSQL_USER", "root"),
                # 数据库密码
                "password": os.getenv("MYSQL_PASSWORD", ""),
                "minsize": 1,
                "maxsize": 10,
                "charset": "utf8mb4",
                "echo": os.getenv("SQL_ECHO", "false").lower() == "true"
            }
        },
    },
    "apps": {
      "models": {
          "models": ["models"],
          "default_connection": "default",
      }
    },
    # 是否使用时区
    "use_tz": True,
    "timezone": "Asia/Shanghai"
}

# JWT 密钥不允许缺省回退：缺省值一旦进入部署，任何拿到公开代码的人都能伪造
# 管理员 token（HS256 对称签名）。未显式设置时启动即失败，宁可不可用也不可伪造。
# 唯一豁免：unittest 进程（测试从不签发对外 token，给它一个固定占位密钥）。
import sys as _sys
_JWT_SECRET = os.getenv("JWT_SECRET", "").strip()
if not _JWT_SECRET:
    if "unittest" in _sys.modules:
        _JWT_SECRET = "unit-test-secret-not-for-production"
    else:
        raise RuntimeError("必须设置 JWT_SECRET 环境变量（随机长字符串）；本地开发请在 .env 中生成")
JWT_SECRET = _JWT_SECRET
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_HOURS = 24

