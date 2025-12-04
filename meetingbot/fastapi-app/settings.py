TORTOISE_ORM = {
    "connections": {
        "default": {
            "engine": "tortoise.backends.mysql",
            "credentials": {
                "host": "localhost",
                "port": 3306,
                # 数据库名称
                "database": "ai_meeting",
                "user": "root",
                # 数据库密码
                "password": "123456",
                "minsize": 1,
                "maxsize": 10,
                "charset": "utf8mb4",
                "echo": True
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

JWT_SECRET = "ai-meeting-jwt-secret"
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_HOURS = 24

