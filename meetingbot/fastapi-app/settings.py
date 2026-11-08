import os

TORTOISE_ORM = {
    "connections": {
        "default": {
            "engine": "tortoise.backends.mysql",
            "credentials": {
                # 部署时由环境变量覆盖，默认值保持原版本的本地开发配置
                "host": os.getenv("MYSQL_HOST", "localhost"),
                "port": int(os.getenv("MYSQL_PORT", "3306")),
                # 数据库名称
                "database": os.getenv("MYSQL_DATABASE", "ai_meeting"),
                "user": os.getenv("MYSQL_USER", "root"),
                # 数据库密码
                "password": os.getenv("MYSQL_PASSWORD", "123456"),
                "minsize": 1,
                "maxsize": 10,
                "charset": "utf8mb4",
                "echo": os.getenv("SQL_ECHO", "true").lower() == "true"
            }
        },
    },
    "apps": {
      "models": {
          "models": ["models"],
          "default_connection": "default",
      }
    },
    # 页面提交和展示的都是东八区时间，库里也直接存东八区，不转成 UTC。
    # 开着的话 auto_now_add / auto_now 会按 UTC 落库，比手动传入的时间早 8 小时
    "use_tz": False,
    "timezone": "Asia/Shanghai"
}

# 密钥与有效期可由部署环境覆盖，默认值保持原版本行为
JWT_SECRET = os.getenv("JWT_SECRET", "ai-meeting-jwt-secret")
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_HOURS = int(os.getenv("JWT_EXPIRE_HOURS", "24"))

