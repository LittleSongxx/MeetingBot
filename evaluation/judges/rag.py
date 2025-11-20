"""轴 5：检索与回答（四个指标，逐指标报，不报加权总分）。

## 四个指标与各自的出处

| 指标 | 它回答的问题 | 与业界口径的关系 |
|---|---|---|
| `context_precision` | 检索到的上下文里有多少是回答该问题真正用得上的 | 对齐 RAGAS 的 context_precision（判定式，不用 LLM 排序加权） |
| `context_recall` | 该问题应有的事实有多少能在检索到的上下文里找到 | 对齐 RAGAS 的 context_recall；**需要金标**（`gold/qa_seed.json`） |
| `faithfulness` | 答案里的断言有多少能被上下文支撑 | 对齐 RAGAS 的 faithfulness（逐句判定）。**两个口径**：`faithfulness`（全部断言，历史可比）与 `faithfulness_content`（内容级，剔掉出处说明类元陈述，**这是可引用的口径**） |
| `answer_relevancy` | 答案是否正面回答了问题 | **与 RAGAS 有口径差异**：业界那版用嵌入算"反推问题与原问题的相似度"，本项目评测侧没有嵌入客户端，改用判定式并如实标注 |

**逐指标报、不报加权总分**（宪法第 6 条）：四个指标衡量的是四件不同的事，
把它们加权平均成一个分数，等于把一个可诊断的信号压成一个不可诊断的数字。

## 调用预算

每次判定都要花调用，所以：`--judge` 必须显式传 `--authorized-llm-calls`，
且**每个 case 两次调用**（上下文质量一次、答案质量一次）——把四件事压进一次调用
会让判定互相干扰（判定者要在一段输出里同时做四种判断），分开更稳也更贵。
预算不足时按顺序判前 N 个 case，并如实记录判了几个。

## 没有金标时怎么办

`context_recall` 需要 `expected_facts`。语料里没有金标的 case（例如从真实会话里
抓来但没有标注的问题）该指标**报"检不出"**，而不是拿 0 或 1 填空——
这是宪法第 3 条的要求：算不出的差异写"检不出"。

## 两个 faithfulness 口径（2026-10-16 起的仪器）

首测的 81 条"不支撑"断言里 **67 条（83%）是出处说明类元陈述**（"以上信息均来自…"、
"该会议的 meeting_id=2"）——它们是答案的引用装置，不是会议事实断言，而旧提示词把
"上下文没写这句话"记成了不支撑。剔除后内容级支撑率 97.6%（A）/ 98.6%（B）。

因此判定提示词要求判定者把这类句子标出来（`meta: true`），报告分列两个口径：

* `faithfulness`：全部断言（**历史可比**，与 label a/b 同一口径）；
* `faithfulness_content`：内容级（剔出元陈述），**这是可引用的口径**。

**改了提示词就是换了仪器**：新口径用新 label（`--label c`）重判，旧标签不覆盖。
旧标签的判定里没有 `meta` 字段，因此它们的内容级口径报 `null` 而**不是**等于全部——
"没有元陈述"与"没标过"分不清，就不能冒充一个数（同"判不了不计为通过"）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterable, Mapping

if __package__ in (None, ""):  # allow direct script execution
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from lib import model_client, stats  # type: ignore
else:
    from lib import model_client, stats

EVALUATION_DIR = Path(__file__).resolve().parents[1]
RESULTS_DIR = EVALUATION_DIR / "results"
DEFAULT_PACKET = RESULTS_DIR / "rag_packet.json"
DEFAULT_QA_SET = EVALUATION_DIR / "gold" / "qa_seed.json"
DEFAULT_ANSWERS = RESULTS_DIR / "chat_answers.json"

CONTEXT_SYSTEM = """你在判定**检索质量**。

我会给你一个问题、该问题的标准事实（可能为空），以及检索到的若干段上下文。
你要做两件事：

1. 逐段判断该段是否**对回答这个问题有用**（有用 = 包含回答问题所需的信息；
   只是同事提及、同场会议的无关内容都算无用）。
2. 如果给了标准事实，逐条判断该事实能否在**这些上下文里**找到（近义表述也算找到）。

只依据给你的上下文，不要用你自己的知识补全。只输出 JSON：
{"contexts": [{"index": 1, "useful": true, "why": "一句话"}],
 "facts": [{"index": 1, "found": true, "in_context": 2}],
 "missing_facts": [3]}"""

ANSWER_SYSTEM = """你在判定**回答质量**。

我会给你一个问题、检索到的上下文、以及产品的回答。你要做三件事：

1. 把回答拆成**独立的断言**（每句或每个事实一条，不要合并；text 抄答案里的原句）。
2. 逐条判断该断言能否由给定的上下文支撑（允许改写与正确归纳；上下文没有的
   具体数字、日期、人名一律算不支撑）。
3. 判断这条回答是否**正面回答了问题**（绕开问题、答非所问、只说"没有找到"
   而上下文里明明有 → 判 false）。

第 2 步里有一类句子要单独标出来（`meta: true`）：**纯粹的出处说明**——整句话只讲
"信息是从哪来的"，本身不承载会议内容，判定"上下文有没有这句话"对它没有意义。
下面每一条都是这一类的例子（只是例子，不要照抄进结果）：
- 以上信息均来自某场会议。 / 以上数据来源于人工确认纪要。
- 该会议的 meeting_id=2。 / 来源编号为 [3]。
- 该条目为人工确认条目。 / 回答引用了来源 [7] 作为依据。

**带引述内容的句子不属于这一类（meta: false）**——它说的是"会议里讲了什么"，
内容本身必须判定支撑，**转述与归属措辞不算错**。例如下面两句都要照常判支撑
（若上下文确有该内容则 supported=true）：
- 人工确认纪要中明确记录：覆盖 10%-30% 的活跃用户[1]。
- 会议转写中张晨说：本次灰度从 8 月 20 日开始（start_second=51）[2]。
带引用编号的结论句同样属于这一档（编号只是引用装置，句子本身仍是对会议的陈述），
例如：上线时间定在十月[1]。

**输出必须是一个完整的 JSON 对象，claims、answered、why 三个字段一个都不能省**；
不要输出 JSON 以外的任何文字。text 字段保持句子原样，不要额外加引号。
格式：
{"claims": [{"index": 1, "text": "…", "supported": true, "meta": false}],
 "answered": true, "why": "一句话"}"""


# --------------------------------------------------------------------------- #
# 抓取：驱动产品接口拿真实问答记录（需要授权调用）
# --------------------------------------------------------------------------- #


class CaptureError(RuntimeError):
    """抓取失败。消息里不含凭据。"""


def _http_json(url: str, *, method: str = "POST", payload: dict | None = None,
               token: str = "", timeout: float = 300.0) -> dict:
    data = json.dumps(payload or {}).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Content-Type", "application/json;charset=utf-8")
    request.add_header("token", token)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - 地址来自命令行
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")[:200]
        raise CaptureError(f"{method} {url} → HTTP {error.code}: {detail}") from error
    except urllib.error.URLError as error:
        raise CaptureError(f"{method} {url} → {error.reason}") from error
    try:
        return json.loads(body)
    except ValueError as error:
        raise CaptureError(f"{method} {url} → 响应不是 JSON：{body[:200]}") from error


def _ask(base_url: str, token: str, session_id: int, question: str,
         timeout: float = 300.0) -> dict[str, Any]:
    """问一次并读回落库的结果。

    **先读 SSE、再读消息列表**：SSE 负责触发与等待结束，真正的产物从
    `/chat/messages` 取——后者是产品落库的形态（含引用快照与工具轨迹），
    与前端刷新后看到的是同一份数据。
    """
    request = urllib.request.Request(f"{base_url}/chat/stream",
                                    data=json.dumps({"session_id": session_id,
                                                     "question": question}).encode("utf-8"),
                                    method="POST")
    request.add_header("Content-Type", "application/json;charset=utf-8")
    request.add_header("token", token)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            for line in response:
                if line.startswith(b"event: done") or line.startswith(b"event: error"):
                    pass  # 读到流结束即可，产物从 /chat/messages 取
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")[:200]
        raise CaptureError(f"提问失败 HTTP {error.code}: {detail}") from error
    except urllib.error.URLError as error:
        raise CaptureError(f"提问失败：{error.reason}") from error

    history = _http_json(f"{base_url}/chat/messages?session_id={session_id}&limit=50",
                         method="GET", token=token)
    rows = (history.get("data") or {}).get("list") or []
    if not rows or rows[-1].get("role") != "ASSISTANT":
        raise CaptureError("提问后没有读到助手回复")
    return rows[-1]


def meeting_transcripts(base_url: str, token: str, meeting_id: int) -> dict[str, Any]:
    """把一场会的全部转写抓成一段文本：**离线判"引用能不能定位"的对照物**。

    为什么不从检索结果里取对照：那等于自己跟自己比（引文本来就是从切片里拷的）。
    对照物必须是另一个来源——转写表里的原始片段（`/transcription/selectById`），
    于是这条检查真的在问"答案引用的那句话，在这场会的转写里存在吗"。
    """
    page = _http_json(f"{base_url}/transcription/selectPage?page_num=1&page_size=50"
                      f"&meetingId={meeting_id}", method="GET", token=token)
    tasks = [row for row in ((page.get("data") or {}).get("list") or [])
             if row.get("status") == "SUCCEEDED"]
    segments: list[dict[str, Any]] = []
    for task in tasks:
        detail = _http_json(f"{base_url}/transcription/selectById/{task['id']}", method="GET",
                            token=token)
        segments.extend((detail.get("data") or {}).get("segments") or [])
    segments.sort(key=lambda item: (item.get("task_id"), item.get("segment_no")))
    return {
        "meeting_id": meeting_id,
        "task_ids": [task["id"] for task in tasks],
        "segment_count": len(segments),
        "text": "\n".join(str(item.get("content") or "") for item in segments),
        # 片段 → 时间映射：检索侧指标（recall@k / MRR）要把检索到的切片时间窗映射回
        # 金标里的片段号，缺了它那套指标算不出来（旧产物没有这个字段，指标会如实报无样本）
        "segments": [{
            "task_id": item.get("task_id"), "segment_no": item.get("segment_no"),
            "start_ms": item.get("start_ms"), "end_ms": item.get("end_ms"),
        } for item in segments],
    }


def capture(qa_set: Mapping[str, Any], *, base_url: str, token: str,
            output_path: Path, meeting_id: int | None = None,
            progress=print) -> dict[str, Any]:
    """逐条提问并抓取产物，写成 `results/chat_answers.json`。"""
    created = _http_json(f"{base_url}/chat/session",
                         payload={"meeting_ids": [meeting_id] if meeting_id else None},
                         token=token)
    session_id = (created.get("data") or {}).get("id")
    if not session_id:
        raise CaptureError(f"建会话失败：{created.get('msg') or created}")
    sources: dict[str, Any] = {}
    if meeting_id:
        sources[str(meeting_id)] = meeting_transcripts(base_url, token, meeting_id)
        progress(f"  转写对照物：{sources[str(meeting_id)]['segment_count']} 个片段")
    turns: list[dict[str, Any]] = []
    for case in qa_set["cases"]:
        message = _ask(base_url, token, session_id, case["question"])
        turns.append({
            "case_id": case["id"],
            "meeting_id": meeting_id,
            "question": case["question"],
            "answer": message.get("content") or "",
            "message_id": message.get("id"),
            "stop_reason": message.get("stop_reason"),
            "citations": message.get("citations") or [],
            "tools": message.get("tools") or [],
            # 会话限定范围就是这次提问的允许集合（抓取时固定一场会，便于复核）
            "allowed_meeting_ids": [meeting_id] if meeting_id else None,
            "used_refs": _refs_from_answer(message),
            "unknown_refs": [],
            # 本轮检索到的**全部**切片（含没被引用的）：检索侧指标的分母。
            # 只存被引用的那一部分会把召回算高（分母里少了没被引用的东西）。
            "retrieved": message.get("retrieved") or [],
        })
        progress(f"  [{case['id']}] {case['question'][:30]}… → {len(turns[-1]['citations'])} 条引用")
        _write(output_path, {
            "artifact_version": 1,
            "captured_from": {"base_url": base_url, "session_id": session_id,
                              "meeting_id": meeting_id, "captured_at": _now(),
                              "product_model": os.getenv("RAG_CAPTURE_MODEL", "")},
            # 每场会的转写全文：零调用检查用它判"引用的那句话真的在会议里吗"
            "source_texts": sources,
            "turns": turns,
        })
        time.sleep(0.2)
    return {"session_id": session_id, "turns": len(turns)}


def _refs_from_answer(message: Mapping[str, Any]) -> list[int]:
    """答案里出现过的引用编号（与页面渲染用的是同一个模式：`[n]`）。"""
    import re
    return sorted({int(m.group(1)) for m in re.finditer(r"\[(\d{1,3})\]", message.get("content") or "")})


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


# --------------------------------------------------------------------------- #
# 组包（零调用）与判定（需授权）
# --------------------------------------------------------------------------- #


def build_packet(qa_set: Mapping[str, Any], answers: Mapping[str, Any] | None) -> dict[str, Any]:
    """把标注集与抓取的问答记录配成判定包。**零模型调用**。"""
    by_question = {}
    for turn in (answers or {}).get("turns") or []:
        by_question[turn.get("question")] = turn
    cases: list[dict[str, Any]] = []
    for case in qa_set["cases"]:
        turn = by_question.get(case["question"],
                               by_question.get(case.get("question", "").strip()))
        contexts = []
        for citation in ((turn or {}).get("citations") or []):
            text = citation.get("source_text") or citation.get("quote") or ""
            contexts.append({
                "ref": citation.get("ref"), "kind": citation.get("kind"),
                "meeting_id": citation.get("meeting_id"), "text": text,
            })
        cases.append({
            "case_id": case["id"],
            "question": case["question"],
            "expected_facts": case.get("expected_facts") or [],
            "answer": (turn or {}).get("answer") or "",
            "contexts": contexts,
            "has_answer": bool(turn),
        })
    return {
        "packet_version": 1,
        "purpose": "轴 5 检索与回答：context precision/recall、faithfulness、answer relevancy",
        "qa_set": qa_set.get("name"),
        "qa_set_discipline": qa_set.get("discipline"),
        "case_count": len(cases),
        "answered_case_count": sum(1 for case in cases if case["has_answer"]),
        "cases": cases,
    }


def _existing_judgement(case: Mapping[str, Any], label: str) -> Mapping[str, Any]:
    """这一族已有的判定（`a` 存在 `judgement` 里，其它族存在 `judgements[label]` 里）。"""
    value = case.get("judgement") if label == "a" else (case.get("judgements") or {}).get(label)
    return value if isinstance(value, Mapping) else {}


def _needs_judging(case: Mapping[str, Any], label: str) -> bool:
    """这一轮还需要（重新）判定吗：这一族还没判过、或上一次有调用失败。"""
    judgement = _existing_judgement(case, label)
    if not judgement:
        return True
    return any(f"{part}_error" in judgement for part in ("context", "answer"))


def _context_shape_problem(parsed: Mapping[str, Any]) -> str:
    """上下文判定响应的形状问题。空串表示合格。

    为什么要查：**"合法的 JSON"不等于"完整的判定"**。实测（2026-10-16）：判定模型
    中途停住并补上闭合括号，返回的对象 JSON 可解析、但内容被截断——最后一条断言少了
    `supported`，后面的断言整段消失。不查的话它会以"判成功"入库，分母静默变小
    （那一轮断言总数从 495 掉到 125，报告上完全看不出异常）。
    """
    for entry in parsed.get("contexts") or []:
        if not isinstance(entry, Mapping) or "useful" not in entry:
            return "有一条上下文缺少 useful（响应被截断？）"
    for entry in parsed.get("facts") or []:
        if not isinstance(entry, Mapping) or "found" not in entry:
            return "有一条标准事实缺少 found（响应被截断？）"
    return ""


def _answer_shape_problem(parsed: Mapping[str, Any]) -> str:
    """答案判定响应的形状问题。空串表示合格。"""
    if "answered" not in parsed:
        return "缺少 answered 字段（响应被截断？）"
    claims = parsed.get("claims")
    if not isinstance(claims, list):
        return "claims 不是数组"
    for index, entry in enumerate(claims, start=1):
        if not isinstance(entry, Mapping) or "supported" not in entry:
            return f"第 {index} 条断言缺少 supported（响应被截断？）"
    return ""


# 每一半判定最多尝试几次真实调用。存在理由：实测（2026-10-16）判定模型偶尔会在
# JSON 模式下**中途停住并补上闭合括号**（返回的 JSON 可解析但内容被截断），
# 换一次调用通常就好了。重试的那次会附上"上一次被截断"的说明（instructor 式 re-ask，
# 与产品 `invoke_json` 同一套做法）。上限 2：不设上限就会在模型稳定失败时烧预算。
JUDGE_ATTEMPTS = 2

_REASK_NOTE = (
    "\n\n【上一次输出不合格】你上一次的输出被截断了（JSON 能解析但内容不完整："
    "有断言缺少 supported / 缺少 answered 字段）。请重新完整输出一次，"
    "确保 claims、answered、why 都在，且每一条断言都带 supported 与 meta。"
)


def judge_packet(packet: dict[str, Any], *, output_path: Path, authorized_calls: int,
                 provider: str = "qwen", label: str = "a", thinking: bool = False,
                 progress=print) -> dict[str, Any]:
    """逐 case 判定：每 case 两次调用（上下文质量、答案质量）。"""
    url, model, key = model_client.endpoint(provider)
    # 待判 = 还没判过的 + **上一次判定里有调用失败的**。后者要能重试：
    # 实测判定模型偶尔会读超时（长上下文 + 思考），把它当成"永久失败"就白白丢样本。
    pending = [
        case for case in packet["cases"]
        if case["has_answer"] and _needs_judging(case, label)
    ]
    budget_cases = authorized_calls // 2
    if budget_cases < len(pending):
        progress(f"授权 {authorized_calls} 次调用 = {budget_cases} 个 case，"
                 f"待判 {len(pending)} 个，本次只判前 {budget_cases} 个")
        pending = pending[:budget_cases]
    packet.setdefault("adjudicator_disclosure", {})
    if label == "a":
        packet["adjudicator_disclosure"].update({
            "model_used": model, "provider": provider,
        "definition": ("逐段上下文有用性 + 逐条事实覆盖 + 逐断言支撑 + 是否正面回答；"
                       "answer_relevancy 与 RAGAS 的嵌入版口径不同，见模块文档"),
            "not_gold": "这是模型判定，不是人类金标",
        })
    else:
        # 第二个族另存一份：**标签只追加不覆盖**（与仓库既有的 label_b 纪律一致），
        # 否则"跨族一致性"这件事本身就没法复核了
        packet.setdefault("adjudicators", {})[label] = {
            "model_used": model, "provider": provider, "thinking": thinking,
            "not_gold": "这是模型判定，不是人类金标",
        }
    calls = 0
    for position, case in enumerate(pending, start=1):
        context_listing = "\n".join(
            f"[{index}] {item['text'][:1500]}" for index, item in enumerate(case["contexts"], start=1))
        facts_listing = "\n".join(
            f"{index}. {fact}" for index, fact in enumerate(case["expected_facts"], start=1))
        context_user = (
            f"【问题】\n{case['question']}\n\n"
            f"【标准事实】\n{facts_listing or '（无）'}\n\n"
            f"【检索到的上下文】\n{context_listing or '（没有检索到上下文）'}\n\n请按格式输出 JSON。")
        answer_user = (
            f"【问题】\n{case['question']}\n\n"
            f"【检索到的上下文】\n{context_listing or '（没有检索到上下文）'}\n\n"
            f"【产品回答】\n{case['answer'][:4000]}\n\n请按格式输出 JSON。")
        judgement: dict[str, Any] = {}
        # 重试时只补失败的那一半：已经判成功的部分不重复花钱
        # store 必须是"这一族的判定字典"，不能是 case 本身——case 里有个 `answer` 键
        # （答案正文），拿它当 store 会让"只补失败的那一半"误判成"answer 已判过"，
        # 于是少发一次调用（实测：单测里 calls_made 从 2 变 1）。
        if label == "a":
            store = case.setdefault("judgement", {})
        else:
            store = case.setdefault("judgements", {}).setdefault(label, {})
        previous = store if isinstance(store, Mapping) else {}
        for part in ("context", "answer"):
            if part in previous and f"{part}_error" not in previous:
                judgement[part] = previous[part]
        for name, system, user in (("context", CONTEXT_SYSTEM, context_user),
                                   ("answer", ANSWER_SYSTEM, answer_user)):
            if name in judgement:
                continue   # 这一半上轮已判成功
            attempt = 0
            while True:
                # **硬预算闸**：重试也要走闸门，否则"授权 N 次"会被重试撑破
                if calls >= authorized_calls:
                    judgement[f"{name}_error"] = "授权的判定调用已用完，这一半没判"
                    break
                attempt += 1
                parsed: Mapping[str, Any] = {}
                truncated = False
                try:
                    reply = model_client.call_json(url, model, key, system, user,
                                                  total_deadline=240, thinking=thinking)
                    parsed = reply["parsed"]
                    # 形状不合格按**判定失败**记：截断的响应混进"判成功"会让分母静默变小
                    problem = (_context_shape_problem(parsed) if name == "context"
                               else _answer_shape_problem(parsed))
                    if problem:
                        truncated = True
                        problem = f"响应形状不合格：{problem}"
                except model_client.ModelClientError as error:
                    problem = f"{type(error).__name__}: {str(error)[:200]}"
                calls += 1
                if not problem:
                    judgement[name] = parsed
                    break
                if attempt < JUDGE_ATTEMPTS:
                    if truncated:
                        user = user + _REASK_NOTE   # 只作用于这一次重试
                    continue
                judgement[f"{name}_error"] = problem
                break
            _write(output_path, packet)
        if label == "a":
            case["judgement"] = judgement
        else:
            case["judgements"][label] = judgement
        progress(f"  [{position}/{len(pending)}] {case['case_id']} → "
                 f"{'失败' if judgement.get('context_error') or judgement.get('answer_error') else '已判'}")
        time.sleep(0.2)
    return {"provider": provider, "model": model, "label": label, "calls_made": calls,
            "judged": sum(1 for case in packet["cases"] if case.get("judgement"))}


def _rate(successes: int, total: int) -> dict[str, Any] | None:
    return stats.proportion_report(successes, total) if total else None


def summarize(packet: Mapping[str, Any], *, label: str = "a") -> dict[str, Any]:
    """四个指标分别汇总；判不了的样本（缺金标、缺判定）单独报，不混进分母。

    `label` 选哪一族的判定：`a` 是主判（`case["judgement"]`），其它族的另存
    `case["judgements"][label]`——换裁判等于换仪器，两族都要能各自成数。

    faithfulness 报两个口径（见模块文档）：全部断言与内容级（剔出处说明）。
    内容级要求判定标过 `meta`；旧提示词产的标签（a/b）没标过，因此那些标签的
    内容级报 `null`——分不清"没有元陈述"与"没标过"就不能冒充一个数。
    """
    context_useful = context_total = 0
    facts_found = facts_total = 0
    claims_supported = claims_total = 0
    content_supported = content_total = 0
    meta_claims = 0
    meta_marked = False
    answered_ok = answered_total = 0
    cases_with_judgement = 0
    cases_without_gold = 0
    per_case: list[dict[str, Any]] = []

    for case in packet["cases"]:
        judgement = (case.get("judgement") or {}) if label == "a" \
            else ((case.get("judgements") or {}).get(label) or {})
        if not judgement:
            continue
        context = judgement.get("context") or {}
        answer = judgement.get("answer") or {}
        row: dict[str, Any] = {"case_id": case["case_id"]}

        contexts = [entry for entry in (context.get("contexts") or [])
                    if isinstance(entry, Mapping) and "useful" in entry]
        context_total += len(contexts)
        context_useful += sum(1 for entry in contexts if entry["useful"])
        row["context_precision"] = (sum(1 for entry in contexts if entry["useful"]) / len(contexts)
                                    if contexts else None)

        facts = [entry for entry in (context.get("facts") or [])
                 if isinstance(entry, Mapping) and "found" in entry]
        if facts:
            facts_total += len(facts)
            facts_found += sum(1 for entry in facts if entry["found"])
            row["context_recall"] = sum(1 for entry in facts if entry["found"]) / len(facts)
        else:
            cases_without_gold += 1
            row["context_recall"] = None

        claims = [entry for entry in (answer.get("claims") or [])
                  if isinstance(entry, Mapping) and "supported" in entry]
        marked = any("meta" in entry for entry in claims)
        meta_marked = meta_marked or marked
        content = [entry for entry in claims if not entry.get("meta")]
        claims_total += len(claims)
        claims_supported += sum(1 for entry in claims if entry["supported"])
        content_total += len(content)
        content_supported += sum(1 for entry in content if entry["supported"])
        meta_claims += len(claims) - len(content)
        row["faithfulness"] = (sum(1 for entry in claims if entry["supported"]) / len(claims)
                               if claims else None)
        row["faithfulness_content"] = (
            sum(1 for entry in content if entry["supported"]) / len(content)
            if content and marked else None)
        row["meta_claims"] = len(claims) - len(content) if marked else None

        if "answered" in answer:
            answered_total += 1
            answered_ok += 1 if answer["answered"] else 0
            row["answer_relevancy"] = 1.0 if answer["answered"] else 0.0

        cases_with_judgement += 1
        per_case.append(row)

    return {
        "cases_judged": cases_with_judgement,
        "cases_total": packet.get("case_count"),
        "cases_step_without_gold": cases_without_gold,
        # 四个指标分别给点估计与两个区间；不合成总分（宪法第 6 条）
        "context_precision": _rate(context_useful, context_total),
        "context_recall": _rate(facts_found, facts_total),
        "faithfulness": _rate(claims_supported, claims_total),
        "faithfulness_content": _rate(content_supported, content_total) if meta_marked else None,
        "faithfulness_meta_claims": meta_claims if meta_marked else None,
        "answer_relevancy": _rate(answered_ok, answered_total),
        "per_case": per_case,
        "reading": (
            "context_precision = 检索到的上下文里被判『有用』的比例；"
            "context_recall = 标注事实里能在上下文中找到的比例（只有带金标的 case 计入）；"
            "faithfulness = 答案断言里被上下文支撑的比例（全部断言口径）；"
            "faithfulness_content = 同上但**剔掉出处说明类元陈述**（可引用口径，"
            "判定标了 meta 才有；旧提示词的标签报 null）；"
            "answer_relevancy = 正面回答了问题的 case 比例（与 RAGAS 的嵌入口径不同）。"
            "四个指标各自独立报出，**没有加权总分**。"
        ),
        "not_measured": (
            "本次未测：产品在真实模型下的答案质量（需要一次授权运行）；"
            "本文件里的数字若是空值，说明对应样本没有判定，而不是 0 分。"
        ),
    }


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--packet", type=Path, default=DEFAULT_PACKET)
    parser.add_argument("--qa-set", type=Path, default=DEFAULT_QA_SET)
    parser.add_argument("--answers", type=Path, default=DEFAULT_ANSWERS)
    parser.add_argument("--build", action="store_true", help="组包（零调用）")
    parser.add_argument("--attach-transcripts", action="store_true",
                        help="给已有产物补上会议转写对照物（零调用；只读产品的转写接口）")
    parser.add_argument("--ask", action="store_true", help="驱动产品接口抓取问答记录（需授权）")
    parser.add_argument("--judge", action="store_true", help="逐 case 判定（需授权）")
    parser.add_argument("--base-url", default="http://127.0.0.1:19091")
    parser.add_argument("--token", default="")
    parser.add_argument("--meeting-id", type=int, default=None,
                        help="把会话范围限定在这一场会（便于复核引用来源）")
    parser.add_argument("--provider", default="qwen", choices=sorted(model_client.PROVIDERS))
    parser.add_argument("--label", default="a", choices=("a", "b", "c"),
                        help="判定族的标签：a 是主判，其它标签另存一份（跨族复判用）。"
                             "判定提示词变更也要换新标签——换了提示词就是换了仪器")
    parser.add_argument("--thinking", default="off", choices=("off", "on"),
                        help="判定调用是否开思考：机械判定默认关（又慢又贵，实测 30–120s/次）")
    parser.add_argument("--authorized-llm-calls", type=int, default=None)
    args = parser.parse_args(list(argv) if argv is not None else None)

    qa_set = json.loads(args.qa_set.read_text(encoding="utf-8"))
    if args.ask:
        if not args.authorized_llm_calls:
            print("拒绝执行：--ask 会触发真实问答（每次提问多次模型调用），"
                  "必须显式传 --authorized-llm-calls", file=sys.stderr)
            return 2
        if not args.token:
            print("拒绝执行：--ask 需要 --token（登录后的 JWT）", file=sys.stderr)
            return 2
        try:
            result = capture(qa_set, base_url=args.base_url, token=args.token,
                             output_path=args.answers, meeting_id=args.meeting_id)
        except CaptureError as error:
            print(f"抓取失败：{error}", file=sys.stderr)
            return 1
        print(json.dumps(result, ensure_ascii=False))
        return 0
    if args.attach_transcripts:
        if not args.token:
            print("拒绝执行：--attach-transcripts 需要 --token（登录后的 JWT）", file=sys.stderr)
            return 2
        artifact = json.loads(args.answers.read_text(encoding="utf-8"))
        meetings = {turn.get("meeting_id") for turn in artifact.get("turns") or []}
        meetings.discard(None)
        if not meetings and args.meeting_id:
            meetings = {args.meeting_id}
        sources = artifact.setdefault("source_texts", {})
        for meeting_id in sorted(meetings):
            sources[str(meeting_id)] = meeting_transcripts(args.base_url, args.token, meeting_id)
            print(f"会议 {meeting_id}：{sources[str(meeting_id)]['segment_count']} 个转写片段")
        _write(args.answers, artifact)
        print(f"已写回 {args.answers}")
        return 0
    if args.build:
        answers = json.loads(args.answers.read_text(encoding="utf-8")) if args.answers.is_file() else None
        packet = build_packet(qa_set, answers)
        _write(args.packet, packet)
        print(f"wrote {args.packet}: {packet['case_count']} 个 case，"
              f"其中有抓取答案的 {packet['answered_case_count']} 个")
        return 0
    if args.judge:
        if not args.authorized_llm_calls:
            print("拒绝执行：--judge 必须显式传 --authorized-llm-calls", file=sys.stderr)
            return 2
        packet = json.loads(args.packet.read_text(encoding="utf-8"))
        progress = judge_packet(packet, output_path=args.packet,
                               authorized_calls=args.authorized_llm_calls, provider=args.provider,
                               label=args.label, thinking=args.thinking == "on")
        packet["summary" if args.label == "a" else f"summary_{args.label}"] = summarize(
            packet, label=args.label)
        _write(args.packet, packet)
        print(json.dumps(progress, ensure_ascii=False))
        key = "summary" if args.label == "a" else f"summary_{args.label}"
        print(json.dumps(packet[key], ensure_ascii=False, indent=2)[:2000])
        return 0
    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
