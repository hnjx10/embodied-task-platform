"""意图路由测试。

路由的定位是「能确定就省一次模型调用」，不是「替模型做判断」。所以这里既测
分类正确的路径，也测**不确定时不拦截**——后者才是它不会把任务搞砸的前提。
"""

from __future__ import annotations

import pytest

from robot_agent.planner.router import classify


@pytest.mark.parametrize(
    ("instruction", "expected"),
    [
        ("把红色方块推到左边", "manipulate"),
        ("抓起绿色方块", "manipulate"),
        ("把方块放下", "manipulate"),
        ("开到 (1.0, 1.0)", "move"),
        ("走到中间", "move"),
        ("巡逻一圈", "move"),
        ("机器人在哪", "query"),
        ("上报当前状态", "query"),
        ("电量还有多少", "system"),
        ("回充", "system"),
        ("拍张照", "system"),
        ("重置里程计", "system"),
    ],
)
def test_classifies_common_instructions(instruction: str, expected: str) -> None:
    assert classify(instruction).intent == expected


def test_returns_unknown_when_nothing_matches() -> None:
    match = classify("今天天气怎么样")
    assert match.intent == "unknown"
    assert match.certain is False


def test_empty_instruction_is_unknown() -> None:
    assert classify("   ").intent == "unknown"


def test_prefers_specific_over_generic() -> None:
    """「把红块推过去」同时命中「推」和「去」，应判为操作类而不是移动类。"""
    assert classify("把红块推过去").intent == "manipulate"


def test_prefers_longest_keyword_in_same_intent() -> None:
    """`回充` 同时命中 `回`（移动）与 `回充`（系统），长词优先才能判对。"""
    match = classify("回充")
    assert match.intent == "system"
    assert match.keyword == "回充"


def test_match_explains_itself() -> None:
    """路由结论要能解释，否则「为什么这句话被判成 query」永远查不清。"""
    match = classify("机器人在哪")
    assert match.keyword in {"在哪"}
    assert "命中" in match.reason
    assert match.to_dict()["intent"] == "query"
