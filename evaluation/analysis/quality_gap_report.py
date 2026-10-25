#!/usr/bin/env python3
"""质量指标全景 + 不合格指标根因归因：零模型调用的离线复算脚本。

用法（仓库根目录）::

    python3 evaluation/analysis/quality_gap_report.py
    # 默认写 evaluation/results/quality_gap_report.json，另可 --output PATH

设计约束（与评测宪法一致）:
- **零模型调用**：只读既有 JSON 产物，只用标准库（json/re/math/unicodedata/pathlib）。
- **不 import 产品代码、不 import evaluation 包**：Clopper-Pearson 区间在本地实现，
  与 evaluation/lib/stats.py 的算法一致（精确二项区间，双侧 alpha=0.05）。
- **只为「可复算」的东西写代码**：轴 1/2/3/4 的绝对数是历史基板读数，脚本原样登记并
  标注来源与 n，不假装重算；轴 5 / 轨迹 / 引用归因从 packet 重新计算并与既有产物互验。
- 结果文件不覆盖：只写 `evaluation/results/quality_gap_report.json`（--output 可改）。

产物结构（顶层键）:
- meta                     输入清单、方法说明、零调用声明
- status_board             指标状态板（轴1–5 + 可靠性 + Agent/轨迹），每条带 n/CI/来源/阈值
- quality_gate             按 evaluation/quality_gate.py 的阈值与冻结基线复算门禁
- rag_attribution          context_recall 缺失事实的逐案归因、题型分组、top 失败 case
- faithfulness_unsupported supported=false 断言分类、样例、已知漏判
- citation_attribution     数字/引语不在被引上下文 vs 不在任何上下文
- trajectory               tool_call_f1 / first_tool / over-call 明细 / 各实验臂
- protocol_issues          评测口径问题清单（带证据）
- disputed_numbers         不能直接引用/口径有争议的数字
- representative_samples   文档要用的代表性样例（中文原文）
- verification             关键复算与既有产物的一致性检查
"""
from __future__ import annotations

import argparse
import json
import math
import re
import unicodedata
from pathlib import Path

# ---------------------------------------------------------------------------
# 路径与常量
# ---------------------------------------------------------------------------
EVAL = Path(__file__).resolve().parents[1]          # .../evaluation
RESULTS = EVAL / "results"
GOLD = EVAL / "gold"
BASELINE_PATH = EVAL / "baselines" / "quality_gate_baseline.json"
DEFAULT_OUT = RESULTS / "quality_gap_report.json"

Z_95 = 1.959963984540054

# 摘自 evaluation/quality_gate.py（质量闸唯一实现；本脚本镜像其判定逻辑，用于报告复算）。
# 若脚本与 quality_gate.py 不一致，以 quality_gate.py 为准——这里只做"状态板复算"。
FLOORS = {
    "p0": {"context_recall": 0.90, "context_precision": 0.55,
           "faithfulness": 0.85, "answer_relevancy": 0.95},
    "cross30": {"context_recall": 0.70, "context_precision": 0.55,
                "faithfulness": 0.80, "answer_relevancy": 0.90},
    "new_meeting": {"context_recall": 0.80, "context_precision": 0.55,
                    "faithfulness": 0.75, "answer_relevancy": 0.90},
    "mixed100": {"context_recall": 0.85, "context_precision": 0.60,
                 "faithfulness": 0.80, "answer_relevancy": 0.90},
}
TOLERANCE = 0.02          # 相对冻结基线允许的波动（quality_gate.py）
CLAIM_FLOOR = 0.15        # claim not-in-any-context 绝对下限（quality_gate.py）

RAG_METRICS = ("context_precision", "context_recall", "faithfulness",
               "faithfulness_content", "answer_relevancy")
CORE_METRICS = ("context_precision", "context_recall", "faithfulness", "answer_relevancy")

PACKETS = {
    "human27": "rag_packet.json",
    "extended68": "rag_packet_extended_tools2_final.json",
    "p0_r1": "p0_fresh6_r1_packet.json",
    "p0_r2": "p0_fresh6_r2_packet.json",
    "p0_r3": "p0_fresh6_r3_packet.json",
    "cross30_fresh": "cross30_fresh_packet.json",
    "mixed100": "eval100_scope_fixed_packet.json",
    "mixed100_assembled": "eval100_packet.json",
    "new_meeting": "new_meeting_packet.json",
    "new_meeting_scoped": "new_meeting_scoped_packet.json",
}
GATE_SCOPE_TO_PACKET = {
    "p0": ("p0_r1", "p0_r2", "p0_r3"),
    "cross30": ("cross30_fresh",),
    "new_meeting": ("new_meeting",),
    "mixed100": ("mixed100",),
}

# 供状态板引用的固定来源（相对 evaluation/ 的路径）
SRC = {
    "ledger": "evaluation/LEDGER.md",
    "design": "evaluation/DESIGN.md",
    "gate": "evaluation/quality_gate.py",
    "baseline": "evaluation/baselines/quality_gate_baseline.json",
    "cpcer_main": "evaluation/asr/first_test/cpcor/cpcor_report_main_8m.json",
    "cpcer_c3600": "evaluation/asr/first_test/cpcor/cpcor_report_c3600_8m.json",
    "meeting_v2": "evaluation/results/meeting_level_packet_v2.json",
    "meeting_v2r": "evaluation/results/meeting_level_packet_v2r.json",
    "adjudication_lenient": "evaluation/results/adjudication_packet_v2_lenient.json",
    "citation_packet": "evaluation/results/citation_packet.json",
    "completeness_v2": "evaluation/results/completeness_keypoints_v2.json",
    "completeness_v3": "evaluation/results/completeness_keypoints_v3.json",
    "completeness_v3_qwen": "evaluation/results/completeness_keypoints_v3_qwen.json",
    "trajectory_report": "evaluation/results/trajectory_freshbase27_report.json",
    "tool_utility": "evaluation/results/tool_utility_freshbase27.json",
    "claim_audit_fresh": "evaluation/results/claim_audit_cross30_fresh.json",
    "claim_verify": "evaluation/results/claim_verify_cross30.json",
    "claim_audit_multiturn": "evaluation/results/claim_audit_cross30.json",
    "gold_cross30": "evaluation/gold/cross_meeting_qa_30.json",
    "gold_eval100": "evaluation/gold/eval_100_mixed.json",
    "gold_new_meeting": "evaluation/gold/new_meeting_15.json",
    "gold_trajectory": "evaluation/gold/trajectory_27.json",
    "judge_card": "evaluation/gold/JUDGE_CARD.md",
    "human_review": "evaluation/gold/HUMAN_REVIEW.md",
    "report_eval100": "evaluation/reports/2026-10-22_eval100_scale.md",
    "report_gate": "evaluation/reports/2026-10-22_rag_quality_gate.md",
    "report_generalization": "evaluation/reports/2026-10-22_generalization_audit_v2.md",
    "report_design_audit": "evaluation/reports/2026-10-22_eval_design_audit.md",
    "report_new_meeting": "evaluation/reports/2026-10-22_new_meeting_eval.md",
    "report_tool_f1": "evaluation/reports/2026-10-22_tool_f1_deep_dive.md",
    "report_agent_deep": "evaluation/reports/2026-10-21_agent_quality_deep_dive.md",
    "report_blind20": "evaluation/reports/2026-10-22_blind20_review.md",
    "report_blind40": "evaluation/reports/2026-10-22_blind40_review.md",
    "docs_rag_params": "docs/RAG链路参数.md",
    "docs_agent_metrics": "docs/Agent指标.md",
    "docs_qa_analysis": "docs/问答链路分析.md",
}


# ---------------------------------------------------------------------------
# 统计与文本工具（只依赖标准库；CP 区间镜像 evaluation/lib/stats.py）
# ---------------------------------------------------------------------------
def _log_binom_pmf(k: int, n: int, p: float) -> float:
    if p <= 0.0:
        return 0.0 if k == 0 else -math.inf
    if p >= 1.0:
        return 0.0 if k == n else -math.inf
    return (math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1)
            + k * math.log(p) + (n - k) * math.log1p(-p))


def binom_cdf(k: int, n: int, p: float) -> float:
    if n < 0:
        raise ValueError("n must be non-negative")
    if k < 0:
        return 0.0
    if k >= n:
        return 1.0
    terms = [_log_binom_pmf(i, n, p) for i in range(k + 1)]
    peak = max(terms)
    return min(1.0, math.exp(peak) * sum(math.exp(t - peak) for t in terms))


def _bisect_p(predicate, low: float = 0.0, high: float = 1.0, iterations: int = 120) -> float:
    for _ in range(iterations):
        mid = (low + high) / 2.0
        if predicate(mid):
            high = mid
        else:
            low = mid
    return (low + high) / 2.0


def clopper_pearson(successes: int, total: int, alpha: float = 0.05):
    """精确二项区间（Clopper-Pearson）。与 evaluation/lib/stats.py 同算法。"""
    if total <= 0:
        return None
    if not 0 <= successes <= total:
        raise ValueError("successes must satisfy 0 <= successes <= total")
    lower = 0.0
    if successes > 0:
        lower = _bisect_p(lambda p: 1.0 - binom_cdf(successes - 1, total, p) > alpha / 2)
    upper = 1.0
    if successes < total:
        upper = _bisect_p(lambda p: binom_cdf(successes, total, p) < alpha / 2)
    return [round(lower, 4), round(upper, 4)]


PUNCT = re.compile(r"[\s，。、；：！？,.;:!?“”\"'‘’（）()\[\]【】<>《》·\-—_/\\|]+")
SENTENCE = re.compile(r"[。！？\n]+")
CITATION = re.compile(r"\[(\d{1,3})\]")
NUMBER = re.compile(r"\d+年\d+月(?:\d+日)?|\d+月\d+日|\d+(?:\.\d+)?(?:[:：/-]\d+)*(?:%|％)?")
QUOTE = re.compile(r"[“\"]([^”\"]{4,})[”\"]")
ELLIPSIS = re.compile(r"……|\.\.\.|…")


def norm(text: str) -> str:
    """归一化：NFKC、去 markdown 强调符、去空白与标点，用于字面近似匹配。"""
    text = unicodedata.normalize("NFKC", text or "")
    text = text.replace("**", "").replace("`", "")
    return PUNCT.sub("", text)


def bigrams(text: str) -> set:
    if len(text) < 2:
        return {text} if text else set()
    return {text[i:i + 2] for i in range(len(text) - 1)}


def coverage_proxy(fact: str, haystack: str) -> float:
    """expected fact 在 haystack（上下文字面/答案）中的 bigram 覆盖率近似（0–1）。

    复合事实先整体算一次；若整体覆盖低，再按分句、分号切开做长度加权平均，取较大值。
    这是**字面代理**：判据是"这句话的措辞有没有出现在材料里"，不是语义判断。
    """
    f, h = norm(fact), norm(haystack)
    if not f or not h:
        return 0.0
    bf = bigrams(f)
    bh = bigrams(h)
    hits = len(bf & bh) / len(bf)
    parts = [p for p in re.split(r"[；;。\n]+", fact) if norm(p)]
    if hits < 0.9 and len(parts) > 1:
        total = sum(len(norm(p)) for p in parts)
        weighted = sum(len(norm(p)) * (len(bigrams(norm(p)) & bh) / max(1, len(bigrams(norm(p)))))
                       for p in parts)
        hits = max(hits, weighted / total if total else hits)
    return round(hits, 4)


def lcs_len(a: str, b: str) -> int:
    """最长公共子串长度（引语近似匹配用）。"""
    if not a or not b:
        return 0
    best = 0
    prev = [0] * (len(b) + 1)
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        ai = a[i - 1]
        for j in range(1, len(b) + 1):
            if ai == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best = cur[j]
        prev = cur
    return best


def pct(value, digits: int = 4):
    return None if value is None else round(float(value), digits)


# ---------------------------------------------------------------------------
# 产物读取
# ---------------------------------------------------------------------------
def load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return None, f"missing: {path}"
    except Exception as exc:  # noqa: BLE001 - 报告工具要容错并显式登记
        return None, f"{type(exc).__name__}: {exc}"


def rel(path: Path) -> str:
    try:
        return str(path.relative_to(EVAL.parent)).replace("\\", "/")
    except ValueError:
        return str(path)


def packet_judgement(case: dict) -> dict:
    """兼容两种 packet 形态：judgement（单裁判） / judgements（多标签 f/e/g）。"""
    judgement = case.get("judgement")
    if judgement is None:
        judgements = case.get("judgements") or {}
        judgement = judgements.get("f") or (next(iter(judgements.values())) if judgements else None)
    return judgement or {}


def packet_summary(packet: dict) -> dict:
    return packet.get("summary") or packet.get("summary_f") or {}


def metric_block(summary: dict, metric: str):
    block = summary.get(metric)
    return block if isinstance(block, dict) else None


def context_items(case: dict) -> list:
    return case.get("contexts") or []


def context_text(case: dict) -> str:
    return "\n".join(str(item.get("text") or "") for item in context_items(case))


def answer_text(case: dict) -> str:
    return case.get("answer") or ""


def claims(case: dict) -> list:
    return packet_judgement(case).get("answer", {}).get("claims") or []


def facts(case: dict) -> list:
    return packet_judgement(case).get("context", {}).get("facts") or []


def question_type(case_id: str, packet_name: str, origin: str | None = None) -> str:
    """题型分组：cm* 跨录音 / cs* 系列题 / ct* 单点 / n* 新会议 / 其它。"""
    cid = origin or case_id
    if cid.startswith("cm"):
        return "cm 跨录音"
    if cid.startswith("cs"):
        return "cs 系列题"
    if cid.startswith("ct"):
        return "ct 单点"
    if cid.startswith("n"):
        return "n 新会议"
    if cid.startswith("q"):
        return "q 旧会议问答"
    if cid.startswith("t"):
        return "t 轨迹题"
    return "other"


def fact_text(case: dict, index) -> str:
    expected = case.get("expected_facts") or []
    if isinstance(index, int) and 1 <= index <= len(expected):
        return expected[index - 1]
    return ""


# ---------------------------------------------------------------------------
# 人工抽查覆盖表（来自本次分析逐条阅读，以及 docs/ 里的既有归因；脚本不调用模型）
# key = "packet:case_id:fact_index"，value = (classification, note)
# ---------------------------------------------------------------------------
RAG_MANUAL_REVIEW = {
    # cross30_fresh（2026-10-22 复跑；缺失 14 条事实全部逐条读过）
    "cross30_fresh:cm08:1": ("retrieval_miss", "上午场“补充两组验收（30 分钟真实录音、多角色内容）”未进上下文；68 条集 q95 同题已归因（docs/RAG链路参数.md）。"),
    "cross30_fresh:cs02:2": ("gold_metadata_or_absence_fact", "“对应待办为 OPEN”是 state 工具/DB 元数据，不在检索上下文；答案用 notice 推断。见 2026-10-22_generalization_audit_v2.md §4 第 4 条。"),
    "cross30_fresh:cs05:1": ("gold_metadata_or_absence_fact", "“系列 1 只包含会议 2”是系列归属元数据；上下只有录音清单，无法从检索上下文获得。"),
    "cross30_fresh:cs06:1": ("gold_metadata_or_absence_fact", "三项结论来自 DRAFT 纪要 summary 字段；packet 的 contexts 只记了会议卡片，结论正文不在 contexts 里（citation[1] 指向的上下文不含该内容）。"),
    "cross30_fresh:cs06:2": ("gold_metadata_or_absence_fact", "同上：灰度 10%/50%/全量来自 DRAFT 纪要 summary，不在 contexts。"),
    "cross30_fresh:cs06:3": ("gold_metadata_or_absence_fact", "同上：通稿 18:00 前定稿来自 DRAFT 纪要 summary，不在 contexts。"),
    "cross30_fresh:cs07:1": ("gold_metadata_or_absence_fact", "“supersedes_id 为空”是 DB 元数据，检索上下文不可能包含。"),
    "cross30_fresh:cs07:2": ("gold_metadata_or_absence_fact", "“只能说明没有替代记录、不能推断内容一定没变”是判定规范结论，不在检索材料中。"),
    "cross30_fresh:cs08:1": ("gold_metadata_or_absence_fact", "“来自 DRAFT 纪要、不是 CONFIRMED”是 authority 元数据，不在 contexts。"),
    "cross30_fresh:cs08:2": ("gold_metadata_or_absence_fact", "“系列已确认记忆中目前没有该条”是否定性结论，不在 contexts。"),
    "cross30_fresh:cs09:1": ("gold_metadata_or_absence_fact", "“只返回 CONFIRMED 记忆与有效待办”是工具 notice 文本，notice 未进 contexts；答案引用了 notice 但 recall 分母只认 contexts。"),
    "cross30_fresh:cs09:2": ("gold_metadata_or_absence_fact", "“PROPOSED 候选不得直接当事实”是工具规则，不在 contexts。"),
    "cross30_fresh:ct02:2": ("partial_context_coverage", "复合时间线 4 个节点中上下文只覆盖“9 月 1 日 9:00”，8 月 30/31 三个节点缺失：部分检索。"),
    "cross30_fresh:ct06:1": ("retrieval_miss", "“9:00 正式发布”节点未进上下文；答案把“公告和通告在发布当天上午十点”写成发布/公告时间，属时间绑定错误（2026-10-21 deep dive ct06 已知）。"),
    # mixed100（组装集；cross30 同题缺失与上面一致，另加 holdout/new 层的缺口）
    "mixed100:m042:1": ("retrieval_miss_answer_wrote", "上下文只有风险描述，没有“负责人负载提示”建议；答案却写成“纪要明确给出该建议”，来源不明（疑似未入上下文的工具结果/参数记忆）。"),
    "mixed100:m058:1": ("retrieval_miss", "“会议创建数量与总时长”在另一决策条目的清单里，本次 top 上下文只检索到日报/观测中心两条窗口。"),
    "mixed100:m058:4": ("retrieval_miss", "“RAG 问答与 Agent 运行质量”同类清单项未进上下文，答案因此说“未提及其他具体指标”。"),
    "mixed100:m093:1": ("gold_transcript_conflict_or_ambiguity", "金标要求“产品研发/测试/运营/客户支持四类账号”，检索到的转写原文是“管理员和员工两种角色”；疑似 gold 与语料冲突或题目歧义（答案如实答资料不足）。"),
    "mixed100:m008:1": ("retrieval_miss", "同 cross30 cm08：上午补充验收要求未进上下文。"),
    "mixed100:m012:2": ("gold_metadata_or_absence_fact", "同 cross30 cs02#2：待办 OPEN 状态是元数据。"),
    "mixed100:m015:1": ("gold_metadata_or_absence_fact", "同 cross30 cs05#1：系列归属元数据。"),
    "mixed100:m017:1": ("gold_metadata_or_absence_fact", "同 cross30 cs07#1：supersedes_id 元数据。"),
    "mixed100:m017:2": ("gold_metadata_or_absence_fact", "同 cross30 cs07#2：判定规范结论。"),
    "mixed100:m018:1": ("gold_metadata_or_absence_fact", "同 cross30 cs08#1：authority 元数据。"),
    "mixed100:m018:2": ("gold_metadata_or_absence_fact", "同 cross30 cs08#2：否定性结论。"),
    "mixed100:m019:1": ("gold_metadata_or_absence_fact", "同 cross30 cs09#1：notice 未进 contexts。"),
    "mixed100:m019:2": ("gold_metadata_or_absence_fact", "同 cross30 cs09#2：工具规则。"),
    "mixed100:m022:2": ("partial_context_coverage", "同 cross30 ct02#2：复合时间线只覆盖一个节点。"),
    "mixed100:m026:1": ("retrieval_miss", "同 cross30 ct06#1：9:00 发布节点未进上下文，答案写成 10:00。"),
    # 新会议（unscoped 主口径）
    "new_meeting:n01:1": ("gold_fixture_limitation", "fixture 未显式标注主持人（gold 由转写开场推断，2026-10-22_new_meeting_eval.md §6 已登记）；检索又把产品发布评审会议（meeting 2）的转写带进来，答案答成李月。"),
    "new_meeting:n03:1": ("retrieval_miss", "“账单纠纷”属知识库未覆盖主题；检索只带回议题标题清单，未带回“未覆盖主题”的纪要原文。"),
    "new_meeting:n03:2": ("retrieval_miss", "“权限申请”同上。"),
    # p0 6×3（cross30 前 6 题）
    "p0_r2:cm02:1": ("retrieval_miss", "“5 项风险含阻断任务延期”中第 5 项未进上下文；答案只列 4 项（partial retrieval）。"),
    # 扩展 68（既有归因在 docs/RAG链路参数.md）
    "extended68:q84:2": ("retrieval_miss", "“导出是低频操作、卡 40 秒更差”一句未被检索到；docs/RAG链路参数.md 已归因“q84 检索少一句”。"),
    "extended68:q89:1": ("retrieval_miss", "上午 4 项风险未整段进上下文；docs 已归因“q89 窗口到 559s、漏风险”。"),
    "extended68:q93:2": ("judge_false_negative_likely", "答案已写“对外通稿定稿由刘颖负责”，judge 仍判 found=false；docs 已归因 q93 判定假阴性。"),
    "extended68:q94:1": ("gold_transcript_conflict_or_ambiguity", "q94“两个功能问题”有两种合理读法，题目歧义（docs 已归因）。"),
    "extended68:q94:2": ("gold_transcript_conflict_or_ambiguity", "同上。"),
    "extended68:q95:1": ("retrieval_miss", "上午补充两组验收数据未进上下文；docs 已归因“q95 段内 query 没问到补充验收数据”。"),
}

# 元数据/否定性结论类金标的启发式模式（用于自动归因；人工表优先）
META_FACT_PATTERNS = re.compile(
    r"supersedes|CONFIRMED|DRAFT|PROPOSED|OPEN|notice|只返回|只包含|为空|未返回|不得|不能推断|"
    r"没有后续|没有替代|没有该条|不能当作|不能推断|元数据|系列.*只|该记录|authority")
ANSWER_WROTE_THRESHOLD = 0.6
CTX_HAS_THRESHOLD = 0.6
CTX_PARTIAL_THRESHOLD = 0.35

# faithfulness 断言分类（规则粗分）。顺序 = 优先级；分类是"断言文本的类型"，
# 不是"它到底对不对"——对不对只有单裁判 supported 字段（及人工/盲评）能说。
FAITH_TYPE_RULES = [
    ("数字/日期", NUMBER),
    ("引语", QUOTE),
    ("元信息/程序性说明", re.compile(
        r"以上(信息|数据|内容|条目|均)|来自|出自|来源|meeting_id|minutes_id|source_id|"
        r"action_item|query_meeting_state|notice|CONFIRMED|PROPOSED|资料(说明|完整|覆盖|不足)|"
        r"检索(结果|命中)|上下文|不返回|未返回|返回的|本回答|见上|待确认|录音清单|工具(返回|明确|提示)")),
    ("时间顺序/因果推断", re.compile(
        r"在.{0,10}(前|后)|顺序|先后|先.{0,8}再|紧接着|之前|之后|随后|推迟|提前|晚于|早于|排在|"
        r"意味着|导致|因此|基于|表明|说明")),
    ("推断/结论性表述", re.compile(
        r"结论|建议|判断|推断|资料|无法|可以|应当|应该|必须|如需|若需|需要|注意|注：|请补充|"
        r"未(找到|发现|提及|明确|给出)|没有(给出|找到|明确)|不(明确|一致|完整)")),
    ("归属/人名", re.compile(r"负责|牵头|协作|提出|表示|指出|称|发言|主持人|负责人|参与|点名|人员|团队")),
    ("否定/相反表述（候选）", re.compile(r"无需|不需要|不会|不是|并非|相反|冲突|不一致|矛盾")),
]

# 「引语」要求是成句引用（≥10 字且不是检索词/会议名/问句模板），否则按术语引用处理
SUBSTANTIAL_QUOTE_MIN = 10


def _substantial_quotes(text: str) -> list:
    out = []
    for quote in QUOTE.findall(text or ""):
        normed = norm(quote)
        if len(normed) < SUBSTANTIAL_QUOTE_MIN:
            continue
        if re.match(r"^(产品发布评审会议|客户支持知识库与智能客服灰度评审会|产品发布评审系列)", quote.strip()):
            continue
        if re.search(r"[/+→]", quote) and len(normed) <= 24:
            continue
        out.append(quote)
    return out


def classify_faith_claim(text: str, meta_flag) -> str:
    if meta_flag:
        return "元信息/程序性说明"
    for name, pattern in FAITH_TYPE_RULES:
        if name == "引语" and not _substantial_quotes(text):
            continue
        if pattern.search(text or ""):
            return name
    return "其他/需人工"


# 输出时把没有命中的类别也列出来（0 也是读数；与原文相反/矛盾需要比对原文，规则判不了）
FAITH_TYPE_NAMES = ["数字/日期", "引语", "元信息/程序性说明", "时间顺序/因果推断",
                    "推断/结论性表述", "归属/人名", "否定/相反表述（候选）", "与原文相反/矛盾（需人工比对）",
                    "其他/需人工"]


# 轨迹报告里各实验臂的既有读数（静态，来自报告；脚本无法复算被删掉的 runner）
TRAJECTORY_ARMS = [
    {"arm": "router off（当前默认）", "tool_f1": 0.7309, "first_strict": "14/27",
     "first_acceptable": "22/27", "coverage": "24/27", "cross30_faithfulness": 0.8863,
     "source": SRC["report_tool_f1"]},
    {"arm": "全量意图路由", "tool_f1": 0.8519, "first_strict": "18/27",
     "first_acceptable": "23/27", "coverage": "26/27", "cross30_faithfulness": 0.8306,
     "source": SRC["report_tool_f1"]},
    {"arm": "选择性路由（cross/state）", "tool_f1": 0.8025, "first_strict": "17/27",
     "first_acceptable": "21/27", "coverage": "24/27", "cross30_faithfulness": 0.7353,
     "source": SRC["report_agent_deep"]},
    {"arm": "仅 hint", "tool_f1": 0.7963, "first_strict": "17/27",
     "first_acceptable": "21/27", "coverage": "24/27", "cross30_faithfulness": 0.8024,
     "source": SRC["report_tool_f1"]},
    {"arm": "仅 budget", "tool_f1": 0.7654, "first_strict": "14/27",
     "first_acceptable": "22/27", "coverage": "25/27", "cross30_faithfulness": 0.8369,
     "source": SRC["report_tool_f1"]},
    {"arm": "强制首工具", "tool_f1": 0.7901, "first_strict": "19/27",
     "first_acceptable": "21/27", "coverage": "23/27", "cross30_faithfulness": 0.8205,
     "source": SRC["report_tool_f1"]},
]


# ---------------------------------------------------------------------------
# 一、状态板
# ---------------------------------------------------------------------------
def p0_aggregates(runs: dict) -> dict:
    """P0 6 题 × 3 轮：同时给 quality_gate 用的 mean-of-rates 与 pooled。"""
    out = {"runs": {}, "mean_of_rates": {}, "pooled": {}}
    for metric in RAG_METRICS:
        rows = []
        for run_name, packet in runs.items():
            if packet is None:
                continue
            block = metric_block(packet_summary(packet), metric)
            if not block or block.get("rate") is None:
                continue
            rows.append({"run": run_name, "successes": block.get("successes"),
                         "total": block.get("total"), "rate": block.get("rate")})
        out["runs"][metric] = rows
        if rows:
            out["mean_of_rates"][metric] = round(sum(r["rate"] for r in rows) / len(rows), 4)
            s = sum(int(r["successes"]) for r in rows)
            n = sum(int(r["total"]) for r in rows)
            out["pooled"][metric] = {"successes": s, "total": n, "rate": round(s / n, 4),
                                     "clopper_pearson_95": clopper_pearson(s, n)}
        else:
            out["mean_of_rates"][metric] = None
            out["pooled"][metric] = None
    return out


def build_status_board(packets: dict, baseline: dict | None) -> dict:
    axes = []

    def row(axis, metric, scope, value, successes=None, total=None, unit=None, ci=None,
            source=None, floor=None, frozen=None, project_target=None, gate=None,
            recomputed=None, notes=None):
        entry = {
            "axis": axis, "metric": metric, "scope": scope,
            "value": pct(value), "n": {"successes": successes, "total": total, "unit": unit},
            "ci95": ci, "source": source, "floor": pct(floor), "frozen_baseline": pct(frozen),
            "project_target": pct(project_target), "gate": gate,
            "pass": None, "gap_to_floor_pp": None, "gap_to_target_pp": None,
            "recomputed_from_packet": recomputed, "notes": notes,
        }
        if value is not None:
            if floor is not None:
                entry["gap_to_floor_pp"] = round((value - floor) * 100, 2)
                entry["pass"] = bool(value >= floor and (frozen is None or value >= frozen - TOLERANCE))
            if project_target is not None:
                entry["gap_to_target_pp"] = round((value - project_target) * 100, 2)
        axes.append(entry)

    # —— 轴 1 转写（静态登记；来源 cpcor 报告 + LEDGER，旧基板读数）
    cp_main = load_json(EVAL / "asr/first_test/cpcor/cpcor_report_main_8m.json")[0]
    cp_c3600 = load_json(EVAL / "asr/first_test/cpcor/cpcor_report_c3600_8m.json")[0]
    n_meetings = len((cp_main or {}).get("meetings") or []) or 8
    if cp_main and cp_main.get("macro"):
        m = cp_main["macro"]
        row("轴1 转写", "CER no_punct（宏平均）", "AliMeeting Eval 8 场", m.get("cer_no_punct"),
            total=n_meetings, unit="场", source=SRC["cpcer_main"], gate="未设门禁（仅登记）",
            recomputed=False, notes="远场单声道真实链路 vs 近讲共享 TextGrid（M2MeT 官方口径，系统性高估）；早期 3 场 36.5% 已作废。")
        row("轴1 转写", "cpCER（原默认 c1200+平均下混）", "AliMeeting Eval 8 场", m.get("cpcor"),
            total=n_meetings, unit="场", source=SRC["cpcer_main"], gate="未设门禁（仅登记）",
            recomputed=False, notes="说话人归属性错误；碎片化标签为主因（hyp 说话人数=真实人数×切块数）。")
        row("轴1 转写", "Δcp = cpCER − CER（原默认）", "AliMeeting Eval 8 场", m.get("delta_cp"),
            total=n_meetings, unit="场", source=SRC["cpcer_main"], gate="未设门禁（仅登记）",
            recomputed=False, notes="归属损失比识别错误大一个量级（LEDGER 轴1，2026-09-27）。")
    if cp_c3600 and cp_c3600.get("macro"):
        m = cp_c3600["macro"]
        row("轴1 转写", "cpCER（c3600 单块切分，当前默认）", "AliMeeting Eval 8 场", m.get("cpcor"),
            total=n_meetings, unit="场", source=SRC["cpcer_c3600"], gate="未设门禁（仅登记）",
            recomputed=False, notes="预注册两项全过（8/8 场 Δcp≤10pp、CER 差 +0.13pp），产品默认 1200→3600 已翻转。")
        row("轴1 转写", "Δcp（c3600）", "AliMeeting Eval 8 场", m.get("delta_cp"),
            total=n_meetings, unit="场", source=SRC["cpcer_c3600"], gate="未设门禁（仅登记）",
            recomputed=False, notes="参考：MOSS 0.9B cpCER 22.17%、豆包 37.57%（不同口径外部数）。")

    # —— 轴 2 忠实度（历史基板；LEDGER 存活结论 + 产物）
    v2 = load_json(EVAL / "results/meeting_level_packet_v2.json")[0] or {}
    v2r = load_json(EVAL / "results/meeting_level_packet_v2r.json")[0] or {}
    b2 = ((v2.get("summary") or {}).get("by_arm") or {}).get("baseline") or {}
    b2r = ((v2r.get("summary") or {}).get("by_arm") or {}).get("baseline") or {}
    axes.append({
        "axis": "轴2 忠实度", "metric": "会议级问题率（合并两次独立判定）", "scope": "VCSum test26（旧基板）",
        "value": 0.2308, "n": {"successes": 12, "total": 52, "unit": "会议·判定（26 场×2 次）"},
        "ci95": None, "source": f"{SRC['ledger']}（存活结论登记·轴2）; {SRC['meeting_v2']}; {SRC['meeting_v2r']}",
        "floor": None, "frozen_baseline": None, "project_target": None, "gate": "未设门禁（历史登记）",
        "pass": None, "gap_to_floor_pp": None, "gap_to_target_pp": None,
        "recomputed_from_packet": False,
        "notes": (f"两个独立判定包分别：v2 baseline {b2.get('meetings_with_unsupported')}/26="
                  f"{(b2.get('problem_rate') or {}).get('rate')}；v2r {b2r.get('meetings_with_unsupported')}/26="
                  f"{(b2r.get('problem_rate') or {}).get('rate')}。thinking 开关敏感性：OFF 2/12 → ON 11/12。"),
    })
    axes.append({
        "axis": "轴2 忠实度", "metric": "条目级无依据（宽松档≈业界口径）", "scope": "VCSum test26（旧基板）",
        "value": 0.205, "n": {"successes": None, "total": None, "unit": "分层加权（LEDGER 只给点估计）"},
        "ci95": None, "source": f"{SRC['ledger']}; {SRC['adjudication_lenient']}",
        "floor": None, "frozen_baseline": None, "project_target": None, "gate": "未设门禁（历史登记）",
        "pass": None, "gap_to_floor_pp": None, "gap_to_target_pp": None,
        "recomputed_from_packet": False,
        "notes": ("宽松档允许改写/压缩/正确推导；严格档同批为 55%（同族）/60.2%（跨族）——报哪个必须写清。"
                  "抽样包 88 条中宽松档标签分布：unsupported 25、partial 37、supported 25、derivable 1；"
                  "分层抽样导致该分布不能直接当语料比例。"),
    })
    cit = load_json(EVAL / "results/citation_packet.json")[0] or {}
    cit_sum = cit.get("summary") or {}
    cp = cit_sum.get("citation_precision") or {}
    isr = cit_sum.get("item_support_rate") or {}
    row("轴2 忠实度", "引用级 citation precision（条目支撑）", "VCSum test26 引用包",
        cp.get("rate"), cp.get("successes"), cp.get("total"), "引用", cp.get("clopper_pearson_95"),
        SRC["citation_packet"], recomputed=False,
        notes="宽松档；对照 ALCE 长文强模型带（vanilla 50–53.4%、最优重排 67.8%）。")
    row("轴2 忠实度", "条目支撑率（至少一条支撑引用）", "VCSum test26 引用包",
        isr.get("rate"), isr.get("successes"), isr.get("total"), "条目", isr.get("clopper_pearson_95"),
        SRC["citation_packet"], recomputed=False, notes="对应 ALCE 的 recall 侧。")
    row("轴2 忠实度", "多余引用率", "VCSum test26 引用包",
        cit_sum.get("unnecessary_citation_rate"), cit_sum.get("unnecessary_citation_count"),
        cp.get("total"), "引用", None, SRC["citation_packet"], recomputed=False,
        notes="ALCE 的 shotgun citation；独立判口径 ~33%。")

    # —— 轴 3 完整性（历史基板）
    comp2 = load_json(EVAL / "results/completeness_keypoints_v2.json")[0] or {}
    arms2 = ((comp2.get("summary") or {}).get("by_arm") or {})
    base2 = arms2.get("baseline") or {}
    micro2 = base2.get("micro_pooled_coverage") or {}
    axes.append({
        "axis": "轴3 完整性", "metric": "关键点覆盖（仪器 v2，baseline 臂）", "scope": "VCSum test26（旧基板）",
        "value": base2.get("macro_mean_coverage"), "n": {"successes": micro2.get("successes"),
                                                          "total": micro2.get("total"), "unit": "关键点（微平均分母）"},
        "ci95": micro2.get("clopper_pearson_95"),
        "source": f"{SRC['ledger']}; {SRC['completeness_v2']}",
        "floor": None, "frozen_baseline": None, "project_target": None, "gate": "未设门禁（历史登记）",
        "pass": None, "gap_to_floor_pp": None, "gap_to_target_pp": None,
        "recomputed_from_packet": False,
        "notes": "主口径 = 逐场覆盖率宏平均（每场等权）；微平均一并报。",
    })
    comp3 = load_json(EVAL / "results/completeness_keypoints_v3.json")[0] or {}
    arms3 = ((comp3.get("summary") or {}).get("by_arm") or {})
    base3 = arms3.get("baseline") or {}
    micro3 = base3.get("micro_pooled_coverage") or {}
    core3 = (base3.get("core_only") or {}).get("micro_pooled_coverage") or {}
    axes.append({
        "axis": "轴3 完整性", "metric": "关键点覆盖（仪器 v3，baseline 臂，deepseek 裁判）",
        "scope": "VCSum test26（旧基板）",
        "value": base3.get("macro_mean_coverage"),
        "n": {"successes": micro3.get("successes"), "total": micro3.get("total"), "unit": "关键点（微平均分母）"},
        "ci95": micro3.get("clopper_pearson_95"),
        "source": f"{SRC['ledger']}; {SRC['completeness_v3']}",
        "floor": None, "frozen_baseline": None, "project_target": None, "gate": "未设门禁（历史登记）",
        "pass": None, "gap_to_floor_pp": None, "gap_to_target_pp": None,
        "recomputed_from_packet": False,
        "notes": (f"core 分层宏平均 {round((base3.get('core_only') or {}).get('macro_mean_coverage', 0), 4)}、"
                  f"微平均 {round(core3.get('rate', 0), 4)}；换裁判不重述：v2/v3 复合点口径不可比；qwen 裁判系统性宽 7–9pp，"
                  "v3 的 revised 臂相对 baseline −2.1pp（26 场，覆盖损失长尾）。"),
    })
    axes.append({
        "axis": "轴3 完整性", "metric": "plan+gap-fill 对覆盖的增益", "scope": "VCSum test26（R4 预注册）",
        "value": 0.0192, "n": {"successes": None, "total": 26, "unit": "场（配对）"},
        "ci95": None, "source": f"{SRC['ledger']}; evaluation/results/completeness_plangap_v3.json",
        "floor": None, "frozen_baseline": None, "project_target": 0.03, "gate": "预注册阈值 +3pp 未过 → 检不出",
        "pass": False, "gap_to_floor_pp": None, "gap_to_target_pp": -1.08,
        "recomputed_from_packet": False,
        "notes": "11 升 12 降 3 平；plan+gap-fill 保持默认关。",
    })

    # —— 轴 4 行动项（产品侧读数缺失——如实报"未测"，不拿夹具自检冒充）
    metrics_json = load_json(EVAL / "results/metrics.json")[0] or {}
    amc = metrics_json.get("amc_aid") or {}
    dev_self = ((amc.get("dev") or {}).get("self_check") or {})
    axes.append({
        "axis": "轴4 结构准（行动项）", "metric": "产品侧 AMC-AID positive F1", "scope": "产品输出映射",
        "value": None, "n": {"successes": None, "total": None, "unit": "—"},
        "ci95": None, "source": f"{SRC['ledger']}（W6 待办）; {SRC['design']}",
        "floor": None, "frozen_baseline": None, "project_target": None,
        "gate": "不可用（未测）", "pass": None, "gap_to_floor_pp": None, "gap_to_target_pp": None,
        "recomputed_from_packet": False,
        "notes": (f"仪器与金标流水线在（amc_aid dev self_check macro/micro positive F1="
                  f"{dev_self.get('macro_positive_f1')}），但那是**夹具自检**不是模型质量；产品输出到 AMC-AID 的映射未接（LEDGER W6）。"
                  "语料基线：dev 65 场/45,869 句/222 正例，test1 82 场/43,228 句/236 正例；"
                  "公开 positive F1 基线 BERT 64.76 / Longformer 65.35 / StructBERT 67.84 / context-drop 70.82（不同来源口径不一，仅作锚）。"),
    })

    # —— 轴 5 检索与回答（从 packet 复算）
    def add_rag_scope(axis_name, scope_label, packet_name, gate_scope, notes,
                      include_content=True, project_targets=None):
        packet = packets.get(packet_name)
        if packet is None:
            return
        summary = packet_summary(packet)
        floor_map = FLOORS.get(gate_scope, {})
        frozen_map = (baseline or {}).get(gate_scope, {}) if baseline else {}
        for metric in RAG_METRICS:
            if metric == "faithfulness_content" and not include_content:
                continue
            block = metric_block(summary, metric)
            if not block or block.get("rate") is None:
                continue
            row(axis_name, metric, scope_label, block.get("rate"),
                block.get("successes"), block.get("total"),
                "断言" if metric.startswith("faithfulness") else
                ("事实" if metric == "context_recall" else
                 ("轮次" if metric == "answer_relevancy" else "上下文")),
                block.get("clopper_pearson_95"),
                f"{SRC['ledger']}; evaluation/results/{PACKETS[packet_name]}",
                floor=floor_map.get(metric), frozen=frozen_map.get(metric),
                project_target=(project_targets or {}).get(metric),
                gate=("quality_gate: " + gate_scope) if gate_scope else "仅登记",
                recomputed=True, notes=notes)

    add_rag_scope("轴5 检索与回答", "human27（27 题人工标注，旧会议，当前链路）", "human27", None,
                  "qa_set=种子会议人工标注 27 题/62 事实；注意该包也不含人类签名金标（本集问题与金标为人工标注、意图标签为代理起草）。")
    add_rag_scope("轴5 检索与回答", "extended68（代理起草扩展集，旧会议）", "extended68", None,
                  "68 条代理起草、机械核对；单会议/单裁判，不能外推为普遍水平。项目内既有目标：precision ≥68%、fact 有用率 ≥65%。",
                  project_targets={"context_precision": 0.68})
    add_rag_scope("轴5 检索与回答", "cross30_fresh（30 题/58 事实，fresh 会话）", "cross30_fresh", "cross30",
                  "质量闸主口径之一；金标 independent_agent_reviewed、human_checked=false。")
    add_rag_scope("轴5 检索与回答", "new_meeting（15 题/23 事实，unscoped 主口径）", "new_meeting", "new_meeting",
                  "unscoped（不限定会议）是主口径；scoped 100% 只是 smoke，不能当泛化。")
    add_rag_scope("轴5 检索与回答", "new_meeting_scoped（smoke，不入门禁）", "new_meeting_scoped", None,
                  "限定 meeting_ids=[3] + 题面与事实同源 + 合成语料：只验证链路可用，不产出质量结论。")
    add_rag_scope("轴5 检索与回答", "mixed100（组装集，100 题/167 事实）", "mixed100", "mixed100",
                  "由 cross30/new meeting/holdout A/D/B 的既有 fresh 产物组装，不是单次 100 题运行；holdout A/B/D 参与过调参。")

    # p0：均值口径按 quality_gate.py 复算（另在 quality_gate 段给逐轮与 pooled）
    p0_agg = p0_aggregates({name: packets.get(name) for name in ("p0_r1", "p0_r2", "p0_r3")})
    for metric in CORE_METRICS:
        value = p0_agg["mean_of_rates"].get(metric)
        if value is None:
            continue
        pooled = p0_agg["pooled"].get(metric) or {}
        row("轴5 检索与回答", f"{metric}（3 轮均值口径）", "p0（6 题×3 轮 fresh）", value,
            pooled.get("successes"), pooled.get("total"), "3 轮合并",
            pooled.get("clopper_pearson_95"),
            f"{SRC['gate']}; evaluation/results/p0_fresh6_r{{1,2,3}}_packet.json",
            floor=FLOORS["p0"].get(metric), frozen=(baseline or {}).get("p0", {}).get(metric),
            gate="quality_gate: p0", recomputed=True,
            notes=("均值口径复算自 3 个 packet 的 summary.rate 平均值（quality_gate.py 同法）；"
                   "CI 用 3 轮合并计数算 CP，重复测同一批 6 题，区间只作量级参考。"))

    # fact 类上下文有用率（扩展集既有目标 65%）
    row("轴5 检索与回答", "context_precision: fact 来源有用率", "extended68（结构化工具版）",
        0.5714, 20, 35, "fact 上下文", None,
        f"{SRC['docs_rag_params']}（2026-10-17 读数）", project_target=0.65,
        gate="项目内既有目标（未达）", recomputed=False,
        notes="fact 有用率 18.3%→42.9%→57.1%（历史），目标 65% 未达；数据来自 docs/RAG链路参数.md（既有判定，未在本脚本复算）。")

    # —— 可靠性 / Agent / 轨迹（metrics.json + trajectory packet + 报告；部分指标为历史读数）
    metrics_json = metrics_json or {}
    reliability = metrics_json.get("reliability") or {}
    e2e = ((reliability.get("end_to_end") or {}).get("success_rate") or {})
    call = (((reliability.get("call_success") or {}).get("overall") or {}).get("success_rate") or {})
    by_type = (reliability.get("call_success") or {}).get("by_call_type") or {}
    minutes = (by_type.get("MINUTES") or {}).get("success_rate") or {}
    failure = reliability.get("failure_modes") or {}
    latency_all = ((reliability.get("latency_ms") or {}).get("by_call_type") or {}).get("all_calls") or {}
    tokens_total = (reliability.get("tokens") or {}).get("total") or {}
    if e2e:
        row("可靠性（体验层）", "端到端逐场成功率", "19+ 运行目录/33 个 run dir 汇总（含调试期失败）",
            e2e.get("rate"), e2e.get("successes"), e2e.get("total"), "场尝试",
            e2e.get("clopper_pearson_95"),
            "evaluation/results/metrics.json#reliability", recomputed=True,
            notes="分母是跨全部运行目录的逐场尝试（修复期失败也计入），刻意不掩盖真实可靠性。")
    if call:
        row("可靠性（体验层）", "调用级成功率", "ai_call_log 派发账本", call.get("rate"),
            call.get("successes"), call.get("total"), "次派发", call.get("clopper_pearson_95"),
            "evaluation/results/metrics.json#reliability", recomputed=True,
            notes=(f"失败 {failure.get('failed_call_count')} 次，桶分布 {failure.get('by_bucket')}；"
                   "SDK 自动重试关闭，一行账本=一次真实派发。"))
    if minutes:
        row("可靠性（体验层）", "MINUTES 调用成功率（纪要主链路）", "ai_call_log 按调用类型",
            minutes.get("rate"), minutes.get("successes"), minutes.get("total"), "次派发",
            minutes.get("clopper_pearson_95"), "evaluation/results/metrics.json#reliability",
            recomputed=True, notes="所有调用类型里最低（19/25）——长输出+timeout 是主要失败桶。")
    if latency_all:
        row("可靠性（体验层）", "全链路模型调用延迟 p50/p95", "ai_call_log（所有调用类型）",
            None, None, latency_all.get("n"), "次调用", None,
            "evaluation/results/metrics.json#reliability", recomputed=False,
            notes=(f"p50 {round(latency_all.get('median', 0) / 1000, 2)}s、"
                   f"p95 {round(latency_all.get('p95_nearest_rank', 0) / 1000, 2)}s（nearest-rank，n={latency_all.get('n')}）；"
                   "含 MINUTES/AGENT 等异步长调用，不等于用户问答延迟。问答延迟见 docs/Agent指标.md：27 轮 CHAT p50 4.4s/p95 7.2s（2026-10-17）。"))
    if tokens_total:
        row("可靠性（体验层）", "模型 token 总量", "ai_call_log（所有调用类型）",
            None, None, tokens_total.get("total_tokens"), "token", None,
            "evaluation/results/metrics.json#reliability", recomputed=True,
            notes=(f"input {tokens_total.get('input_tokens')} / output {tokens_total.get('output_tokens')}；"
                   "按供应商返回用量计，不是账单金额；CHAT/嵌入单价仍未定价（docs/Agent指标.md）。"))

    intent = metrics_json.get("intent_metrics") or {}
    if intent:
        per_class = intent.get("per_class") or {}
        row("Agent 意图识别", "意图宏平均 F1（四类等权）", "gold/qa_seed 100 题（AI 复核标签）",
            intent.get("macro_f1"), None, intent.get("sample"), "题", None,
            "evaluation/results/metrics.json#intent_metrics", recomputed=True,
            notes=("逐类 F1：" + "、".join(
                f"{name} {round(v.get('f1', 0), 3)}（支持 {v.get('support')}）"
                for name, v in per_class.items()) +
                "；标签来源 AI 复核（human_verified=0/100），只作方向信号。"))
    retrieval = metrics_json.get("retrieval_metrics") or {}
    if retrieval:
        row("Agent 检索侧", "recall@k", "27 条人工集（当前检索链路）", retrieval.get("recall_at_k"),
            None, retrieval.get("sample"), "题", None,
            "evaluation/results/metrics.json#retrieval_metrics", recomputed=True,
            notes=f"k 分布 {retrieval.get('k_distribution')}；零调用复算，每次 make smoke 重跑。")
        row("Agent 检索侧", "MRR / nDCG@k", "27 条人工集（当前检索链路）",
            retrieval.get("mrr"), None, retrieval.get("sample"), "题", None,
            "evaluation/results/metrics.json#retrieval_metrics", recomputed=True,
            notes=f"nDCG@k={round(retrieval.get('ndcg_at_k', 0), 4)}；录音覆盖 {retrieval.get('recording_coverage', {}).get('rate')}。")
    chat = metrics_json.get("chat_citations") or {}
    if chat:
        loc = chat.get("locatable") or {}
        auth = chat.get("authorized") or {}
        row("Agent 引用不变量", "引用可定位率（零调用）", "27 轮真实问答",
            loc.get("rate"), loc.get("successes"), loc.get("total"), "条转写类引用",
            loc.get("clopper_pearson_95"), "evaluation/results/metrics.json#chat_citations",
            recomputed=True, notes=f"不适用逐字口径 {chat.get('not_applicable_count')} 条（纪要条目）；编编号 {chat.get('missing_ref_count')}。")
        row("Agent 引用不变量", "引用未越权率（零调用）", "27 轮真实问答",
            auth.get("rate"), auth.get("successes"), auth.get("total"), "条引用",
            auth.get("clopper_pearson_95"), "evaluation/results/metrics.json#chat_citations",
            recomputed=True, notes="会议在提问者可见范围内；口径沿用产品契约 evidence 定义。")
    trajectory_report = load_json(RESULTS / "trajectory_freshbase27_report.json")[0]
    if trajectory_report:
        tr = trajectory_report
        first = tr.get("first_tool") or {}
        ts = tr.get("tool_set") or {}
        coverage = (tr.get("required_coverage") or {}).get("covered") or {}
        must = (tr.get("must_cite") or {}).get("citations_present") or {}
        row("Agent 工具轨迹", "tool_call_f1（macro，预注册主指标）", "fresh trajectory 27 题（启发式金标）",
            (ts.get("macro") or {}).get("f1"), None, ts.get("sample"), "题", None,
            f"{SRC['trajectory_report']}; {SRC['gold_trajectory']}", recomputed=True,
            notes=("micro precision/recall/F1 = "
                   f"{(ts.get('micro') or {}).get('precision')}/{(ts.get('micro') or {}).get('recall')}/"
                   f"{(ts.get('micro') or {}).get('f1')}（matched/used/required="
                   f"{(ts.get('micro') or {}).get('matched_required')}/{(ts.get('micro') or {}).get('used_total')}/"
                   f"{(ts.get('micro') or {}).get('required_total')}）；金标 agent_draft_heuristic、human_checked=false。"))
        row("Agent 工具轨迹", "首轮工具 strict / acceptable", "fresh trajectory 27 题",
            (first.get("strict") or {}).get("rate"), (first.get("strict") or {}).get("successes"),
            (first.get("strict") or {}).get("total"), "题", (first.get("strict") or {}).get("clopper_pearson_95"),
            SRC["trajectory_report"], recomputed=True,
            notes=f"acceptable={((first.get('acceptable') or {}).get('successes'))}/"
                  f"{((first.get('acceptable') or {}).get('total'))}；"
                  "cross_meeting 首轮 strict 0/5（先调 list_facts）、state strict 1/4。")
        row("Agent 工具轨迹", "required 工具覆盖 / must_cite / forbidden", "fresh trajectory 27 题",
            coverage.get("rate"), coverage.get("successes"), coverage.get("total"), "题", None,
            SRC["trajectory_report"], recomputed=True,
            notes=(f"must_cite {must.get('successes')}/{must.get('total')}；"
                   f"forbidden 命中 {(tr.get('forbidden') or {}).get('hits')}；"
                   f"超预算 {(tr.get('tool_budget') or {}).get('cases_over_budget')}；"
                   f"pass^3 不可评（evaluated_cases={(tr.get('pass_k') or {}).get('evaluated_cases')}，"
                   f"单 trial 参考 {(tr.get('pass_k') or {}).get('single_trial_reference', {}).get('successes')}/27）。"))
    row("Agent 引用/claim", "数字不在任何上下文率（规则口径上界）", "fresh cross30 30 题",
        0.0939, 20, 213, "数字断言", clopper_pearson(20, 213),
        f"{SRC['claim_audit_fresh']}; 本脚本复算", recomputed=True,
        notes="规则上界：深判显示约 37.5% 是引用/时间元数据、22.9% 规则假阴性（claim_verify_cross30.json，多轮包）。")
    row("Agent 盲评", "无 expected facts 盲评 correct/partial/wrong", "mixed100 抽 20 条（AI 盲评）",
        0.7, 14, 20, "条", clopper_pearson(14, 20),
        f"{SRC['report_blind20']}; {SRC['report_blind40']}", recomputed=False,
        notes=("20 条：14 correct/6 partial/0 wrong；40 条扩展：34/6/0。评审为 subagent（非人工签名）；"
               "盲评暴露的问题 judge 大多判 supported（见 faithfulness_unsupported.known_judge_misses）。"))

    return {"axes": axes, "p0": p0_agg}


def build_quality_gate(packets: dict, baseline: dict | None) -> dict:
    """按 quality_gate.py 的判定逻辑复算 gate 行（不替代 quality_gate.py，只做报告口径）。"""
    rows = []
    gate_values = {}
    # p0：3 轮 rate 的均值（quality_gate.py 的唯一算法）
    p0_blocks = {}
    for metric in CORE_METRICS:
        rates = []
        for run in ("p0_r1", "p0_r2", "p0_r3"):
            packet = packets.get(run)
            block = metric_block(packet_summary(packet or {}), metric)
            if block and block.get("rate") is not None:
                rates.append(block["rate"])
        p0_blocks[metric] = round(sum(rates) / len(rates), 4) if rates else None
    gate_values["p0"] = p0_blocks
    for scope in ("cross30", "new_meeting", "mixed100"):
        packet_name = GATE_SCOPE_TO_PACKET[scope][0]
        packet = packets.get(packet_name)
        summary = packet_summary(packet or {})
        gate_values[scope] = {}
        for metric in CORE_METRICS:
            block = metric_block(summary, metric)
            if block and block.get("rate") is not None:
                gate_values[scope][metric] = round(block["rate"], 4)
    for scope, metrics in gate_values.items():
        frozen_scope = (baseline or {}).get(scope, {})
        for metric, value in metrics.items():
            if value is None:
                continue
            floor = FLOORS[scope][metric]
            frozen = frozen_scope.get(metric)
            ok = value >= floor and (frozen is None or value >= frozen - TOLERANCE)
            rows.append({"scope": scope, "metric": metric, "value": value, "floor": floor,
                         "frozen": frozen, "delta_vs_frozen_pp": pct(None if frozen is None else (value - frozen) * 100, 2),
                         "ok": ok})
    claim_audit = load_json(RESULTS / "claim_audit_cross30_fresh.json")[0] or {}
    claim_rate = claim_audit.get("not_in_any_context_rate")
    frozen_claim = (baseline or {}).get("claim_not_in_any_context")
    claim_ok = None
    if claim_rate is not None:
        claim_ok = claim_rate <= CLAIM_FLOOR and (
            frozen_claim is None or claim_rate <= frozen_claim + TOLERANCE)
    rows.append({"scope": "cross30", "metric": "claim_not_in_any_context",
                 "value": claim_rate, "floor": CLAIM_FLOOR, "frozen": frozen_claim,
                 "delta_vs_frozen_pp": pct(None if (frozen_claim is None or claim_rate is None)
                                           else (claim_rate - frozen_claim) * 100, 2),
                 "ok": claim_ok})
    failed = [r for r in rows if r["ok"] is False]
    return {"floors": FLOORS, "tolerance": TOLERANCE, "claim_floor": CLAIM_FLOOR,
            "baseline_path": SRC["baseline"], "baseline": baseline,
            "gate_values": gate_values, "rows": rows,
            "failed_rows": failed, "verdict_recomputed": "FAIL" if failed else "PASS",
            "notes": ("判定逻辑镜像 evaluation/quality_gate.py（floor + 冻结基线 2pp 容忍）；"
                      "与脚本 quality_gate.py 输出不一致时以 quality_gate.py 为准。"
                      "质量闸只覆盖 4 个 RAG 指标 + claim 越权率，不覆盖轴1–4 与 Agent 指标。")}


# ---------------------------------------------------------------------------
# 二、RAG context_recall 逐案归因
# ---------------------------------------------------------------------------
def build_rag_attribution(packets: dict) -> dict:
    gold100 = load_json(GOLD / "eval_100_mixed.json")[0] or {}
    mix_prov = {c["case_id"]: c for c in gold100.get("cases") or []}

    def mixed_question_type(cid: str) -> str:
        prov = mix_prov.get(cid, {})
        origin = prov.get("original_id")
        qtype = question_type(cid, "mixed100", origin)
        if qtype == "other":
            qtype = f"{prov.get('source_set', '?')} 层"
        return qtype

    facts_rows = []
    by_packet = {}
    for packet_name in ("cross30_fresh", "mixed100", "new_meeting", "p0_r1", "p0_r2", "p0_r3", "extended68"):
        packet = packets.get(packet_name)
        if packet is None:
            continue
        stats = {"cases": len(packet.get("cases") or []), "facts_total": 0, "facts_missing": 0,
                 "missing_by_classification": {}, "missing_by_question_type": {}}
        for case in packet.get("cases") or []:
            cid = case.get("case_id", "?")
            qtype = (mixed_question_type(cid) if packet_name == "mixed100"
                     else question_type(cid, packet_name))
            ctx = context_text(case)
            ans = answer_text(case)
            for fact_row in facts(case):
                index = fact_row.get("index")
                text = fact_text(case, index)
                stats["facts_total"] += 1
                if fact_row.get("found") is not False:
                    continue
                stats["facts_missing"] += 1
                key = f"{packet_name}:{cid}:{index}"
                manual = RAG_MANUAL_REVIEW.get(key)
                ctx_cov = coverage_proxy(text, ctx)
                ans_cov = coverage_proxy(text, ans)
                anchors = [t for t in NUMBER.findall(text) if not (t.isdigit() and len(t) <= 2)]
                anchors_in_ctx = [t for t in anchors if t in ctx]
                if manual:
                    classification, note = manual
                elif META_FACT_PATTERNS.search(text):
                    classification = "gold_metadata_or_absence_fact"
                    note = "自动归因：金标含实现元数据/否定性结论（检索上下文通常不可能包含）。"
                elif ctx_cov >= CTX_HAS_THRESHOLD:
                    classification = "context_has_fact_judge_missed"
                    note = "字面覆盖率≥0.6：疑似 judge 漏判，但需人工确认（judge 的 found 允许语义支撑，字面代理不精确）。"
                elif ctx_cov >= CTX_PARTIAL_THRESHOLD:
                    classification = "partial_context_coverage"
                    note = "上下文只覆盖该事实的一部分（字面覆盖 0.35–0.6）。"
                elif ans_cov >= ANSWER_WROTE_THRESHOLD:
                    classification = "retrieval_miss_answer_wrote"
                    note = "检索缺失，但答案写出了该事实（来源可能是未入上下文的工具结果/参数记忆，值得复核引用归属）。"
                else:
                    classification = "retrieval_miss"
                    note = "上下文没有、答案也没写：检索缺口直接传导到答案。"
                stats["missing_by_classification"][classification] = \
                    stats["missing_by_classification"].get(classification, 0) + 1
                stats["missing_by_question_type"][qtype] = \
                    stats["missing_by_question_type"].get(qtype, 0) + 1
                facts_rows.append({
                    "packet": packet_name, "case_id": cid, "question_type": qtype,
                    "question": case.get("question"),
                    "fact_index": index, "fact": text,
                    "judge_found": fact_row.get("found"),
                    "context_coverage_proxy": ctx_cov,
                    "answer_coverage_proxy": ans_cov,
                    "numbers_in_fact": anchors,
                    "numbers_found_in_contexts": anchors_in_ctx,
                    "classification": classification,
                    "manual_reviewed": bool(manual),
                    "note": note,
                })
        # 按题型给 found/total（全量事实，不只缺失）
        qtype_stats = {}
        for case in packet.get("cases") or []:
            cid = case.get("case_id", "?")
            qtype = (mixed_question_type(cid) if packet_name == "mixed100"
                     else question_type(cid, packet_name))
            for fact_row in facts(case):
                qtype_stats.setdefault(qtype, {"found": 0, "total": 0})
                qtype_stats[qtype]["total"] += 1
                if fact_row.get("found") is True:
                    qtype_stats[qtype]["found"] += 1
        stats["question_type_found"] = {
            k: {"successes": v["found"], "total": v["total"],
                "rate": round(v["found"] / v["total"], 4) if v["total"] else None}
            for k, v in qtype_stats.items()}
        by_packet[packet_name] = stats

    # top 失败 case：按缺失事实数排序，再按上下文 precision（低者优先）
    case_rank = {}
    for row in facts_rows:
        case_rank.setdefault((row["packet"], row["case_id"]), []).append(row)
    top_cases = []
    for (packet_name, cid), rows in case_rank.items():
        packet = packets.get(packet_name) or {}
        case = next((c for c in packet.get("cases") or [] if c.get("case_id") == cid), {})
        ctx_judged = (packet_judgement(case).get("context") or {}).get("contexts") or []
        useful = sum(1 for x in ctx_judged if x.get("useful") is True)
        precision = round(useful / len(ctx_judged), 4) if ctx_judged else None
        rel = (packet_judgement(case).get("answer") or {}).get("answered")
        top_cases.append({
            "packet": packet_name, "case_id": cid,
            "question_type": rows[0]["question_type"], "question": rows[0]["question"],
            "missing_count": len(rows),
            "missing_facts": [{"index": r["fact_index"], "fact": r["fact"],
                               "classification": r["classification"]} for r in rows],
            "context_precision_case": precision,
            "answer_answered": rel,
        })
    top_cases.sort(key=lambda c: (-c["missing_count"],
                                  c["context_precision_case"] if c["context_precision_case"] is not None else 2))
    # 代理字面匹配的标定：judge 判 found=true 的事实在上下文里的字面覆盖率分布
    calibration = {}
    for packet_name in ("cross30_fresh", "new_meeting", "extended68"):
        packet = packets.get(packet_name)
        if packet is None:
            continue
        vals = []
        for case in packet.get("cases") or []:
            ctx = context_text(case)
            for fact_row in facts(case):
                if fact_row.get("found") is True:
                    vals.append(coverage_proxy(fact_text(case, fact_row.get("index")), ctx))
        vals.sort()
        if vals:
            calibration[packet_name] = {
                "judge_found_facts": len(vals),
                "context_coverage_min": vals[0],
                "context_coverage_p25": vals[len(vals) // 4],
                "context_coverage_median": vals[len(vals) // 2],
                "context_coverage_max": vals[-1],
                "note": "judge 的 found=true 允许语义支撑，字面覆盖率天然偏低；因此对缺失事实用字面覆盖判断“上下文其实有”是保守的（宁可漏报）。",
            }
    mixed_layers = build_mixed100_layers(packets)
    # 题型分组（context_recall 的 found/total）：按"哪一类题最拖 recall"排序用
    by_question_type = {}
    for packet_name in ("cross30_fresh", "mixed100", "new_meeting"):
        st = by_packet.get(packet_name)
        if not st:
            continue
        by_question_type[packet_name] = {}
        for qtype, found in st.get("question_type_found", {}).items():
            missing = sum(1 for r in facts_rows if r["packet"] == packet_name and r["question_type"] == qtype)
            by_question_type[packet_name][qtype] = {
                "facts_found": found.get("successes"), "facts_total": found.get("total"),
                "context_recall": found.get("rate"), "facts_missing": missing,
            }
        if packet_name == "mixed100":
            def layer_key(qtype: str) -> str | None:
                if qtype.startswith(("cm", "cs", "ct")):
                    return "cross30"
                if qtype.startswith("n "):
                    return "newmeeting"
                if qtype.endswith(" 层"):
                    return qtype[:-2]
                return None

            by_question_type[packet_name] = {
                qtype: {**stat, "layer": layer_key(qtype)}
                for qtype, stat in by_question_type[packet_name].items()}
    return {
        "definition": ("context_recall = 标注 expected facts 能在检索上下文里找到的比例。缺失事实的归因是"
                       "**字面 bigram 覆盖代理 + 人工抽查覆盖表**，不是语义判定；结论用于定位方向，不替代人工复核。"),
        "thresholds": {"context_has": CTX_HAS_THRESHOLD, "partial": CTX_PARTIAL_THRESHOLD,
                       "answer_wrote": ANSWER_WROTE_THRESHOLD},
        "by_packet": by_packet,
        "by_question_type": by_question_type,
        "mixed100_layers_recomputed": mixed_layers,
        "calibration": calibration,
        "missing_facts": facts_rows,
        "top_failing_cases": top_cases[:20],
        "reading": ("cross30_fresh 的 14 条缺失里：cs 系列题 11 条（8/19=42.1% recall），其中 11 条都是"
                    "“金标含实现元数据/否定性结论”（supersedes_id、CONFIRMED/DRAFT、工具 notice、系列只含会议 2 等）——"
                    "这类事实按定义不可能出现在检索上下文，context_recall 被系统性低估；"
                    "cm 跨录音 18/19=94.7%、ct 单点 18/20=90.0%。真正“检索没拿到”的是 cm08 补充验收、"
                    "ct02 复合时间线、ct06 9:00 节点、m042/m058 类清单项。new_meeting 的 3 条缺失"
                    "（主持人李娜、未覆盖主题两条）是 fixture 限制与检索缺口。"),
    }


# ---------------------------------------------------------------------------
# 三、faithfulness 不支持断言分类
# ---------------------------------------------------------------------------
def build_faithfulness(packets: dict) -> dict:
    per_packet = {}
    samples = []
    type_totals = {}
    for packet_name in ("cross30_fresh", "mixed100", "new_meeting", "p0_r1", "p0_r2", "p0_r3", "extended68"):
        packet = packets.get(packet_name)
        if packet is None:
            continue
        summary = packet_summary(packet)
        faith = metric_block(summary, "faithfulness") or {}
        content = metric_block(summary, "faithfulness_content") or {}
        info = {"claims_total": faith.get("total"), "claims_supported": faith.get("successes"),
                "unsupported_total": None, "unsupported_content": None,
                "meta_claims": summary.get("faithfulness_meta_claims"),
                "content_total": content.get("total"), "content_supported": content.get("successes"),
                "by_type": {}}
        unsupported = []
        for case in packet.get("cases") or []:
            for claim in claims(case):
                if claim.get("supported") is not False:
                    continue
                text = claim.get("text") or ""
                ctype = classify_faith_claim(text, claim.get("meta"))
                unsupported.append({"packet": packet_name, "case_id": case.get("case_id"),
                                    "type": ctype, "meta": bool(claim.get("meta")),
                                    "text": text})
                info["by_type"][ctype] = info["by_type"].get(ctype, 0) + 1
                type_totals.setdefault(ctype, []).append((packet_name, case.get("case_id"), text))
        info["unsupported_total"] = len(unsupported)
        info["unsupported_content"] = sum(1 for u in unsupported if not u["meta"])
        per_packet[packet_name] = info
        samples.extend(unsupported)
    # 每类取 2 条样例（内容级优先），总样例 <= 18
    picked = []
    seen_types = {}
    for ctype in list(type_totals):
        for item in samples:
            if item["type"] != ctype or item["meta"]:
                continue
            if seen_types.get(ctype, 0) >= 2:
                break
            seen_types[ctype] = seen_types.get(ctype, 0) + 1
            picked.append(item)
        if len(picked) >= 16:
            break
    # 已知 judge 漏判（人工盲评抓到、judge 判 supported=true 的样例；来自盲评报告，非本次复算）
    known_misses = [
        {"case_id": "m030", "type": "与原文相反/矛盾",
         "claim_text": "回滚耗时：最快 5 分钟能切回旧版本（因数据库改动为加字段且向后兼容，回滚无需动数据）[1]。",
         "judge_supported": True,
         "evidence": f"{SRC['report_blind40']}；同一答案另一断言原文引用“回滚步需要动数据”（转写 [124s][李悦]）。",
         "note": "judge 同一答案里同时判“回滚无需动数据”与“回滚步需要动数据”都 supported：faithfulness 对自相矛盾不敏感。"},
        {"case_id": "m032", "type": "归属/人名冲突",
         "claim_text": "下午会汇报功能验收情况的是王敏。",
         "judge_supported": True,
         "evidence": f"{SRC['report_blind40']}（点名“王敏”与说话人标签“张晨”冲突）；转写开场“先请王敏说一下功能验收的情况”后由张晨接续汇报。",
         "note": "judge 只看 claim 是否被上下文支持，不检查说话人归属链。"},
        {"case_id": "m060", "type": "时间/对象过度推断",
         "claim_text": "发布当天（9月1日）10:00需要完成两项事项：发布公告和通告……完成常见问题知识库建设……",
         "judge_supported": True,
         "evidence": f"{SRC['report_blind20']}；10:00 出现在转写，但“发布公告/FAQ 建设都 10:00 完成”是合并推断。",
         "note": "盲评 20 条里 m060 属“时间/对象过度解读”，judge 全部 supported。"},
        {"case_id": "m011", "type": "引语/发言人归属",
         "claim_text": "原文依据为李月在会上的发言：“同意于9月1日9:00正式发布AI智能会议助手与任务跟踪系统” [1]。",
         "judge_supported": False,
         "evidence": f"{SRC['report_blind20']}；judge 本次判了 unsupported（这条是 judge 抓到的），但 20 条盲评仍把它当主要问题，说明引用归属需要独立指标。",
         "note": "faithfulness 能抓“不在上下文”，抓不到“引语拼错/归属错但字面相似”。"},
    ]
    return {
        "definition": ("faithfulness = 答案断言里能被上下文支撑的比例（全部断言口径）；"
                       "faithfulness_content 剔除“以上信息来自…”这类元陈述后重算。"
                       "分类是**基于断言文本的规则粗分**，只用于定位类型，不替代逐条人工判定。"),
        "rule_priority": [name for name, _ in FAITH_TYPE_RULES],
        "per_packet": per_packet,
        "type_totals": {name: len(type_totals.get(name, [])) for name in FAITH_TYPE_NAMES},
        "samples": picked[:18],
        "known_judge_misses": known_misses,
        "reading": ("跨 packet 合看，内容级不支持断言集中在：①时间/因果顺序推断（把复述顺序当时间顺序，ct05/m025）；"
                    "②引语/归属（李月、刘颖、notice 原文不在被引上下文）；③数字/日期归属（9:00/10:00、2024 年猜错）；"
                    "④从材料外推的建议/结论（m042 的“负责人负载提示”）。盲评同时给出反向证据：m030/m032/m060 这类"
                    "矛盾/归属/过度推断 judge 判 supported=true —— judge 的 89% 不覆盖这部分答案错误。"),
    }


# ---------------------------------------------------------------------------
# 四、数字/引语 attribution（数字复用 claim_citation_audit.py 的判定逻辑）
# ---------------------------------------------------------------------------
def audit_numbers(case: dict) -> dict:
    """与 evaluation/analysis/claim_citation_audit.py 相同的零调用规则（重新实现，不 import）。"""
    source_items = case.get("citations") or context_items(case)
    citation_by_ref = {int(item.get("ref")): item for item in source_items if item.get("ref") is not None}
    unsupported_cited, unsupported_any = [], []
    total_numbers = 0
    all_contexts = "\n".join(str(item.get(k) or "") for item in context_items(case)
                             for k in ("source_text", "quote", "text"))
    for sentence in SENTENCE.split(case.get("answer") or ""):
        sentence = sentence.strip()
        if not sentence:
            continue
        refs = [int(m.group(1)) for m in CITATION.finditer(sentence)]
        if not refs:
            continue
        texts = []
        for ref in refs:
            item = citation_by_ref.get(ref) or {}
            for key in ("source_text", "quote", "text"):
                value = item.get(key)
                if isinstance(value, str) and value:
                    texts.append(value)
        haystack = "\n".join(texts)
        for token in dict.fromkeys(NUMBER.findall(sentence)):
            if token.isdigit() and len(token) <= 2:
                continue
            total_numbers += 1
            if token not in haystack:
                unsupported_cited.append({"token": token, "refs": refs, "sentence": sentence[:200]})
                if token not in all_contexts:
                    unsupported_any.append({"token": token, "refs": refs, "sentence": sentence[:200]})
    return {"case_id": case.get("case_id"), "numbers_checked": total_numbers,
            "unsupported_cited": unsupported_cited, "unsupported_any": unsupported_any}


def _quote_is_template(quote: str) -> bool:
    """过滤明显不是“引语”的引号内容：检索关键词、会议名、问句模板。"""
    q = quote.strip()
    if re.search(r"[/+]", q) and len(norm(q)) <= 24:
        return True
    if re.match(r"^(产品发布评审会议|客户支持知识库与智能客服灰度评审会|产品发布评审系列)", q):
        return True
    if re.search(r"几点|何时|哪些|分别|怎么|如何|会议名称|来源类型", q):
        return True
    return len(norm(q)) < 8


def audit_quotes(case: dict) -> dict:
    """引语 attribution 近似：引语是否出现在被引上下文 / 任何上下文（LCS≥10 视为命中）。"""
    items = context_items(case)
    by_ref = {int(it.get("ref")): it for it in items if it.get("ref") is not None}
    all_ctx = norm("".join(str(it.get("text") or "") for it in items))
    checked = gaps_cited = gaps_any = skipped_template = 0
    examples = []
    for sentence in SENTENCE.split(case.get("answer") or ""):
        refs = [int(m.group(1)) for m in CITATION.finditer(sentence)]
        if not refs:
            continue
        cited = norm("".join(str((by_ref.get(r) or {}).get("text") or "") for r in refs))
        for raw_quote in QUOTE.findall(sentence):
            if _quote_is_template(raw_quote):
                skipped_template += 1
                continue
            parts = [norm(p) for p in ELLIPSIS.split(raw_quote) if len(norm(p)) >= 6]
            if not parts:
                parts = [norm(raw_quote)]
            checked += 1
            miss_c = [p for p in parts if lcs_len(p, cited) < 10]
            miss_a = [p for p in parts if lcs_len(p, all_ctx) < 10]
            if miss_c:
                gaps_cited += 1
                if len(examples) < 8:
                    examples.append({"case_id": case.get("case_id"), "refs": refs,
                                     "quote": raw_quote[:140],
                                     "not_in_any_context": bool(miss_a)})
            if miss_a:
                gaps_any += 1
    return {"checked": checked, "skipped_template": skipped_template,
            "not_in_cited": gaps_cited, "not_in_any": gaps_any, "examples": examples}


def build_citation_attribution(packets: dict) -> dict:
    numbers = {}
    quotes = {}
    number_samples = []
    quote_samples = []
    audit_fresh = None
    for packet_name in ("cross30_fresh", "mixed100", "new_meeting", "p0_r1", "p0_r2", "p0_r3", "extended68"):
        packet = packets.get(packet_name)
        if packet is None:
            continue
        checked = bad_cited = bad_any = 0
        q_checked = q_bad_cited = q_bad_any = q_skipped = 0
        for case in packet.get("cases") or []:
            num = audit_numbers(case)
            checked += num["numbers_checked"]
            bad_cited += len(num["unsupported_cited"])
            bad_any += len(num["unsupported_any"])
            for item in num["unsupported_any"]:
                if len(number_samples) < 12:
                    number_samples.append({"packet": packet_name, "case_id": case.get("case_id"),
                                           "token": item["token"], "refs": item["refs"],
                                           "sentence": item["sentence"]})
            quo = audit_quotes(case)
            q_checked += quo["checked"]
            q_bad_cited += quo["not_in_cited"]
            q_bad_any += quo["not_in_any"]
            q_skipped += quo["skipped_template"]
            for item in quo["examples"]:
                if len(quote_samples) < 10 and item.get("not_in_any_context"):
                    quote_samples.append({"packet": packet_name, **item})
        numbers[packet_name] = {
            "numbers_checked": checked,
            "unsupported_cited": bad_cited,
            "unsupported_any": bad_any,
            "cited_gap_rate": round(bad_cited / checked, 4) if checked else None,
            "not_in_any_context_rate": round(bad_any / checked, 4) if checked else None,
            "clopper_pearson_95_not_in_any": clopper_pearson(bad_any, checked) if checked else None,
        }
        quotes[packet_name] = {
            "quotes_checked": q_checked, "skipped_template": q_skipped,
            "not_in_cited": q_bad_cited, "not_in_any": q_bad_any,
            "not_in_cited_rate": round(q_bad_cited / q_checked, 4) if q_checked else None,
            "not_in_any_rate": round(q_bad_any / q_checked, 4) if q_checked else None,
        }
        if packet_name == "cross30_fresh":
            audit_fresh = {"recomputed": numbers[packet_name], "quotes": quotes[packet_name]}
    existing = load_json(RESULTS / "claim_audit_cross30_fresh.json")[0] or {}
    verify = load_json(RESULTS / "claim_verify_cross30.json")[0] or {}
    verify_summary = verify.get("summary") or {}
    verify_total = sum(int(v or 0) for v in verify_summary.values()) or None
    deep_verify = {
        "artifact": SRC["claim_verify"],
        "based_on": {"audit": verify.get("audit"), "packet": verify.get("packet")},
        "summary": verify_summary,
        "shares": {k: (round(int(v) / verify_total, 4) if verify_total else None)
                   for k, v in verify_summary.items()},
        "reading": ("这是对**多轮** generalized_cross_packet 的 48 个规则缺口的 LLM 深判：18 条是引用/时间定位元数据"
                    "（不是事实断言）、11 条其实被被引上下文支持（规则假阴性）、18 条真不支持、1 条派生。"
                    "用它折算规则口径：真 unsupported ≈ 18/188 = 9.6%；规则 25.5% 高估约 2.7 倍。"
                    "但该深判的 packet 不是 fresh cross30_fresh_packet.json，不能直接套到 fresh 的 18.3%/9.4% 上。"),
    }
    if audit_fresh and existing:
        audit_fresh["matches_existing_artifact"] = {
            "artifact": SRC["claim_audit_fresh"],
            "recomputed": {"numbers_checked": audit_fresh["recomputed"]["numbers_checked"],
                           "unsupported_cited": audit_fresh["recomputed"]["unsupported_cited"],
                           "unsupported_any": audit_fresh["recomputed"]["unsupported_any"]},
            "artifact_values": {"numbers_checked": existing.get("numbers_checked"),
                                "unsupported_cited": existing.get("unsupported_cited_numbers"),
                                "unsupported_any": existing.get("unsupported_any_numbers")},
            "match": (audit_fresh["recomputed"]["numbers_checked"] == existing.get("numbers_checked")
                      and audit_fresh["recomputed"]["unsupported_cited"] == existing.get("unsupported_cited_numbers")
                      and audit_fresh["recomputed"]["unsupported_any"] == existing.get("unsupported_any_numbers")),
        }
    # 敏感性估计（明确标注：跨 packet 借用比例，只给量级，不能当结论）
    sensitivity = {}
    fresh_numbers = numbers.get("cross30_fresh") or {}
    if fresh_numbers.get("numbers_checked") and verify_total:
        shares = deep_verify["shares"]
        not_false_positive = 1.0 - (shares.get("citation_mapping") or 0) - (shares.get("supported_by_cited") or 0)
        checked = fresh_numbers["numbers_checked"]
        for key in ("unsupported_any", "unsupported_cited"):
            rule_count = fresh_numbers.get(key)
            if rule_count is None:
                continue
            est = rule_count * not_false_positive
            sensitivity[key] = {
                "rule_count": rule_count, "rule_rate": round(rule_count / checked, 4),
                "scenario_estimate_count": round(est, 1),
                "scenario_estimate_rate": round(est / checked, 4),
            }
        sensitivity["assumption"] = ("借用多轮 48 条深判的构成比例：citation_mapping 37.5% + supported_by_cited 22.9% "
                                     "视为规则假阳性；只作量级。fresh packet 没有做过同样的深判，不能当结论。")
    return {
        "definition": ("数字/引语 attribution 是零模型调用的规则审计：带引用句里的数字/引语是否出现在"
                       "**被引上下文**（citation attribution）与**任何检索上下文**（更宽的口径）。"
                       "规则会把“引用/时间定位元数据”“派生数字”“跨上下文拼接”判成缺口，是**上界**；"
                       "真值需要 LLM/人工深判（见 deep_verify）。"),
        "numbers": numbers,
        "quotes": quotes,
        "number_samples": number_samples,
        "quote_samples": quote_samples,
        "fresh_cross30": audit_fresh,
        "deep_verify": deep_verify,
        "sensitivity_estimate": sensitivity,
    }


# ---------------------------------------------------------------------------
# 五、工具轨迹
# ---------------------------------------------------------------------------
def build_trajectory() -> dict:
    report = load_json(RESULTS / "trajectory_freshbase27_report.json")[0] or {}
    utility = load_json(RESULTS / "tool_utility_freshbase27.json")[0] or {}
    out = {"available": bool(report), "source": f"{SRC['ledger']}; {SRC['trajectory_report']}",
           "gold_provenance": "gold/trajectory_27.json：label_source=agent_draft_heuristic，human_checked=false（27 题启发式金标）"}
    if not report:
        return out
    first = report.get("first_tool") or {}
    tool_set = report.get("tool_set") or {}
    out["sample"] = report.get("sample")
    out["first_tool"] = {
        "strict": first.get("strict"),
        "acceptable": first.get("acceptable"),
        "per_intent": first.get("per_intent"),
    }
    out["tool_call_f1"] = {
        "micro": tool_set.get("micro"),
        "macro": tool_set.get("macro"),
        "primary_metric_note": ("预注册主指标是逐题工具集合 F1 的均值（macro，0.7309）；"
                                "micro（0.6769）按调用并集算，两者不可混报。"),
    }
    out["required_coverage"] = report.get("required_coverage")
    out["must_cite"] = report.get("must_cite")
    out["forbidden"] = report.get("forbidden")
    out["tool_budget"] = {"trials": (report.get("tool_budget") or {}).get("trials"),
                          "cases_over_budget": (report.get("tool_budget") or {}).get("cases_over_budget"),
                          "trials_over_budget": (report.get("tool_budget") or {}).get("trials_over_budget")}
    out["pass_k"] = report.get("pass_k")
    # over-call 明细：extra = used - required；是否贡献 citation 用 citation kind 代理
    extra_by_tool = {}
    rows = utility.get("rows") or []
    for row in rows:
        for tool in row.get("extra") or []:
            entry = extra_by_tool.setdefault(tool, {"extra_calls": 0, "without_citation_contribution": 0})
            entry["extra_calls"] += 1
            if tool in (row.get("extra_without_citation_kind") or []):
                entry["without_citation_contribution"] += 1
    for _tool, entry in extra_by_tool.items():
        attributed = entry["extra_calls"] - entry["without_citation_contribution"]
        entry["with_citation_contribution"] = attributed
        entry["contribution_rate"] = round(attributed / entry["extra_calls"], 4)
    out["over_call"] = {
        "extra_calls": utility.get("extra_calls"),
        "extra_without_citation_kind": utility.get("extra_without_citation_kind"),
        "by_tool": extra_by_tool,
        "rows": rows,
        "reading": ("18 次 extra call 中 7 次没有任何对应 citation kind（多余且无贡献），11 次多调的工具确实贡献了引用。"
                    "被压掉的实验臂都掉 faithfulness —— 只能按“是否贡献引用”压，不能按数量压。"),
    }
    out["first_tool_failures"] = []
    for case in report.get("per_case") or []:
        ft = case.get("first_trial") or {}
        if ft.get("strict") is True:
            continue
        out["first_tool_failures"].append({
            "case_id": case.get("case_id"), "intent": case.get("intent"),
            "question": case.get("question"),
            "first_tool": ft.get("first_tool"), "required": ft.get("required_tools"),
            "acceptable_tools": ft.get("acceptable_tools"), "used": ft.get("used_tools"),
            "strict": ft.get("strict"), "acceptable": ft.get("acceptable"),
        })
    out["arms"] = TRAJECTORY_ARMS
    out["reading"] = ("tool_call_f1 低的主因是 over-call（12 只多调、3 既漏又多余、0 只漏调）；"
                      "但 over-call 里 11/18 有 citation 贡献，且所有压 over-call 的实验臂都伴随 cross30 faithfulness 下降 —— "
                      "F1 不能当唯一目标；pass^3 因只有 1 份 trial 产物而**不可评**（0/27 进分母，单 trial 参考 24/27）。")
    return out


# ---------------------------------------------------------------------------
# 六、评测口径问题清单 + 争议数字
# ---------------------------------------------------------------------------
def build_protocol_issues(packets: dict, mixed_layers: dict) -> list:
    issues = [
        {"id": "P01", "severity": "高", "title": "mixed100 是组装集，不是单次 100 题运行",
         "evidence": (f"{SRC['report_eval100']} §1：答案由既有 fresh-session 产物组装（cross30/new meeting 为当前配置，"
                      "holdout A/B/D 来自各自最近一轮）；2026-10-22_generalization_audit_v2.md §4.1 也把它列为口径风险。"),
         "impact": "100 题汇总数（91.02/71.83/89.21/97.00）不能当“同一配置单次运行”的读数；跨层差异混入轮次/配置差异。"},
        {"id": "P02", "severity": "高", "title": "同一批 cross30 题在两份产物里数字不同（fresh 75.86% vs mixed100 分层 81.03%）",
         "evidence": (f"cross30_fresh_packet.json summary：recall 44/58=75.86%、precision 84/132=63.64%、"
                      f"faithfulness 226/255=88.63%；eval100_scope_fixed_packet.json 的 cross30 分层复算："
                      f"recall {mixed_layers.get('cross30', {}).get('context_recall', {}).get('successes')}/"
                      f"{mixed_layers.get('cross30', {}).get('context_recall', {}).get('total')}="
                      f"{mixed_layers.get('cross30', {}).get('context_recall', {}).get('rate')}；"
                      "两份产物的 30 题中 17 题逐题计数不同（回答/判定来自不同轮次）。"),
         "impact": "引用 cross30 数字必须写清是哪份产物；2026-10-22_eval100_scale.md 的分层表数字（81.7/74.6/78.6）"
                   "与当前两份 packet 都对不上（precision 66.67%、faithfulness 84.47%），不可复现。"},
        {"id": "P03", "severity": "高", "title": "单裁判：所有 RAG 指标来自 deepseek-flash（label f）单族判定",
         "evidence": ("所有 packet 的 adjudicator_disclosure 均为 model_used=deepseek-flash、provider=deepseek、not_gold；"
                      "JUDGE_CARD.md 校准状态 pending；LEDGER 记录跨族标签翻转 26–34%。"),
         "impact": "绝对数只能当方向信号；RAG 四指标与 faithfulness 的 89% 不能当“答案正确率”。"},
        {"id": "P04", "severity": "高", "title": "金标 provenance：人工金标为 0；cross30/new meeting/eval100 都是代理或合成",
         "evidence": ("gold/cross_meeting_qa_30.json human_checked=false、label_source=independent_agent_reviewed；"
                      "gold/new_meeting_15.json label_source=synthetic_agent_draft；"
                      "gold/eval_100_mixed.json label_source=mixed_existing_golds；"
                      "2026-10-22_eval_design_audit.md §2：“人工金标为 0”；gold/HUMAN_REVIEW.md 的 100 条意图与 68 条扩展复核仍未导入。"),
         "impact": "所有用“金标”二字的地方都应写成 draft-gold/AI-reviewed gold；不能写“人工确认的召回率”。"},
        {"id": "P05", "severity": "高", "title": "quality_gate 的阈值在报告与代码之间不一致，且 frozen baseline 等于当前值",
         "evidence": ("quality_gate.py FLOORS：p0 precision 0.55 / cross30 precision 0.55 / faithfulness 0.80 / "
                      "mixed100 recall 0.85 等；2026-10-22_rag_quality_gate.md 写的阈值是 p0 precision ≥0.58、"
                      "cross30 ≥0.60、faithfulness ≥0.85；2026-10-22_eval100_scale.md 写 recall≥88/68/87/94。"
                      "冻结基线 cross30 与当前 packet 完全相同（0.7586/0.6364/0.8863/0.9333），所以 PASS 是 0-delta 复述。"),
         "impact": "“QUALITY GATE PASS”只能说明“没比冻结那天的产物更差”，且换用代码阈值后局部仍 PASS；"
                   "报告里的阈值数字不能引用，引用必须写 evaluation/quality_gate.py 的 FLOORS。"},
        {"id": "P06", "severity": "中", "title": "P0 6 题×3 轮不是 18 个独立样本，CI 被重复测量高估",
         "evidence": ("p0_fresh6_r1/r2/r3 三份 packet 是同一批 cross30 前 6 题（cm01–cm06）的 3 次重复；"
                      "quality_gate.py 只取 3 个 rate 的均值，不带 CI；本报告给的 pooled Clopper-Pearson 区间把 3 轮当独立样本。"),
         "impact": "p0 的 97.22% recall（35/36）、64.23% precision（79/123）只能当小样本方向信号；"
                   "n=6 题对“发布决策”级别的指标功效不足（DESIGN §6.3）。"},
        {"id": "P07", "severity": "高", "title": "cross30 的 expected_facts 含实现元数据/否定性结论，context_recall 被系统性低估",
         "evidence": ("cross30_fresh 的 14 条缺失事实里，cs02#2（OPEN 状态）、cs05#1（系列只含会议 2）、cs06#1-3（DRAFT summary）、"
                      "cs07#1-2（supersedes_id/无法推断）、cs08#1-2（DRAFT vs CONFIRMED）、cs09#1-2（工具 notice）共 12 条"
                      "不是检索材料里能出现的内容；2026-10-22_generalization_audit_v2.md §4 第 4 条已登记“cross30 gold 含实现元数据”。"),
         "impact": "cross30 的 recall 75.86% 不是纯检索能力读数；按“可检索事实”口径应更高。修复方向是 gold 分层（剔除元数据事实/单列）。"},
        {"id": "P08", "severity": "中", "title": "claim/citation 规则审计是上界，会高估真 unsupported 约 2.7 倍",
         "evidence": ("fresh cross30：213 个数字断言里 39 条不在被引上下文（18.3%）、20 条不在任何上下文（9.4%，与 claim_audit_cross30_fresh.json 完全复现）；"
                      "claim_verify_cross30.json 对多轮 48 个缺口深判：citation_mapping 18、supported_by_cited 11、unsupported 18、derived 1。"),
         "impact": "18.3% 不能当幻觉率；真 unsupported 方向约 9–10%（且深判 packet 不是 fresh）。"
                   "该数字进报告必须写成“规则口径上界”。"},
        {"id": "P09", "severity": "中", "title": "盲评是 AI/subagent 盲评，不是人工签名；且 new meeting 答案盲评 CSV 曾带 expected_facts",
         "evidence": ("2026-10-22_eval_design_audit.md §1/§3：answer blind review 的 CSV 仍带 expected_facts 列，不是严格盲评；"
                      "blind20/40 为 subagent 盲评（reports/2026-10-22_blind20_review.md 注明评审=subagent-blind）；"
                      "new meeting 15 条答案 15 correct 的审核者也是独立 Agent。"),
         "impact": "盲评结果（20 条 14/6/0；40 条 34/6/0）只能当诊断线索；“0 wrong”不能写成事实正确率。"},
        {"id": "P10", "severity": "中", "title": "pass^3 不可评：每次只传了 1 份 trial 产物",
         "evidence": ("trajectory_freshbase27_report.json：artifact_count=1、k=3、trial_counts.questions_with_k_trials=0，"
                      "pass_k.evaluated_cases=0；single_trial_reference=24/27。Makefile 要求同一配置重复抓 k 次并按 trial 传产物。"),
         "impact": "“pass^3 12/12”是旧的小集（12 题×3，2026-10-21 深研报告）读数，不能套到 27 题轨迹集；"
                   "轨迹评测的重复性目前没有读数。"},
        {"id": "P11", "severity": "中", "title": "轴 1–4 读数是旧基板，不能与轴 5 当前链路拼成一个总分或一张“当前质量”表",
         "evidence": ("轴1 cpCER 2026-09-27（AliMeeting 8 场）；轴2/轴3 为 2026-09-25/10-05 VCSum test26 旧流水线；"
                      "轴4 产品侧未接；轴5 当前为 2026-10-16 后的混合检索+rerank 链路。评测宪法第 6 条也禁止加权总分。"),
         "impact": "跨轴比较只能比“各轴自身的方向”，不能声称“系统整体 80 分”；换 ASR/换链路后轴 2–4 必须重基线。"},
        {"id": "P12", "severity": "中", "title": "质量闸不做统计检验、CI 不进门禁；2pp 容忍 + n 小，漂移可能被判 PASS",
         "evidence": ("quality_gate.py 只比较点估计；p0 6 题、cross30 30 题/58 事实、new meeting 15 题/23 事实，"
                      "CP 区间宽（如 cross30 recall 95% CI 62.8–86.1%）。"),
         "impact": "门禁是“防退化护栏”而不是“质量达标证明”；决策需配对设计与功效计算（DESIGN §6.3）。"},
        {"id": "P13", "severity": "低", "title": "报告/脚本对同一指标存在多处历史口径（CER 36.5% 已作废、new meeting 100% scoped、extended 68 的 78.4%）",
         "evidence": ("LEDGER 轴1 明确“早期 3 场口径 36.5% 作废”；2026-10-22_new_meeting_eval.md 顶部修正 scoped 100% 不是泛化；"
                      "docs/RAG链路参数.md 提示 27 条人工集的 78.4% 不等于普遍水平。"),
         "impact": "引用旧报告时必须读 LEDGER 存活结论登记，作废数字不能出现在新文档里。"},
    ]
    return issues


def build_mixed100_layers(packets: dict) -> dict:
    """从 eval100_scope_fixed_packet 复算分层指标，用于和报告分层表对照。"""
    packet = packets.get("mixed100")
    gold = load_json(GOLD / "eval_100_mixed.json")[0] or {}
    prov = {c["case_id"]: c for c in gold.get("cases") or []}
    if packet is None:
        return {}
    layers = {}
    for case in packet.get("cases") or []:
        source_set = prov.get(case.get("case_id"), {}).get("source_set", "?")
        layer = layers.setdefault(source_set, {})
        judgement = packet_judgement(case)
        checks = {
            "context_recall": (judgement.get("context", {}).get("facts"), lambda x: x.get("found") is True),
            "context_precision": (judgement.get("context", {}).get("contexts"), lambda x: x.get("useful") is True),
            "faithfulness": (judgement.get("answer", {}).get("claims"), lambda x: x.get("supported") is True),
            "answer_relevancy": ([judgement.get("answer")] if judgement.get("answer") else [],
                                 lambda x: x.get("answered") is True),
        }
        for metric, (block, pos) in checks.items():
            layer.setdefault(metric, {"successes": 0, "total": 0})
            layer[metric]["successes"] += sum(1 for x in (block or []) if pos(x))
            layer[metric]["total"] += len(block or [])
    out = {}
    for source_set, layer in layers.items():
        out[source_set] = {}
        for metric, counts in layer.items():
            success, total = counts["successes"], counts["total"]
            out[source_set][metric] = {"successes": success, "total": total,
                                       "rate": round(success / total, 4) if total else None}
    return out


def build_disputed_numbers(packets: dict, mixed_layers: dict, status: dict) -> list:
    cross_layer = mixed_layers.get("cross30", {})
    return [
        {"number": "cross30 分层 recall 81.7% / precision 74.6% / faithfulness 78.6%（2026-10-22_eval100_scale.md §3）",
         "status": "不可复现",
         "recomputed": {k: cross_layer.get(k) for k in ("context_recall", "context_precision", "faithfulness")},
         "reason": "从当前 eval100_scope_fixed_packet.json 复算得 81.03% / 66.67% / 84.47%，precision 与 faithfulness 差 8–6pp；"
                   "从 eval100_packet.json 复算得 75.9% / 65.9% / 86.7% 也不是。分层表来源不明（可能来自中间版本产物）。"},
        {"number": "100 题总 precision 71.1%（eval100_scale.md §3 结论段）",
         "status": "与同报告表格不一致",
         "recomputed": {"context_precision": 0.7183},
         "reason": "同报告 §2 表格与 packet summary 都是 71.83%；结论段 71.1% 是旧数或笔误。"},
        {"number": "new meeting scoped recall 100% / precision 97.96%",
         "status": "只作 smoke，不能引用为质量",
         "recomputed": {"unscoped": {"context_recall": 0.8696, "context_precision": 0.7097}},
         "reason": "限定 meeting_ids=[3] + 题面与事实同源 + 合成语料；2026-10-22_eval_design_audit.md 已把主口径改为 unscoped。"},
        {"number": "p0 recall 97.22% / precision 64.23% 的 95% CI",
         "status": "重复测量，区间不能当独立样本区间",
         "recomputed": status["p0"]["pooled"].get("context_recall"),
         "reason": "3 轮跑同一批 6 题；quality_gate 取均值，本报告 pooled CP 把 3 轮当独立样本，均只能作量级参考。"},
        {"number": "claim 数字不在任何上下文 9.4%（fresh cross30）",
         "status": "规则上界",
         "recomputed": {"numbers_checked": 213, "not_in_any": 20, "rate": 0.0939},
         "reason": "claim_verify 深判显示规则缺口里约 37.5% 是引用/时间元数据、22.9% 是规则假阴性；真 unsupported 方向约 9.6%（且深判基于多轮包）。"},
        {"number": "faithfulness 89.21%（mixed100）/ 88.63%（cross30 fresh）",
         "status": "全部断言口径；含元陈述",
         "recomputed": {"cross30_fresh_content": 0.8788, "mixed100_content": 0.9017},
         "reason": "剔除“以上信息来自…”类元陈述后为 87.88% / 90.17%；两口径必须一起报；且单裁判、非人工金标。"},
        {"number": "CER 36.5%（早期 3 场）",
         "status": "已作废",
         "recomputed": {"cer_no_punct_8m": 0.2491},
         "reason": "LEDGER 轴1 明确作废：前三场恰好最难；引用一律用 8 场 24.9%。"},
        {"number": "轴2 条目级无依据“≈20.5%”",
         "status": "分层加权点估计，没有 n/CI",
         "recomputed": {"lenient_label_distribution_sampled": {"unsupported": 25, "partial": 37,
                                                               "supported": 25, "derivable": 1}},
         "reason": "adjudication_packet_v2_lenient.json 是分层抽样 88 条；20.5% 是加权后的语料估计，LEDGER 未给分母/CI，"
                   "不能与条目级严格档 55%/60.2% 相减。"},
        {"number": "AMC-AID positive F1 self_check=1.0",
         "status": "夹具自检，不是质量指标",
         "recomputed": None,
         "reason": "metrics.json amc_aid.dev.self_check 是仪器自检；产品侧输出映射未接（LEDGER W6），当前没有产品读数。"},
        {"number": "轨迹 pass^3（12 题 12/12）",
         "status": "旧小集读数，不能套到 27 题",
         "recomputed": {"pass_k_evaluated_cases": 0, "single_trial_reference": "24/27"},
         "reason": "freshbase27 每个问题只有 1 次 trial；pass^3 需要同配置 3 份产物。2026-10-21 深研的 12×3 是另一批题。"},
        {"number": "quality_gate PASS 的“当前阈值”",
         "status": "报告与代码不一致",
         "recomputed": {"code_floors": FLOORS},
         "reason": "2026-10-22_rag_quality_gate.md 与 eval100_scale.md 的阈值数字与 quality_gate.py 的 FLOORS 不一致；"
                   "且 baseline 等于当前读数，PASS 表示 0-delta。"},
    ]


def build_representative_samples(rag: dict, faith: dict, citation: dict, trajectory: dict) -> list:
    """给文档用的 18 条中文原文样例（自动从归因结果挑关键条目 + 轨迹/盲评固定样例）。"""
    samples = []
    picked_keys = {
        ("cross30_fresh", "ct06", 1), ("cross30_fresh", "cm08", 1), ("cross30_fresh", "ct02", 2),
        ("cross30_fresh", "cs06", 1), ("cross30_fresh", "cs09", 1), ("new_meeting", "n01", 1),
        ("new_meeting", "n03", 1), ("extended68", "q93", 2), ("mixed100", "m042", 1),
    }
    for row in rag["missing_facts"]:
        if (row["packet"], row["case_id"], row["fact_index"]) in picked_keys:
            samples.append({
                "id": f"S{len(samples) + 1:02d}", "category": "RAG context_recall 缺口",
                "case_id": row["case_id"], "packet": row["packet"],
                "text": row["fact"],
                "context": (f"judge found=false；字面上下文覆盖 {row['context_coverage_proxy']:.2f}、"
                            f"答案覆盖 {row['answer_coverage_proxy']:.2f}；归因：{row['classification']}"),
                "note": row["note"],
            })
    for item in faith["samples"][:6]:
        samples.append({
            "id": f"S{len(samples) + 1:02d}", "category": f"faithfulness 不支持断言（{item['type']}）",
            "case_id": item["case_id"], "packet": item["packet"], "text": item["text"],
            "context": "judge supported=false（内容级）", "note": "单裁判判定；分类为规则粗分。",
        })
    for miss in faith["known_judge_misses"][:3]:
        samples.append({
            "id": f"S{len(samples) + 1:02d}", "category": f"faithfulness 已知漏判（{miss['type']}）",
            "case_id": miss["case_id"], "packet": "mixed100", "text": miss["claim_text"],
            "context": f"judge supported={miss['judge_supported']}", "note": miss["note"],
        })
    # 引用归因样例
    for item in citation["number_samples"][:3]:
        samples.append({
            "id": f"S{len(samples) + 1:02d}", "category": "数字 attribution（不在任何上下文）",
            "case_id": item["case_id"], "packet": item["packet"],
            "text": f"{item['token']} | {item['sentence']}",
            "context": f"refs={item['refs']}", "note": "规则审计上界；可能是引用/时间元数据或派生数字。",
        })
    for item in citation["quote_samples"][:2]:
        samples.append({
            "id": f"S{len(samples) + 1:02d}", "category": "引语 attribution（引语不在任何上下文）",
            "case_id": item["case_id"], "packet": item["packet"], "text": item["quote"],
            "context": f"refs={item.get('refs')}", "note": "LCS≥10 近似匹配；模板/关键词引号已过滤。",
        })
    # 轨迹样例
    for case_id in ("t26", "t13", "t27"):
        failure = next((x for x in trajectory.get("first_tool_failures", []) if x["case_id"] == case_id), None)
        if not failure:
            continue
        samples.append({
            "id": f"S{len(samples) + 1:02d}", "category": "工具轨迹（first_tool / over-call）",
            "case_id": case_id, "packet": "trajectory_freshbase27",
            "text": failure["question"],
            "context": (f"first_tool={failure['first_tool']}，required={failure['required']}，"
                        f"used={failure['used']}，strict={failure['strict']}，acceptable={failure['acceptable']}"),
            "note": "轨迹金标是代理起草启发式（human_checked=false）。",
        })
    # 去重保持前 18 条
    return samples[:18]


def build_verification(packets: dict, citation: dict, trajectory: dict, mixed_layers: dict) -> dict:
    verify = {}
    fresh = citation.get("fresh_cross30", {}).get("matches_existing_artifact") or {}
    verify["claim_audit_cross30_fresh"] = {
        "match": fresh.get("match"),
        "recomputed": fresh.get("recomputed"),
        "artifact": fresh.get("artifact_values"),
        "note": "数字 attribution 规则与 evaluation/analysis/claim_citation_audit.py 逐项一致（numbers_checked / cited / any）。",
    }
    report_claim = load_json(RESULTS / "claim_audit_cross30_fresh.json")[0] or {}
    claim_rows = report_claim.get("rows") or []
    if claim_rows:
        verify["claim_audit_artifact_internal_consistency"] = {
            "numbers_checked": report_claim.get("numbers_checked"),
            "sum_case_numbers": sum(int(r.get("numbers_checked") or 0) for r in claim_rows),
            "unsupported_cited": report_claim.get("unsupported_cited_numbers"),
            "sum_case_cited": sum(len(r.get("unsupported_cited") or []) for r in claim_rows),
            "unsupported_any": report_claim.get("unsupported_any_numbers"),
            "sum_case_any": sum(len(r.get("unsupported_any") or []) for r in claim_rows),
            "consistent": (
                int(report_claim.get("numbers_checked") or 0)
                == sum(int(r.get("numbers_checked") or 0) for r in claim_rows)
                and int(report_claim.get("unsupported_cited_numbers") or 0)
                == sum(len(r.get("unsupported_cited") or []) for r in claim_rows)
                and int(report_claim.get("unsupported_any_numbers") or 0)
                == sum(len(r.get("unsupported_any") or []) for r in claim_rows)),
        }
    # 轨迹聚合复算
    tr = load_json(RESULTS / "trajectory_freshbase27_report.json")[0] or {}
    if tr:
        per_case = tr.get("per_case") or []
        strict = sum(1 for c in per_case if (c.get("first_trial") or {}).get("strict") is True)
        acceptable = sum(1 for c in per_case if (c.get("first_trial") or {}).get("acceptable") is True)
        f1s = [c["first_trial"]["f1"] for c in per_case if (c.get("first_trial") or {}).get("f1") is not None]
        verify["trajectory_aggregates"] = {
            "strict_recomputed": strict, "strict_reported": (tr.get("first_tool") or {}).get("strict", {}).get("successes"),
            "acceptable_recomputed": acceptable,
            "acceptable_reported": (tr.get("first_tool") or {}).get("acceptable", {}).get("successes"),
            "macro_f1_recomputed": round(sum(f1s) / len(f1s), 6) if f1s else None,
            "macro_f1_reported": (tr.get("tool_set") or {}).get("macro", {}).get("f1"),
        }
    verify["mixed100_layers"] = {"recomputed_from_scope_fixed_packet": mixed_layers}
    # p0 口径说明
    p0 = {}
    for run in ("p0_r1", "p0_r2", "p0_r3"):
        packet = packets.get(run)
        if packet:
            p0[run] = {m: (metric_block(packet_summary(packet), m) or {}).get("rate") for m in CORE_METRICS}
    verify["p0_runs"] = p0
    return verify


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def build_report(packets: dict, baseline: dict | None) -> dict:
    status = build_status_board(packets, baseline)
    gate = build_quality_gate(packets, baseline)
    rag = build_rag_attribution(packets)
    faith = build_faithfulness(packets)
    citation = build_citation_attribution(packets)
    trajectory = build_trajectory()
    mixed_layers = build_mixed100_layers(packets)
    issues = build_protocol_issues(packets, mixed_layers)
    disputed = build_disputed_numbers(packets, mixed_layers, status)
    samples = build_representative_samples(rag, faith, citation, trajectory)
    verification = build_verification(packets, citation, trajectory, mixed_layers)
    meta = {
        "script": "evaluation/analysis/quality_gap_report.py",
        "zero_model_calls": True,
        "stdlib_only": True,
        "generated_from": [f"evaluation/results/{name}" for name in PACKETS.values()],
        "extra_inputs": [SRC[k] for k in (
            "baseline", "claim_audit_fresh", "claim_verify", "claim_audit_multiturn",
            "trajectory_report", "tool_utility", "gold_eval100", "gold_cross30", "gold_new_meeting",
            "meeting_v2", "meeting_v2r", "citation_packet", "completeness_v2", "completeness_v3",
            "cpcer_main", "cpcer_c3600")],
        "not_recomputable": ("轴1–4 绝对数为历史基板读数（来源固定在 LEDGER 与对应产物），本脚本只登记；"
                             "轨迹实验臂读数来自报告（runner 已删除）；盲评为历史 AI 盲评结果。"),
        "method_notes": [
            "RAG 四指标/轨迹/claim 数字来自 packet summary 或从 packet 逐案复算，复算处标 recomputed_from_packet=true。",
            "缺失事实归因 = 字面 bigram 覆盖代理（0–1）+ 人工抽查覆盖表；不是语义判定。",
            "数字 attribution 复用 claim_citation_audit.py 的规则（重新实现，不 import），已与既有产物互验一致。",
            "引语 attribution 为 LCS≥10 近似，过滤会议名/检索关键词/问句模板；仍会有假阳性。",
            "所有 CI 为 Clopper-Pearson 95%（与 evaluation/lib/stats.py 同算法）；p0 的 CI 按 3 轮合并计数算，重复测量，仅量级参考。",
        ],
    }
    return {
        "meta": meta,
        "status_board": status,
        "quality_gate": gate,
        "rag_attribution": rag,
        "faithfulness_unsupported": faith,
        "citation_attribution": citation,
        "trajectory": trajectory,
        "protocol_issues": issues,
        "disputed_numbers": disputed,
        "representative_samples": samples,
        "verification": verification,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="MeetingBot 质量指标全景与缺口归因（零模型调用）")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    packets = {}
    missing = []
    for name, filename in PACKETS.items():
        path = RESULTS / filename
        data, error = load_json(path)
        if error:
            missing.append({"packet": name, "path": rel(path), "error": error})
        packets[name] = data
    baseline, baseline_error = load_json(BASELINE_PATH)
    if baseline_error:
        missing.append({"packet": "quality_gate_baseline", "path": rel(BASELINE_PATH), "error": baseline_error})

    report = build_report(packets, baseline)
    report["meta"]["missing_inputs"] = missing
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")

    if not args.quiet:
        status = report["status_board"]
        print(f"written: {args.output}  ({args.output.stat().st_size} bytes)")
        print("zero_model_calls:", report["meta"]["zero_model_calls"], "| missing inputs:", len(missing))
        print(f"quality gate (recomputed): {report['quality_gate']['verdict_recomputed']} "
              f"(failed rows: {len(report['quality_gate']['failed_rows'])})")
        for scope in ("human27", "extended68", "cross30_fresh", "new_meeting", "mixed100"):
            stats = report["rag_attribution"]["by_packet"].get(scope)
            if stats:
                print(f"  {scope}: facts {stats['facts_total']} missing {stats['facts_missing']}")
        print("status rows:", len(status["axes"]), "| protocol issues:", len(report["protocol_issues"]),
              "| samples:", len(report["representative_samples"]))
        for metric in ("context_precision", "context_recall", "faithfulness", "answer_relevancy"):
            for row in status["axes"]:
                if row["metric"] == metric and row["scope"].startswith("mixed100"):
                    print(f"  mixed100 {metric}: {row['value']} n={row['n']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
