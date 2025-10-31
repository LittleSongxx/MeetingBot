"""AI 裁决驱动：把盲态裁决包交给一个模型逐条标注，产出可复算的标签与一致性统计。

## 它替代的是什么，不替代的是什么

`adjudication.py` 的盲态包本来是给人工标注的。这个模块用模型把它跑完，于是
"审查 precision 未定义"这个状态**第一次变成可报的数**——但要看清它是什么：

* **它替代了"人工读 100 条"这件体力活**，不替代"独立参照物"这一属性。
* 本机可用的模型与产品主模型**同族**（DashScope 额度已耗尽、国际站 key 无效，
  见 2026-09-25 探测记录），因此这里测出来的不是"AI 与产品的跨族一致性"，
  而是**同族模型在两个不同提示下的可复现性**。这比随机高不了多少，
  也不能当作人类有效性证据。产出里 `adjudicator_disclosure` 会把这件事写在数据里，
  而不是只写在文档里。

## 真正有价值的产出

不是那个 κ，而是**分歧清单**：审查看得出问题、独立裁决看不出；或反过来。
"标注 100 条"由此变成"读 20 条分歧"。清单与两个方向的错误率一起落盘。

## 冻结标准被写进提示词

标签用 `adjudication.LABELS`（supported / derivable / partial / unsupported），
其中 `derivable` 必须**具名许可操作**（D1–D5，来自 `JUDGMENT_STANDARD.md`）——
这是该标准最关键的一步：没有它，"我推导出来的"无法判定，`derivable` 会退化成自由心证。

## 运行方式

需要模型凭据，因此**必须显式传 `--authorized-llm-calls`**；按 id 顺序逐条标注，
每标完一条就落盘，中断后直接重跑（已标的条目跳过，不会重复计费）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

if __package__ in (None, ""):  # allow direct script execution
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from judges import packets as adjudication  # type: ignore
    from lib import model_client  # type: ignore
else:
    from judges import packets as adjudication
    from lib import model_client

RESULTS_DIR = Path(__file__).resolve().parent / "results"

# 裁决提示词。**不能用 ASCII 双引号包中文**（本项目踩过三次），因此这里的
# 引号一律用「」。
SYSTEM_PROMPT = """你是会议纪要的独立裁决者。你看到的是一条纪要内容，以及它可能被写出来的那段转写窗口。
你的任务：判断这条内容的**每一条原子断言**能否由该窗口蕴含，并按下面的标准给出唯一标签。

标签（四选一）：
- supported：核心断言都能在转写窗口里找到依据。允许同义改写、语态变化、压缩、上下位替换。
- derivable：转写没有直接陈述，但**可由已陈述事实正确推出**。取这个标签时你必须同时给出
  所用的操作名（D1 改写 / D2 汇编合取 / D3 计数算术 / D4 时间单位归一 / D5 指代消解）。
  写不出具名操作的一律不算 derivable。
- partial：核心有依据，但补入了转写里没有的具体细节（数字、日期、人名、范围、程度）。
- unsupported：核心断言在转写里找不到依据。

硬性规则：
1) 情态加强、把意见说成事实、范围扩大（「全部」「均」）→ partial 或 unsupported，不许给 supported。
2) 转写里没提到的世界常识，即使为真，也不算有依据。
3) 只依据给你的窗口判断，不要用你自己的领域知识补全。
4) 判断的单元是**断言**，不是字符串是否出现过。

只输出一个 JSON 对象，不要任何解释文字：
{"label": "...", "operation": "D1|D2|D3|D4|D5|null", "reason": "一句话中文理由",
 "evidence": "窗口里支撑它的原文片段（找不到就填空字符串）"}"""


LENIENT_SYSTEM = """你是会议纪要的独立裁决者。你看到的是一条纪要内容，以及它可能被写出来的那段转写窗口。

请判断：这条内容**在转写里有依据吗**？

判定标准（宽松档，与业界事实一致性评测口径一致）：
- 允许改写、同义替换、语态变化、压缩、上下位替换；
- 允许**正确推导**：由已陈述事实可合理推出（清点、归纳、相对时间解析、指代消解）；
- 允许读者能接受的常识性补充；
- 只有下面两种情况算没有依据：
  a) 转写里找不到任何支撑（凭空添加的实体、数字、日期、事件）；
  b) 与转写内容矛盾，或把有依据的事归给了错误的人。

只输出一个 JSON 对象：
{"grounded": true 或 false, "reason": "一句话中文理由"}"""


def _parse_label(raw: Mapping[str, Any]) -> tuple[str, str, str]:
    """取出标签、操作名、理由。标签不合法时**拒绝**而不是猜一个最近的。"""
    label = str(raw.get("label") or "").strip().lower()
    if label not in adjudication.LABELS:
        raise ValueError(f"非法标签：{label!r}")
    operation = raw.get("operation")
    operation = "" if operation in (None, "null", "None", "") else str(operation).strip()
    if label == "derivable" and operation not in {"D1", "D2", "D3", "D4", "D5"}:
        # 标准要求：写不出具名操作的"推导"不成立
        return "unsupported", "", (str(raw.get("reason") or "") + "（未给出具名操作，按标准降为 unsupported）")[:300]
    return label, operation, str(raw.get("reason") or "")[:300]


def adjudicate_packet(
    packet: dict[str, Any],
    *,
    output_path: Path,
    authorized_calls: int,
    provider: str = "default",
    label_field: str = "label",
    lenient: bool = False,
    progress: Callable[[str], None] = print,
) -> dict[str, Any]:
    """逐条标注包内条目。已标注的跳过；每标完一条落盘。

    `provider`：选哪个模型族（见 `model_client.PROVIDERS`）。
    **`label_field` 必须两个族各用一个**（默认 `label`，第二族用 `label_b`）——
    覆盖同一字段会把第一族的证据抹掉，跨族一致性就再也算不出来了。
    """
    url, model, key = model_client.endpoint(provider)
    items = packet["items"]
    pending = [item for item in items if not item.get(label_field)]
    if not pending:
        progress("没有待标注的条目")
        return {"labelled": 0, "skipped": len(items)}

    if authorized_calls < len(pending):
        progress(f"授权 {authorized_calls} 次 < 待标注 {len(pending)} 条：本次只标前 {authorized_calls} 条")
        pending = pending[:authorized_calls]

    packet.setdefault("adjudicator_disclosure", {
        "model_used": model,
        "provider": provider,
        "same_family_as_product_model": provider == "default",
        "why": (
            "provider=default 时与产品主模型同族，测的是同族复现性；"
            "provider=qwen 时是**不同族**的独立标注，两者必须分开报告。"
        ),
    })
    calls_made = 0
    for position, item in enumerate(pending, start=1):
        user = (
            f"【纪要内容】（字段：{item['field']}）\n{item['item_text']}\n\n"
            f"【转写窗口】\n{item['transcript_window']}\n\n"
            "请按标准输出 JSON。"
        )
        try:
            reply = model_client.call_json(
                url, model, key, LENIENT_SYSTEM if lenient else SYSTEM_PROMPT, user
            )
            if lenient:
                # 宽松档是二值：grounded / not_grounded，不做四分类也不要求具名操作
                grounded = reply["parsed"].get("grounded")
                if not isinstance(grounded, bool):
                    raise ValueError(f"grounded 必须是布尔值，收到 {grounded!r}")
                label, operation = ("supported" if grounded else "unsupported"), ""
                reason = str(reply["parsed"].get("reason") or "")[:300]
            else:
                label, operation, reason = _parse_label(reply["parsed"])
            item[label_field] = label
            item[f"{label_field}_note"] = reason
            item[f"{label_field}_operation"] = operation
            item.setdefault("_meta", {})["tokens"] = reply["usage"].get("total_tokens")
        except (model_client.ModelClientError, ValueError) as error:
            item.setdefault("_meta", {})[f"error_{label_field}"] = (
                f"{type(error).__name__}: {str(error)[:200]}"
            )
            # 失败不静默：记在条目上，输出里数得出来
        finally:
            calls_made += 1
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(json.dumps(packet, ensure_ascii=False, indent=2), encoding="utf-8")
        progress(f"  [{position}/{len(pending)}] {item['anonymous_id']} → "
                 f"{item.get(label_field) or '失败'}（累计调用 {calls_made}）")
        time.sleep(0.2)
    return {"provider": provider, "model": model, "label_field": label_field,
            "labelled": sum(1 for i in items if i.get(label_field)),
            "failed": sum(1 for i in items if (i.get("_meta") or {}).get(f"error_{label_field}")),
            "calls_made": calls_made}


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--packet", type=Path, required=True)
    parser.add_argument("--authorized-llm-calls", type=int, required=True,
                        help="必须显式给出；每条约 1 次调用，缺失即拒绝")
    parser.add_argument("--report", action="store_true", help="标注后立刻出一致性报告")
    parser.add_argument("--provider", default="default",
                        choices=sorted(model_client.PROVIDERS),
                        help="default=主模型族；qwen=第二个族（跨族独立标注）")
    parser.add_argument("--label-field", default="label",
                        help="标签写入哪个字段；第二个族必须换一个（如 label_b）")
    parser.add_argument("--lenient", action="store_true",
                        help="用宽松档（与业界事实一致性口径可比），输出二值 grounded")
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.authorized_llm_calls < 1:
        print("拒绝执行：--authorized-llm-calls 必须 ≥ 1", file=sys.stderr)
        return 2

    packet = json.loads(args.packet.read_text(encoding="utf-8"))
    summary = adjudicate_packet(packet, output_path=args.packet,
                               authorized_calls=args.authorized_llm_calls,
                               provider=args.provider, label_field=args.label_field,
                               lenient=args.lenient)
    print(json.dumps(summary, ensure_ascii=False))

    if args.report:
        result = adjudication.reconcile(packet)
        report_path = args.packet.with_name(args.packet.stem + "_report.json")
        report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"wrote {report_path}")
        print(json.dumps({k: v for k, v in result.items() if k != "discordant_items"},
                         ensure_ascii=False, indent=2)[:2000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
