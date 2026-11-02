"""Action 导航封装的降级路径测试。

这里不测「Nav2 能不能导航」——那需要真实 ROS2 环境，属于 ``@pytest.mark.ros``
的范畴，CI 里跑不了。真正容易出事的是**没有 ROS2 的时候会发生什么**：
是在 import 阶段就崩掉，还是安静地返回一个说得清楚的失败原因。

一个在缺依赖时抛 ImportError 的模块，会让整条流水线在「只是没装 Nav2」的
机器上无法启动；而一个静默返回成功的桩，会让上层以为导航真的完成了。
两种都不可接受。
"""

from __future__ import annotations

import math

import pytest

from robot_agent.config import Settings
from robot_agent.skills.ros2_action import (
    ACTION_AVAILABLE,
    NavActionClient,
    NavOutcome,
    _yaw_to_quat,
)


def test_yaw_to_quat_matches_planar_rotation() -> None:
    z, w = _yaw_to_quat(0.0)
    assert (z, w) == pytest.approx((0.0, 1.0))

    z, w = _yaw_to_quat(math.pi / 2)
    assert z == pytest.approx(math.sqrt(0.5))
    assert w == pytest.approx(math.sqrt(0.5))

    # 单位四元数：z² + w² 必须为 1，否则朝向会随时间漂移
    for deg in (0, 37, 90, 180, -120):
        z, w = _yaw_to_quat(math.radians(deg))
        assert z * z + w * w == pytest.approx(1.0)


def test_unavailable_without_action_stack() -> None:
    client = NavActionClient(node=None)
    assert client.available() is False


def test_navigate_fails_loudly_without_rclpy() -> None:
    """缺依赖时必须返回「说清楚原因」的失败，而不是抛异常或假装成功。"""
    client = NavActionClient(node=None)
    outcome = client.navigate(x=0.5, y=0.5)

    assert isinstance(outcome, NavOutcome)
    assert outcome.ok is False
    assert outcome.detail
    if not ACTION_AVAILABLE:
        assert "rclpy" in outcome.detail


def test_nav_action_disabled_by_default() -> None:
    """默认关闭：TurtleBot3 的 Gazebo 示例世界不带 Nav2，开了要白等 3 秒。"""
    settings = Settings()
    assert settings.executor.use_nav_action is False
    assert settings.executor.nav_action_name == "/navigate_to_pose"
    assert settings.executor.nav_frame_id == "map"


def test_nav_action_can_be_enabled_by_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ROBOT_AGENT_EXECUTOR__USE_NAV_ACTION", "true")
    monkeypatch.setenv("ROBOT_AGENT_EXECUTOR__NAV_ACTION_NAME", "/custom_nav")
    settings = Settings()
    assert settings.executor.use_nav_action is True
    assert settings.executor.nav_action_name == "/custom_nav"
