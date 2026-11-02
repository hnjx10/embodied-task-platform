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


@pytest.mark.parametrize(
    "text,skill,args",
    [
        # 相对位移：方向 + 距离，中文数字与阿拉伯数字等价
        ("往前开一米", "move_relative", {"dx": 0.0, "dy": 1.0}),
        ("后退半米", "move_relative", {"dx": 0.0, "dy": -0.5}),
        ("向左移动半米", "move_relative", {"dx": -0.5, "dy": 0.0}),
        ("右移两米", "move_relative", {"dx": 2.0, "dy": 0.0}),
        # 能源与外设
        ("回充", "dock", {}),
        ("充电", "dock", {}),
        ("补充电量", "dock", {}),
        ("离开充电桩0.5米", "undock", {"distance": 0.5}),
        ("从充电桩出来一米", "undock", {"distance": 1.0}),
        ("检查一下电池", "get_battery", {}),
        ("把灯调成蓝色", "set_led", {"color": "blue", "brightness": 1.0}),
        ("关灯", "set_led", {"color": "off", "brightness": 0.0}),
        ("拍张照", "capture_image", {"label": "snapshot"}),
        ("重置里程计", "reset_odom", {}),
        ("reset odom", "reset_odom", {}),
    ],
)
def test_clause_shapes(text: str, skill: str, args: dict) -> None:
    """每个句式都要落到确定的技能与参数上——降级路径不能靠猜。"""
    plan = RulePlanner().plan(text)
    assert len(plan) == 1
    assert plan[0].skill == skill
    for key, value in args.items():
        if isinstance(value, float):
            assert plan[0].args[key] == pytest.approx(value)
        else:
            assert plan[0].args[key] == value


def test_supplement_charge_is_not_battery_query() -> None:
    """「补充电量」同时含「电量」与「充电」，必须先判回充。

    顺序反了，一条充电指令会被当成只读查询放过去——语义完全相反，
    而且查询永远不会失败，所以这个 bug 在演示里根本不会暴露。
    """
    assert RulePlanner().plan("补充电量")[0].skill == "dock"
    assert RulePlanner().plan("电量还剩多少")[0].skill == "get_battery"


def test_undock_before_dock() -> None:
    """「离开充电桩」含「充电」，不能先被回充规则吃掉。"""
    assert RulePlanner().plan("退出充电")[0].skill == "undock"


def test_relative_speed_keyword_applies() -> None:
    step = RulePlanner().plan("全速往前开半米")[0]
    assert step.skill == "move_relative"
    assert step.args["speed"] == pytest.approx(0.5)


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
