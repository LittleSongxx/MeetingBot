"""模型用途解析：一个用途一条启用中的配置，允许显式回退。

为什么要单独一层：`AiModelConfig.filter(model_type=...).first()` 散落在十几处，
新增 JUDGE 用途时如果每处都自己写回退，迟早有一处忘掉。这里只做两件事：
按用途取启用配置；取不到时按调用方给的 fallback 取（JUDGE → AGENT）。
"""

from __future__ import annotations

from models import AiModelConfig


class ModelConfigError(RuntimeError):
    pass


async def resolve(purpose: str, *, fallback: str | None = None) -> AiModelConfig:
    config = await AiModelConfig.filter(
        model_type=purpose, enabled=True).order_by("-id").first()
    if config is not None:
        return config
    if fallback:
        fallback_config = await AiModelConfig.filter(
            model_type=fallback, enabled=True).order_by("-id").first()
        if fallback_config is not None:
            return fallback_config
    raise ModelConfigError(f"没有启用中的 {purpose} 模型配置")


__all__ = ["resolve", "ModelConfigError"]
