"""会议级（整篇纪要）事实一致性：与业界"摘要幻觉率"同单位、同口径的读数。

## 为什么要有这一层

`citation_metrics.py` 判的是**条目**；而业界最常引用的数字（Vectara HHEM 排行榜）
判的是**整篇摘要**："这份摘要里有没有无依据的内容"。两者单位不同，直接比会被质疑。
因此这里按业界的单位再测一次：一份纪要被判为"有问题"，当且仅当它含至少一处无依据内容。
于是"有问题的纪要占比"就是可与排行榜同台对照的量。

口径取**宽松档**（允许改写、压缩、正确推导与常识补充）——这正是业界"事实一致性"的定义；
严格档（补入原文没有的具体细节即算）另有测量，两者都在报告里。

## 三臂各判一次

`baseline / passthrough / reflected` 三份产物在同一份转写下各判一次，于是除了"我们位于业界何处"，
还多出一个**与措辞、删除、覆盖都不同的独立读数**：审查是否降低了"有问题的纪要占比"。

## 配置在看数据之前写死（预注册）

此前这一层 0/26 全部超时，失败一律记作 `read operation timed out`，于是"6 万字符输入在
当前接口延迟下不可行"成了结论。2026-09-26 实测推翻了这个归因，慢来自两处、都不是"接口本身慢"：

| 观测 | 实测 |
|---|---|
| 宿主每次建连先白等 | **133.5 秒**（IPv6 不可达，回落到 IPv4 才成功）；容器内同一行 **0.0 秒** |
| 同一份输入、只切思考开关 | `thinking=OFF` **1.2 秒** / `thinking=ON` **317.3 秒**（qwen3.8-max） |

因此**主配置定为 `thinking=OFF`**，理由有两条，且都在看数据之前成立：

1. 对照对象（Vectara HHEM 排行榜）用的是**单趟判定器**，不是长链推理；
   要对齐它的单位与口径，单趟更接近被对照的那件仪器；
2. 317 秒/条 × 78 条（三臂）≈ **6.5 小时**，在本次会话窗口内跑不完，而 1.2 秒/条只需几分钟。

但**思考开关会改变判定**（同一份输入：OFF 判"无问题"、ON 判"有问题"），
所以它不是一个可以悄悄选掉的旋钮：另跑一个**固定种子、逐臂分层**的敏感性子样本
（`--sample`），用 `thinking=ON` 复判，**无论结果朝哪个方向都必须报**。

    python evaluation/metrics/meeting_level.py --build            # 零调用
    python evaluation/metrics/meeting_level.py --sample 12 --seed 20260926 \\
        --output results/meeting_level_sensitivity.json            # 零调用：抽敏感性样本
    python evaluation/metrics/meeting_level.py --judge --arm baseline \\
        --authorized-llm-calls 26 --provider qwen [--thinking on|off] [--packet PATH]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Iterable, Mapping

if __package__ in (None, ""):  # allow script direct execution
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from gate import arms as aa  # type: ignore
    from lib import corpora as rm, model_client, stats  # type: ignore
else:
    from gate import arms as aa
    from lib import corpora as rm, model_client, stats

RESULTS_DIR = Path(__file__).resolve().parent / "results"
DEFAULT_PACKET = RESULTS_DIR / "meeting_level_packet.json"
MAX_TRANSCRIPT_CHARS = 60000

SYSTEM = """你在检查**一份会议纪要是否含有原文不支持的内容**（事实一致性，宽松档）。

判定标准（与业界摘要事实一致性评测一致）：
- 允许改写、同义替换、压缩、上下位替换；
- 允许**正确推导**（由已陈述事实可合理推出：清点、归纳、相对时间解析、指代消解）；
- 允许读者能接受的常识性补充；
- 只有下面两种情况算"不支持"：
  a) 转写里找不到任何支撑（凭空出现的实体、数字、日期、事件、交付物）；
  b) 与转写内容矛盾，或把有依据的事归给了错误的人。

只输出 JSON：
{"has_unsupported": true 或 false,
 "count": 不支持的内容条数（没有就 0）,
 "examples": ["最多三条，每条一句话"]}"""


DEFAULT_MANIFEST = (Path(__file__).resolve().parents[1] / "benchmarks"
                    / "manifest_test_chunk6000.json")


def _fixture_titles(manifest: Path = DEFAULT_MANIFEST) -> dict[str, str]:
    """从**夹具清单**取每场的会议标题（脚手架设定的元数据）。

    为什么要给判定者标题：纪要是照着会议元数据写的，标题里的名称会进纪要
    （实测例：纪要写「本次乡村教育论坛（VCSum 31）」），而转写片段里当然没有它，
    不给标题就会被判成"凭空出现的编号"。标题的事实来源是清单，不是我们的转写，
    所以这里读清单，而不是在评测侧另写一份命名规则（那会漂移）。
    清单不存在时返回空表，提示词里仍有"标题来自元数据"的通用说明兜底。
    """
    if not manifest.exists():
        return {}
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except ValueError:
        return {}
    entries = payload.get("cases") if isinstance(payload, dict) else payload
    titles: dict[str, str] = {}
    for entry in entries or []:
        if isinstance(entry, Mapping) and entry.get("id"):
            titles[str(entry["id"])] = str(entry.get("title") or "")
    return titles


def build_packet(arm_dir: Path = aa.DEFAULT_ARM_DIR) -> dict[str, Any]:
    """三臂各一条/场：整篇纪要 + 转写。**零模型调用。**"""
    cases = rm.load_new_contract_cases()
    arm_outputs = aa.load_arm_outputs(arm_dir)
    titles = _fixture_titles()
    items: list[dict[str, Any]] = []
    for case in cases:
        transcript = case["transcript"][:MAX_TRANSCRIPT_CHARS]
        outputs = {
            "baseline": case["baseline_output"],
            "reflected": case.get("revised_output"),
            "passthrough": arm_outputs.get(case["case_id"]),
        }
        for arm, output in outputs.items():
            if not isinstance(output, Mapping):
                continue
            items.append({
                "anonymous_id": f"mtg_{case['case_id']}_{arm}",
                "case_id": case["case_id"],
                "arm": arm,
                "transcript": transcript,
                # 转写是否真的被截断，**分条记下来**，不再在提示词里无条件说"可能已截断"。
                # 实测踩过：那句无条件的免责声明本身会被判定者当成证据——
                # 「纪要称这是完整合并纪要，但转写明确提示可能已截断，无法支持『完整』」，
                # 于是**评测脚手架自己制造了一个"无依据"**。本语料最长 47,972 < 上限 60,000，
                # 实际一次都没有截断，所以正常路径下这条提示根本不该出现。
                "transcript_truncated": len(case["transcript"]) > MAX_TRANSCRIPT_CHARS,
                # 会议标题来自夹具清单（脚手架的元数据），一并给判定者；
                # 不给就会把标题里的名称判成"凭空出现的编号"（实测例见 _fixture_titles）。
                "meeting_title": (titles.get(case["case_id"])
                                  or case.get("meeting_title") or case.get("title") or ""),
                "minutes": json.dumps(output, ensure_ascii=False),
            })
    return {
        "packet_version": 1,
        "unit": "整篇纪要（与业界摘要幻觉率同单位）",
        "definition": "宽松档事实一致性：含至少一处无依据内容即计为「有问题」",
        "items": items,
    }


def build_subsample(packet: Mapping[str, Any], *, sample: int, seed: int) -> dict[str, Any]:
    """逐臂分层、固定种子的敏感性子样本。**零模型调用。**

    分层是必须的：三臂之间的问题率差就是要比的量，随机抽可能整臂抽空。
    抽样规则写在这里而不是留在会话记录里——一次性脚本算出的数字复现不了，
    这个项目已经为此撤回过三条结论。
    """
    import random as _random

    rng = _random.Random(seed)
    by_arm: dict[str, list[dict[str, Any]]] = {}
    for item in packet["items"]:
        by_arm.setdefault(item["arm"], []).append(item)
    picked: list[dict[str, Any]] = []
    for arm in sorted(by_arm):
        pool = sorted(by_arm[arm], key=lambda entry: entry["anonymous_id"])
        rng.shuffle(pool)
        picked.extend(pool[:sample])
    picked.sort(key=lambda entry: entry["anonymous_id"])
    return {
        "packet_version": packet.get("packet_version", 1),
        "purpose": "思考开关的敏感性（预注册）：同一批条目用 thinking=ON 复判，方向无论正负都报",
        "parent_packet": packet.get("source", "meeting_level_packet.json"),
        "sampling": {"per_arm": sample, "seed": seed,
                     "rule": "逐臂先按 anonymous_id 排序再以该种子打乱，取前 N 条"},
        "items": picked,
    }


def judge(packet: dict[str, Any], *, arm: str, output_path: Path, authorized_calls: int,
          provider: str = "qwen", thinking: bool = False,
          progress=print) -> dict[str, Any]:
    url, model, key = model_client.endpoint(provider)
    pending = [i for i in packet["items"] if i["arm"] == arm and not i.get("judgement")]
    if authorized_calls < len(pending):
        progress(f"授权 {authorized_calls} < 待判 {len(pending)}，本次只判前 {authorized_calls} 条")
        pending = pending[:authorized_calls]
    packet.setdefault("adjudicator_disclosure", {
        "model_used": model, "provider": provider,
        "not_gold": "模型判定，非人类金标；口径为宽松档（与业界事实一致性一致）",
        # 思考开关改变了判定（同一输入 OFF 判无问题、ON 判有问题），因此必须写进产物
        "thinking": thinking,
    })
    # 传输诊断落盘：这一层此前 0/26 全部超时（`read operation timed out`），
    # 归因写成「6 万字符输入在当前接口延迟下必然超时」。实测发现宿主每次连接先白等
    # 约 133 秒（IPv6 不可达），超时里有一半是它——所以必须把探针写进产物，
    # 否则「不可行」这个经济学结论可能只是本机网络的产物。
    if "transport" not in packet:
        try:
            packet["transport"] = model_client.probe_transport(url.split("/v1/")[0].split("//")[-1])
        except model_client.ModelClientError as error:
            packet["transport"] = {"error": str(error)[:200]}
    calls = 0
    for position, item in enumerate(pending, start=1):
        # 截断提示**只在真的截断时才给**：无条件给会被判定者当成"纪要不完整"的证据，
        # 于是评测脚手架自己制造"无依据"（实测例证见 build_packet 的注释）。
        if item.get("transcript_truncated"):
            header = (f"【会议转写】（已截断到前 {MAX_TRANSCRIPT_CHARS} 字符；"
                      "后文未提供，**不得据此判定后文不存在依据**）")
        else:
            header = "【会议转写】（完整原文）"
        title = item.get("meeting_title") or ""
        title_line = f"【会议标题】{title}\n" if title else ""
        user = (f"{header}\n{item['transcript']}\n\n"
                f"{title_line}"
                # 标题是会议元数据、不是从转写里推的，因此先声明它的来源，
                # 避免"纪要里出现标题里的编号/名称"被判成凭空捏造。
                f"（注意：会议标题来自会议元数据，纪要中出现标题所包含的名称或编号不算无依据。）\n\n"
                f"【纪要】\n{item['minutes']}\n\n请按标准输出 JSON。")
        try:
            # 单趟判定（thinking=OFF）是预注册的主配置；socket 上限必须高于总上限，
            # 否则 300 秒的总墙钟会被 180 秒的 socket 超时先打断（实测踩过：
            # 失败消息里只有 read timeout，看不出是配置打断的）。
            reply = model_client.call_json(url, model, key, SYSTEM, user,
                                           timeout=900, total_deadline=900,
                                           thinking=thinking)
            packet_item = reply["parsed"]
            if not isinstance(packet_item.get("has_unsupported"), bool):
                raise ValueError(f"has_unsupported 必须是布尔值：{packet_item.get('has_unsupported')!r}")
            item["judgement"] = packet_item
        except (model_client.ModelClientError, ValueError) as error:
            item["judgement_error"] = f"{type(error).__name__}: {str(error)[:200]}"
        calls += 1
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(packet, ensure_ascii=False, indent=2), encoding="utf-8")
        progress(f"  [{position}/{len(pending)}] {item['anonymous_id']} → "
                 f"{'失败' if item.get('judgement_error') else packet_item.get('has_unsupported')}")
        time.sleep(0.2)
    return {"provider": provider, "model": model, "arm": arm, "calls_made": calls}


def summarize(packet: Mapping[str, Any]) -> dict[str, Any]:
    per_arm: dict[str, dict[str, Any]] = {}
    for item in packet["items"]:
        judgement = item.get("judgement")
        if not isinstance(judgement, Mapping):
            continue
        bucket = per_arm.setdefault(item["arm"], {"judged": 0, "problem": 0, "counts": []})
        bucket["judged"] += 1
        if judgement.get("has_unsupported"):
            bucket["problem"] += 1
        bucket["counts"].append(int(judgement.get("count") or 0))
    out: dict[str, Any] = {}
    for arm, bucket in sorted(per_arm.items()):
        judged, problem = bucket["judged"], bucket["problem"]
        out[arm] = {
            "meetings_judged": judged,
            "meetings_with_unsupported": problem,
            "problem_rate": stats.proportion_report(problem, judged) if judged else None,
            "unsupported_items_per_meeting": (sum(bucket["counts"]) / judged) if judged else None,
        }
    return {
        "by_arm": out,
        "reading": (
            "`problem_rate` = 含至少一处无依据内容的纪要占比，**与业界摘要幻觉率同单位**"
            "（Vectara HHEM 排行榜即此定义）。`unsupported_items_per_meeting` 给严重度。"
            "⚠️ 同一份数据上，**条目级**比例会高于纪要级（一处即算）——报哪一个必须写清。"
        ),
    }


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--packet", type=Path, default=DEFAULT_PACKET)
    parser.add_argument("--build", action="store_true")
    parser.add_argument("--judge", action="store_true")
    parser.add_argument("--arm", default="baseline", choices=["baseline", "reflected", "passthrough"])
    parser.add_argument("--provider", default="qwen", choices=sorted(model_client.PROVIDERS))
    parser.add_argument("--authorized-llm-calls", type=int, default=None)
    parser.add_argument("--thinking", choices=["on", "off"], default="off",
                        help="主配置为 off（见模块 docstring 的预注册）；on 用于敏感性子样本")
    parser.add_argument("--sample", type=int, default=0, help="逐臂抽 N 条做子样本（零调用）")
    parser.add_argument("--seed", type=int, default=20260926)
    parser.add_argument("--output", type=Path, default=None,
                        help="--sample 的落盘位置（默认与 --packet 同址）")
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.build:
        packet = build_packet()
        args.packet.write_text(json.dumps(packet, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"wrote {args.packet}: {len(packet['items'])} 条（三臂合计）")
        return 0
    if args.sample:
        packet = json.loads(args.packet.read_text(encoding="utf-8"))
        subsample = build_subsample(packet, sample=args.sample, seed=args.seed)
        destination = args.output or args.packet.with_name(
            args.packet.stem.replace("packet", "sensitivity") + args.packet.suffix)
        destination.write_text(json.dumps(subsample, ensure_ascii=False, indent=2),
                               encoding="utf-8")
        print(f"wrote {destination}: {len(subsample['items'])} 条"
              f"（逐臂 {args.sample}，seed={args.seed}）")
        return 0
    if args.judge:
        if not args.authorized_llm_calls:
            print("拒绝执行：--judge 必须显式传 --authorized-llm-calls", file=sys.stderr)
            return 2
        packet = json.loads(args.packet.read_text(encoding="utf-8"))
        result = judge(packet, arm=args.arm, output_path=args.packet,
                       authorized_calls=args.authorized_llm_calls, provider=args.provider,
                       thinking=(args.thinking == "on"))
        packet["summary"] = summarize(packet)
        args.packet.write_text(json.dumps(packet, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False))
        print(json.dumps(packet["summary"], ensure_ascii=False, indent=2))
        return 0
    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
