"""供应商能力声明与推理策略的单元测试。

锁的是**声明表与解析规则**，不是某次调用的输出——供应商能力是可枚举的事实，
而调用输出不稳定。这样换供应商时测试仍有意义。
"""

from __future__ import annotations

import os
import unittest

from common import providers


class HostParsingTest(unittest.TestCase):
    def test_host_extraction_handles_common_shapes(self):
        self.assertEqual(providers.host_of("https://api.deepseek.com"), "api.deepseek.com")
        self.assertEqual(providers.host_of("https://api.deepseek.com/v1"), "api.deepseek.com")
        self.assertEqual(
            providers.host_of("https://dashscope.aliyuncs.com/compatible-mode/v1"),
            "dashscope.aliyuncs.com",
        )
        self.assertEqual(providers.host_of("api.deepseek.com"), "api.deepseek.com")

    def test_host_extraction_is_total_not_raising(self):
        for value in ("", "   ", None, 123, "http://"):
            self.assertIsInstance(providers.host_of(value), str)

    def test_subdomain_matches_its_declared_parent(self):
        self.assertEqual(
            providers.structured_output_strategy("https://eu.api.deepseek.com/v1"),
            providers.JSON_OBJECT,
        )


class StructuredOutputStrategyTest(unittest.TestCase):
    def test_declared_providers_map_to_their_real_capability(self):
        self.assertEqual(
            providers.structured_output_strategy("https://api.deepseek.com"), providers.JSON_OBJECT
        )
        self.assertEqual(
            providers.structured_output_strategy("https://dashscope.aliyuncs.com/compatible-mode/v1"),
            providers.JSON_SCHEMA,
        )

    def test_unknown_provider_defaults_to_the_conservative_strategy(self):
        """猜错会让请求直接失败，所以未声明的按保守策略走。"""
        self.assertEqual(
            providers.structured_output_strategy("https://nobody-heard-of.example/v1"),
            providers.JSON_OBJECT,
        )
        self.assertEqual(providers.structured_output_strategy(""), providers.JSON_OBJECT)

    def test_every_declared_strategy_is_implemented(self):
        for host, strategy in providers.declared_providers().items():
            self.assertIn(strategy, providers.STRUCTURED_OUTPUT_STRATEGIES, msg=host)


class ReasoningPolicyTest(unittest.TestCase):
    def test_mechanical_calls_do_not_reason_and_judgement_calls_do(self):
        for call_type in ("minutes", "speaker_match"):
            self.assertFalse(providers.wants_reasoning(call_type), msg=call_type)
        for call_type in ("agent_review", "agent_refine"):
            self.assertTrue(providers.wants_reasoning(call_type), msg=call_type)

    def test_unknown_call_type_defaults_to_no_reasoning(self):
        self.assertFalse(providers.wants_reasoning("brand_new_call_type"))
        self.assertFalse(providers.wants_reasoning(""))

    def test_environment_override_wins_over_the_per_type_policy(self):
        self.assertFalse(providers.wants_reasoning("agent_review", override="false"))
        self.assertTrue(providers.wants_reasoning("minutes", override="true"))
        self.assertTrue(providers.wants_reasoning("agent_review", override="TRUE"))

    def test_blank_or_invalid_override_falls_back_to_the_policy(self):
        for override in ("", "   ", "maybe"):
            self.assertTrue(providers.wants_reasoning("agent_review", override=override),
                            msg=repr(override))

    def test_every_declared_call_type_has_a_policy(self):
        """契约层的调用类型都必须有明确的推理声明，避免漏配后静默走默认值。"""
        for call_type in ("minutes", "agent_review", "agent_refine",
                          "speaker_match"):
            self.assertIn(call_type, providers.REASONING_BY_CALL_TYPE, msg=call_type)


class ThinkingParameterShapeTest(unittest.TestCase):
    def test_deepseek_uses_a_thinking_object(self):
        shape = providers.thinking_shape("https://api.deepseek.com")
        self.assertEqual(shape, "thinking_object")
        self.assertEqual(providers.thinking_extra_body(shape, True), {"thinking": {"type": "enabled"}})
        self.assertEqual(providers.thinking_extra_body(shape, False), {"thinking": {"type": "disabled"}})

    def test_dashscope_uses_a_boolean_flag(self):
        shape = providers.thinking_shape("https://dashscope.aliyuncs.com/compatible-mode/v1")
        self.assertEqual(shape, "enable_thinking")
        self.assertEqual(providers.thinking_extra_body(shape, True), {"enable_thinking": True})
        self.assertEqual(providers.thinking_extra_body(shape, False), {"enable_thinking": False})

    def test_unknown_provider_gets_a_default_shape_not_an_error(self):
        self.assertEqual(
            providers.thinking_shape("https://nobody-heard-of.example/v1"),
            providers.DEFAULT_THINKING_SHAPE,
        )

    def test_reasoning_tokens_are_documented_as_billed(self):
        """实测依据写进代码，避免后人把 max_tokens 调回小值。"""
        self.assertTrue(providers.reasoning_is_billed_as_output())


class OutputBudgetTest(unittest.TestCase):
    """输出预算是实测的硬限制，不是推断。锁住它以免后人把合并调回非思考模式。"""

    def test_budget_matches_the_measured_caps(self):
        self.assertEqual(providers.output_budget("non_thinking"), 8192)
        self.assertEqual(providers.output_budget("thinking"), 65536)
        self.assertEqual(
            providers.output_budget("unknown_mode"),
            providers.MAX_OUTPUT_TOKENS_BY_MODE["non_thinking"],
        )

    def test_large_merge_needs_the_extended_budget(self):
        """实测：38,804 字的会议在合并步被 8192 截断，必须申请扩展预算。"""
        self.assertTrue(providers.needs_extended_output_budget("minutes", 48013))
        self.assertTrue(providers.needs_extended_output_budget("minutes", 24000))

    def test_small_merge_does_not_pay_for_the_extended_budget(self):
        self.assertFalse(providers.needs_extended_output_budget("minutes", 5000))
        self.assertFalse(providers.needs_extended_output_budget("minutes", 0))

    def test_chunk_extraction_also_needs_it_near_the_chunk_limit(self):
        """实测：接近 12,000 字上限的分块，其 JSON 输出同样撑爆 8192。"""
        self.assertTrue(providers.needs_extended_output_budget("minutes", 35000))
        self.assertFalse(providers.needs_extended_output_budget("minutes", 4000))

    def test_call_types_whose_output_does_not_grow_with_input_stay_lean(self):
        """审查与修订的输出由纪要规模决定，不随转写长度增长。"""
        for call_type in ("agent_review", "agent_refine", "speaker_match"):
            self.assertFalse(
                providers.needs_extended_output_budget(call_type, 100000), msg=call_type
            )

    def test_threshold_boundary_is_declared(self):
        threshold = providers.EXTENDED_OUTPUT_INPUT_THRESHOLD_CHARS
        self.assertFalse(providers.needs_extended_output_budget("minutes", threshold - 1))
        self.assertTrue(providers.needs_extended_output_budget("minutes", threshold))

    def test_applied_budget_separates_the_declared_ceiling_from_the_request(self):
        """声明的能力上限与真正发出去的 max_tokens 是两件事。

        实测踩过：截断失败消息把 65536（思考模式声明上限）当作「预算很宽裕」打出来，
        而实际请求的 max_tokens 来自环境变量、可能只有 16384。把声明读成生效会让排查
        走向调错方向，因此这个函数必须同时给出两者，且**不得**把声明值当成生效值。
        """
        extended = providers.applied_output_budget("minutes", 48013,
                                                  env_max_tokens="16384")
        self.assertTrue(extended["extended_budget_requested"])
        self.assertEqual(extended["mode"], "thinking")
        self.assertEqual(extended["declared_ceiling_tokens"], 65536)  # 声明
        self.assertEqual(extended["applied_max_tokens"], 16384)       # 生效
        self.assertIn("LLM_MAX_TOKENS", extended["applied_source"])

        lean = providers.applied_output_budget("agent_review", 100000, env_max_tokens="")
        self.assertFalse(lean["extended_budget_requested"])
        self.assertIsNone(lean["applied_max_tokens"])
        self.assertIn("供应商默认", lean["applied_source"])

    def test_applied_budget_tolerates_a_malformed_env_value(self):
        """环境变量写错时不得把它当成一个数字，而是回落到「未指定」。"""
        budget = providers.applied_output_budget("minutes", 48013,
                                                 env_max_tokens="not-a-number")
        self.assertIsNone(budget["applied_max_tokens"])


class TruncationDetectionTest(unittest.TestCase):
    """截断必须与"模型返回坏 JSON"区分开，否则排查方向会错。"""

    def test_recognizes_the_real_langchain_truncation_message(self):
        self.assertTrue(providers.truncation_marker(
            "Could not parse response content as the length limit was reached - "
            "CompletionUsage(completion_tokens=8192, prompt_tokens=12545)"
        ))

    def test_does_not_misclassify_ordinary_failures(self):
        for message in ("模型返回的不是JSON对象", "timeout", "Connection error", "", None):
            self.assertFalse(providers.truncation_marker(message), msg=repr(message))


if __name__ == "__main__":
    unittest.main()


class PerCallTypeMaxTokensTest(unittest.TestCase):
    """按调用类型分档的输出预算：根因是合并步思考 token 与 JSON 共享全局一档。"""

    def test_per_type_overrides_global(self):
        from common import providers
        with unittest.mock.patch.dict("os.environ",
                                      {"LLM_MAX_TOKENS": "8192",
                                       "LLM_MAX_TOKENS_MINUTES": "49152"}):
            self.assertEqual(providers.max_tokens_env("minutes"), "49152")
            # 没有专属档位的类型回落到全局值
            self.assertEqual(providers.max_tokens_env("agent_review"), "8192")

    def test_falls_back_to_global_and_empty(self):
        from common import providers
        with unittest.mock.patch.dict("os.environ", {"LLM_MAX_TOKENS": "16384"}, clear=False):
            os.environ.pop("LLM_MAX_TOKENS_MINUTES", None)
            self.assertEqual(providers.max_tokens_env("minutes"), "16384")
        with unittest.mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(providers.max_tokens_env("minutes"), "")

    def test_budget_report_uses_effective_value(self):
        from common import providers
        with unittest.mock.patch.dict("os.environ",
                                      {"LLM_MAX_TOKENS_MINUTES": "49152"}):
            report = providers.applied_output_budget(
                "minutes", 20000, env_max_tokens=providers.max_tokens_env("minutes"))
        self.assertEqual(report["applied_max_tokens"], 49152)
