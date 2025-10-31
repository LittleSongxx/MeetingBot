"""完整性（漏记）评测：关键点清单法。

## 为什么必须建这个

本项目所有其他量都是**"已写的有没有依据"**方向的。整个 NLI/蕴含方法族都做不到
"该写的有没有漏"——而遗漏很可能是压缩式纪要的主导失败模式（`GAP_REMEDIATION_RESEARCH.md`
第三节：这是整个方法族的盲区）。因此召回侧此前完全没有度量。

文献给了明确的可用配方（同前引）：

* **清单，不是打分。** FineSurE 实测：让模型自由打完整性分与人工的相关只有 0.295，
  给它一份关键点清单逐项判断能到 0.677。**自由打分不可采信，清单法可用。**
* **遗漏是最可靠标注的错误类型。** MESA/QMSum Mistake：遗漏在九类错误里
  Krippendorff α 最高（0.832），LLM 判断的平衡准确率 95.3%（幻觉类只有 77.6%）。
* **否定式提问抵消长度偏置。** 不问"覆盖了哪些"（长文天然得分高），
  而问"**列出未被覆盖的**关键点"。
* 宏平均与微平均都算并声明用哪个（AutoMin 的前车之鉴：会议级裁判与人工相关仅 0.13–0.17）。

## 三条纪律

1. **清单生成时看不到任何模型输出**——否则清单会被输出锚定，覆盖率高得没有意义。
2. **判定只依据转写**，不依据"这条写得好不好"。
3. **关键点是"理想纪要应包含什么"，不是"原文说过什么"**（照 TofuEval 附录 F.2 的措辞）。
   因此它测的是**内容选择**，与"有没有依据"是正交的两件事。

## 与三个臂的关系

同一份清单可以判多份输出，于是 `baseline / passthrough / reflected` 三臂第一次
可以在**召回**维度上比较——这是空反馈对照臂原本缺的那一半结局指标。

## 运行方式

需要模型凭据，因此必须显式传 `--authorized-llm-calls`；逐场逐臂标注、每步落盘，可续跑。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

if __package__ in (None, ""):  # allow direct script execution
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from lib import corpora, model_client, stats  # type: ignore
else:
    from lib import corpora, model_client, stats

RESULTS_DIR = Path(__file__).resolve().parent / "results"
# v3 起默认 18 条：原子化（一点一事实）后同等会议需要更多条目才能覆盖，
# 11 条是 v2 复合点时代的上限。
DEFAULT_KEYPOINTS = 18
# kind 的封闭集合：core=会议实质内容；housekeeping=开场/自我介绍/串场等事务性内容。
# 解析时缺省或非法值一律兜底为 core——标漏比标错类别安全（core 是主指标层）。
KEYPOINT_KINDS = ("core", "housekeeping")
# 每次调用的**总墙钟上限**（秒）。socket 超时挡不住"服务端持续滴流"的挂起，
# 实测一次判定挂了 12 分钟以上；这里给死上限，超时按失败记录并继续。
GENERATE_DEADLINE_SECONDS = 240
JUDGE_DEADLINE_SECONDS = 300
# 机械类调用关推理：清单生成是"从转写里抽取要点"，属于抽取而非判断题；
# 判定覆盖保留推理。实测依据：deepseek-flash 默认开思考，一次抽取曾经 10.29s → 关掉 0.57s。
GENERATE_THINKING = False
JUDGE_THINKING = True

GENERATE_SYSTEM = """你在为一场会议准备**关键点清单**，用来检查一份纪要有没有漏掉重要内容。

清单的标准是：**一份理想的、覆盖该议题的会议纪要应当包含什么**。
注意：不是"原文说过什么"，而是"读者需要知道什么才算这场会议被记录完整"。

要求：
1. **一点一事实**：每条只写一个可核对的事实（一个决定、一个负责人、一个期限、
   一个数字、一个结论、一条方法建议），不要把多个事实合并成一条——
   复合点会让"覆盖了一半"无法判定。
2. **每条都是读者理解这场会议不可缺少的内容**：决定、结论、负责人、期限、
   风险、关键数据、方法建议、核心观点。举例、铺垫、修辞、背景常识、
   重复强调不列——清单用来查"漏记"，不是查"压缩率"。
3. 每条一句话，只写内容要点，不要写措辞、格式或风格要求。
4. 覆盖会议的各个部分，不要只写开头。
5. 只依据给你的转写，不要加入你自己的领域知识或猜测。
6. 每条标注 kind：
   - "core"：会议实质内容（决定、负责人、期限、风险、观点、数据、方法建议）；
   - "housekeeping"：开场致辞、主持人串场、嘉宾自我介绍、会议规则说明等事务性内容。
   两类都要列，检查漏记时主要核对 core。

只输出 JSON：{"keypoints": [{"id": 1, "text": "...", "kind": "core"}, ...]}"""

JUDGE_SYSTEM = """你在检查一份会议纪要**漏掉了哪些关键内容**。

我会给你：会议转写、一份纪要、一份关键点清单。
你的任务：**列出纪要没有覆盖到的关键点**（即漏记）。

判定规则：
1. 只依据转写判断这个关键点在会议里是否真的被讨论/决定过，以及纪要有没有把它写出来。
2. 纪要用了不同措辞但意思到了，算覆盖；只写了标题式的一句话而要点缺失，算未覆盖。
3. 不要因为纪要写得漂亮就多算覆盖，也不要因为措辞冗长就多算覆盖。
4. 只输出 JSON，不要解释文字：

{"uncovered": [{"id": 3, "why": "一句话说明纪要里缺了什么"}],
 "covered_count": 8,
 "total_count": 11}"""


def _transcript_header(transcript: str, max_transcript_chars: int) -> str:
    """转写头部：截断声明**只在真的截断时**给（2026-09-26 修复）。

    此前无条件写着「可能被截断到 N 字符」，而 meeting_level 的同类伪影已实测：
    判定者会把这句免责声明当成"纪要不完整/内容未覆盖"的证据使用。本语料最长
    转写 47,972 字，从未真的截断——它是纯伪影。对齐 meeting_level 修法：
    条件式、且明确说明"不能据此断言后文没有相关内容"。
    """
    if len(transcript) > max_transcript_chars:
        return (f"【会议转写】（已截断到前 {max_transcript_chars} 字符；后续内容未提供，"
                "不能据此断言后文不存在相关内容）\n")
    return "【会议转写】\n"


def generate_keypoints(transcript: str, *, limit: int = DEFAULT_KEYPOINTS,
                       max_transcript_chars: int = 60000,
                       provider: str = "default") -> list[dict[str, Any]]:
    """按转写生成关键点清单。**调用方必须保证没有把模型输出喂进来。**"""
    url, model, key = model_client.endpoint(provider)
    clipped = transcript[:max_transcript_chars]
    header = _transcript_header(transcript, max_transcript_chars)
    user = (f"{header}\n{clipped}\n\n"
            f"请给出 {limit} 条关键点。")
    reply = model_client.call_json(url, model, key, GENERATE_SYSTEM, user,
                                   total_deadline=GENERATE_DEADLINE_SECONDS,
                                   thinking=GENERATE_THINKING)
    raw = reply["parsed"].get("keypoints")
    if not isinstance(raw, list) or not raw:
        raise model_client.ModelClientError("关键点清单为空或格式不对")
    keypoints: list[dict[str, Any]] = []
    for position, entry in enumerate(raw[:limit], start=1):
        if isinstance(entry, Mapping) and entry.get("text"):
            kind = str(entry.get("kind") or "core").strip().lower()
            if kind not in KEYPOINT_KINDS:
                kind = "core"
            keypoints.append({"id": position, "text": str(entry["text"])[:400], "kind": kind})
        elif isinstance(entry, str) and entry.strip():
            keypoints.append({"id": position, "text": entry.strip()[:400], "kind": "core"})
    if not keypoints:
        raise model_client.ModelClientError("关键点清单解析后为空")
    return keypoints


def judge_coverage(transcript: str, keypoints: Sequence[Mapping[str, Any]], output: Mapping[str, Any],
                   *, max_transcript_chars: int = 60000,
                   provider: str = "default") -> dict[str, Any]:
    """否定式提问：让裁判**列出未被覆盖的**关键点，返回逐条覆盖判定。"""
    url, model, key = model_client.endpoint(provider)
    listing = "\n".join(f"{k['id']}. {k['text']}" for k in keypoints)
    minutes_text = json.dumps(output, ensure_ascii=False)
    user = (
        f"{_transcript_header(transcript, max_transcript_chars)}\n{transcript[:max_transcript_chars]}\n\n"
        f"【纪要】\n{minutes_text}\n\n"
        f"【关键点清单】（共 {len(keypoints)} 条）\n{listing}\n\n"
        "请列出纪要**未覆盖**的关键点。"
    )
    reply = model_client.call_json(url, model, key, JUDGE_SYSTEM, user,
                                   total_deadline=JUDGE_DEADLINE_SECONDS,
                                   thinking=JUDGE_THINKING)
    parsed = reply["parsed"]
    uncovered_raw = parsed.get("uncovered")
    uncovered_ids: set[int] = set()
    reasons: dict[int, str] = {}
    if isinstance(uncovered_raw, list):
        for entry in uncovered_raw:
            if isinstance(entry, Mapping) and entry.get("id") is not None:
                try:
                    identifier = int(entry["id"])
                except (TypeError, ValueError):
                    continue
                uncovered_ids.add(identifier)
                reasons[identifier] = str(entry.get("why") or "")[:200]
            elif isinstance(entry, (int, str)):
                try:
                    uncovered_ids.add(int(entry))
                except (TypeError, ValueError):
                    continue
    valid_ids = {k["id"] for k in keypoints}
    uncovered_ids &= valid_ids
    per_keypoint = [
        {"id": k["id"], "covered": k["id"] not in uncovered_ids,
         "reason": reasons.get(k["id"], ""), "text": k["text"],
         "kind": str(k.get("kind") or "core")}
        for k in keypoints
    ]
    covered = sum(1 for entry in per_keypoint if entry["covered"])
    total = len(per_keypoint)
    return {
        "per_keypoint": per_keypoint,
        "covered_count": covered,
        "total_count": total,
        "coverage": stats.proportion_report(covered, total) if total else None,
        "tokens": reply["usage"].get("total_tokens"),
        "timings": reply.get("timings"),
        "judge_reported": {"covered_count": parsed.get("covered_count"),
                           "total_count": parsed.get("total_count")},
    }


def summarize_arms(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """逐臂汇总覆盖率：宏平均（逐场先算再平均）与微平均（合并计数）都报。

    两层口径并列：**全部关键点**与 **core_only**（剔除 kind=housekeeping）。
    纪要的职责就是压缩事务性内容（开场/自我介绍/串场），把这层也算进漏记会
    惩罚正确的压缩——核心层才是漏记主指标。两层都是绝对数，不报加权总分。
    """
    by_arm: dict[str, list[float]] = {}
    micro: dict[str, list[int]] = {}
    core_by_arm: dict[str, list[float]] = {}
    core_micro: dict[str, list[int]] = {}
    for record in records:
        for arm, block in (record.get("arms") or {}).items():
            coverage = (block or {}).get("coverage") or {}
            if coverage.get("rate") is None:
                continue
            by_arm.setdefault(arm, []).append(float(coverage["rate"]))
            micro.setdefault(arm, [0, 0])
            micro[arm][0] += int(coverage["successes"])
            micro[arm][1] += int(coverage["total"])
            per_point = (block or {}).get("per_keypoint") or []
            core_points = [p for p in per_point
                           if isinstance(p, Mapping) and str(p.get("kind") or "core") == "core"]
            if core_points:
                core_covered = sum(1 for p in core_points if p.get("covered"))
                core_by_arm.setdefault(arm, []).append(core_covered / len(core_points))
                core_micro.setdefault(arm, [0, 0])
                core_micro[arm][0] += core_covered
                core_micro[arm][1] += len(core_points)
    summary: dict[str, Any] = {}
    for arm, rates in sorted(by_arm.items()):
        pooled = stats.proportion_report(micro[arm][0], micro[arm][1]) if micro[arm][1] else None
        entry: dict[str, Any] = {
            "meetings": len(rates),
            "macro_mean_coverage": sum(rates) / len(rates),
            "macro_median_coverage": sorted(rates)[len(rates) // 2],
            "micro_pooled_coverage": pooled,
        }
        core_rates = core_by_arm.get(arm) or []
        if core_rates:
            core_pooled = (stats.proportion_report(core_micro[arm][0], core_micro[arm][1])
                           if core_micro.get(arm) and core_micro[arm][1] else None)
            entry["core_only"] = {
                "meetings": len(core_rates),
                "macro_mean_coverage": sum(core_rates) / len(core_rates),
                "macro_median_coverage": sorted(core_rates)[len(core_rates) // 2],
                "micro_pooled_coverage": core_pooled,
            }
        summary[arm] = entry
    return {
        "by_arm": summary,
        "averaging_note": (
            "宏平均 = 逐场覆盖率再平均（每场等权，推荐主口径）；微平均 = 合并所有关键点。"
            "两者都会报，因为会议长度差异大时它们会分开，只报一个等于替读者选了口径。"
        ),
        "layer_note": (
            "core_only = 剔除 kind=housekeeping（开场/自我介绍/串场）后的子层，"
            "是漏记主指标；两层并列呈现，不合成加权总分。"
        ),
    }


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output", type=Path,
                        default=RESULTS_DIR / "completeness_keypoints.json")
    parser.add_argument("--case-id", action="append", default=[])
    parser.add_argument("--keypoints", type=int, default=DEFAULT_KEYPOINTS)
    parser.add_argument("--arms", default="baseline,revised",
                        help="要判定的臂；可加 passthrough（需先跑空反馈臂）")
    parser.add_argument("--passthrough-dir", type=Path,
                        default=Path(__file__).resolve().parents[1] / "benchmarks" / "results" / "passthrough-arm-20260925")
    parser.add_argument("--authorized-llm-calls", type=int, required=True)
    parser.add_argument("--provider", default="default",
                        choices=sorted(model_client.PROVIDERS),
                        help="裁判模型族：default=与被测纪要同族（测复现性）；qwen=跨族独立判定")
    parser.add_argument("--run-names", default=None,
                        help="逗号分隔的运行目录名（benchmarks/results 下），替代钉住的 v2 run 对；"
                             "用于判定新跑的产品臂（如保默认重跑、机制臂）")
    parser.add_argument("--rejudge", type=Path, default=None,
                        help="复用既有产物的清单、只重判三臂（伪影修复后的再测量）；"
                             "值为既有产物路径，输出仍走 --output")
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.authorized_llm_calls < 1:
        print("拒绝执行：--authorized-llm-calls 必须 ≥ 1", file=sys.stderr)
        return 2

    # 语料入口钉在 lib/corpora.py（评测重构后从 run_metrics 拆出）；
    # 本文件顶部已按脚本/包两种模式完成导入，这里不再重复兜底。
    if args.run_names:
        cases = corpora.load_new_contract_cases(tuple(args.run_names.split(",")))
    else:
        cases = corpora.load_new_contract_cases()
    if args.case_id:
        cases = [c for c in cases if c["case_id"] in set(args.case_id)]
    prior_keypoints: dict[str, Any] = {}
    if args.rejudge is not None:
        prior = json.loads(args.rejudge.read_text(encoding="utf-8"))
        for rec in prior.get("records") or []:
            if rec.get("keypoints"):
                prior_keypoints[rec["case_id"]] = rec["keypoints"]
    state: dict[str, Any] = {"keypoint_count": args.keypoints, "arms": args.arms.split(","),
                             "records": []}
    if args.output.exists():
        try:
            state = json.loads(args.output.read_text(encoding="utf-8"))
        except ValueError:
            pass
    # **失败的场次不算完成**：只把"清单生成成功"的记录视为已完成，否则一次网络抖动
    # 会让那一场永久留在"已跳过"里（本轮踩到：两场生成超时后再跑全被跳过，白跑一轮）。
    done = {r["case_id"] for r in state["records"] if r.get("keypoints") and not r.get("error")}
    calls = 0

    for case in cases:
        case_id = case["case_id"]
        if case_id in done:
            print(f"跳过 {case_id}（已完成）")
            continue
        record: dict[str, Any] = {"case_id": case_id, "arms": {}}
        if case_id in prior_keypoints:
            # rejudge 模式：清单复用旧产物（同一份清单判三臂才可比），不花生成调用
            keypoints = prior_keypoints[case_id]
            record["keypoints_reused_from"] = str(args.rejudge)
        else:
            # 1) 生成清单：**只用转写**
            if calls + 1 > args.authorized_llm_calls:
                print(f"停止：授权已用尽（{calls}/{args.authorized_llm_calls}）", file=sys.stderr)
                break
            started = time.monotonic()
            try:
                keypoints = generate_keypoints(case["transcript"], limit=args.keypoints,
                                               provider=args.provider)
            except model_client.ModelClientError as error:
                # 生成失败**必须显式记录并继续**：它在 try 之外时，一次网络抖动会终止整轮
                record["keypoints"] = []
                record["error"] = f"keypoint_generation: {error}"[:300]
                state["records"].append(record)
                args.output.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                                       encoding="utf-8")
                print(f"  {case_id}：清单生成失败（{str(error)[:80]}）", file=sys.stderr)
                calls += 1
                continue
            record["generation_elapsed_ms"] = int((time.monotonic() - started) * 1000)
            calls += 1
        record["keypoints"] = keypoints
        outputs = {"baseline": case["baseline_output"], "revised": case["revised_output"]}
        passthrough_path = args.passthrough_dir / f"{case_id}.json"
        if "passthrough" in state["arms"] and passthrough_path.exists():
            arm_payload = json.loads(passthrough_path.read_text(encoding="utf-8"))
            if isinstance(arm_payload.get("output"), Mapping):
                outputs["passthrough"] = arm_payload["output"]
        # 2) 逐臂判定覆盖
        for arm, output in outputs.items():
            if arm not in state["arms"] or not isinstance(output, Mapping):
                continue
            if calls + 1 > args.authorized_llm_calls:
                break
            arm_started = time.monotonic()
            try:
                record["arms"][arm] = judge_coverage(case["transcript"], keypoints, output,
                                                     provider=args.provider)
                record["arms"][arm]["elapsed_ms"] = int((time.monotonic() - arm_started) * 1000)
            except model_client.ModelClientError as error:
                record["arms"][arm] = {"error": str(error)[:200]}
            calls += 1
            time.sleep(0.2)
        state["records"].append(record)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  {case_id}：清单 {len(keypoints)} 条，已判 {list(record['arms'])}（调用累计 {calls}）")

    state["summary"] = summarize_arms(state["records"])
    # 传输诊断落盘：此前「一次判定 169–188 秒」被读成供应商慢，实际是宿主 IPv6 不可达，
    # 每次调用先白等约 133 秒（实测同一行代码容器内 0.0 秒）。把连接探针写进产物，
    # 下一次「很慢」就能一眼看出是网络还是模型。
    try:
        _judge_url, _judge_model, _judge_key = model_client.endpoint(args.provider)
        # netloc 取法：去 scheme 后截到第一个 "/"——qwen 的 base 带 /compatible-mode 路径，
        # 用 "/v1/" 切会把路径误当主机名（DNS 探针会报出误导性的 gaierror）
        _judge_host = _judge_url.split("//")[-1].split("/")[0]
        state["transport"] = model_client.probe_transport(_judge_host)
    except model_client.ModelClientError as error:
        _judge_model = None
        state["transport"] = {"error": str(error)[:200]}
    state["disclosure"] = {
        "keypoint_source": "模型依据转写生成（人工写清单是文献里的做法，此处以 AI 替代）",
        "judge_provider": args.provider,
        "judge_model": _judge_model,
        "judge_family_note": (
            "provider=default 与被测纪要（deepseek-flash 生成）同族，测的是同族复现性；"
            "provider=qwen 是**跨族**独立判定。两族必须分开报告；单裁判读数只作方向信号，"
            "驱动产品决策需双族一致（评测宪法第 2 条）。"
        ),
        "not_a_gold_standard": "清单法给小的是**相对**比较的能力，不是人类完整性评分",
        "instrument_version": (
            "v3：原子化（一点一事实）+ core/housekeeping 分层；与 v2 复合点口径不可比，"
            "v2 绝对数保留旧标签不重述。"
        ),
    }
    args.output.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(state["summary"], ensure_ascii=False, indent=2))
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
