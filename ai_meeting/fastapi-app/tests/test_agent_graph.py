import unittest

from services.agent_graph import build_review_graph, recursion_limit, review_passed


class FakeActions:
    """替代 agent_service 注入的四个动作，记录图调了哪些、按什么顺序调。"""

    def __init__(self, verdicts, stop_after_reviews=None):
        # 每次审查依次返回的判定，True 表示通过
        self.verdicts = list(verdicts)
        self.stop_after_reviews = stop_after_reviews
        self.calls = []

    async def review(self, working, round_no):
        self.calls.append(("review", round_no, working["version"]))
        passed = self.verdicts.pop(0)
        return {"passed": passed, "score": 90 if passed else 60, "issues": [] if passed else [{"field": "decisions"}]}

    async def refine(self, working, issues, round_no):
        self.calls.append(("refine", round_no, working["version"]))
        return {"version": working["version"] + 1}

    async def finalize(self):
        self.calls.append(("finalize",))

    async def should_stop(self):
        reviews = sum(1 for call in self.calls if call[0] == "review")
        return self.stop_after_reviews is not None and reviews >= self.stop_after_reviews

    def state(self, max_rounds, round_no=0):
        return {
            "working": {"version": 0},
            "round_no": round_no,
            "max_rounds": max_rounds,
            "review": self.review,
            "refine": self.refine,
            "finalize": self.finalize,
            "should_stop": self.should_stop,
        }


class AgentGraphTest(unittest.IsolatedAsyncioTestCase):
    async def run_graph(self, actions, max_rounds, round_no=0):
        return await build_review_graph().ainvoke(
            actions.state(max_rounds, round_no), {"recursion_limit": recursion_limit(max_rounds)}
        )

    async def test_first_review_passes_goes_straight_to_finalize(self):
        actions = FakeActions([True])
        state = await self.run_graph(actions, max_rounds=2)
        self.assertEqual(actions.calls, [("review", 1, 0), ("finalize",)])
        self.assertTrue(state["passed"])

    def test_review_result_rejects_conflicting_verdicts(self):
        for passed, issues in ((True, [{"field": "decisions"}]), (False, [])):
            with self.subTest(passed=passed, issues=issues):
                with self.assertRaisesRegex(ValueError, "审查结果矛盾"):
                    review_passed({"passed": passed, "issues": issues})

    async def test_conflicting_review_cannot_route_to_refine_or_finalize(self):
        for passed, issues in ((True, [{"field": "decisions"}]), (False, [])):
            with self.subTest(passed=passed, issues=issues):
                actions = FakeActions([])

                async def conflicting_review(working, round_no):
                    actions.calls.append(("review", round_no, working["version"]))
                    return {"passed": passed, "score": 80, "issues": issues}

                actions.review = conflicting_review
                with self.assertRaisesRegex(ValueError, "审查结果矛盾"):
                    await self.run_graph(actions, max_rounds=2)
                self.assertEqual(actions.calls, [("review", 1, 0)])

    async def test_refined_version_is_reviewed_again(self):
        """第一轮不通过就改一版，第二轮审查看到的是改过的那一版。"""
        # 两次审查依次返回不通过、通过
        actions = FakeActions([False, True])
        await self.run_graph(actions, max_rounds=2)
        # 调用记录的第三项是这一版纪要的 version，第二轮审查拿到的是重写后的 version 1
        self.assertEqual(actions.calls, [("review", 1, 0), ("refine", 1, 0), ("review", 2, 1), ("finalize",)])

    async def test_round_limit_finalizes_without_another_review(self):
        actions = FakeActions([False, False, False])
        state = await self.run_graph(actions, max_rounds=2)
        self.assertEqual(
            actions.calls,
            [("review", 1, 0), ("refine", 1, 0), ("review", 2, 1), ("refine", 2, 1), ("finalize",)],
        )
        self.assertEqual(state["working"], {"version": 2})

    async def test_largest_round_limit_fits_recursion_limit(self):
        """接口允许的最大轮数 5 一直不通过时，状态图也能在步数上限内走完。"""
        actions = FakeActions([False] * 5)
        await self.run_graph(actions, max_rounds=5)
        self.assertEqual(actions.calls[-1], ("finalize",))
        self.assertEqual(sum(1 for call in actions.calls if call[0] == "review"), 5)

    async def test_interrupt_ends_without_finalize(self):
        actions = FakeActions([False, False], stop_after_reviews=1)
        state = await self.run_graph(actions, max_rounds=2)
        self.assertEqual(actions.calls, [("review", 1, 0), ("refine", 1, 0)])
        self.assertTrue(state["interrupted"])

    async def test_resume_continues_from_saved_round(self):
        """中断恢复时从已完成的轮次接着跑，第二轮审查之后到上限就收尾。"""
        actions = FakeActions([False])
        await self.run_graph(actions, max_rounds=2, round_no=1)
        self.assertEqual(actions.calls, [("review", 2, 0), ("refine", 2, 0), ("finalize",)])


if __name__ == "__main__":
    unittest.main()
