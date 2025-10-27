"""纪要自检的反思环状态图。

    审查 ─┬─判定通过──────────────────────→ 汇总 ─→ 结束
          ├─人在页面上点了中断─────────────────────→ 结束
          └─判定不通过─→ 重写 ─┬─到轮数上限─→ 汇总
                  ↑            └─没到上限──┐
                  └────────────────────────┘

一轮等于「审查一次 + 按问题改一版」。转几轮由模型每一轮的审查判定决定，
代码只负责兜住轮数上限。

这张图不碰数据库，也不直接调模型：审查、重写、汇总、中断检查四个动作
都由 agent_service 注入进状态，图只决定什么时候调哪一个，
所以可以脱离数据库和模型单独测试。
"""

from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph


class ReviewState(TypedDict, total=False):
    """状态从头传到尾，每个节点只返回自己改了的字段，框架负责合并。"""

    # 当前待审的这一版纪要，重写节点每改一次就换成新的一版
    working: dict
    # 最近一轮审查挑出的问题清单，重写节点照着它改
    issues: list[dict]
    # 最近一轮审查的判定和得分
    passed: bool
    score: int
    # 已经跑完的轮次。中断恢复时从 agent_run.current_round 带进来，不从 0 开始
    round_no: int
    # 轮数上限，来自 agent_run.max_rounds
    max_rounds: int
    # 审查节点发现运行已被中断时置为 True，路由直接结束
    interrupted: bool

    # 下面四个由 agent_service 注入，图只管调用时机
    # async (working, round_no) -> 审查结果 dict
    review: Any
    # async (working, issues, round_no) -> 改好的新一版 dict
    refine: Any
    # async () -> None，写汇总步骤并给运行记录收尾
    finalize: Any
    # async () -> bool，运行状态是否已经不是 RUNNING
    should_stop: Any


def review_passed(result: dict) -> bool:
    """审查结论与问题列表必须一致；矛盾结果不能驱动状态图。"""
    if not isinstance(result, dict) or type(result.get("passed")) is not bool or not isinstance(result.get("issues"), list):
        raise ValueError("审查结果格式错误：passed 必须为布尔值，issues 必须为数组")
    passed = result["passed"]
    if passed != (len(result["issues"]) == 0):
        raise ValueError("审查结果矛盾：passed=true 时 issues 必须为空，passed=false 时 issues 必须非空")
    return passed


async def review_node(state: ReviewState) -> ReviewState:
    """审查节点：让模型检查当前这一版，挑出问题并给出判定。"""
    # 每轮开头先确认运行还是 RUNNING，页面点了中断就不再发起新的模型调用
    if await state["should_stop"]():
        return {"interrupted": True}
    # 本轮轮次号 = 已完成轮次 + 1
    round_no = state.get("round_no", 0) + 1
    # 调注入的审查函数，传入当前这一版纪要
    result = await state["review"](state["working"], round_no)
    passed = review_passed(result)
    issues = result["issues"]
    # 返回的字段由 LangGraph 合并进状态
    return {
        "round_no": round_no,
        "issues": issues,
        "passed": passed,
        "score": int(result.get("score") or 0),
    }


async def refine_node(state: ReviewState) -> ReviewState:
    """重写节点：把上一轮的问题清单交回模型，改出新的一版。"""
    working = await state["refine"](state["working"], state["issues"], state["round_no"])
    # 换成新的一版，下一轮审查看到的就是改过的稿子
    return {"working": working}


async def finalize_node(state: ReviewState) -> ReviewState:
    """汇总节点：写一句自检结论，给这次运行收尾。"""
    await state["finalize"]()
    # 汇总不改状态字段，返回空字典
    return {}


def route_after_review(state: ReviewState) -> str:
    """审查之后往哪走。"""
    if state.get("interrupted"):
        # 中断时不写汇总，运行状态已经在中断接口里改成 INTERRUPTED
        return END
    if state.get("passed"):
        # 这一版判定通过，环在这里结束，不再重写
        return "finalize"
    return "refine"


def route_after_refine(state: ReviewState) -> str:
    """重写之后往哪走：轮数用完就汇总，这一版不再复审；没用完回到审查。"""
    if state.get("round_no", 0) >= state.get("max_rounds", 0):
        return "finalize"
    return "review"


def build_review_graph():
    """把节点和边组装成状态图。

    流程写在边上、业务写在节点里、状态写在一个地方，
    看这个函数就是一张流程图。
    """
    # 状态结构是 ReviewState
    graph = StateGraph(ReviewState)
    graph.add_node("review", review_node)
    graph.add_node("refine", refine_node)
    graph.add_node("finalize", finalize_node)

    # 从审查开始
    graph.add_edge(START, "review")
    # 条件边：下一步去哪由 route_after_review 读状态决定
    graph.add_conditional_edges(
        "review",
        route_after_review,
        {"finalize": "finalize", "refine": "refine", END: END},
    )
    # 重写之后由 route_after_refine 决定复审还是汇总
    graph.add_conditional_edges(
        "refine",
        route_after_refine,
        {"review": "review", "finalize": "finalize"},
    )
    # 汇总之后结束
    graph.add_edge("finalize", END)
    # compile 返回可以 ainvoke 的状态图
    return graph.compile()


def recursion_limit(max_rounds: int) -> int:
    """LangGraph 的步数上限：每轮审查、重写两个节点，最后一个汇总节点，再留一步余量。"""
    return max_rounds * 2 + 2
