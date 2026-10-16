"""实体核查：答案里的"新东西"要能被标出来（**只标注，不拦截**）。

## 为什么需要它

实测（2026-10-16，q14）答案里出现了种子里**根本不存在**的人名"李婷"与通知渠道
（"产品内弹窗/官网公告/邮件通知"），还错标成"来自对话摘要"——摘要里也没有这句。
判定侧逮到了它（faithfulness 里那条不支撑断言），但**产品侧没有任何环节会报警**：
27 轮里 1–2 条（≈0.3%），频率低、性质严重（编造人名）。

## 边界（先说清楚它不是万能的）

这是一个**代理**，不是"实体核查"的完整解：

- 它判的是"这个词有没有在**这一轮检索到的内容**里出现过"，不是"这个断言对不对"——
  断言的对错仍归判定侧（`evaluation/judges/rag.py`）。
- 人名候选只从两种**强模式**里捞：角色词 + 人名（"测试负责人陈琳"）与
  前置动词 + 人名（"提到李婷""由王工负责"）。"人名 + 动词"这种宽模式**刻意不要**
  ——任何 2–4 个汉字都能凑上，实测把"来自人工确认纪要"切成"来自人工"+"确认"、
  把"产品负责人陈琳说"切成"产品"+"负责"。**宁可漏检，也不要乱标注**：
  噪声一旦进到用户眼前的红色告警里，这个功能就没人信了。
  代价写在明处：写成"李婷汇报了…""李婷负责…"这类没有角色词也没有前置动词的编造人名会漏检。
- 同样地，用词表判"哪些词可疑"在本仓被明确否决过（`CONTRACT.md` 第一节：缺陷类有限、
  写法无限，加规则必然过拟合）。这里只用模式**捞候选**，判定交给"在不在检索结果里"
  这条可判定的判据，并且**只标注不拦截**——误报的代价是一句提示，
  漏报也不会让错误内容变成"通过"。

## 处置与 `no_retrieval` 一致

标注进 `error_message`（页面用红色告警渲染）与 `done` 事件，`stop_reason` 不变。
不拦的原因：模型可以建议、不能成交；核查器同理——它可以提醒，不能替人下结论。
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

# 人名形状：2–3 个汉字（本语料 张晨 / 李月 / 王工 / 陈琳 / 李婷 / 张涛 都是这一档）。
# 不放到 4 个汉字：贪婪匹配会把相邻的动词吃进来——实测"产品负责人陈琳说"里的
# "陈琳说月"就是这么被捞出来的。
_NAME_SHAPE = r"[\u4e00-\u9fa5]{2,3}"
# 人名后面应当是这些中的一种：分隔符、言说/动作动词。它是把"人名"与"人名前面那几个字"
# 区分开的关键（没有它，`负责人` 后面的捕获会一路吃到动词上）
_NAME_TAIL = r"(?=[、，。；：\s（(]|说|表示|负责|汇报|提出|指出|介绍|强调|补充|等|和|与)"
# 角色词：人名紧跟在它后面（"产品负责人张晨"）
ROLE_WORDS = ("负责人", "主持人", "管理员", "创建人", "协作人", "参会人", "发言人",
              "评审人", "记录人", "接口人", "演讲人")
# 角色/集体称谓不是人名（"负责人""成员""余人"）
_ROLEISH = ROLE_WORDS + ("人工", "人员", "成员", "专人", "余人", "数人", "本组", "全体")
# 言说/动作动词：人名在它**后面**（"提到李婷""由王工负责"）
VERBS_AFTER = ("提到", "由", "让", "请", "指派")
# 角色词 + 人名：`负责人张晨`、`测试负责人陈琳`
_ROLE_PATTERN = re.compile("(?:" + "|".join(ROLE_WORDS) + ")(" + _NAME_SHAPE + ")" + _NAME_TAIL)
# 动词 + 人名：`提到李婷`、`由王工负责`
_PREFIX_PATTERN = re.compile("(?:" + "|".join(VERBS_AFTER) + ")(" + _NAME_SHAPE + ")" + _NAME_TAIL)
# 数字/日期：`8月31日`、`17:00`、`12%`、`3 天`
# **中文日期要排在前面**：交替式从左往右匹配，写成 `\d+…|\d+月\d+日` 会把"8月31日"
# 拆成 "8" 与 "31" 两个token（实测踩过），于是"日期在不在检索结果里"这件事彻底失真
_NUMBER_PATTERN = re.compile(
    r"\d+年\d+月(?:\d+日)?|\d+月\d+日|\d+(?:\.\d+)?(?:[:：/-]\d+)*(?:%|％)?")
# 不要拿引用编号去核对：`[1]` 是宿主编的，不在检索结果正文里
_CITATION = re.compile(r"\[\d{1,3}\]")


def _candidate_names(answer: str) -> list[str]:
    """答案里"看起来是人名"的候选（去重、保序）。"""
    found: list[str] = []
    for pattern in (_ROLE_PATTERN, _PREFIX_PATTERN):
        for match in pattern.finditer(answer):
            name = match.group(1)
            if any(word in name for word in _ROLEISH):
                continue      # 角色/集体称谓不是人名（"来自人工"+"确认"就是这么被误捞的）
            if name not in found:
                found.append(name)
    return found


def _numbers(answer: str) -> list[str]:
    return list(dict.fromkeys(_NUMBER_PATTERN.findall(answer)))


def _present(token: str, haystack: str) -> bool:
    """这个词在检索内容里出现过吗。

    数字按**等价写法**核对：答案写 `17:00`、原话说 `17:00`（相同）没问题；
    答案写 `8月31日`、原话说 `8 月 31 日`（带空格）也算找到——标点与空格不算差异，
    否则会把格式差异报成"编造"。这与契约层的证据定位用的是同一条思路（共享归一）。
    """
    if token in haystack:
        return True
    compact = re.sub(r"[\s，,、。．:：/／\-—]", "", haystack)
    return re.sub(r"[\s，,、。．:：/／\-—]", "", token) in compact


def check_entities(answer: str, retrieved: Iterable[Mapping[str, Any]] | str, *,
                   limit: int = 5) -> list[dict[str, str]]:
    """扫描答案，返回"检索结果里找不到"的实体（人名 / 数字日期）。

    `retrieved` 是本轮检索到的内容：给引用登记的 `as_dicts()`（每条带 quote/source_text）
    或直接给一段文本。**只读不判**——返回值是标注素材，处置由调用方决定。
    """
    text = (answer or "").strip()
    if not text:
        return []
    if isinstance(retrieved, str):
        haystack = retrieved
    else:
        parts: list[str] = []
        for item in retrieved or []:
            for key in ("source_text", "quote", "text"):
                value = item.get(key)
                if isinstance(value, str) and value:
                    parts.append(value)
        haystack = "\n".join(parts)
    if not haystack.strip():
        return []

    clean = _CITATION.sub("", text)   # 引用编号不是关于会议内容的断言
    flags: list[dict[str, str]] = []
    for name in _candidate_names(clean):
        if not _present(name, haystack):
            flags.append({"kind": "person", "text": name,
                          "reason": "本轮检索结果里没有出现过这个名字"})
        if len(flags) >= limit:
            return flags
    for number in _numbers(clean):
        if not _present(number, haystack):
            flags.append({"kind": "number", "text": number,
                          "reason": "本轮检索结果里没有出现过这个数字/日期"})
        if len(flags) >= limit:
            break
    return flags


def describe(flags: list[dict[str, str]]) -> str:
    """把标注渲染成一句给用户看的话（与 `no_retrieval` 同款：如实标注、不拦截）。"""
    if not flags:
        return ""
    people = [flag["text"] for flag in flags if flag["kind"] == "person"]
    numbers = [flag["text"] for flag in flags if flag["kind"] == "number"]
    parts = []
    if people:
        parts.append("人名 " + "、".join(people))
    if numbers:
        parts.append("数字/日期 " + "、".join(numbers))
    return ("本轮回答里出现了检索结果中找不到的" + "；".join(parts)
            + "。没有拦截这条回答，但请人工复核它的来源——"
              "这类内容可能不是会议里说过的。")


__all__ = ["check_entities", "describe", "ROLE_WORDS", "VERBS_AFTER"]
