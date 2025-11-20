import json
import re

# 最近一次解析的抢救诊断，供调用方记录到调用日志
LAST_SALVAGE: dict = {"repaired": False, "dropped_chars": 0, "attempts": 0}

from langchain_core.output_parsers import JsonOutputParser


class SchemaJsonParser(JsonOutputParser):
    """按给定的 JSON Schema 约束模型输出，并把模型返回的文本解析成 JSON。

    parse() 沿用 JsonOutputParser 的实现：能剥掉 ```json 代码块围栏，也能忽略 JSON 后面多出来的文字。
    get_format_instructions() 换成中文说明，本项目发给模型的提示词都是中文。
    """

    # 各个服务里定义的 SCHEMA 常量，只在拼格式说明时使用
    schema_dict: dict = {}

    def get_format_instructions(self) -> str:
        return (
            "\n\n输出要求：只返回一个 JSON 对象，不要使用 Markdown 代码块，不要输出任何解释文字。"
            "JSON 必须符合下面的结构定义：\n" + json.dumps(self.schema_dict, ensure_ascii=False)
        )


def build_schema_instruction(schema: dict) -> str:
    """把 JSON Schema 拼成一段附加提示，告诉模型必须输出哪些字段。

    结构定义只写在各个服务的 SCHEMA 常量里，管理员在页面上改 Prompt 模板时
    不会影响这段结构约束。
    """
    return SchemaJsonParser(schema_dict=schema).get_format_instructions()


def parse_model_json(text: str) -> dict:
    """把模型返回的文本解析成字典，空文本、非 JSON 文本、JSON 数组都会抛出异常。

    常规解析失败时先做一次**有界抢救**：大而深的 JSON 里一处括号笔误会丢掉整块内容，
    回退到最近结构边界再补齐可以保住绝大部分，并把丢弃量记进 `LAST_SALVAGE`。
    """
    global LAST_SALVAGE
    try:
        result = SchemaJsonParser().parse(text or "")
    except Exception:
        # 先试**语法**修复（多余逗号），它比结构抢救更保守：只删逗号，不丢弃内容
        fixed, actions = repair_syntax(text or "")
        if actions:
            try:
                repaired = SchemaJsonParser().parse(fixed)
                if isinstance(repaired, dict):
                    LAST_SALVAGE = {"repaired": True, "dropped_chars": 0,
                                    "attempts": 1, "actions": actions}
                    return repaired
            except Exception:
                pass
        # 再试**悬挂字段合并**（提前闭括号）：同样不丢弃任何内容
        merged, merge_actions = repair_dangling_fields(text or "")
        if merge_actions:
            try:
                repaired = json.loads(merged)
                if isinstance(repaired, dict):
                    LAST_SALVAGE = {"repaired": True, "dropped_chars": 0,
                                    "attempts": 1, "actions": actions + merge_actions}
                    return repaired
            except Exception:
                pass
        # 最后试**结构**抢救（括号不平衡），代价是可能丢弃尾部内容
        salvaged, diagnosis = salvage_json(text or "")
        all_actions = actions + merge_actions
        if all_actions:
            diagnosis = dict(diagnosis, syntax_actions=all_actions)
        LAST_SALVAGE = diagnosis
        if salvaged is None:
            raise
        return salvaged
    LAST_SALVAGE = {"repaired": False, "dropped_chars": 0, "attempts": 0}
    if not isinstance(result, dict):
        # 容错解析器可能把结构损坏的文本"部分解析"成非字典（实测：截断的 JSON 会走到这里），
        # 因此非字典分支同样要尝试抢救——只挂在解析异常路径上会漏掉这一整类。
        # 悬挂字段合并优先于结构抢救：它不丢内容
        merged, merge_actions = repair_dangling_fields(text or "")
        if merge_actions:
            try:
                repaired = json.loads(merged)
                if isinstance(repaired, dict):
                    LAST_SALVAGE = {"repaired": True, "dropped_chars": 0,
                                    "attempts": 1, "actions": merge_actions}
                    return repaired
            except Exception:
                pass
        salvaged, diagnosis = salvage_json(text or "")
        if merge_actions:
            diagnosis = dict(diagnosis, syntax_actions=merge_actions)
        LAST_SALVAGE = diagnosis
        if salvaged is None:
            raise RuntimeError("模型返回的不是JSON对象")
        return salvaged
    return result


def _scan_state(text: str) -> tuple[int, bool]:
    """返回（未闭合的容器数, 是否停在字符串中间）。"""
    depth = 0
    in_string = False
    escaped = False
    for char in text:
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if in_string:
            if char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "{[":
            depth += 1
        elif char in "}]":
            depth -= 1
    return depth, in_string


def salvage_json(text: str, *, min_prefix: int = 200) -> tuple[dict | None, dict]:
    """尽最大努力从一段结构性损坏的 JSON 里抢救出可解析的部分。

    为什么需要它：产品让模型在一次调用里输出一大段深层嵌套 JSON（一个分块约 12k 字），
    **任何一处括号笔误都会让整块内容全丢**。实测 `vcsum_108`（最长会议）的最大分块
    就是这样失败的：解析器在 9127 位置发现多余的闭括号提前关闭了容器，
    其后 359 字符全部作废，本来可以保住的内容一起被丢弃。

    做法：从尾部逐级回退到最近的结构边界，补齐未闭合的容器后再解析，
    并把**丢弃了多少字符**如实返回。这样损失是有界的、可观测的，而不是全丢。

    返回 (解析结果或 None, 诊断信息)。诊断信息里 `repaired` 为真时说明发生了抢救。
    """
    diagnosis: dict = {"repaired": False, "dropped_chars": 0, "attempts": 0}
    if not isinstance(text, str) or not text.strip():
        return None, diagnosis

    depth, in_string = _scan_state(text)
    if depth <= 0 and not in_string:
        # 结构看起来是闭合的，损坏在中间，回退也救不回开头，直接交给调用方
        diagnosis["note"] = "结构闭合但解析失败，无法通过补齐抢救"
        return None, diagnosis

    # 从尾部回退到结构边界，逐次尝试补齐
    for cut in range(len(text) - 1, min_prefix, -1):
        if text[cut] not in "}]":
            continue
        prefix = text[: cut + 1]
        depth, in_string = _scan_state(prefix)
        if in_string or depth < 0:
            continue
        padded = prefix + ("]" * depth) if depth else prefix
        # 只补右方括号：分块结果的外层是数组，闭合它比补 } 更安全
        diagnosis["attempts"] += 1
        try:
            result = json.loads(padded)
        except Exception:
            continue
        if isinstance(result, dict):
            diagnosis.update(repaired=True, dropped_chars=len(text) - len(prefix))
            return result, diagnosis
    diagnosis["note"] = "回退到最小前缀仍未得到可解析对象"
    return None, diagnosis

# 大模型 JSON 最常见的**语法**笔误（区别于结构缺失）：
# * 尾随逗号：`{"a": 1, }` —— 实测一次 AGENT_REVIEW 就因此被严格解析器拒绝，
#   而该响应完整、括号平衡，只是多了一个逗号。
# * 连续逗号：`{"a": 1,, "b": 2}`
# 这类修复是**有界且可记录**的：只做删除多余逗号这一件事，不做任何语义猜测。
_TRAILING_COMMA = re.compile(r",(\s*[}\]])")
_DOUBLE_COMMA = re.compile(r",\s*,")


def repair_syntax(text: str) -> tuple[str, list[str]]:
    """删除多余的逗号。返回（修复后文本, 做过的动作）。不做其他任何改动。"""
    actions: list[str] = []
    fixed = text
    if _DOUBLE_COMMA.search(fixed):
        fixed = _DOUBLE_COMMA.sub(",", fixed)
        actions.append("collapsed_double_comma")
    for _ in range(3):
        updated = _TRAILING_COMMA.sub(r"\1", fixed)
        if updated == fixed:
            break
        fixed = updated
        actions.append("removed_trailing_comma")
    return fixed, actions


def repair_dangling_fields(text: str) -> tuple[str, list[str]]:
    """把"提前闭合 + 悬挂字段"拼回一个对象。返回（修复后文本, 做过的动作）。

    实测形态一（2026-10-05，vcsum_221 的 AGENT_REVIEW）：模型把根对象提前闭合，
    剩余字段以 `, "key": ...` 续在后面、最末再补一个闭括号——
    `{"passed": false, "issues": [...]} , "conclusion": "..."}`。
    实测形态二（同日，vcsum_72 的 AGENT_REFINE）：闭括号后还多一个游离的 `]`
    （模型顺手多关了一个数组），然后才是悬挂字段——
    `{...六字段...}] , "dispositions": [...]}`。
    两种形态下内容都一个字符不少，严格解析却都报 Extra data。

    修复只做一件事：删掉根对象被提前闭合的那个 `}`（以及至多一个游离闭括号），
    让悬挂字段回到对象内部。三个硬条件，缺一不碰（不做语义猜测）：
    1. 首个完整 JSON 值必须解析为**字典**（两个独立对象 `{...}{...}` 不合并）；
    2. 余部剥掉至多一个游离 `]`/`}` 后必须以 `,` 开头、以 `}` 结尾；
    3. 拼接结果必须整体通过 json.loads，且合并后字段确实变多。
    """
    stripped = (text or "").lstrip()
    if not stripped.startswith("{"):
        return text, []
    decoder = json.JSONDecoder()
    try:
        first, end = decoder.raw_decode(stripped)
    except json.JSONDecodeError:
        return text, []
    if not isinstance(first, dict):
        return text, []
    remainder = stripped[end:].strip()
    stray_bracket = ""
    if remainder[:1] in ("]", "}"):
        # 至多容忍一个游离闭括号（形态二）；第二个出现就超出有界范围，不碰
        stray_bracket = remainder[0]
        remainder = remainder[1:].lstrip()
    if not (remainder.startswith(",") and remainder.endswith("}")):
        return text, []
    candidate = stripped[: end - 1] + remainder
    try:
        merged = json.loads(candidate)
    except json.JSONDecodeError:
        return text, []
    if not isinstance(merged, dict) or len(merged) <= len(first):
        # 合并后字段没有变多，说明不是"字段被关在外面"的形态
        return text, []
    actions = ["merged_dangling_fields"]
    if stray_bracket:
        actions.append("dropped_stray_bracket")
    return candidate, actions
