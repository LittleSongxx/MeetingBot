"""文本模型调用。

纪要生成、纪要自检、说话人建议匹配三处都通过 invoke_json 调用模型：

    ai_model_config 里的配置
      -> langchain_openai.ChatOpenAI 调百炼 OpenAI 兼容接口，要求严格按 JSON Schema 返回
      -> common.model_json 里的 langchain_core 解析器解析返回文本
      -> 成功和失败都写一条 ai_call_log

音视频转写走百炼录音文件识别接口，由 transcription_service 用 httpx 直接调用，不经过这里。
"""

from time import perf_counter
import asyncio
import os
import random
from typing import Any, Callable
from uuid import uuid4

import openai
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import Runnable
from langchain_openai import ChatOpenAI

from common import providers
from common.model_json import build_schema_instruction, parse_model_json
from models import AiCallLog, AiModelConfig


def _max_attempts() -> int:
    """单次 invoke_json 最多尝试几次真实调用（含首次）。环境变量 LLM_MAX_ATTEMPTS
    控制，默认 3，设 1 即回到"绝不重试"的旧行为。"""
    value = os.getenv("LLM_MAX_ATTEMPTS", "3").strip()
    try:
        attempts = int(value)
    except ValueError as error:
        raise ValueError("LLM_MAX_ATTEMPTS 必须为正整数") from error
    if attempts <= 0:
        raise ValueError("LLM_MAX_ATTEMPTS 必须为正整数")
    return attempts


def is_transient(error: BaseException) -> bool:
    """这次失败值不值得原样再试一次。

    只认瞬态类：超时、连接失败、429 限流、5xx 服务端错误（OpenAI SDK 异常类型
    加字符串兜底，后者覆盖被包装过的异常）。解析/校验错误不算瞬态——它们走
    invoke_json 里单独的 re-ask 分支（错误回注提示词后再试）；输出截断
    （LengthFinishReasonError）完全不重试：同参数重试只会同样截断。
    """
    if isinstance(error, (openai.APITimeoutError, openai.APIConnectionError,
                          openai.RateLimitError, openai.InternalServerError)):
        return True
    if isinstance(error, openai.APIStatusError):
        return getattr(error, "status_code", 0) >= 500
    if isinstance(error, openai.LengthFinishReasonError):
        return False
    text = str(error).lower()
    return any(marker in text for marker in ("timed out", "timeout", "connection reset", "429"))


def build_chat_model(config: AiModelConfig, schema: dict, call_type: str,
                     prompt_chars: int = 0) -> Runnable:
    """按一条模型配置构造对话模型，并要求它返回符合 schema 的 JSON 对象。

    `call_type` 同时用于两件事：结构化输出的 schema 名，以及**推理策略的选择**
    （`providers.REASONING_BY_CALL_TYPE`）。它不是可有可无的标签。
    """
    options = {}
    override = os.getenv("LLM_ENABLE_THINKING", "").strip()
    if override and override.lower() not in {"true", "false"}:
        raise ValueError("LLM_ENABLE_THINKING must be true or false")
    # 推理开关：环境变量非空时它是权威（保持既有部署行为不变）；为空才按调用类型
    # 的策略走。参数形态由供应商决定，所以"要不要推理"与"怎么传参"分开声明。
    enabled = providers.wants_reasoning(call_type, override=override)
    # 另有一条与"任务是否需要推理"无关的理由要开思考：**输出预算**。
    # 实测该供应商非思考模式硬顶 8192 输出 token（发 max_tokens=16384 也停在 8192），
    # 而合并多段结果会超过这个量——此时开思考是为了拿到 64K 预算，不是因为任务难。
    extended = providers.needs_extended_output_budget(call_type, prompt_chars)
    if extended:
        enabled = True
    options["extra_body"] = providers.thinking_extra_body(
        providers.thinking_shape(config.base_url), enabled
    )
    # 按调用类型分档：LLM_MAX_TOKENS_<CALLTYPE> 优先于全局 LLM_MAX_TOKENS
    #（合并等大输出调用需要与思考 token 共享的预算分开管理，防截断根因复发）
    max_tokens = providers.max_tokens_env(call_type)
    if max_tokens:
        options["max_tokens"] = int(max_tokens)
    model = ChatOpenAI(
        # 模型名称、API Key、接口地址都来自第 7 章 AI 模型配置页面保存的这条记录
        model=config.model_name,
        api_key=config.api_key,
        base_url=config.base_url,
        # 纪要生成、审查、匹配都要求同样的输入给出稳定的结果
        temperature=0,
        timeout=config.timeout_seconds,
        # 不自动重试，ai_call_log 里的一行对应模型服务的一次真实调用
        max_retries=0,
        **options,
    )
    # 结构化输出的方式由供应商能力决定，不再硬编码为严格 schema：
    # 支持 json_schema 的供应商用最强约束；只支持 json_object 的（例如 DeepSeek）
    # 退到"声明返回 JSON + 提示词里的 schema 说明 + 落库前校验器"三层兜底。
    # 猜错这里会直接导致请求失败，所以能力写在一张声明表里（common/providers.py）。
    strategy = providers.structured_output_strategy(config.base_url)
    if strategy == providers.JSON_SCHEMA:
        # 严格结构化输出：模型返回的 JSON 必须符合 schema，必填字段一个都不能少
        return model.bind(
            response_format={
                "type": "json_schema",
                "json_schema": {"name": call_type, "strict": True, "schema": schema},
            }
        )
    # json_object：结构约束来自 build_schema_instruction 附在提示词后的 schema 原文，
    # 以及 invoke_json 传入的 validator。这里只声明"要返回一个 JSON 对象"。
    return model.bind(response_format={"type": "json_object"})


async def invoke_json(
    config: AiModelConfig,
    system_prompt: str,
    user_prompt: str,
    schema: dict,
    call_type: str,
    biz_id: int,
    label: str,
    validator: Callable[[dict], Any] | None = None,
) -> Any:
    """调一次模型并解析返回的 JSON，成功和失败都写一条 AI 调用日志。

    call_type 和 biz_id 写进日志，第 17 章的运行观测按它们统计和判断归属；
    label 是失败提示里的模型名称；validator 不为空时，校验不通过同样记为失败。

    两类重试共用上限 `LLM_MAX_ATTEMPTS`（instructor 式标准行为）：
    - 瞬态失败（超时/连接/429/5xx，见 `is_transient`）：退避后**原样**重试；
    - 输出不合格（JSON 解析失败 / validator 校验失败）：把错误回注到下一次
      尝试的用户提示词里，要求模型修正后重新输出。
    输出截断不重试（同参数重试只会同样截断，且有专门的原因报告）。
    **每次尝试各写一条日志**——"ai_call_log 的一行对应模型服务的一次真实调用"
    在重试下依然成立，失败率的分母自然含全部尝试。
    """
    request_size = len((system_prompt + user_prompt).encode("utf-8"))
    attempts = _max_attempts()
    # 输出不合格时的 re-ask 附注：只作用于下一次尝试，成功后不再携带
    reask_note = ""
    for attempt in range(1, attempts + 1):
        started = perf_counter()
        response_size = 0
        try:
            # schema 名称用调用类型的小写形式，例如 agent_review
            message = await build_chat_model(
                config, schema, call_type.lower(),
                prompt_chars=len(system_prompt) + len(user_prompt),
            ).ainvoke(
                [
                    # 系统指令来自 Prompt 模板的 system_prompt 字段
                    SystemMessage(system_prompt),
                    # 结构要求附在用户提示词末尾，内容由调用方传入的 SCHEMA 生成
                    HumanMessage(user_prompt + build_schema_instruction(schema) + reask_note),
                ]
            )
            # ainvoke 返回 LangChain 的 AIMessage，text 是模型回复的正文
            text = str(message.text)
            # 响应大小按模型返回文本的 UTF-8 字节数算，写进 ai_call_log.response_size
            response_size = len(text.encode("utf-8"))
            result = parse_model_json(text)
            # 纪要生成传入 MinutesResult.model_validate，说话人匹配和自检不传
            if validator is not None:
                result = validator(result)
            # 成功日志，字段含义见第 17 章 ai_call_log 表结构
            await AiCallLog.create(
                request_id="AIC" + uuid4().hex.upper(),
                call_type=call_type,
                biz_id=biz_id,
                model_config_id=config.id,
                model_name=config.model_name,
                request_size=request_size,
                response_size=response_size,
                status="SUCCEEDED",
                elapsed_ms=int((perf_counter() - started) * 1000),
                # LangChain 统一整理的 Token 用量，包含 input_tokens、output_tokens、total_tokens
                usage_json=message.usage_metadata,
            )
            return result
        except Exception as error:
            # 有些异常 str() 出来是空字符串，这时记类型名，页面上的失败原因不会是空白
            detail = (str(error) or type(error).__name__)[:2000]
            truncated = providers.truncation_marker(detail)
            # 输出被长度上限截断要单独标出来：否则它看起来像"模型返回了坏 JSON"，
            # 而真正的原因是输出预算不够，排查方向完全不同。
            #
            # 消息里必须写清**实际生效**的 max_tokens 与是否申请了扩展预算：声明的能力上限
            # （思考 65536）与真正发给请求的值是两件事，只印前者会让人以为预算很宽裕。
            if truncated:
                budget = providers.applied_output_budget(
                    call_type, len(system_prompt) + len(user_prompt),
                    env_max_tokens=providers.max_tokens_env(call_type),
                )
                applied = (
                    "未指定（由供应商默认决定）" if budget["applied_max_tokens"] is None
                    else f"{budget['applied_max_tokens']}（{budget['applied_source']}）"
                )
                detail = (
                    f"[输出被长度上限截断] 该调用类型的输出预算不足；"
                    f"扩展预算={'已申请' if budget['extended_budget_requested'] else '未申请'}"
                    f"（{budget['mode']}），声明上限 {budget['declared_ceiling_tokens']} token，"
                    f"实际 max_tokens={applied}，输入约 {budget['prompt_chars']} 字符。"
                    f"原始错误：{detail}"
                )[:2000]
            if attempts > 1:
                # 重试开启时标明这是第几次尝试，观测页能看出哪些失败行后面跟了重试
                detail = f"[尝试 {attempt}/{attempts}] {detail}"[:2000]
            # 失败也留一条日志，第 17 章的观测页据此统计失败率
            await AiCallLog.create(
                request_id="AIC" + uuid4().hex.upper(),
                call_type=call_type,
                biz_id=biz_id,
                model_config_id=config.id,
                model_name=config.model_name,
                request_size=request_size,
                response_size=response_size,
                status="FAILED",
                elapsed_ms=int((perf_counter() - started) * 1000),
                error_message=detail,
            )
            if attempt < attempts:
                if is_transient(error):
                    # 指数退避加抖动：429/5xx 通常是共享的拥塞，多路同时立刻重试会加重它
                    await asyncio.sleep(0.5 * (2 ** (attempt - 1)) + random.uniform(0, 0.3))
                    continue
                if not truncated:
                    # 输出不合格（解析/校验失败）：错误回注下一次尝试（instructor 式 re-ask）。
                    # 瞬态已在上面的分支处理；截断不重试——同参数只会同样截断。
                    reask_note = (
                        "\n\n【上一次输出不合格】你上一次的输出未通过解析或结构校验，错误信息：\n"
                        f"{detail[:800]}\n请修正上述问题，严格按结构定义重新输出完整的 JSON 对象。"
                    )
                    continue
            # 统一换成带模型名称的提示，例如“说话人匹配模型调用失败：……”，接口层把它转成页面提示
            raise RuntimeError(f"{label}调用失败：{detail[:1000]}") from error
