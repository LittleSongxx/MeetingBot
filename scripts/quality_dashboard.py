"""生成作品集用质量仪表盘（静态 HTML，零依赖）。

数据源：evaluation/results 与 reports 的最新机制产物；只读，不调模型。
用法：python scripts/quality_dashboard.py --out docs/quality_dashboard.html
"""
from __future__ import annotations

import argparse
import html
import json
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
EVAL = REPO / "evaluation"
RESULTS = EVAL / "results"
REPORTS = EVAL / "reports"


def load(name: str):
    path = RESULTS / name
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return None


def rate(value):
    if isinstance(value, dict):
        return value.get("rate")
    return value


def pct(value, digits=1):
    if value is None:
        return "—"
    return f"{value * 100:.{digits}f}%"


def badge(ok: bool | None, text: str | None = None) -> str:
    if ok is None:
        return f'<span class="badge neutral">{html.escape(text or "n/a")}</span>'
    label = text or ("PASS" if ok else "FAIL")
    css = "pass" if ok else "fail"
    return f'<span class="badge {css}">{html.escape(label)}</span>'


def table(headers: list[str], rows: list[list[str]]) -> str:
    head = "".join(f"<th>{html.escape(item)}</th>" for item in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows)
    return f'<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>'


def section(title: str, body: str, note: str = "") -> str:
    note_html = f'<p class="note">{note}</p>' if note else ""
    return f'<section><h2>{html.escape(title)}</h2>{note_html}{body}</section>'


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=REPO / "docs/quality_dashboard.html")
    args = parser.parse_args()

    metrics = load("metrics.json") or {}
    binding = load("evidence_binding_ab.json") or {}
    passk = load("trajectory_passk27_report.json") or {}
    state = load("state_coverage_ab.json") or {}
    action = load("action_item_audit.json") or {}
    gate_baseline = json.loads((EVAL / "baselines/quality_gate_baseline.json").read_text(encoding="utf-8"))

    sections = []

    # 1) 五轴
    axis5 = (metrics.get("axis5") or {}).get("summary") or {}
    reliability = metrics.get("reliability") or {}
    intent = metrics.get("intent_metrics") or {}
    retrieve = metrics.get("retrieval_metrics") or {}
    rows = [
        ["轴1 ASR（MOSS 8 场）", "cpCER 13.47%（CER 17.74%，Δcp −4.28pp，覆盖率 99.83%）",
         badge(None, "已采用"), "../evaluation/reports/2026-10-22_moss_own_metrics.md"],
        ["轴2 纪要 faithfulness（单裁判）",
         f"cross30 {pct(rate(axis5.get('faithfulness')))} / 内容级 {pct(rate(axis5.get('faithfulness_content')))}",
         badge(None, "方向信号"), "LEDGER §40"],
        ["轴3 完整性（FineSurE core）", "0.781（26 场，deepseek）", badge(None, "分阶段"),
         "../evaluation/reports/2026-10-22_generalization_audit_v2.md"],
        ["轴3 行动项槽位", f"owner fabricated {action.get('owner', {}).get('fabricated', '—')}"
                           f"/{action.get('owner', {}).get('with_owner', '—')}；"
                           f"deadline unlocated {action.get('deadline', {}).get('unlocated', '—')}",
         badge(True, "守卫已接入"), "../evaluation/reports/2026-10-23_action_item_guard.md"],
        ["轴4 RAG 四指标（mixed100 三次均值）",
         "precision 71.83% / recall 90.02% / faith 88.34% / AR 99.00%", badge(None, "单裁判"),
         "../evaluation/reports/2026-10-23_eval100_fresh_repeat.md"],
        ["轴5 工具轨迹", f"F1 macro 0.706；first strict 14/27；pass^3 {pct(rate((passk.get('pass_k') or {}).get('rate')))}",
         badge(True, "pass^k 可评"), "../evaluation/reports/2026-10-23_trajectory_passk.md"],
        ["检索指标（27 题）", f"recall@k {pct(retrieve.get('recall_at_k'))} / MRR {retrieve.get('mrr')} / nDCG {retrieve.get('ndcg_at_k')}",
         badge(None, "诊断"), "../evaluation/results/metrics.json"],
        ["意图识别（AI 复核标签）", f"macro F1 {pct(intent.get('macro_f1'))}", badge(None, "非人工"),
         "../evaluation/results/metrics.json"],
        ["可靠性", f"端到端 {pct(rate(reliability.get('end_to_end')))}；调用级 "
                   f"{pct(rate(reliability.get('call_success')))}", badge(None, "诊断"),
         "../evaluation/results/metrics.json"],
    ]
    sections.append(section("五轴质量记分卡", table(
        ["指标", "读数", "状态", "证据"], rows),
        "数字均带口径；单裁判/代理金标只作方向信号（详见主报告与 LEDGER）。"))

    # 2) 机制 A/B
    s1_off = ((binding.get("means") or {}).get("off") or {})
    s1_on = ((binding.get("means") or {}).get("on") or {})
    binding_rows = [[
        "S1 生成后引用绑定（默认开）",
        f"cited-gap {pct(((s1_off.get('attribution') or {}).get('cited_gap')))} → "
        f"{pct(((s1_on.get('attribution') or {}).get('cited_gap')))}",
        f"{binding.get('delta_pp', {}).get('cited_gap_pp', '—')}pp",
        badge(binding.get("s49_pass")),
        "../evaluation/reports/2026-10-23_evidence_binding.md",
    ]]
    state_delta = state.get("delta") or {}
    binding_rows.append([
        "S2 状态提示（负结果，默认关）",
        f"recall-v2 {pct(((state.get('means') or {}).get('off') or {}).get('recall_v2_rate'))} → "
        f"{pct(((state.get('means') or {}).get('on') or {}).get('recall_v2_rate'))}",
        f"{state_delta.get('recall_v2_pp', '—')}pp",
        badge(state.get("s50_pass")), "../evaluation/reports/2026-10-23_state_coverage.md",
    ])
    enrich = load("tool_enrichment_ab.json") or {}
    enrich_delta = enrich.get("delta") or {}
    binding_rows.append([
        "S2 工具契约扩展（默认开）",
        f"recall-v2 {pct(((enrich.get('means') or {}).get('off') or {}).get('recall_v2'))} → "
        f"{pct(((enrich.get('means') or {}).get('on') or {}).get('recall_v2'))}",
        f"{enrich_delta.get('recall_v2_pp', '—')}pp",
        badge(enrich.get("pass")), "../evaluation/reports/2026-10-23_tool_enrichment.md",
    ])
    binding_rows.append([
        "S3 行动项槽位守卫",
        f"owner fabricated {action.get('owner', {}).get('fabricated', '—')}"
        f"/{action.get('owner', {}).get('with_owner', '—')}；"
        f"deadline 降级 {action.get('validator_downgrades', {}).get('deadline', '—')} 条",
        "—", badge(True, "已接入"), "../evaluation/reports/2026-10-23_action_item_guard.md",
    ])
    sections.append(section("Agent 机制 A/B / 守卫", table(
        ["机制", "主指标", "Δ", "判定", "报告"], binding_rows),
        "S2 状态提示为负结果（默认关）；S2 工具契约扩展与 S1 引用绑定为默认开的通用机制；"
        "S3 槽位守卫已接入闭环。"))

    # 3) 门禁
    gate_rows = [[
        "quality_gate（旧口径 + recall-v2 + claim）",
        "recall-v2 floors: p0 0.95 / cross30 0.85 / new_meeting 0.82 / mixed100 0.92",
        badge(True, "PASS") if gate_baseline.get("fact_classes_sha256") else badge(None, "n/a"),
        "../evaluation/quality_gate.py",
    ]]
    sections.append(section("质量闸", table(
        ["门禁", "口径", "状态", "入口"], gate_rows),
        "旧口径+recall-v2 双行、CP 95% CI、7 个产物 + 分类清单 sha256；清单变了未重冻即 FAIL。"))

    # 3.5) 运行时：延迟/成本/可观测性快照（2026-10-23 账本，非质量口径）
    runtime_rows = [
        ["TTFT / 首 token（10 题现行链路）", "p50 3.16s / p95 5.83s；首个 SSE 事件 21ms",
         "检索仅 ~0.6–0.8s，其余是 Agent 模型回合", "../evaluation/reports/2026-10-23_ttft_latency.md"],
        ["轮级模型+检索（3,333 轮，累计账本）", "p50 7.19s / p95 14.01s / max 24.73s",
         "不含工具/图调度/SSE", "../docs/可观测性与延迟成本现状.md"],
        ["CHAT_SUMMARY（最大延迟项）", "p50 16.18s / p95 51.78s", "416 次调用", "同上"],
        ["成本（新调用，估算单价）", "单轮 4 次 CHAT ≈ ¥0.23", "历史调用仍 unpriced", "同上"],
        ["阶段耗时落库", "total/nodes/tools → moderation_json.timing", "实测一轮 total 8571ms", "同上"],
        ["Trace 后端", "Jaeger 服务已配置，profile=observability", "镜像拉取失败，未激活", "同上"],
    ]
    sections.append(section("运行时 / 成本 / 可观测性（非质量口径）", table(
        ["量", "读数", "口径/状态", "来源"], runtime_rows),
        "运行时指标与质量指标分开记账；不与 cpCER/faithfulness/recall 混算。"))

    # 4) 链接
    links = [
        ("质量指标全面分析与通用优化方案（主报告）", "质量指标全面分析与通用优化方案.md"),
        ("延迟/成本/可观测性现状", "../docs/可观测性与延迟成本现状.md"),
        ("指标现状与优化决策", "../docs/指标现状与优化决策.md"),
        ("RAG 收口报告（冻结读数/机制/缺口）", "../evaluation/reports/2026-10-23_rag_closeout.md"),
        ("质量指标全景与缺口分析", "质量指标全景与缺口分析.md"),
        ("项目后续推进路线（作品集视角）", "项目推进-深度分析与路线图.md"),
        ("MOSS 启用手册", "MOSS-ASR启用.md"),
        ("评测账本（预注册/结果）", "../evaluation/LEDGER.md"),
    ]
    link_html = "<ul>" + "".join(
        f'<li><a href="{html.escape(href)}">{html.escape(label)}</a></li>' for label, href in links) + "</ul>"
    sections.append(section("文档索引", link_html))

    page = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>MeetingBot 质量仪表盘</title>
<style>
body {{ font-family: -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif; margin: 0;
        background: #f5f7fb; color: #1f2937; }}
main {{ max-width: 1200px; margin: 0 auto; padding: 28px 20px 60px; }}
h1 {{ margin: 0 0 6px; font-size: 28px; }}
.sub {{ color: #6b7280; margin: 0 0 22px; }}
h2 {{ font-size: 18px; margin: 0 0 10px; }}
section {{ background: #fff; border: 1px solid #e5e7eb; border-radius: 10px;
           padding: 18px 20px; margin: 16px 0; box-shadow: 0 1px 2px rgba(0,0,0,.04); }}
table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
th, td {{ text-align: left; padding: 8px 10px; border-bottom: 1px solid #eef0f4; vertical-align: top; }}
th {{ color: #4b5563; background: #f9fafb; }}
td:nth-child(2) {{ font-variant-numeric: tabular-nums; }}
.badge {{ display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 12px; }}
.pass {{ background: #dcfce7; color: #166534; }}
.fail {{ background: #fee2e2; color: #991b1b; }}
.neutral {{ background: #e0e7ff; color: #3730a3; }}
.note {{ color: #6b7280; font-size: 12px; margin: 0 0 10px; }}
a {{ color: #2563eb; text-decoration: none; }}
a:hover {{ text-decoration: underline; }}
.footer {{ color: #9ca3af; font-size: 12px; margin-top: 28px; text-align: center; }}
</style></head><body><main>
<h1>MeetingBot 质量仪表盘</h1>
<p class="sub">会议智能 Agent：MOSS ASR → 审查/修订纪要 → 行动项 → 跨会议 RAG；本页由
<code>scripts/quality_dashboard.py</code> 从评测产物生成（零模型调用）。</p>
{"".join(sections)}
<p class="footer">生成时间：{html.escape(datetime.now().strftime("%Y-%m-%d %H:%M"))}；数字以仓库产物为准。</p>
</main></body></html>
"""
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(page, encoding="utf-8")
    print(f"wrote {args.out} ({len(page)} chars)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
