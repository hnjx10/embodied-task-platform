"""扩展技能的行为测试。

这些技能的价值不在「能跑通」，而在**失败路径是否保真**：抓不到就得报错，
夹爪空着就不能放，角度要累加而不是覆盖。一个永远成功的 mock 会让编排层的
错误隐性化——流水线有 bug 也测不出来。
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from robot_agent.config import Settings
from robot_agent.skills.base import SkillRegistry
from robot_agent.skills.mock import MockWorld, build_mock_registry


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


# ---------------------------------------------------------------- 能源 / 外设


def test_dock_stops_outside_charging_geofence(registry: SkillRegistry, world: MockWorld) -> None:
    """停靠点必须落在禁区外——这条断言防的是「安全策略和业务目标打架」。

    充电桩本身是禁区。如果停靠点算在禁区里，回充这条指令会被自己的
    GeofencePolicy 拦下，表现成「机器人拒绝给自己充电」，而且日志里
    看不出是配置问题。所以 clearance 与 geofence 半径必须有一致性护栏。
    """
    settings = Settings()
    fence = next(f for f in settings.safety.geofences if f.name == "charging_dock")

    result = registry.get("dock").execute()
    assert result.ok
    cx, cy = world.arena.dock_pose
    dist = math.hypot(world.robot[0] - cx, world.robot[1] - cy)
    assert dist == pytest.approx(world.arena.dock_clearance, abs=1e-6)
    assert dist > fence.radius, "停靠点落进了禁区：回充会被安全策略拦下"
    assert world.arena.dock_pose == pytest.approx((fence.x, fence.y)), (
        "arena.dock_pose 与 safety.geofences 里的充电桩坐标不一致"
    )


def test_dock_recharges_battery(registry: SkillRegistry, world: MockWorld) -> None:
    registry.get("goto_point").execute(x=1.0, y=0.0)
    assert world.battery() < 100.0
    registry.get("dock").execute()
    assert world.battery() == pytest.approx(100.0)
    assert world.docked is True


def test_battery_drains_with_travel(registry: SkillRegistry, world: MockWorld) -> None:
    before = registry.get("get_battery").execute().data["battery_pct"]
    registry.get("goto_point").execute(x=1.0, y=0.0)
    after = registry.get("get_battery").execute().data["battery_pct"]
    assert after < before


def test_undock_moves_away_from_dock(registry: SkillRegistry, world: MockWorld) -> None:
    registry.get("dock").execute()
    cx, cy = world.arena.dock_pose
    before = math.hypot(world.robot[0] - cx, world.robot[1] - cy)

    registry.get("undock").execute(distance=0.3)
    after = math.hypot(world.robot[0] - cx, world.robot[1] - cy)
    assert after == pytest.approx(before + 0.3, abs=1e-6)
    assert world.docked is False


def test_set_led_records_state(registry: SkillRegistry, world: MockWorld) -> None:
    result = registry.get("set_led").execute(color="red", brightness=0.5)
    assert result.ok
    assert world.led == ("red", 0.5)


def test_set_led_rejects_unknown_color(registry: SkillRegistry) -> None:
    from robot_agent.errors import SkillExecutionError

    with pytest.raises(SkillExecutionError):
        registry.get("set_led").execute(color="purple")


def test_capture_without_image_dir_does_not_persist(
    registry: SkillRegistry, world: MockWorld
) -> None:
    """没配目录时如实报告「没落盘」，而不是给一个不存在的路径。"""
    result = registry.get("capture_image").execute(label="scene")
    assert result.ok
    assert result.data["persisted"] is False
    assert result.data["path"] == ""


def test_capture_persists_when_image_dir_configured(tmp_path: Path) -> None:
    registry, _ = build_mock_registry(image_dir=str(tmp_path))
    result = registry.get("capture_image").execute(label="scene-01")
    assert result.data["persisted"] is True

    path = Path(str(result.data["path"]))
    assert path.parent == tmp_path
    assert path.read_bytes().startswith(b"P3\n")


def test_capture_rejects_path_traversal_label(registry: SkillRegistry) -> None:
    """label 会进文件名。不限制字符集等于把 ../ 交给模型去填。"""
    from robot_agent.errors import SkillExecutionError

    with pytest.raises(SkillExecutionError):
        registry.get("capture_image").execute(label="../../etc/passwd")


def test_reset_odom_makes_pose_relative(registry: SkillRegistry, world: MockWorld) -> None:
    """重置 odom 是「编码器清零」，不是「把车搬回原点」。"""
    registry.get("goto_point").execute(x=1.0, y=0.0)
    registry.get("reset_odom").execute()

    pose = registry.get("get_pose").execute(target="robot")
    assert pose.data["x"] == pytest.approx(0.0, abs=1e-9)
    assert pose.data["y"] == pytest.approx(0.0, abs=1e-9)
    assert world.robot[0] == pytest.approx(1.0), "物理位置不该被重置改变"
