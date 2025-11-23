"""Configure a local Docker installation from environment variables, without model calls."""
import asyncio
import os

from common import security
from models import AiModelConfig, User
from settings import TORTOISE_ORM
from tortoise import Tortoise


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
        # 单价（微元/千 token）从环境读：填了才参与成本核算，否则整条配置是"未定价"。
        # 不在代码里内置任何价格——价格是采购事实，写死在代码里必然过期，
        # 而过期的价格比没有价格更糟（报表看起来有数，实际是错的）。
        llm_price_in = int(os.getenv("LLM_INPUT_PRICE_MICRO_PER_1K", "0"))
        llm_price_out = int(os.getenv("LLM_OUTPUT_PRICE_MICRO_PER_1K", "0"))
        embed_price_in = int(os.getenv("EMBEDDING_INPUT_PRICE_MICRO_PER_1K", "0"))
        specs = [
            ("TRANSCRIPTION", asr_key, "DASHSCOPE", os.getenv("DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/api/v1").rstrip("/"), os.getenv("ASR_MODEL", "qwen-audio-3.1-asr-flash-filetrans"), int(os.getenv("ASR_TIMEOUT_SECONDS", "600")), 0, 0),
            *[(kind, llm_key, "OPENAI_COMPATIBLE", llm_url, os.getenv(kind + "_MODEL") or llm_model, int(os.getenv("LLM_TIMEOUT_SECONDS", "300")), llm_price_in, llm_price_out) for kind in ("MINUTES", "AGENT", "SPEAKER")],
            # 嵌入走同一套 OpenAI 兼容端点（百炼 compatible-mode 提供 /embeddings）。
            # 单独一条配置：换嵌入模型要重建知识库，所以它必须能独立于对话模型切换。
            ("EMBEDDING", llm_key, "OPENAI_COMPATIBLE", llm_url,
             os.getenv("EMBEDDING_MODEL", "text-embedding-v4"), int(os.getenv("EMBEDDING_TIMEOUT_SECONDS", "120")),
             embed_price_in, 0),
        ]
        # 跨族裁判是可选用途：只有显式给了 JUDGE_MODEL 才建配置；
        # 未配置时产品侧 resolve("JUDGE", fallback="AGENT") 自动回退。
        cheap_model = (os.getenv("CHEAP_MODEL") or "").strip()
        if cheap_model:
            specs.append((
                "CHEAP",
                (os.getenv("CHEAP_API_KEY") or "").strip() or llm_key,
                "OPENAI_COMPATIBLE",
                (os.getenv("CHEAP_BASE_URL") or llm_url).rstrip("/"),
                cheap_model,
                int(os.getenv("CHEAP_TIMEOUT_SECONDS", "300")),
                int(os.getenv("CHEAP_INPUT_PRICE_MICRO_PER_1K", "0")),
                int(os.getenv("CHEAP_OUTPUT_PRICE_MICRO_PER_1K", "0")),
            ))
        judge_model = (os.getenv("JUDGE_MODEL") or "").strip()
        if judge_model:
            specs.append((
                "JUDGE",
                (os.getenv("JUDGE_API_KEY") or "").strip() or llm_key,
                "OPENAI_COMPATIBLE",
                (os.getenv("JUDGE_BASE_URL") or llm_url).rstrip("/"),
                judge_model,
                int(os.getenv("JUDGE_TIMEOUT_SECONDS", "180")),
                0, 0,
            ))
        configured = []
        for kind, key, provider, base_url, model, timeout, price_in, price_out in specs:
            if not key:
                continue
            config = await AiModelConfig.filter(model_type=kind).order_by("-id").first()
            values = {"provider": provider, "base_url": base_url, "api_key": key, "model_name": model, "timeout_seconds": timeout, "enabled": True,
                      "input_price_micro_per_1k": price_in, "output_price_micro_per_1k": price_out}
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
