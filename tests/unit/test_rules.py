"""规则规划器测试：分句、坐标解析、失败即报错。"""

from __future__ import annotations

import pytest

from robot_agent.errors import PlanningError
from robot_agent.planner.rules import RulePlanner, split_clauses


def test_split_keeps_coordinate_comma() -> None:
    assert split_clauses("把红块推到 (1.0, 0.5)") == ["把红块推到 (1.0, 0.5)"]


def test_split_multi_step() -> None:
    parts = split_clauses("把红块推到 (1.0, 0.5)，然后开到左边")
    assert parts == ["把红块推到 (1.0, 0.5)", "开到左边"]


def test_split_chinese_brackets() -> None:
    assert split_clauses("开到（1.0，0.5）") == ["开到（1.0，0.5）"]


@pytest.mark.parametrize(
    "text,expected",
    [
        ("把红色方块推到左边", (-1.2, 0.0)),
        ("把绿色方块推到右边", (1.2, 0.0)),
        ("把蓝色方块推到中间", (0.0, 0.0)),
        ("把红色方块推到 (0.5, -0.5)", (0.5, -0.5)),
    ],
)
def test_push_targets(text: str, expected: tuple[float, float]) -> None:
    plan = RulePlanner().plan(text)
    assert len(plan) == 1
    step = plan[0]
    assert step.skill == "push_cube"
    assert (step.args["x"], step.args["y"]) == expected


def test_goto_with_explicit_coords() -> None:
    plan = RulePlanner().plan("开到 x=1.2 y=-0.3")
    assert plan[0].skill == "goto_point"
    assert plan[0].args["x"] == pytest.approx(1.2)
    assert plan[0].args["speed"] == pytest.approx(0.15)


def test_default_speed_followed() -> None:
    plan = RulePlanner(default_speed=0.1).plan("开到 (0, 0.5)")
    assert plan[0].args["speed"] == pytest.approx(0.1)


def test_fast_keyword_raises_speed() -> None:
    plan = RulePlanner().plan("全速开到 (1.0, 1.0)")
    assert plan[0].args["speed"] > 0.22  # 交给安全层降速，不在规划层偷偷改


def test_multi_step_plan() -> None:
    plan = RulePlanner().plan("把红块推到左边，然后回到原点")
    assert [s.skill for s in plan] == ["push_cube", "goto_point"]


def test_unknown_instruction_raises() -> None:
    with pytest.raises(PlanningError) as exc:
        RulePlanner().plan("给我唱首歌")
    assert "无法解析" in str(exc.value)


def test_empty_instruction_raises() -> None:
    with pytest.raises(PlanningError):
        RulePlanner().plan("   ")


def test_partially_parsed_instruction_is_rejected() -> None:
    """半条指令比整条失败更危险：宁可全拒，也不执行一部分。"""
    with pytest.raises(PlanningError) as exc:
        RulePlanner().plan("把红色方块推到左边，然后跳个舞")
    assert "无法解析的片段" in str(exc.value)


def test_expect_field_populated() -> None:
    step = RulePlanner().plan("把红色方块推到左边")[0]
    assert step.expect is not None
    assert step.expect["color"] == "red"
    assert step.expect["tolerance"] > 0
