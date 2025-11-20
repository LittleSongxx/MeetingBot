"""成本换算：把一次调用的 token 用量换算成钱。

## 单位与精度

金额一律用**微元**（1e-6 元）的整数表示，不出现浮点。理由不是洁癖：成本要做
求和、排行与预算比较，浮点累加会出现"明细加起来不等于总额"的经典问题，而这里
的总额要用来做闸门判定（超了就拒），差一点点就意味着闸门可被绕过。

单价存在模型配置上（`ai_model_config.input_price_micro_per_1k` 等），**调用时**
取当前配置的单价写入 `ai_call_log.cost_micro`。因此改单价不会把历史调用的成本
按新价重估——历史成本是事实，不是派生值。

## 未定价 = 不知道，不是免费

单价为 0 时成本记 0，但 `priced=False` 会被一路带到观测页与报表：
**"未定价"与"花了 0 元"是两件事**，把前者显示成后者会让人以为白调了很多次。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# 每 1000 token 的微元数 → 每次调用先乘后除，避免先除造成的精度丢失
_TOKENS_PER_UNIT = 1000


@dataclass(frozen=True)
class Cost:
    """一次调用的成本与它的可信度。"""

    micro: int
    input_tokens: int
    output_tokens: int
    priced: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "cost_micro": self.micro,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "priced": self.priced,
        }


def tokens_of(usage: dict[str, Any] | None) -> tuple[int, int]:
    """从 usage 里取出输入/输出 token 数。字段名两套写法都要认（与观测页一致）。"""
    usage = usage or {}
    input_tokens = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
    return input_tokens, output_tokens


def cost_of(usage: dict[str, Any] | None, *, input_price_micro_per_1k: int,
            output_price_micro_per_1k: int) -> Cost:
    """按单价与用量算成本。单价为 0 时 `priced=False`（含义是"未定价"）。"""
    input_tokens, output_tokens = tokens_of(usage)
    priced = bool(input_price_micro_per_1k or output_price_micro_per_1k)
    micro = (input_tokens * int(input_price_micro_per_1k or 0)
             + output_tokens * int(output_price_micro_per_1k or 0)) // _TOKENS_PER_UNIT
    return Cost(micro=micro, input_tokens=input_tokens, output_tokens=output_tokens,
                priced=priced)


def cost_of_config(config: Any, usage: dict[str, Any] | None) -> Cost:
    """按一条 `AiModelConfig` 的单价算成本（配置缺列时按未定价处理）。"""
    return cost_of(
        usage,
        input_price_micro_per_1k=int(getattr(config, "input_price_micro_per_1k", 0) or 0),
        output_price_micro_per_1k=int(getattr(config, "output_price_micro_per_1k", 0) or 0),
    )


def format_micro(micro: int) -> str:
    """把微元格式化成给页面看的金额（6 位小数，去掉多余的 0）。"""
    value = int(micro or 0)
    sign = "-" if value < 0 else ""
    value = abs(value)
    text = f"{value // 1_000_000}.{value % 1_000_000:06d}".rstrip("0").rstrip(".")
    return f"{sign}{text or '0'}"


__all__ = ["Cost", "cost_of", "cost_of_config", "tokens_of", "format_micro"]
