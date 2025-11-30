"""模型用途解析：一个用途一条启用中的配置，允许显式回退。

为什么要单独一层：`AiModelConfig.filter(model_type=...).first()` 散落在十几处，
新增 JUDGE 用途时如果每处都自己写回退，迟早有一处忘掉。这里只做两件事：
按用途取启用配置；取不到时按调用方给的 fallback 取（JUDGE → AGENT）。
"""

from __future__ import annotations

from models import AiModelConfig, ModelRoute

# 可路由的调用类型/用途。路由为空时回退到 fallback_purpose（默认不改变现有行为）。
ROUTE_KEYS = (
    "MINUTES", "AGENT", "SPEAKER", "CHAT", "CHAT_SUMMARY", "CHAT_FIRST_HOP",
    "KB_QUERY_PLAN", "KB_CONTEXTUALIZE", "ONLINE_EVAL_JUDGE",
)
# 这些路由目标会做结构化输出（JSON schema）；启动时校验其能力，
# 避免把不支持 strict json_schema 的便宜模型路由到纪要/自检/规划/摘要上。
STRUCTURED_ROUTE_KEYS = (
    "MINUTES", "AGENT", "SPEAKER", "CHAT_SUMMARY",
    "KB_QUERY_PLAN", "KB_CONTEXTUALIZE",
)


class ModelConfigError(RuntimeError):
    pass


async def resolve_route(route_key: str, fallback_purpose: str) -> AiModelConfig:
    """先查启用路由，再回退到原用途。

    路由目标被停用/删除时**自动回退**，不把整条链路弄挂；这比"路由失效即 500"
    更符合"增强能力不该有能力把主链路弄挂"的既有原则。路由本身仍只在
    `model_route` 表里生效，默认空表时行为与改造前完全一致。
    """
    route = await ModelRoute.filter(route_key=route_key, enabled=True).first()
    if route is not None:
        config = await AiModelConfig.filter(
            id=route.model_config_id, enabled=True).first()
        if config is not None:
            return config
    return await resolve(fallback_purpose)


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


__all__ = ["resolve", "resolve_route", "ROUTE_KEYS", "STRUCTURED_ROUTE_KEYS",
           "ModelConfigError"]
