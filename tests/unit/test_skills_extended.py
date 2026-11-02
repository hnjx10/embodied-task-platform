"""扩展技能的行为测试。

这些技能的价值不在「能跑通」，而在**失败路径是否保真**：抓不到就得报错，
夹爪空着就不能放，角度要累加而不是覆盖。一个永远成功的 mock 会让编排层的
错误隐性化——流水线有 bug 也测不出来。
"""

from __future__ import annotations

import math

import pytest

from robot_agent.skills.base import SkillRegistry
from robot_agent.skills.mock import MockWorld


def test_move_relative_offsets_from_current_pose(registry: SkillRegistry, world: MockWorld) -> None:
    registry.get("goto_point").execute(x=0.5, y=0.0, speed=0.15)
    result = registry.get("move_relative").execute(dx=0.0, dy=0.3, speed=0.1)
    assert result.ok
    assert world.robot[0] == pytest.approx(0.5)
    assert world.robot[1] == pytest.approx(0.3)


def test_rotate_accumulates_heading(registry: SkillRegistry, world: MockWorld) -> None:
    registry.get("rotate").execute(yaw_deg=90.0)
    assert math.degrees(world.robot[2]) == pytest.approx(90.0, abs=1e-6)
    registry.get("rotate").execute(yaw_deg=-135.0)
    assert math.degrees(world.robot[2]) == pytest.approx(-45.0, abs=1e-6)


def test_look_at_points_towards_target(registry: SkillRegistry, world: MockWorld) -> None:
    result = registry.get("look_at").execute(x=0.0, y=1.0)
    assert result.ok
    assert math.degrees(world.robot[2]) == pytest.approx(90.0, abs=1e-6)


def test_patrol_visits_every_waypoint(registry: SkillRegistry, world: MockWorld) -> None:
    result = registry.get("patrol").execute(
        waypoints=[
            {"x": 0.5, "y": 0.0},
            {"x": 0.5, "y": 0.5},
            {"x": 0.0, "y": 0.5},
        ],
        loops=2,
    )
    assert result.ok
    assert result.data["visited"] == 6
    assert world.robot[:2] == pytest.approx((0.0, 0.5))


def test_pick_fails_when_out_of_reach(registry: SkillRegistry) -> None:
    """距离不够必须失败——这是真机上最容易撞车、mock 最容易放过的场景。"""
    result = registry.get("pick_cube").execute(color="red")
    assert not result.ok
    assert "超出抓取范围" in result.detail


def test_pick_succeeds_when_close(registry: SkillRegistry, world: MockWorld) -> None:
    registry.get("goto_point").execute(x=0.8, y=0.5, speed=0.15)
    result = registry.get("pick_cube").execute(color="red")
    assert result.ok
    assert world.holding == "red"
    assert world.gripper_open is False
    assert "red" not in world.cubes, "方块被抓起后不应还留在场上"


def test_pick_rejects_second_object(registry: SkillRegistry, world: MockWorld) -> None:
    registry.get("goto_point").execute(x=0.8, y=0.5, speed=0.15)
    registry.get("pick_cube").execute(color="red")
    result = registry.get("pick_cube").execute(color="green")
    assert not result.ok
    assert "已持有" in result.detail


def test_place_requires_holding_something(registry: SkillRegistry) -> None:
    result = registry.get("place_cube").execute(x=0.2, y=0.2)
    assert not result.ok
    assert "没有物体" in result.detail


def test_pick_then_place_round_trip(registry: SkillRegistry, world: MockWorld) -> None:
    registry.get("goto_point").execute(x=0.8, y=0.5, speed=0.15)
    assert registry.get("pick_cube").execute(color="red").ok
    result = registry.get("place_cube").execute(x=-0.4, y=0.2)
    assert result.ok
    assert world.holding is None
    assert world.cubes["red"] == pytest.approx((-0.4, 0.2))


def test_gripper_toggle(registry: SkillRegistry, world: MockWorld) -> None:
    assert registry.get("close_gripper").execute().ok
    assert world.gripper_open is False
    assert registry.get("open_gripper").execute().ok
    assert world.gripper_open is True


def test_set_speed_records_cap(registry: SkillRegistry, world: MockWorld) -> None:
    result = registry.get("set_speed").execute(max_speed=0.1)
    assert result.ok
    assert world.speed_cap == pytest.approx(0.1)


def test_set_speed_rejects_non_positive(registry: SkillRegistry) -> None:
    from robot_agent.errors import SkillExecutionError

    with pytest.raises(SkillExecutionError, match="参数非法"):
        registry.get("set_speed").execute(max_speed=0.0)


def test_report_status_exposes_snapshot(registry: SkillRegistry) -> None:
    registry.get("goto_point").execute(x=0.3, y=0.4, speed=0.15)
    result = registry.get("report_status").execute()
    assert result.ok
    robot = result.data["robot"]
    assert isinstance(robot, dict)
    assert robot["x"] == pytest.approx(0.3)
    assert set(result.data["cubes"]) == {"red", "green", "blue"}


def test_wait_is_capped_in_mock(registry: SkillRegistry) -> None:
    """mock 环境不能真的睡 5 秒，但必须如实告诉调用方被截断了。"""
    result = registry.get("wait").execute(duration_s=5.0)
    assert result.ok
    assert result.data["waited_s"] < 1.0
    assert "截断" in result.detail


def test_wait_rejects_out_of_range(registry: SkillRegistry) -> None:
    from robot_agent.errors import SkillExecutionError

    with pytest.raises(SkillExecutionError):
        registry.get("wait").execute(duration_s=0.0)
