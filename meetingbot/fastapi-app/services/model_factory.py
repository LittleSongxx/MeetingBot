"""文本模型调用。

纪要生成、纪要自检、说话人建议匹配三处都通过 invoke_json 调用模型：

    ai_model_config 里的配置
      -> langchain_openai.ChatOpenAI 调百炼 OpenAI 兼容接口，要求严格按 JSON Schema 返回
      -> common.model_json 里的 langchain_core 解析器解析返回文本
      -> 成功和失败都写一条 ai_call_log

音视频转写走百炼录音文件识别接口，由 transcription_service 用 httpx 直接调用，不经过这里。
"""

import logging
from time import perf_counter
from typing import Any, Callable
from uuid import uuid4

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import Runnable
from langchain_openai import ChatOpenAI

from common.model_json import build_schema_instruction, parse_model_json
from models import AiCallLog, AiModelConfig

logger = logging.getLogger(__name__)


def build_chat_model(config: AiModelConfig, schema: dict, schema_name: str) -> Runnable:
    """按一条模型配置构造对话模型，并要求它严格按 schema 返回 JSON 对象。"""
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
    )
    # 严格结构化输出：模型返回的 JSON 必须符合 schema，必填字段一个都不能少
    return model.bind(
        response_format={
            "type": "json_schema",
            "json_schema": {"name": schema_name, "strict": True, "schema": schema},
        }
    )


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
    """
    started = perf_counter()
    # 请求大小按提示词的 UTF-8 字节数算，写进 ai_call_log.request_size
    request_size = len((system_prompt + user_prompt).encode("utf-8"))
    response_size = 0
    try:
        # schema 名称用调用类型的小写形式，例如 agent_review
        message = await build_chat_model(config, schema, call_type.lower()).ainvoke(
            [
                # 系统指令来自 Prompt 模板的 system_prompt 字段
                SystemMessage(system_prompt),
                # 结构要求附在用户提示词末尾，内容由调用方传入的 SCHEMA 生成
                HumanMessage(user_prompt + build_schema_instruction(schema)),
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
        logger.warning(
            "模型调用失败",
            extra={"call_type": call_type, "biz_id": biz_id, "model": config.model_name, "error": detail},
        )
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
        # 统一换成带模型名称的提示，例如“说话人匹配模型调用失败：……”，接口层把它转成页面提示
        raise RuntimeError(f"{label}调用失败：{detail[:1000]}") from error
