"""Configure a local Docker installation from environment variables, without model calls."""
import asyncio
import os

from tortoise import Tortoise

from common import security
from models import AiModelConfig, User
from settings import TORTOISE_ORM


async def main():
    await Tortoise.init(config=TORTOISE_ORM)
    try:
        username = os.getenv("APP_ADMIN_USERNAME", "admin")
        password = os.getenv("APP_ADMIN_PASSWORD", "")
        if password:
            hashed = security.hash_password(password)
            user = await User.get_or_none(username=username)
            if user:
                await User.filter(id=user.id).update(password=hashed, role="ADMIN", status="NORMAL")
            else:
                await User.create(username=username, password=hashed, name="本地管理员", role="ADMIN", status="NORMAL")
        # 迁移：把库里遗留的明文密码原位升级为哈希（幂等，已是哈希的跳过）。
        # 没跑 bootstrap 的部署也可以手动执行本函数完成迁移。
        migrated = 0
        for user in await User.all():
            if security.migrate_if_plaintext(user):
                await User.filter(id=user.id).update(password=user.password)
                migrated += 1
        if migrated:
            print(f"Legacy plaintext passwords upgraded to bcrypt: {migrated}")

        shared_key = os.getenv("DASHSCOPE_API_KEY", "").strip()
        llm_key = os.getenv("LLM_API_KEY", "").strip() or shared_key
        asr_key = os.getenv("ASR_API_KEY", "").strip() or shared_key
        llm_url = os.getenv("LLM_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1").rstrip("/")
        llm_model = os.getenv("LLM_MODEL", "qwen3.8-max-0902")
        specs = [
            ("TRANSCRIPTION", asr_key, "DASHSCOPE", os.getenv("DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/api/v1").rstrip("/"), os.getenv("ASR_MODEL", "qwen-audio-3.1-asr-flash-filetrans"), int(os.getenv("ASR_TIMEOUT_SECONDS", "600"))),
            *[(kind, llm_key, "OPENAI_COMPATIBLE", llm_url, os.getenv(kind + "_MODEL") or llm_model, int(os.getenv("LLM_TIMEOUT_SECONDS", "300"))) for kind in ("MINUTES", "AGENT", "SPEAKER")],
        ]
        configured = []
        for kind, key, provider, base_url, model, timeout in specs:
            if not key:
                continue
            config = await AiModelConfig.filter(model_type=kind).order_by("-id").first()
            values = dict(provider=provider, base_url=base_url, api_key=key, model_name=model, timeout_seconds=timeout, enabled=True)
            await AiModelConfig.filter(model_type=kind).update(enabled=False)
            if config:
                await AiModelConfig.filter(id=config.id).update(**values)
            else:
                await AiModelConfig.create(name="本地 " + kind, model_type=kind, **values)
            configured.append(kind)
        print("Local configuration loaded. Model purposes configured: " + (", ".join(configured) or "none; waiting for API key"))
    finally:
        await Tortoise.close_connections()


if __name__ == "__main__":
    asyncio.run(main())
