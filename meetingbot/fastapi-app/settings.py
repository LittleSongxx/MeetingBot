"""运行时配置。

这里的值全部来自环境变量：代码中不保存数据库口令、JWT 密钥或任何模型 Key。
本地开发把 `.env.example` 复制成 `.env` 后由 docker compose 注入，或在 shell 里
`set -a; source .env; set +a` 后再启动。测试进程（`unittest`）会拿到一个仅用于
测试的 JWT 占位密钥，因此测试不需要准备 .env。
"""

import os
import sys


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as error:
        raise RuntimeError(f"环境变量 {name} 必须是整数，当前值为 {raw!r}") from error


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


TORTOISE_ORM = {
    "connections": {
        "default": {
            "engine": "tortoise.backends.mysql",
            "credentials": {
                "host": os.getenv("MYSQL_HOST", "127.0.0.1"),
                "port": _env_int("MYSQL_PORT", 3306),
                # 数据库名称
                "database": os.getenv("MYSQL_DATABASE", "ai_meeting"),
                "user": os.getenv("MYSQL_USER", "root"),
                # 数据库密码
                "password": os.getenv("MYSQL_PASSWORD", ""),
                "minsize": 1,
                "maxsize": 10,
                "charset": "utf8mb4",
                # 默认关闭 SQL 回显：生产日志里不应出现每条 SQL
                "echo": _env_bool("SQL_ECHO", False),
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

# JWT 密钥不允许缺省回退：缺省值一旦进入部署，任何拿到公开代码的人都能伪造
# 管理员 token（HS256 对称签名）。未显式设置时启动即失败。
# 唯一豁免：unittest 进程（测试从不签发对外 token，给它一个固定占位密钥）。
_JWT_SECRET = os.getenv("JWT_SECRET", "").strip()
if not _JWT_SECRET:
    if "unittest" in sys.modules:
        _JWT_SECRET = "unit-test-secret-not-for-production"
    else:
        raise RuntimeError(
            "必须设置 JWT_SECRET 环境变量（随机长字符串，可用 openssl rand -hex 32 生成）"
        )
JWT_SECRET = _JWT_SECRET
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_HOURS = _env_int("JWT_EXPIRE_HOURS", 24)

# 是否开放注册：企业内部系统默认关闭，账号由管理员创建
REGISTER_ENABLED = _env_bool("REGISTER_ENABLED", False)

# 允许跨域的来源，逗号分隔；默认同源部署、不启用 CORS 中间件。
# 本地前端 dev server 需要跨域时，在 .env 里显式写 CORS_ALLOW_ORIGINS=http://localhost:5173
_cors_raw = os.getenv("CORS_ALLOW_ORIGINS")
CORS_ALLOW_ORIGINS = [item.strip() for item in (_cors_raw or "").split(",") if item.strip()]

# 转写/纪要/自检的行为参数（可在 .env 里按机器预算调整）
CHUNK_SECONDS = _env_int("CHUNK_SECONDS", 1200)
MAX_CHUNK_CHARS = _env_int("MAX_CHUNK_CHARS", 12000)
AGENT_REVIEW_MAX_TRANSCRIPT_CHARS = _env_int("AGENT_REVIEW_MAX_TRANSCRIPT_CHARS", 20000)

# 生产默认关闭 /docs、/redoc 与 /openapi.json（本机调试可在 .env 设 DOCS_ENABLED=1）
DOCS_ENABLED = _env_bool("DOCS_ENABLED", False)

# ai_call_log 保留天数：启动时清理更早的调用日志，0 表示不清理
AI_CALL_LOG_RETENTION_DAYS = _env_int("AI_CALL_LOG_RETENTION_DAYS", 365)

# LLM 结构化输出方式：
#   json_schema  OpenAI 兼容接口支持严格 json_schema（百炼等，默认）
#   json_object  只支持 json_object 的提供商（例如 DeepSeek），schema 约束由提示词 + 后端校验兜底
LLM_JSON_MODE = os.getenv("LLM_JSON_MODE", "json_schema").strip().lower()
