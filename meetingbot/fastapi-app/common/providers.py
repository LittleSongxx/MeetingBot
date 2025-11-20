"""文本模型供应商的能力声明。

为什么要单独声明：结构化输出的可用方式**取决于供应商**，而旧实现把它硬编码成
`response_format={"type":"json_schema","strict":true}`。这在支持该模式的供应商上没问题，
在不支持的供应商上会直接请求失败——而失败原因看起来像"模型不听话"，实际是协议不支持。

因此把"这家供应商支持哪种结构化输出"变成**一张声明表**，由 `structured_output_strategy`
解析。新增供应商 = 加一行，不改调用代码。

两种策略：

* `json_schema`：请求里带严格 JSON Schema。约束最强，但要求供应商实现 OpenAI 的
  json_schema 模式。
* `json_object`：只声明"返回 JSON 对象"，结构约束靠**提示词里的 schema 说明**
  （`common/model_json.build_schema_instruction` 已经会把它附在提示词后）
  加**落库前的校验器**（`invoke_json(validator=...)`）。约束弱一些，但通用。

未在表中列出的主机默认按 `json_object` 处理：**保守默认优先于乐观默认**——猜错的代价是
请求直接失败，而 `json_object` 在任何实现了 OpenAI 兼容接口的供应商上都能工作，
且契约层与校验器仍会兜住结构。

已知的供应商注意事项（来自各自官方文档，写入此处以免后人重复踩）：

* DeepSeek：`response_format` 只支持 `text` 与 `json_object`；严格的 schema 只能通过
  tool calling 的 `strict`（beta）实现，**不能**通过 `response_format`。官方还提示
  JSON 模式必须自己在提示词里要求输出 JSON，否则可能一直吐空白直到触顶。
* DashScope/通义：支持 `json_schema` 严格模式；`enable_thinking` 是它专有的额外参数
  （见 `model_factory` 里按模型名前缀判断的处理）。
"""

from __future__ import annotations

import os


from urllib.parse import urlparse

JSON_SCHEMA = "json_schema"
JSON_OBJECT = "json_object"

STRUCTURED_OUTPUT_STRATEGIES = (JSON_SCHEMA, JSON_OBJECT)

# 主机名后缀 -> 支持的结构化输出策略。主机名后缀匹配，因此带端口或路径都不影响。
KNOWN_STRUCTURED_OUTPUT: dict[str, str] = {
    "api.deepseek.com": JSON_OBJECT,
    "dashscope.aliyuncs.com": JSON_SCHEMA,
    "api.openai.com": JSON_SCHEMA,
    "open.bigmodel.cn": JSON_SCHEMA,
    "api.moonshot.cn": JSON_SCHEMA,
    "ark.cn-beijing.volces.com": JSON_SCHEMA,
}

DEFAULT_STRATEGY = JSON_OBJECT


def host_of(base_url: str) -> str:
    """从 base_url 取出主机名；非法输入返回空串而不是抛异常。"""
    if not isinstance(base_url, str) or not base_url.strip():
        return ""
    candidate = base_url.strip()
    if "://" not in candidate:
        candidate = "https://" + candidate
    try:
        return (urlparse(candidate).hostname or "").lower()
    except ValueError:
        return ""


def structured_output_strategy(base_url: str) -> str:
    """这家供应商该用哪种结构化输出方式。未声明的一律按保守策略处理。"""
    host = host_of(base_url)
    if not host:
        return DEFAULT_STRATEGY
    for known_host, strategy in KNOWN_STRUCTURED_OUTPUT.items():
        if host == known_host or host.endswith("." + known_host):
            return strategy
    return DEFAULT_STRATEGY


def declared_providers() -> dict[str, str]:
    """暴露声明表，供文档与测试使用。"""
    return dict(KNOWN_STRUCTURED_OUTPUT)


# --------------------------------------------------------------------------- #
# 推理（思考）控制
# --------------------------------------------------------------------------- #

# 各家的"推理开关"参数形态不同，因此分成**任务性质**与**参数形态**两件事声明：
# 前者由调用类型决定（与供应商无关），后者由主机决定。混在一起写就会出现
# "换个供应商就得改调用代码"的老问题。
THINKING_PARAM_SHAPE: dict[str, str] = {
    "api.deepseek.com": "thinking_object",       # extra_body={"thinking": {"type": ...}}
    "dashscope.aliyuncs.com": "enable_thinking",  # extra_body={"enable_thinking": bool}
    "open.bigmodel.cn": "thinking_level",          # 智谱 GLM-5.3：始终思考，只接受 low/high/max（实测 code 1210）
}
DEFAULT_THINKING_SHAPE = "thinking_object"

# 按调用类型声明是否需要推理。依据是**任务性质**：
# 机械抽取/合并不需要推理，而推理会显著拖慢串行的分段循环、并吃掉输出预算；
# 判断"原文里到底有没有依据""在约束下改写"是真正的推理任务。
REASONING_BY_CALL_TYPE: dict[str, bool] = {
    "minutes": False,
    "speaker_match": False,
    "agent_review": True,
    "agent_refine": True,
    # 支撑复核是判定类任务：判"依据是否支撑这条意见"，开推理
    "agent_review_support": True,
}
DEFAULT_REASONING = False


def thinking_shape(base_url: str) -> str:
    host = host_of(base_url)
    for known_host, shape in THINKING_PARAM_SHAPE.items():
        if host == known_host or host.endswith("." + known_host):
            return shape
    return DEFAULT_THINKING_SHAPE


def wants_reasoning(call_type: str, *, override: str = "") -> bool:
    """这个调用类型要不要开推理。

    `override` 来自环境变量 LLM_ENABLE_THINKING：非空时**它是权威**（保持既有部署
    行为不变），为空时才按调用类型的默认策略走。
    """
    normalized = (override or "").strip().lower()
    if normalized in {"true", "false"}:
        return normalized == "true"
    return REASONING_BY_CALL_TYPE.get((call_type or "").lower(), DEFAULT_REASONING)


def thinking_extra_body(shape: str, enabled: bool) -> dict:
    """按参数形态生成 extra_body 片段。"""
    if shape == "enable_thinking":
        return {"enable_thinking": enabled}
    if shape == "thinking_level":
        # 智谱 GLM-5.3 实测（HTTP 400 / code 1210 原文）：
        # 「该模型始终思考，不支持关闭思考；请使用 low、high 或 max」。
        # 因此这个形态下 `enabled=False` **不可表达**——不发参数它也照样思考。
        # 诚实映射：请求关闭时给最低档 low（发 disabled 会被 400 拒绝），
        # 并且**这个形态下的「关思考省 token」收益不存在**，成本模型要按思考开启算。
        return {"thinking": {"type": "low" if not enabled else "high"}}
    return {"thinking": {"type": "enabled" if enabled else "disabled"}}


def reasoning_is_billed_as_output() -> bool:
    """实测结论，供成本分析引用。

    deepseek-flash 默认开启思考，实测一次结构化抽取中 **97% 的输出 token 是推理
    token**（2242 输出里 2175 是推理），且关闭后同样的答案只用 81 个输出 token、
    延迟从 10.29s 降到 0.57s。因此推理 token 计入输出计费与 max_tokens 预算——
    把 max_tokens 卡在原值会让长纪要的 JSON 被推理挤掉而截断。
    """
    return True


# --------------------------------------------------------------------------- #
# 输出预算：实测的硬限制
# --------------------------------------------------------------------------- #

# 一次响应最多能产出多少输出 token。**这是实测值，不是文档推断**：
# 同一请求在非思考模式下发送 max_tokens=16384 仍停在 8192（且截断在半条目
# 记录里），思考模式下同参数产出 34867。因此请求大输出时必须走思考模式——
# 这是为拿到预算，不是因为任务需要推理。
MAX_OUTPUT_TOKENS_BY_MODE: dict[str, int] = {
    "non_thinking": 8192,
    "thinking": 65536,
}

# 输入超过多少字符时该申请扩展输出预算。
#
# 实测依据（两次真实失败）：
# * 合并 38,804 字的会议时，非思考模式停在 8192 token，截断在半条目记录里；
# * 分段抽取一个接近 12,000 字上限的分块时，同样撑爆 8192，返回的 JSON 无法解析成对象。
# 因此"输出会随输入增长"的调用有两类：合并，以及**接近上限的分块抽取**。
# 8192 token 约合 8k 个中文字符的 JSON，据此把阈值定在 10,000 字符——
# 分块上限是 12,000 字，加上提示词正文后接近该阈值，正好把"接近满块"的情况纳入。
EXTENDED_OUTPUT_INPUT_THRESHOLD_CHARS = 10000

# 明确需要大输出的调用类型：输出与输入规模同阶的两类调用。
LARGE_OUTPUT_CALL_TYPES = frozenset({"minutes"})


def output_budget(mode: str) -> int:
    return MAX_OUTPUT_TOKENS_BY_MODE.get(mode, MAX_OUTPUT_TOKENS_BY_MODE["non_thinking"])


def needs_extended_output_budget(call_type: str, prompt_chars: int) -> bool:
    """这次调用是否需要扩展输出预算（即必须走思考模式）。"""
    if (call_type or "").lower() not in LARGE_OUTPUT_CALL_TYPES:
        return False
    return int(prompt_chars or 0) >= EXTENDED_OUTPUT_INPUT_THRESHOLD_CHARS


def max_tokens_env(call_type: str) -> str:
    """该调用类型实际生效的 max_tokens 环境值。

    优先级：`LLM_MAX_TOKENS_<CALLTYPE>`（如 LLM_MAX_TOKENS_MINUTES）>
    全局 `LLM_MAX_TOKENS`。存在的理由：全局一档时，思考类调用的推理 token 与
    JSON 输出共享同一预算——合并步的实际 JSON 预算可能反而比分段步还小，
    这是两次 LengthFinishReasonError 截断的根因。按类型分档后，大输出调用
    可以单独给足预算而不放大其余调用的成本。
    """
    per_type = os.getenv(f"LLM_MAX_TOKENS_{(call_type or '').upper()}", "").strip()
    if per_type:
        return per_type
    return os.getenv("LLM_MAX_TOKENS", "").strip()


def applied_output_budget(call_type: str, prompt_chars: int, *, env_max_tokens: str = "") -> dict:
    """这次调用**实际**会用的输出预算，以及它是怎么来的。

    存在的理由：`MAX_OUTPUT_TOKENS_BY_MODE` 声明的是**供应商能力上限**，而真正发给
    请求的 `max_tokens` 来自环境变量 `LLM_MAX_TOKENS`。两者不是一回事——失败信息曾经
    把 65536 当作「思考模式上限」打出来，而实际请求的 max_tokens 可能只有环境变量设的
    16384。把「声明的能力」读成「生效的预算」会让排查走错方向：看起来预算很宽裕，
    实际早已被环境变量压住。因此 `declared_ceiling_tokens` 与 `applied_max_tokens`
    必须分开读，本函数把它们放在同一个返回值里。
    """
    extended = needs_extended_output_budget(call_type, prompt_chars)
    mode = "thinking" if extended else "non_thinking"
    requested = (env_max_tokens or "").strip()
    try:
        applied = int(requested) if requested else None
    except ValueError:
        applied = None
    return {
        "call_type": call_type,
        "prompt_chars": int(prompt_chars or 0),
        "extended_budget_requested": extended,
        "mode": mode,
        "declared_ceiling_tokens": output_budget(mode),
        "applied_max_tokens": applied,
        "applied_source": (
            "环境变量 LLM_MAX_TOKENS" if applied is not None else "未指定，由供应商默认决定"
        ),
    }


def truncation_marker(error_text: str) -> bool:
    """这个错误是不是"输出被长度上限截断"。

    单独识别它的理由：截断当前表现为 JSON 解析失败，看起来像模型不听话，
    实际是输出预算不够。两者要分开报告，否则排查方向会错。
    """
    if not isinstance(error_text, str):
        return False
    lowered = error_text.lower()
    return any(marker in lowered for marker in (
        "length limit was reached",
        "length limit reached",
        "finish_reason=length",
        "max_tokens",
    ))
