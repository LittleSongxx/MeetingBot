"""文本模型调用。

纪要生成、纪要自检、说话人建议匹配三处都通过 invoke_json 调用模型：

    ai_model_config 里的配置
      -> langchain_openai.ChatOpenAI 调百炼 OpenAI 兼容接口，要求严格按 JSON Schema 返回
      -> common.model_json 里的 langchain_core 解析器解析返回文本
      -> 成功和失败都写一条 ai_call_log

音视频转写走百炼录音文件识别接口，由 transcription_service 用 httpx 直接调用，不经过这里。
"""

import asyncio
import os
import random
from time import perf_counter
from typing import Any, AsyncIterator, Awaitable, Callable
from uuid import uuid4

import httpx
import openai
from common import metrics, providers
from common.model_json import build_schema_instruction, parse_model_json
from common.tracing import model_attributes, span, usage_attributes
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.runnables import Runnable
from langchain_openai import ChatOpenAI
from models import AiCallLog, AiModelConfig

from services import pricing


def _cost_micro(config: AiModelConfig, usage: dict | None) -> int:
    """这次调用的成本（微元）。单价来自当前配置，所以改价不会重估历史调用。"""
    return pricing.cost_of_config(config, usage).micro


def _observe_call(config: AiModelConfig, *, call_type: str, status: str, elapsed_ms: int,
                  usage: dict | None) -> None:
    """一次调用收尾时统一上报指标与 span 属性。

    放在**唯一出口**上：日志（ai_call_log）与指标（/metrics）本来就是同一件事的
    两种读法，分开埋点必然漂移——那种漂移的表现是"日志里有、面板上没有"，
    而排查的人会先怀疑面板。
    """
    cost = pricing.cost_of_config(config, usage)
    metrics.record_model_call(
        model=config.model_name, call_type=call_type, status=status, elapsed_ms=elapsed_ms,
        input_tokens=cost.input_tokens, output_tokens=cost.output_tokens,
        cost_micro=cost.micro, priced=cost.priced)
    # span 用同一组数字：属性写在当前 span 上（没有父 span 时是根 span）
    with span(f"gen_ai.{call_type.lower()}", model_attributes(
            call_type=call_type, model=config.model_name, base_url=config.base_url)) as current:
        for key, value in usage_attributes(
                input_tokens=cost.input_tokens, output_tokens=cost.output_tokens,
                cost_micro=cost.micro, cost_priced=cost.priced).items():
            current.set_attribute(key, value)
        current.set_attribute("meetingbot.call_status", status)


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
    model = chat_model(config, call_type, prompt_chars)
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


def reasoning_enabled(config: AiModelConfig, call_type: str, prompt_chars: int = 0) -> bool:
    """这次调用**实际**会不会开思考。抽出来共用，因为供应商能力的判定依赖它。

    两条来源合并成一处（`chat_model` 与 `required_tool_choice_supported` 都用它）：
    任务性质的默认策略（`REASONING_BY_CALL_TYPE`，可被 LLM_ENABLE_THINKING 覆盖）
    与"为拿输出预算而开"（大输出调用，见 `needs_extended_output_budget`）。
    分开写就会漂移——那种漂移的表现是"某个开关下请求被 400 打回"。
    """
    override = os.getenv("LLM_ENABLE_THINKING", "").strip()
    if override and override.lower() not in {"true", "false"}:
        raise ValueError("LLM_ENABLE_THINKING must be true or false")
    enabled = providers.wants_reasoning(call_type, override=override)
    # 另有一条与"任务是否需要推理"无关的理由要开思考：**输出预算**。
    # 实测该供应商非思考模式硬顶 8192 输出 token（发 max_tokens=16384 也停在 8192），
    # 而合并多段结果会超过这个量——此时开思考是为了拿到 64K 预算，不是因为任务难。
    return enabled or providers.needs_extended_output_budget(call_type, prompt_chars)


def chat_model(config: AiModelConfig, call_type: str, prompt_chars: int = 0) -> ChatOpenAI:
    """构造一个**不带任何输出约束**的对话模型（工具绑定前的底座）。

    与 `build_chat_model` 的差别只有一处：这里不 bind `response_format`。
    工具调用与结构化输出是两种互斥的输出方式（一个要模型写 function call，
    一个要模型写 JSON 正文），所以聊天走这条，纪要类调用走上面那条。
    """
    options: dict[str, Any] = {}
    # 推理开关：环境变量非空时它是权威（保持既有部署行为不变）；为空才按调用类型
    # 的策略走。参数形态由供应商决定，所以"要不要推理"与"怎么传参"分开声明。
    enabled = reasoning_enabled(config, call_type, prompt_chars)
    options["extra_body"] = providers.thinking_extra_body(
        providers.thinking_shape(config.base_url), enabled
    )
    # 按调用类型分档：LLM_MAX_TOKENS_<CALLTYPE> 优先于全局 LLM_MAX_TOKENS
    #（合并等大输出调用需要与思考 token 共享的预算分开管理，防截断根因复发）
    max_tokens = providers.max_tokens_env(call_type)
    if max_tokens:
        options["max_tokens"] = int(max_tokens)
    # 流式调用要拿到 token 用量（预算闸与成本核算都靠它）。langchain-openai 只在
    # 默认 base_url（api.openai.com）下自动开它，自定义网关（本项目的部署形态）
    # 一律要显式声明；拿到的是 `stream_options: {"include_usage": true}`，
    # 属 OpenAI 兼容协议的一部分。
    options["stream_usage"] = True
    return ChatOpenAI(
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


# 工具选择策略的两个取值。`required` 强制模型**必须**发起一次工具调用（首轮用它堵住
# "不检索就作答"），`auto` 是供应商默认（模型自己决定）。
TOOL_CHOICE_AUTO = "auto"
TOOL_CHOICE_REQUIRED = "required"


def build_tool_model(config: AiModelConfig, tools: list[dict[str, Any]], call_type: str,
                     prompt_chars: int = 0, *, tool_choice: str = TOOL_CHOICE_AUTO) -> Runnable:
    """构造**绑定工具**的对话模型。`tools` 是 OpenAI function calling 形状的声明。

    供应商能力先查声明表（`providers.supports_tool_calling`）：未声明的供应商
    在这里就报出可读错误，而不是等着请求被 400 打回来——后者看起来像"模型不听话"。

    `tool_choice` 只在**非默认值**时才发进请求：默认的 `auto` 不发，既有调用路径的
    请求体因此一个字节都没变（减少"改动波及无关路径"的面）。
    """
    if not providers.supports_tool_calling(config.base_url):
        raise RuntimeError(
            f"模型配置 {config.name}（{providers.host_of(config.base_url)}）未声明支持工具调用；"
            "确认可用后在 common/providers.KNOWN_TOOL_CALLING 登记，聊天功能才会启用")
    bound = chat_model(config, call_type, prompt_chars)
    if tool_choice and tool_choice != TOOL_CHOICE_AUTO:
        return bound.bind_tools(list(tools), tool_choice=tool_choice)
    return bound.bind_tools(list(tools))


def first_round_tool_choice(config: AiModelConfig, call_type: str, prompt_chars: int = 0) -> str:
    """首轮的工具选择策略：能力允许就 `required`，否则 `auto`（退回"提醒"路径）。

    为什么值得强制：实测 27 轮里 **24 轮**首轮完全不检索、靠一句提醒才补救，
    为此多花了约 26% 的 token（≈102k/390k）与一次往返。提示词要求是概率性的，
    `tool_choice` 是协议层的约束——把"要不要先查"从模型的自由裁量变成不可绕过的动作。

    为什么必须按供应商判定后又退回：`required` **不是所有供应商在所有配置下都支持**
    （DeepSeek 开着思考会 400，见 `KNOWN_REQUIRED_TOOL_CHOICE`）。判不了就走原路径，
    图里的提醒与 `no_retrieval` 标注仍然在——它们是兜底，不是被替换掉的旧实现。
    """
    if providers.supports_required_tool_choice(
            config.base_url, thinking=reasoning_enabled(config, call_type, prompt_chars)):
        return TOOL_CHOICE_REQUIRED
    return TOOL_CHOICE_AUTO



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
            elapsed_ms = int((perf_counter() - started) * 1000)
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
                elapsed_ms=elapsed_ms,
                # LangChain 统一整理的 Token 用量，包含 input_tokens、output_tokens、total_tokens
                usage_json=message.usage_metadata,
                # 成本按"调用时的单价 × 本次真实用量"当场算出并落库
                cost_micro=_cost_micro(config, message.usage_metadata),
            )
            _observe_call(config, call_type=call_type, status="SUCCEEDED",
                          elapsed_ms=elapsed_ms, usage=message.usage_metadata)
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
            elapsed_ms = int((perf_counter() - started) * 1000)
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
                elapsed_ms=elapsed_ms,
                error_message=detail,
            )
            _observe_call(config, call_type=call_type, status="FAILED",
                          elapsed_ms=elapsed_ms, usage=None)
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


# --------------------------------------------------------------------------- #
# 问答：工具绑定 + 流式
# --------------------------------------------------------------------------- #

CHAT_CALL_TYPE = "CHAT"


def _message_chars(messages: Any) -> int:
    """消息的字符数，用于估算用量与记录请求大小。"""
    total = 0
    for message in messages or []:
        content = getattr(message, "content", message)
        total += len(str(content))
    return total


def usage_or_estimate(message: Any, prompt_chars: int) -> dict:
    """这次调用的用量。拿不到真实用量就按字符数粗估，并**标明这是估算**。

    为什么要有估算分支：用量来自流式响应的最后一个 chunk（`include_usage`）。
    个别网关不返回它——此时若按 0 记账，预算闸会静默失效（看起来一切正常，
    实际没有上限），比"数字不准"危险得多。所以宁可给一个标了 `estimated` 的
    粗估：它让闸门继续有效，也让观测页能一眼分出"量出来的"和"估出来的"。
    """
    usage = dict(getattr(message, "usage_metadata", None) or {})
    if usage.get("total_tokens"):
        return usage
    answer_chars = len(str(getattr(message, "content", "") or ""))
    return {
        "prompt_tokens": int(prompt_chars), "completion_tokens": answer_chars,
        "total_tokens": int(prompt_chars) + answer_chars,
        "estimated": True,
        "estimate_basis": "按输入与输出字符数粗估，网关未返回 usage 明细",
    }


async def stream_chat(
    config: AiModelConfig,
    messages: list[Any],
    *,
    tools: list[dict[str, Any]] | None = None,
    call_type: str = CHAT_CALL_TYPE,
    biz_id: int = 0,
    label: str = "问答模型",
    model: Any = None,
    tool_choice: str = TOOL_CHOICE_AUTO,
) -> AsyncIterator[tuple[str, Any]]:
    """流式调一次对话模型，逐个 token 产出，最后产出完整消息。

    产出的事件：`("token", 文本片段)` 与 `("message", AIMessage)`（最后一个）。

    三条与仓库口径一致的约定：

    1. **一行 = 一次真实调用**：无论成功失败都写一条 `ai_call_log`，用量取自
       最后一个 chunk 的 `usage_metadata`（拿不到就粗估并标 `estimated`）。
    2. **重试只在"还没吐字"之前**：已经推给客户端的 token 收不回来，半截回答
       后面再补一遍是错的。首个 token 之前的瞬态失败照常退避重试。
    3. **客户端中断也要留痕**：调用真的发生过（网关那边已经计费），只是没有
       完整答案——落一条 FAILED 日志并写明"客户端中断"，而不是当它没发生。
    """
    request_size = len("".join(str(getattr(m, "content", "")) for m in messages or []).encode("utf-8"))
    prompt_chars = _message_chars(messages)
    attempts = _max_attempts()
    emitted = False
    active = model or build_tool_model(config, tools or [], call_type, prompt_chars=prompt_chars,
                                       tool_choice=tool_choice)

    for attempt in range(1, attempts + 1):
        started = perf_counter()
        accumulated: Any = None
        answer_chars = 0
        try:
            async for chunk in active.astream(messages):
                text = _chunk_text(chunk)
                if text:
                    emitted = True
                    answer_chars += len(text)
                    yield ("token", text)
                accumulated = chunk if accumulated is None else accumulated + chunk
            message = accumulated if accumulated is not None else AIMessage(content="")
            usage = usage_or_estimate(message, prompt_chars)
            elapsed_ms = int((perf_counter() - started) * 1000)
            await AiCallLog.create(
                request_id="AIC" + uuid4().hex.upper(),
                call_type=call_type, biz_id=biz_id,
                model_config_id=config.id, model_name=config.model_name,
                request_size=request_size,
                response_size=len(str(getattr(message, "content", "") or "").encode("utf-8")),
                status="SUCCEEDED",
                elapsed_ms=elapsed_ms,
                usage_json=usage,
                cost_micro=_cost_micro(config, usage),
            )
            _observe_call(config, call_type=call_type, status="SUCCEEDED",
                          elapsed_ms=elapsed_ms, usage=usage)
            yield ("message", message)
            return
        except Exception as error:  # noqa: BLE001 - 逐类分流后再抛，与 invoke_json 同款
            detail = (str(error) or type(error).__name__)[:2000]
            elapsed_ms = int((perf_counter() - started) * 1000)
            await AiCallLog.create(
                request_id="AIC" + uuid4().hex.upper(),
                call_type=call_type, biz_id=biz_id,
                model_config_id=config.id, model_name=config.model_name,
                request_size=request_size, response_size=answer_chars,
                status="FAILED",
                elapsed_ms=elapsed_ms,
                error_message=f"[尝试 {attempt}/{attempts}] {detail}",
            )
            if attempt < attempts and not emitted and is_transient(error):
                await asyncio.sleep(0.5 * (2 ** (attempt - 1)) + random.uniform(0, 0.3))
                continue
            raise RuntimeError(f"{label}调用失败：{detail[:1000]}") from error
        except BaseException as error:  # noqa: BLE001 - 客户端中断（GeneratorExit 等）
            elapsed_ms = int((perf_counter() - started) * 1000)
            await AiCallLog.create(
                request_id="AIC" + uuid4().hex.upper(),
                call_type=call_type, biz_id=biz_id,
                model_config_id=config.id, model_name=config.model_name,
                request_size=request_size, response_size=answer_chars,
                status="FAILED",
                elapsed_ms=elapsed_ms,
                error_message=f"客户端中断，本次调用未产出完整回答（{type(error).__name__}）",
            )
            _observe_call(config, call_type=call_type, status="FAILED",
                          elapsed_ms=elapsed_ms, usage=None)
            raise


def _vector_of(item: Any) -> list[float]:
    """从一条嵌入结果里取向量。

    实测踩过：写成 `item.vector` 时本地测试（打桩了 client）全绿，真调用才报
    `'Embedding' object has no attribute 'vector'`——**打桩的返回值形状是测试自己
    编的**，所以这类错误只能靠"跑一次真的"发现。这里以协议字段 `embedding` 为准，
    并对旧式 `vector` 兜一层，取不到就报能看懂的错误。
    """
    for attribute in ("embedding", "vector"):
        value = getattr(item, attribute, None)
        if value is None and isinstance(item, dict):
            value = item.get(attribute)
        if value is not None:
            return list(value)
    raise RuntimeError(f"嵌入结果里没有向量字段（可用字段：{sorted(vars(item)) if hasattr(item, '__dict__') else type(item).__name__}）")


def _embedding_usage(response: Any) -> dict | None:
    """嵌入响应的用量转成 dict（不同网关给的对象/字典两种形态都接）。"""
    usage = getattr(response, "usage", None)
    if usage is None:
        return None
    if isinstance(usage, dict):
        return usage
    return {key: getattr(usage, key, None) for key in
            ("prompt_tokens", "total_tokens", "input_tokens", "output_tokens")}


def _chunk_text(chunk: Any) -> str:
    """从流式 chunk 里取正文。多模态形态下 content 是列表，只取文本部分。"""
    content = getattr(chunk, "content", "")
    if isinstance(content, list):
        return "".join(str(part.get("text") or "") for part in content
                       if isinstance(part, dict))
    return str(content or "")


def has_tool_result(messages: list[Any]) -> bool:
    """消息流里有没有工具结果——**"这一轮是不是首轮"的判据**。

    为什么不数轮次：轮次是图的内部状态，而"已经检索过了吗"正是我们真正要问的问题。
    两者在本图里等价（每轮至多一次模型调用，工具结果只由 ToolNode 追加），
    但前者要在图与调用方之间多传一个参数，后者连"提醒之后重问"这种路径都自然覆盖
    ——提醒之后仍然没有工具结果，那么强制检索依然成立。
    """
    return any(getattr(message, "type", "") == "tool" for message in messages or [])


def make_model_caller(
    config: AiModelConfig,
    *,
    tools: list[dict[str, Any]] | None = None,
    call_type: str = CHAT_CALL_TYPE,
    biz_id: int = 0,
    label: str = "问答模型",
    model: Any = None,
    first_round_tool_choice: str = TOOL_CHOICE_AUTO,
) -> Callable[[list[Any]], Awaitable[Any]]:
    """把一次流式调用包成状态图要的"调模型"动作：进去是消息列表，出来是 AIMessage。

    图不关心流式——token 是通过 LangChain 的回调体系冒上去的，SSE 那一层用
    `astream_events` 接（见 api/chat.py）。这里只保证记账与失败分类都走过
    `stream_chat` 这一个出入口。

    `first_round_tool_choice` 决定**还没有任何工具结果时**这一次要不要强制检索，
    之后自动回到 `auto`（模型已经查到东西了，再强制它继续调工具是错的）。
    """
    async def call_model(messages: list[Any]) -> Any:
        choice = TOOL_CHOICE_AUTO
        if first_round_tool_choice != TOOL_CHOICE_AUTO and not has_tool_result(messages):
            choice = first_round_tool_choice
        message: Any = None
        async for kind, payload in stream_chat(config, messages, tools=tools, call_type=call_type,
                                               biz_id=biz_id, label=label, model=model,
                                               tool_choice=choice):
            if kind == "message":
                message = payload
        return message

    return call_model


# --------------------------------------------------------------------------- #
# 重排（检索后、进上下文前）
# --------------------------------------------------------------------------- #

# DashScope 原生 rerank 接口的固定路径（compatible-mode 不提供 rerank）
RERANK_PATH = "/services/rerank/text-rerank/text-rerank"
# 单次重排的文档上限：给再多既慢又贵，且模型对长列表的排序质量会掉（参数上限 500，
# 但检索侧只会有几十条候选）。这是**请求形态**的约束，不是调优阈值。
RERANK_MAX_DOCUMENTS = 50


def rerank_model() -> str:
    return os.getenv("RERANK_MODEL", "").strip() or "qwen3.7-text-rerank"


def _rerank_base(config: AiModelConfig) -> str:
    """rerank 的 API 根地址。

    优先 `RERANK_BASE_URL`（可指向专属端点），否则退回 `DASHSCOPE_BASE_URL`，
    最后才用配置里的 base_url 去掉 `/compatible-mode/v1`——**rerank 是 DashScope 原生
    接口**，与对话/嵌入的 OpenAI 兼容前缀不是一条路。
    """
    explicit = (os.getenv("RERANK_BASE_URL") or "").strip().rstrip("/")
    if explicit:
        return explicit
    native = (os.getenv("DASHSCOPE_BASE_URL") or "").strip().rstrip("/")
    if native:
        return native
    base = (config.base_url or "").rstrip("/")
    return base.replace("/compatible-mode/v1", "")


async def rerank_documents(config: AiModelConfig, query: str, documents: list[str], *,
                           biz_id: int, top_n: int | None = None,
                           call_type: str = "KB_RERANK", client: Any = None) -> list[tuple[int, float]]:
    """调专用 rerank 模型给候选打分，返回 `(原下标, 相关度)` 按分数降序。

    为什么用专用模型而不是 LLM 排序：公开基准里专用 cross-encoder 类重排的
    NDCG 与延迟都优于 LLM 重排（点式 LLM 甚至会把一阶段召回拖垮），成本也低一个量级。

    记账口径与其它调用一致：**成功与失败各写一行 `ai_call_log`**，一行=一次真实请求；
    `model_config_id` 用传入配置（检索侧用嵌入配置的归属，模型名写 rerank 模型本身）。
    """
    texts = [str(text or "") for text in documents][:RERANK_MAX_DOCUMENTS]
    if not texts:
        return []
    limit = len(texts) if top_n is None else max(1, min(int(top_n), len(texts)))
    payload = {
        "model": rerank_model(),
        "input": {"query": query, "documents": texts},
        "parameters": {"return_documents": False, "top_n": limit},
    }
    url = _rerank_base(config) + RERANK_PATH
    owns_client = client is None
    if owns_client:
        # httpx 没有 max_retries 参数（那是 openai SDK 的）；要关掉重试得走 transport。
        # 这类参数名错误本地打桩测不出来（桩不构造真 client），只有真调用才暴露。
        client = httpx.AsyncClient(
            timeout=float(os.getenv("RERANK_TIMEOUT_SECONDS", "20")),
            transport=httpx.AsyncHTTPTransport(retries=0))
    started = perf_counter()
    try:
        response = await client.post(url, json=payload,
                                     headers={"Authorization": f"Bearer {config.api_key}"})
        elapsed_ms = int((perf_counter() - started) * 1000)
        if response.status_code >= 400:
            detail = f"rerank 返回 {response.status_code}：{response.text[:300]}"
            raise RuntimeError(detail)
        body = response.json()
        results = ((body.get("output") or {}).get("results") or [])
        ranked = [(int(item.get("index")), float(item.get("relevance_score") or 0.0))
                  for item in results if isinstance(item, dict) and item.get("index") is not None]
        if not ranked:
            raise RuntimeError(f"rerank 返回里没有 results：{str(body)[:200]}")
        # 按分数降序；未返回的文档（top_n 截断）不参与
        ranked.sort(key=lambda pair: pair[1], reverse=True)
        await AiCallLog.create(
            request_id="AIC" + uuid4().hex.upper(),
            call_type=call_type, biz_id=biz_id,
            model_config_id=config.id, model_name=rerank_model(),
            request_size=sum(len(text.encode("utf-8")) for text in texts) + len(query),
            response_size=sum(len(str(pair[1])) for pair in ranked),
            status="SUCCEEDED", elapsed_ms=elapsed_ms,
            usage_json=body.get("usage") or {},
            cost_micro=_cost_micro(config, body.get("usage") or {}),
        )
        _observe_call(config, call_type=call_type, status="SUCCEEDED",
                      elapsed_ms=elapsed_ms, usage=body.get("usage") or {})
        return ranked
    except Exception as error:  # noqa: BLE001 - 记账后原样抛出，由调用方决定降级
        elapsed_ms = int((perf_counter() - started) * 1000)
        detail = (str(error) or type(error).__name__)[:1000]
        await AiCallLog.create(
            request_id="AIC" + uuid4().hex.upper(),
            call_type=call_type, biz_id=biz_id,
            model_config_id=config.id, model_name=rerank_model(),
            request_size=sum(len(text.encode("utf-8")) for text in texts) + len(query),
            response_size=0, status="FAILED", elapsed_ms=elapsed_ms,
            error_message=detail,
        )
        _observe_call(config, call_type=call_type, status="FAILED",
                      elapsed_ms=elapsed_ms, usage=None)
        raise
    finally:
        if owns_client:
            await client.aclose()


# --------------------------------------------------------------------------- #
# 嵌入（知识库检索用）
# --------------------------------------------------------------------------- #

# 嵌入一律走 OpenAI 兼容的 /embeddings 端点：百炼的 compatible-mode 就提供它，
# 与其他模型配置共用一份 base_url/api_key，不额外引 SDK。
# 百炼 text-embedding-v4 单次最多 10 条，超了会报错，所以在这里分批。
_EMBEDDING_BATCH_SIZE = 10
_DEFAULT_EMBEDDING_DIM = 1024


def embedding_dim() -> int:
    return int(os.getenv("EMBEDDING_DIM", str(_DEFAULT_EMBEDDING_DIM)))


async def embed_texts(
    config: AiModelConfig,
    texts: list[str],
    *,
    biz_id: int,
    call_type: str = "KB_EMBEDDING",
    label: str = "嵌入模型",
    client: Any = None,
) -> list[list[float]]:
    """把一批文本转成向量，返回与输入等长（顺序一致）的向量列表。

    记账口径与 invoke_json 一致：**每批各写一行 ai_call_log，成功失败都写**。
    分批不是实现细节而是"一行=一次真实调用"的要求——一次分批请求就是一次真实调用。

    `client` 可注入（测试用桩替换网络）；不是实例时按 OpenAI 兼容协议自行构造。
    """
    if not texts:
        return []

    owns_client = client is None
    if owns_client:
        client = openai.AsyncOpenAI(api_key=config.api_key, base_url=config.base_url,
                                    timeout=config.timeout_seconds, max_retries=0)

    dimension = embedding_dim()
    vectors: list[list[float]] = []
    attempts = _max_attempts()
    try:
        for start in range(0, len(texts), _EMBEDDING_BATCH_SIZE):
            batch = texts[start:start + _EMBEDDING_BATCH_SIZE]
            last_error: BaseException | None = None
            for attempt in range(1, attempts + 1):
                started = perf_counter()
                request_size = sum(len(t.encode("utf-8")) for t in batch)
                try:
                    response = await client.embeddings.create(
                        model=config.model_name, input=batch, dimensions=dimension,
                    )
                    data = sorted(response.data, key=lambda item: item.index)
                    # OpenAI 兼容协议里向量在 `embedding` 字段上（openai SDK 的
                    # Embedding 模型、百炼 compatible-mode 的返回都是这个形状）。
                    # 只有 DashScope 自有 SDK 才叫 vector——那套接口这里不用。
                    batch_vectors = [_vector_of(item) for item in data]
                    usage = _embedding_usage(response)
                    if len(batch_vectors) != len(batch):
                        raise RuntimeError(f"嵌入返回条数不符：请求 {len(batch)} 条，返回 {len(batch_vectors)} 条")
                    for vector in batch_vectors:
                        if len(vector) != dimension:
                            raise RuntimeError(f"嵌入维度不符：期望 {dimension}，实际 {len(vector)}")
                    elapsed_ms = int((perf_counter() - started) * 1000)
                    await AiCallLog.create(
                        request_id="AIC" + uuid4().hex.upper(),
                        call_type=call_type,
                        biz_id=biz_id,
                        model_config_id=config.id,
                        model_name=config.model_name,
                        request_size=request_size,
                        response_size=sum(len(v) for v in batch_vectors) * 4,
                        status="SUCCEEDED",
                        elapsed_ms=elapsed_ms,
                        usage_json=usage,
                        cost_micro=_cost_micro(config, usage),
                    )
                    _observe_call(config, call_type=call_type, status="SUCCEEDED",
                                  elapsed_ms=elapsed_ms, usage=usage)
                    vectors.extend(batch_vectors)
                    break
                except Exception as error:  # noqa: BLE001 - 逐类分流后再抛，与 invoke_json 同款处理
                    last_error = error
                    detail = (str(error) or type(error).__name__)[:2000]
                    elapsed_ms = int((perf_counter() - started) * 1000)
                    await AiCallLog.create(
                        request_id="AIC" + uuid4().hex.upper(),
                        call_type=call_type,
                        biz_id=biz_id,
                        model_config_id=config.id,
                        model_name=config.model_name,
                        request_size=request_size,
                        response_size=0,
                        status="FAILED",
                        elapsed_ms=elapsed_ms,
                        error_message=f"[尝试 {attempt}/{attempts}] {detail}",
                    )
                    _observe_call(config, call_type=call_type, status="FAILED",
                                  elapsed_ms=elapsed_ms, usage=None)
                    # 瞬态错误退避重试（与对话调用同一套判定与退避公式）
                    if attempt < attempts and is_transient(error):
                        await asyncio.sleep(0.5 * (2 ** (attempt - 1)) + random.uniform(0, 0.3))
                        continue
                    break
            if last_error is not None and len(vectors) < start + len(batch):
                raise RuntimeError(f"{label}调用失败：{(str(last_error) or type(last_error).__name__)[:1000]}") from last_error
    finally:
        if owns_client:
            await client.close()
    return vectors
