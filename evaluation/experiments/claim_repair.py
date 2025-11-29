"""答案级证据校验与有界修复（预注册：evaluation/LEDGER.md §40，2026-10-22）。

**只做实验，不改产品。** 目的：验证「把证据边界从生成前提示挪到生成后校验」能否在
不改检索（context_precision/recall 不动）的前提下提高 faithfulness，且内容覆盖不降。

三臂：
- baseline：既有 packet 里的答案与判定（deepseek 裁判）。
- recitation（零调用确定性臂）：数字/引语在被引 refs 缺失但在其它检索上下文存在时，
  仅追加引用（不改文字、不调模型）。
- repair（LLM 校验 + 机械修复）：deepseek 校验器逐断言判定，机械地「补引用 / 删无支撑断言」；
  校验器不重写文字、不新增事实；守卫：删除断言 >30% / 答案压缩 <60% / 引用清空 → 回退原答案。

判定：复用 judges/rag.py 的 ANSWER_SYSTEM（换仪器 = 换尺子）；上下文不变，因此
baseline 的 context 判定直接复用（作为不变量校验），只重判答案。deepseek 为主裁判（与
被测 qwen 不同族，但与校验器同族 → 闭环风险，必须同时看 qwen 跨族读数）。

用法（WSL，仓库根）：
    set -a; . meetingbot/fastapi-app/.env; set +a
    meetingbot/fastapi-app/.venv/bin/python evaluation/experiments/claim_repair.py \
        --packet evaluation/results/cross30_fresh_packet.json \
        --out evaluation/results/cross30_claim_repair.json \
        --authorized-llm-calls 250

产出：JSON（逐案 + 汇总）、stdout 汇总表。报告写到
`evaluation/reports/2026-10-22_claim_repair_experiment.md`（人工写，不用本脚本生成）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

EVAL = Path(__file__).resolve().parents[1]
if str(EVAL) not in sys.path:
    sys.path.insert(0, str(EVAL))

from judges.rag import ANSWER_SYSTEM, _answer_shape_problem  # noqa: E402
from lib import model_client, stats  # noqa: E402

# --------------------------------------------------------------------------- #
# 与产品/既有审计同口径的确定性模式（评测侧独立持有，不 import 产品实现）
# --------------------------------------------------------------------------- #
SENT_SPLIT = re.compile(r"([。！？\n]+)")
CITATION = re.compile(r"\[(\d{1,3})\]")
NUMBER = re.compile(r"\d+年\d+月(?:\d+日)?|\d+月\d+日|\d+(?:\.\d+)?(?:[:：/-]\d+)*(?:%|％)?")
LOCATOR = re.compile(r"\d{1,6}s|第?\d{1,6}秒")
QUOTE = re.compile(r"[“‘\"]([^”’\"]{4,80})[”’\"]|「([^」]{4,80})」")
HEADING = re.compile(r"^\s*#{1,6}\s")

VERDICTS = {
    "supported_by_cited", "supported_by_other_context", "citation_missing",
    "derived", "meta", "unsupported",
}

VERIFIER_SYSTEM = """你是 RAG 答案的**证据校验器**。给定一个问题、答案里的若干独立断言、
以及本轮检索到的全部上下文（带 [ref] 编号），逐条判定每个断言与本轮上下文的关系。

**只允许使用给定上下文**，不得使用外部知识；不要因为断言"看起来合理"就判 supported。

判定类别（六选一，必须给 reason）：
- `supported_by_cited`：断言的实质内容能在它**已引用的** [ref] 上下文里找到（允许同义改写、
  正确归纳、合并）；
- `supported_by_other_context`：已引用的上下文里没有，但**本轮其它 ref** 的上下文里有；
  在 evidence_refs 里给出这些 ref；
- `citation_missing`：断言没有任何 [ref]，但内容能在某个上下文里找到；evidence_refs 给 ref；
- `derived`：断言是对上下文内容做的正确计算/换算/合并（不是编造），reason 写推导方式；
- `meta`：纯出处说明/引用编号说明，本身不承载会议事实（例如"以上信息来自某场会议"）；
- `unsupported`：以上都不是（上下文没有依据，或与上下文相反）。

特别注意：
- 具体数字、日期、人名、金额、比例、引语：必须在对应上下文里出现，或能被上下文正确推出；
  否则 unsupported。张三说的不能记成李四说（张冠李戴按被引上下文判）。
- 引号里的内容必须能在上下文里找到，否则 unsupported。
- 不要输出解释性长文，reason 一句话。

输出 JSON（必须是完整对象，不要输出 JSON 以外文字）：
{"claims":[{"index":1,"verdict":"supported_by_cited","evidence_refs":[2,3],"reason":"一句话"}],
 "notes":"可选"}
必须对每个 index 输出且只输出一条。
"""


# --------------------------------------------------------------------------- #
# 基础设施
# --------------------------------------------------------------------------- #
def load_env() -> None:
    """把产品 .env 读进环境变量（凭据只从环境读，不打印、不落盘）。"""
    path = EVAL.parent / "meetingbot/fastapi-app/.env"
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip().strip('"').strip("'")


class BudgetExhausted(RuntimeError):
    pass


class CallBudget:
    def __init__(self, authorized: int):
        self.authorized = int(authorized)
        self.calls = 0

    def charge(self) -> None:
        if self.calls >= self.authorized:
            raise BudgetExhausted("授权的模型调用已用完")
        self.calls += 1

    def left(self) -> int:
        return max(0, self.authorized - self.calls)


def compact(text: str) -> str:
    """归一化：去空白与标点（数字/CJK/ASCII 保留），用于确定性字面匹配。"""
    return re.sub(r"[\s，,、。．.!！?？:：;；/／\\\-—()（）\[\]【】{}<>《》\"'“”‘’]", "", text or "")


def segments(answer: str) -> list[str]:
    """按句读切分并保留分隔符（重建时逐段拼接，不丢换行/标点）。"""
    out: list[str] = []
    buffer = ""
    for part in SENT_SPLIT.split(answer or ""):
        buffer += part
        if part and SENT_SPLIT.fullmatch(part):
            out.append(buffer)
            buffer = ""
    if buffer:
        out.append(buffer)
    return out


def claims_of(answer: str) -> tuple[list[str], list[tuple[int, str]]]:
    """返回 (全部分段, 待校验断言[(段下标, 断言原文)])。

    跳过纯标题、过短行与只有引用编号的行；断言保留段首尾空白用于重建。
    """
    all_segments = segments(answer)
    claims: list[tuple[int, str]] = []
    for index, segment in enumerate(all_segments):
        text = segment.strip()
        if not text or HEADING.match(text):
            continue
        if len(compact(text)) < 6:
            continue
        if not re.search(r"[A-Za-z\u4e00-\u9fff]", text):
            continue
        claims.append((index, text))
    return all_segments, claims


def refs_in(text: str) -> list[int]:
    return [int(match.group(1)) for match in CITATION.finditer(text or "")]


def by_ref(case: Mapping[str, Any]) -> dict[int, dict[str, Any]]:
    rows: dict[int, dict[str, Any]] = {}
    for item in case.get("contexts") or []:
        ref = item.get("ref")
        if ref is None:
            continue
        rows[int(ref)] = dict(item)
    return rows


def _texts(items: Iterable[Mapping[str, Any]]) -> str:
    parts = []
    for item in items:
        for key in ("text", "source_text", "quote"):
            value = item.get(key)
            if isinstance(value, str) and value:
                parts.append(value)
                break
    return "\n".join(parts)


def _number_tokens(text: str) -> list[str]:
    text = LOCATOR.sub("", text or "")
    tokens = []
    for token in dict.fromkeys(NUMBER.findall(text)):
        if token.isdigit() and len(token) <= 2:
            continue
        tokens.append(token)
    return tokens


# --------------------------------------------------------------------------- #
# 零调用确定性臂：重引用
# --------------------------------------------------------------------------- #
def recitation_repair(answer: str, contexts: Mapping[int, Mapping[str, Any]]) -> dict[str, Any]:
    """数字/引语在被引上下文缺失、但其它检索上下文有时，仅追加 ref（不改文字）。"""
    all_segments = segments(answer)
    changes: list[dict[str, Any]] = []
    changed = 0
    for index, segment in enumerate(all_segments):
        text = segment.strip()
        if not text or HEADING.match(text):
            continue
        refs = refs_in(text)
        cited_hay = compact(_texts(contexts[ref] for ref in refs if ref in contexts))
        add: list[int] = []
        for token in _number_tokens(text):
            if compact(token) in cited_hay:
                continue
            for ref, item in contexts.items():
                if ref in refs or ref in add:
                    continue
                if compact(token) in compact(str(item.get("text") or "")):
                    add.append(ref)
                    break
        for match in QUOTE.finditer(text):
            quote = (match.group(1) or match.group(2) or "").strip()
            if not quote or compact(quote) in cited_hay:
                continue
            for ref, item in contexts.items():
                if ref in refs or ref in add:
                    continue
                if compact(quote) in compact(str(item.get("text") or "")):
                    add.append(ref)
                    break
        if add:
            marker = "".join(f"[{ref}]" for ref in add)
            new_segment = segment.rstrip("\n") + marker + ("\n" if segment.endswith("\n") else "")
            all_segments[index] = new_segment
            changed += 1
            changes.append({"segment": index, "added_refs": add, "text": text[:120]})
    return {"answer": "".join(all_segments), "changed_segments": changed, "changes": changes}


# --------------------------------------------------------------------------- #
# LLM 校验器 + 机械修复
# --------------------------------------------------------------------------- #
def _contexts_listing(contexts: Mapping[int, Mapping[str, Any]], *, limit: int = 1200) -> str:
    rows = []
    for ref in sorted(contexts):
        item = contexts[ref]
        text = str(item.get("text") or "")[:limit]
        rows.append(f"[{ref}] kind={item.get('kind')} meeting_id={item.get('meeting_id')}\n{text}")
    return "\n\n".join(rows) or "（没有检索到上下文）"


def verify_case(question: str, claims: list[tuple[int, str]],
                contexts: Mapping[int, Mapping[str, Any]], budget: CallBudget,
                provider: str = "deepseek") -> tuple[list[dict[str, Any]] | None, str]:
    """一次校验调用（失败重试一次）。返回 (逐断言判定, 错误信息)。"""
    url, model, key = model_client.endpoint(provider)
    claim_lines = "\n".join(f"{index}. {text}" for index, (_seg, text) in enumerate(claims, start=1))
    prompt = (
        f"【问题】\n{question}\n\n"
        f"【答案断言】\n{claim_lines}\n\n"
        f"【本轮检索到的全部上下文】\n{_contexts_listing(contexts)}\n\n"
        "请逐条校验并输出 JSON。")
    problem = ""
    for _attempt in range(2):
        budget.charge()
        try:
            reply = model_client.call_json(url, model, key, VERIFIER_SYSTEM, prompt,
                                           timeout=180, total_deadline=240,
                                           max_tokens=4096, thinking=False)
        except model_client.ModelClientError as error:
            problem = f"{type(error).__name__}: {str(error)[:160]}"
            time.sleep(0.5)
            continue
        parsed = reply.get("parsed") or {}
        rows = parsed.get("claims")
        if not isinstance(rows, list):
            problem = "claims 不是数组"
            continue
        verdicts: dict[int, dict[str, Any]] = {}
        shape_bad = ""
        for row in rows:
            if not isinstance(row, Mapping):
                shape_bad = "claims 里有非对象"
                break
            try:
                index = int(row.get("index"))
                verdict = str(row.get("verdict") or "")
            except (TypeError, ValueError):
                shape_bad = "index 不是整数"
                break
            if not 1 <= index <= len(claims):
                shape_bad = f"index 越界：{index}"
                break
            if verdict not in VERDICTS:
                shape_bad = f"非法 verdict：{verdict}"
                break
            verdicts[index] = {
                "index": index,
                "verdict": verdict,
                "evidence_refs": [int(ref) for ref in (row.get("evidence_refs") or [])
                                  if isinstance(ref, (int, float)) or str(ref).isdigit()],
                "reason": str(row.get("reason") or "")[:300],
            }
        if shape_bad:
            problem = shape_bad
            continue
        if len(verdicts) != len(claims):
            problem = f"断言数不匹配：校验器 {len(verdicts)} / 断言 {len(claims)}"
            continue
        return [verdicts[index] for index in sorted(verdicts)], ""
    return None, problem


def apply_repair(answer: str, claims: list[tuple[int, str]],
                 verdicts: list[dict[str, Any]],
                 contexts: Mapping[int, Mapping[str, Any]]) -> dict[str, Any]:
    """机械修复：补引用 / 删无支撑断言；不重写、不新增事实。"""
    all_segments = segments(answer)
    claim_segment_index = {index: seg for index, (seg, _text) in enumerate(claims, start=1)}
    remove_segments: set[int] = set()
    append_refs: dict[int, list[int]] = {}
    actions = {"keep": 0, "recite": 0, "remove": 0, "derived_keep": 0, "meta_keep": 0}
    removed_texts: list[str] = []
    recited: list[dict[str, Any]] = []
    for row in verdicts:
        claim_no = int(row["index"])
        seg_index = claim_segment_index[claim_no]
        text = claims[claim_no - 1][1]
        verdict = row["verdict"]
        if verdict == "unsupported":
            remove_segments.add(seg_index)
            removed_texts.append(text[:200])
            actions["remove"] += 1
        elif verdict in ("supported_by_other_context", "citation_missing"):
            refs = [ref for ref in row.get("evidence_refs") or [] if ref in contexts]
            existing = set(refs_in(text))
            fresh = [ref for ref in refs if ref not in existing]
            if fresh:
                append_refs[seg_index] = fresh
                actions["recite"] += 1
                recited.append({"claim": claim_no, "refs": fresh, "text": text[:120]})
            else:
                actions["keep"] += 1
        elif verdict == "derived":
            actions["derived_keep"] += 1
        elif verdict == "meta":
            actions["meta_keep"] += 1
        else:
            actions["keep"] += 1

    fact_claims = max(1, len(claims))
    removal_ratio = len(remove_segments) / fact_claims
    # 守卫 1：单题删除过多 → 回退（防止"删空换分"）
    if removal_ratio > 0.30:
        return {"answer": answer, "fallback": "removed_ratio>0.30", "removal_ratio": removal_ratio,
                "actions": actions, "removed_texts": removed_texts, "recited": recited}
    rebuilt: list[str] = []
    for index, segment in enumerate(all_segments):
        if index in remove_segments:
            continue
        if index in append_refs:
            marker = "".join(f"[{ref}]" for ref in append_refs[index])
            rebuilt.append(segment.rstrip("\n") + marker + ("\n" if segment.endswith("\n") else ""))
        else:
            rebuilt.append(segment)
    repaired = "".join(rebuilt)
    # 守卫 2：压缩过多 → 回退
    if len(compact(repaired)) < 0.60 * len(compact(answer)):
        return {"answer": answer, "fallback": "compressed<0.60", "removal_ratio": removal_ratio,
                "actions": actions, "removed_texts": removed_texts, "recited": recited}
    # 守卫 3：引用被删空 → 回退
    if not refs_in(repaired) and refs_in(answer):
        return {"answer": answer, "fallback": "no_citation_left", "removal_ratio": removal_ratio,
                "actions": actions, "removed_texts": removed_texts, "recited": recited}
    return {"answer": repaired, "fallback": "", "removal_ratio": removal_ratio,
            "actions": actions, "removed_texts": removed_texts, "recited": recited}


# --------------------------------------------------------------------------- #
# 判定（只判答案；上下文判定复用基线，因为上下文完全没变）
# --------------------------------------------------------------------------- #
def judge_answer(question: str, contexts: Mapping[int, Mapping[str, Any]], answer: str,
                 budget: CallBudget, provider: str) -> tuple[dict[str, Any] | None, str]:
    url, model, key = model_client.endpoint(provider)
    listing = "\n".join(f"[{index}] {str(item.get('text') or '')[:1500]}"
                        for index, item in enumerate(
                            [contexts[ref] for ref in sorted(contexts)], start=1))
    prompt = (
        f"【问题】\n{question}\n\n"
        f"【检索到的上下文】\n{listing or '（没有检索到上下文）'}\n\n"
        f"【产品回答】\n{answer[:4000]}\n\n请按格式输出 JSON。")
    problem = ""
    for _attempt in range(2):
        budget.charge()
        try:
            reply = model_client.call_json(url, model, key, ANSWER_SYSTEM, prompt,
                                           timeout=180, total_deadline=240,
                                           max_tokens=4096, thinking=False)
        except Exception as error:  # noqa: BLE001 - 单次判定失败不炸整批
            problem = f"{type(error).__name__}: {str(error)[:160]}"
            time.sleep(0.5)
            continue
        parsed = reply.get("parsed") or {}
        problem = _answer_shape_problem(parsed)
        if not problem:
            return parsed, ""
        time.sleep(0.3)
    return None, problem


def answer_metrics(answer_judgement: Mapping[str, Any] | None) -> dict[str, Any]:
    """从单条答案判定算 faithfulness / 内容级 / relevancy（与 summarize 同口径）。"""
    if not answer_judgement:
        return {"faithfulness": None, "faithfulness_content": None, "answer_relevancy": None,
                "claims": 0, "meta_claims": 0}
    claims = [row for row in (answer_judgement.get("claims") or [])
              if isinstance(row, Mapping) and "supported" in row]
    marked = any("meta" in row for row in claims)
    content = [row for row in claims if not row.get("meta")]
    answered = answer_judgement.get("answered")
    return {
        "faithfulness": (sum(1 for row in claims if row["supported"]) / len(claims)) if claims else None,
        "faithfulness_content": (
            sum(1 for row in content if row["supported"]) / len(content)
            if content and marked else None),
        "answer_relevancy": (1.0 if answered else 0.0) if isinstance(answered, bool) else None,
        "claims": len(claims),
        "meta_claims": len(claims) - len(content) if marked else 0,
    }


# --------------------------------------------------------------------------- #
# 零调用指标：期望事实词法覆盖 / claim 审计
# --------------------------------------------------------------------------- #
def _shingles(text: str, n: int = 3) -> set[str]:
    value = compact(text)
    if len(value) < n:
        return {value} if value else set()
    return {value[index:index + n] for index in range(len(value) - n + 1)}


def fact_coverage(expected_facts: Iterable[str], answer: str) -> dict[str, Any]:
    """期望事实在答案里的词法覆盖（3 字符 shingle 命中率；代理，不是金标）。"""
    answer_shingles = _shingles(answer)
    scores = []
    for fact in expected_facts or []:
        fact_shingles = _shingles(fact)
        if not fact_shingles:
            continue
        hit = len(fact_shingles & answer_shingles) / len(fact_shingles)
        scores.append(hit)
    if not scores:
        return {"mean": None, "facts": 0, "facts_ge_0.6": 0, "scores": []}
    return {"mean": sum(scores) / len(scores), "facts": len(scores),
            "facts_ge_0.6": sum(1 for score in scores if score >= 0.6),
            "scores": [round(score, 4) for score in scores]}


def claim_audit(answer: str, contexts: Mapping[int, Mapping[str, Any]]) -> dict[str, Any]:
    """数字/日期是否在被引上下文 / 任何上下文里（与 analysis/claim_citation_audit 同口径）。"""
    all_hay = compact(_texts(contexts.values()))
    cited_total = cited_missing = any_missing = 0
    examples = []
    for segment_text in segments(answer):
        text = segment_text.strip()
        refs = refs_in(text)
        tokens = _number_tokens(text)
        if not refs or not tokens:
            continue
        cited_hay = compact(_texts(contexts[ref] for ref in refs if ref in contexts))
        for token in tokens:
            cited_total += 1
            if compact(token) not in cited_hay:
                cited_missing += 1
                if compact(token) not in all_hay:
                    any_missing += 1
                    if len(examples) < 5:
                        examples.append({"token": token, "refs": refs, "text": text[:140]})
    return {
        "numbers_cited": cited_total,
        "not_in_cited": cited_missing,
        "not_in_any_context": any_missing,
        "cited_gap_rate": (cited_missing / cited_total) if cited_total else None,
        "not_in_any_context_rate": (any_missing / cited_total) if cited_total else None,
        "examples": examples,
    }


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def repair_case(case: Mapping[str, Any], budget: CallBudget) -> dict[str, Any]:
    question = str(case.get("question") or "")
    answer = str(case.get("answer") or "")
    contexts = by_ref(case)
    _all_segments, claims = claims_of(answer)
    record: dict[str, Any] = {
        "case_id": case.get("case_id"),
        "claims": len(claims),
        "recitation": recitation_repair(answer, contexts),
        "verified": False,
        "verdicts": [],
        "verifier_error": "",
        "repair": {"answer": answer},
        "fallback": "",
        "removal_ratio": 0.0,
        "actions": {},
        "removed_texts": [],
    }
    if not claims or not contexts:
        record["verifier_error"] = "no claims or no contexts"
        return record
    verdicts, error = verify_case(question, claims, contexts, budget)
    if verdicts is None:
        record["verifier_error"] = error or "校验失败"
        record["fallback"] = "verifier_failed"
        return record
    record["verified"] = True
    record["verdicts"] = verdicts
    repair = apply_repair(answer, claims, verdicts, contexts)
    record["repair"] = repair
    record["fallback"] = repair.get("fallback") or ""
    record["removal_ratio"] = repair.get("removal_ratio") or 0.0
    record["actions"] = repair.get("actions") or {}
    record["removed_texts"] = repair.get("removed_texts") or []
    return record


def run_packet(packet_path: Path, out_path: Path, budget: CallBudget,
               *, provider: str = "deepseek", cross_provider: str = "qwen",
               skip_cross: bool = False, limit: int | None = None) -> dict[str, Any]:
    packet = json.loads(packet_path.read_text(encoding="utf-8"))
    cases = list(packet.get("cases") or [])
    if limit is not None:
        cases = cases[:limit]
    rows: list[dict[str, Any]] = []
    stopped = ""
    print(f"[{packet_path.name}] {len(cases)} cases, budget {budget.authorized} calls", flush=True)
    for position, case in enumerate(cases, start=1):
        case_id = case.get("case_id")
        if budget.left() < 4:
            stopped = "budget_exhausted"
            break
        try:
            record = repair_case(case, budget)
            question = str(case.get("question") or "")
            contexts = by_ref(case)
            original_answer = str(case.get("answer") or "")
            repaired_answer = str((record.get("repair") or {}).get("answer") or original_answer)
            recited_answer = str((record.get("recitation") or {}).get("answer") or original_answer)
            unchanged = compact(repaired_answer) == compact(original_answer)
            repaired_ds, err_ds = ((case.get("judgement") or {}).get("answer"), "reused")
            if not unchanged:
                repaired_ds, err_ds = judge_answer(question, contexts, repaired_answer, budget, provider)
            if skip_cross:
                baseline_qw, err_bq = None, "skipped"
                repaired_qw, err_rq = None, "skipped"
            else:
                baseline_qw, err_bq = judge_answer(question, contexts, original_answer,
                                                   budget, cross_provider)
                if unchanged:
                    repaired_qw, err_rq = baseline_qw, "reused_unchanged"
                else:
                    repaired_qw, err_rq = judge_answer(question, contexts, repaired_answer,
                                                       budget, cross_provider)
            row = {
                "case_id": case_id,
                "question": question[:160],
                "baseline_answer": original_answer,
                "repaired_answer": repaired_answer,
                "recited_answer": recited_answer,
                "repair": {key: value for key, value in record.items() if key != "repair"},
                "fallback": record.get("fallback") or "",
                "removal_ratio": record.get("removal_ratio") or 0.0,
                "actions": record.get("actions") or {},
                "verdicts": record.get("verdicts") or [],
                "removed_texts": record.get("removed_texts") or [],
                "recitation_changes": (record.get("recitation") or {}).get("changes") or [],
                "baseline_metrics_ds": answer_metrics((case.get("judgement") or {}).get("answer")),
                "repaired_metrics_ds": answer_metrics(repaired_ds),
                "baseline_metrics_qw": answer_metrics(baseline_qw),
                "repaired_metrics_qw": answer_metrics(repaired_qw),
                "judge_errors": {"repaired_ds": err_ds, "baseline_qw": err_bq,
                                 "repaired_qw": err_rq},
                "fact_coverage": {
                    "baseline": fact_coverage(case.get("expected_facts") or [], original_answer),
                    "repaired": fact_coverage(case.get("expected_facts") or [], repaired_answer),
                    "recited": fact_coverage(case.get("expected_facts") or [], recited_answer),
                },
                "claim_audit": {
                    "baseline": claim_audit(original_answer, contexts),
                    "repaired": claim_audit(repaired_answer, contexts),
                    "recited": claim_audit(recited_answer, contexts),
                },
            }
            rows.append(row)
        except BudgetExhausted:
            stopped = "budget_exhausted"
            break
        print(f"  [{position}/{len(cases)}] {case_id} "
              f"verdicts={len(record.get('verdicts') or [])} "
              f"fallback={record.get('fallback') or '-'} calls={budget.calls}", flush=True)
        time.sleep(0.15)

    result = {
        "pre_registration": "evaluation/LEDGER.md §40（2026-10-22，先于调用写死）",
        "packet": str(packet_path),
        "out": str(out_path),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "providers": {"verifier": provider, "judge_primary": provider, "judge_cross": cross_provider},
        "budget": {"authorized": budget.authorized, "used": budget.calls, "stopped": stopped},
        "cases_total": len(cases),
        "cases_processed": len(rows),
        "rows": rows,
    }
    result["summary"] = summarize_arms(rows)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def _mean(values: Sequence[float] | None) -> float | None:
    values = [value for value in (values or []) if value is not None]
    return (sum(values) / len(values)) if values else None


def _paired_differences(rows: list[dict[str, Any]], before: str, after: str,
                        field: str = "faithfulness_content") -> list[float]:
    diffs = []
    for row in rows:
        a = (row.get(before) or {}).get(field)
        b = (row.get(after) or {}).get(field)
        if a is None or b is None:
            continue
        diffs.append(float(b) - float(a))
    return diffs


def summarize_arms(rows: list[dict[str, Any]]) -> dict[str, Any]:
    def arm_rates(key: str) -> dict[str, Any]:
        claims_total = claims_supported = 0
        content_total = content_supported = 0
        answered_total = answered_ok = 0
        for row in rows:
            metrics = row.get(key) or {}
            claims = int(metrics.get("claims") or 0)
            meta = int(metrics.get("meta_claims") or 0)
            faith = metrics.get("faithfulness")
            content = metrics.get("faithfulness_content")
            relevancy = metrics.get("answer_relevancy")
            if claims:
                claims_total += claims
                if faith is not None:
                    claims_supported += round(faith * claims)
                content_n = claims - meta
                if content is not None and content_n:
                    content_total += content_n
                    content_supported += round(content * content_n)
            if relevancy is not None:
                answered_total += 1
                answered_ok += int(relevancy)
        return {
            "faithfulness": stats.proportion_report(claims_supported, claims_total),
            "faithfulness_content": (stats.proportion_report(content_supported, content_total)
                                     if content_total else None),
            "answer_relevancy": (stats.proportion_report(answered_ok, answered_total)
                                 if answered_total else None),
        }

    ds_faith = _paired_differences(rows, "baseline_metrics_ds", "repaired_metrics_ds")
    qw_faith = _paired_differences(rows, "baseline_metrics_qw", "repaired_metrics_qw")
    ds_rel = _paired_differences(rows, "baseline_metrics_ds", "repaired_metrics_ds",
                                 field="answer_relevancy")
    coverage_diffs = []
    for row in rows:
        baseline = ((row.get("fact_coverage") or {}).get("baseline") or {}).get("mean")
        repaired = ((row.get("fact_coverage") or {}).get("repaired") or {}).get("mean")
        if baseline is not None and repaired is not None:
            coverage_diffs.append(float(repaired) - float(baseline))
    action_totals: dict[str, int] = {}
    fallbacks: dict[str, int] = {}
    removal_ratios = []
    for row in rows:
        for name, count in (row.get("actions") or {}).items():
            action_totals[name] = action_totals.get(name, 0) + int(count)
        if row.get("fallback"):
            fallbacks[row["fallback"]] = fallbacks.get(row["fallback"], 0) + 1
        removal_ratios.append(float(row.get("removal_ratio") or 0.0))

    def audit_totals(arm: str) -> dict[str, Any]:
        cited = missing_cited = missing_any = 0
        for row in rows:
            audit = ((row.get("claim_audit") or {}).get(arm) or {})
            cited += int(audit.get("numbers_cited") or 0)
            missing_cited += int(audit.get("not_in_cited") or 0)
            missing_any += int(audit.get("not_in_any_context") or 0)
        return {
            "numbers_cited": cited,
            "not_in_cited": missing_cited,
            "cited_gap_rate": (missing_cited / cited) if cited else None,
            "not_in_any_context": missing_any,
            "not_in_any_context_rate": (missing_any / cited) if cited else None,
        }

    def mean_fact(arm: str) -> float | None:
        return _mean([((row.get("fact_coverage") or {}).get(arm) or {}).get("mean")
                      for row in rows])

    return {
        "n": len(rows),
        "deepseek_baseline": arm_rates("baseline_metrics_ds"),
        "deepseek_repaired": arm_rates("repaired_metrics_ds"),
        "qwen_baseline": arm_rates("baseline_metrics_qw"),
        "qwen_repaired": arm_rates("repaired_metrics_qw"),
        "faithfulness_content_delta_ds": {
            "mean": _mean(ds_faith),
            "n_pairs": len(ds_faith),
            "improved": sum(1 for diff in ds_faith if diff > 0),
            "worsened": sum(1 for diff in ds_faith if diff < 0),
            "unchanged": sum(1 for diff in ds_faith if diff == 0),
            "wilcoxon": stats.wilcoxon_signed_rank(ds_faith) if ds_faith else None,
        },
        "faithfulness_content_delta_qwen": {
            "mean": _mean(qw_faith),
            "n_pairs": len(qw_faith),
            "improved": sum(1 for diff in qw_faith if diff > 0),
            "worsened": sum(1 for diff in qw_faith if diff < 0),
            "unchanged": sum(1 for diff in qw_faith if diff == 0),
        },
        "answer_relevancy_delta_ds": {
            "mean": _mean(ds_rel),
            "n_pairs": len(ds_rel),
            "dropped": sum(1 for diff in ds_rel if diff < 0),
        },
        "fact_coverage_delta": {
            "mean": _mean(coverage_diffs),
            "n_pairs": len(coverage_diffs),
            "dropped_gt_2pp": sum(1 for diff in coverage_diffs if diff < -0.02),
            "improved_gt_2pp": sum(1 for diff in coverage_diffs if diff > 0.02),
        },
        "removal_ratio": stats.describe(removal_ratios),
        "catastrophic_removal_cases": sum(1 for ratio in removal_ratios if ratio > 0.30),
        "actions": action_totals,
        "fallbacks": fallbacks,
        "claim_audit": {
            "baseline": audit_totals("baseline"),
            "repaired": audit_totals("repaired"),
            "recited": audit_totals("recited"),
        },
        "mean_fact_coverage": {
            "baseline": mean_fact("baseline"),
            "repaired": mean_fact("repaired"),
            "recited": mean_fact("recited"),
        },
        "reading": (
            "deepseek 是主裁判（与校验器同族 → 闭环风险）；qwen 是跨族对照。"
            "faithfulness_content 为内容级口径；词法覆盖是代理不是金标；"
            "claim_audit 的 cited_gap = 数字不在被引上下文的比例，"
            "not_in_any_context = 数字不在任何检索上下文的比例。"),
    }


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--packet", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--authorized-llm-calls", type=int, required=True)
    parser.add_argument("--limit", type=int, default=None, help="只处理前 N 个 case（调试）")
    parser.add_argument("--provider", default="deepseek", choices=sorted(model_client.PROVIDERS))
    parser.add_argument("--cross-provider", default="qwen", choices=sorted(model_client.PROVIDERS))
    parser.add_argument("--skip-cross", action="store_true",
                        help="跳过跨族 qwen 判定（qwen 判定慢；主判走 deepseek）")
    args = parser.parse_args(list(argv) if argv is not None else None)
    load_env()
    budget = CallBudget(args.authorized_llm_calls)
    result = run_packet(args.packet, args.out, budget, provider=args.provider,
                        cross_provider=args.cross_provider, skip_cross=args.skip_cross,
                        limit=args.limit)
    keys = ("n", "deepseek_baseline", "deepseek_repaired", "qwen_baseline", "qwen_repaired",
            "faithfulness_content_delta_ds", "faithfulness_content_delta_qwen",
            "answer_relevancy_delta_ds", "fact_coverage_delta", "removal_ratio",
            "catastrophic_removal_cases", "actions", "fallbacks", "claim_audit",
            "mean_fact_coverage")
    print(json.dumps({key: result["summary"][key] for key in keys},
                     ensure_ascii=False, indent=2))
    print(f"\nbudget used {budget.calls}/{budget.authorized}; out={args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
