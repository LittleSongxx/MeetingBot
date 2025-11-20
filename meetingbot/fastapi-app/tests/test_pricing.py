"""成本核算的单测：整数微元、单价来自当前配置、未定价 ≠ 免费。

成本会被用来做闸门判定（超了就拒），所以这里钉的是**数值精确性**与
"未定价"的语义：算错一分钱不要紧，把"不知道多少钱"算成 0 元要紧——
那会让预算闸与报表同时失真。
"""

from __future__ import annotations

import unittest

from services import pricing


class CostTest(unittest.TestCase):
    def test_cost_is_integer_micro_units(self):
        # 输入 2 元/百万 token = 2000 微元/千 token；输出 8 元/百万 = 8000 微元/千
        cost = pricing.cost_of({"input_tokens": 1500, "output_tokens": 500},
                               input_price_micro_per_1k=2000,
                               output_price_micro_per_1k=8000)
        self.assertEqual(cost.input_tokens, 1500)
        self.assertEqual(cost.output_tokens, 500)
        # 1500*2000/1000 + 500*8000/1000 = 3000 + 4000
        self.assertEqual(cost.micro, 7000)
        self.assertTrue(cost.priced)

    def test_prompt_and_completion_aliases_both_work(self):
        cost = pricing.cost_of({"prompt_tokens": 1000, "completion_tokens": 1000},
                               input_price_micro_per_1k=1000,
                               output_price_micro_per_1k=3000)
        self.assertEqual(cost.micro, 4000)

    def test_unpriced_is_not_free(self):
        cost = pricing.cost_of({"input_tokens": 10_000, "output_tokens": 5_000},
                               input_price_micro_per_1k=0, output_price_micro_per_1k=0)
        self.assertEqual(cost.micro, 0)
        self.assertFalse(cost.priced, "单价为 0 的含义是'未定价'，必须能被调用方区分出来")

    def test_missing_usage_is_zero_cost(self):
        cost = pricing.cost_of(None, input_price_micro_per_1k=1000,
                               output_price_micro_per_1k=1000)
        self.assertEqual(cost.micro, 0)
        self.assertTrue(cost.priced)

    def test_rounding_is_toward_zero_and_never_negative(self):
        # 500 token 按 1 微元/千 算：整数除法向下取整，不会出现负数或小数
        cost = pricing.cost_of({"input_tokens": 500}, input_price_micro_per_1k=1,
                               output_price_micro_per_1k=0)
        self.assertEqual(cost.micro, 0)

    def test_config_helper_reads_the_columns(self):
        class Config:
            input_price_micro_per_1k = 2_000_000
            output_price_micro_per_1k = 8_000_000

        cost = pricing.cost_of_config(Config(), {"input_tokens": 1000, "output_tokens": 1000})
        self.assertEqual(cost.micro, 10_000_000)  # 10 元

    def test_format_micro_is_readable(self):
        self.assertEqual(pricing.format_micro(0), "0")
        self.assertEqual(pricing.format_micro(1), "0.000001")
        self.assertEqual(pricing.format_micro(12_500_000), "12.5")
        self.assertEqual(pricing.format_micro(-1), "-0.000001")


class ObservabilityCostTest(unittest.IsolatedAsyncioTestCase):
    """观测页的成本聚合：明细之和必须等于总额（整数微元的直接收益）。"""

    async def asyncSetUp(self):
        from tortoise import Tortoise
        await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["models"]},
                            use_tz=True, timezone="Asia/Shanghai")
        await Tortoise.generate_schemas()
        self.Tortoise = Tortoise

    async def asyncTearDown(self):
        await self.Tortoise.close_connections()

    async def test_totals_match_the_sum_of_rows(self):
        from models import AiCallLog, AiModelConfig, User
        from services import observability_service

        user = await User.create(username="cost-t", password="x", name="张晨", role="ADMIN")
        priced = await AiModelConfig.create(
            name="已定价", model_type="MINUTES", base_url="https://dashscope.aliyuncs.com",
            api_key="k", model_name="m", enabled=True,
            input_price_micro_per_1k=2000, output_price_micro_per_1k=8000)
        unpriced = await AiModelConfig.create(
            name="未定价", model_type="AGENT", base_url="https://dashscope.aliyuncs.com",
            api_key="k", model_name="m2", enabled=True)
        await AiCallLog.create(request_id="AIC-COST-1", call_type="MINUTES", biz_id=1,
                               model_config_id=priced.id, model_name="m", status="SUCCEEDED",
                               usage_json={"input_tokens": 1000, "output_tokens": 1000},
                               cost_micro=10_000)
        await AiCallLog.create(request_id="AIC-COST-2", call_type="AGENT_REVIEW", biz_id=2,
                               model_config_id=unpriced.id, model_name="m2", status="SUCCEEDED",
                               usage_json={"input_tokens": 500, "output_tokens": 500},
                               cost_micro=0)

        data = await observability_service.build_observability(user, None, None, None)
        summary = data["summary"]
        self.assertEqual(summary["cost_micro"], 10_000)
        self.assertEqual(summary["cost_text"], "0.01")
        self.assertEqual(summary["unpriced_call_count"], 1)
        # 每日趋势之和等于总额（整数相加，不存在浮点误差）
        self.assertEqual(sum(day["cost_micro"] for day in data["daily_trend"]), 10_000)
        # 两个模型的成本分别归属到各自的配置行
        costs = {item["model_config_id"]: item["cost_micro"] for item in data["model_ranking"]}
        self.assertEqual(costs[priced.id], 10_000)


if __name__ == "__main__":
    unittest.main()
