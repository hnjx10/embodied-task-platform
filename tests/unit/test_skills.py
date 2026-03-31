"""技能层测试：参数校验、注册表约束、异常归一化。"""

from __future__ import annotations

import pytest

from robot_agent.errors import SkillExecutionError, SkillNotFoundError
from robot_agent.skills.base import Skill, SkillParams, SkillRegistry, SkillResult
from robot_agent.skills.mock import MockWorld, build_mock_registry


def test_registry_lists_skills(registry: SkillRegistry) -> None:
    names = registry.names()
    # 断言「必须包含哪些能力」，而不是写死全量列表：后者每加一个技能就要改测试，
    # 久而久之会被改成「照着当前输出抄一遍」，失去回归价值。
    assert {"goto_point", "push_cube", "pick_cube", "place_cube", "stop"} <= set(names)
    assert names == sorted(names), "技能清单应保持确定性顺序，便于 prompt 与快照对比"
    assert "launch_missile" not in registry


def test_mock_and_ros2_share_skill_names() -> None:
    """两个后端的技能名必须完全一致。

    编排层是照着技能名做规划的。两端一旦漂移，就会出现「mock 里跑通、上真机
    报 skill not found」——这类问题在本机没有 ROS2 环境时几乎不可能被发现，
    所以用一条不需要 rclpy 的断言把它钉住。
    """
    from robot_agent.skills.ros2 import ros2_skill_names

    mock_registry, _ = build_mock_registry()
    assert mock_registry.names() == ros2_skill_names()


def test_get_unknown_skill_raises(registry: SkillRegistry) -> None:
    with pytest.raises(SkillNotFoundError) as exc:
        registry.get("nope")
    assert "available" in exc.value.details


def test_prompt_description_has_signature(registry: SkillRegistry) -> None:
    text = registry.prompt_description()
    assert "push_cube(color: red|green|blue" in text
    assert "goto_point(x: number, y: number, speed?: number)" in text


def test_extra_params_rejected(registry: SkillRegistry) -> None:
    with pytest.raises(SkillExecutionError) as exc:
        registry.get("goto_point").execute(x=0.0, y=0.0, boom="yes")
    assert "参数非法" in str(exc.value)


def test_missing_params_rejected(registry: SkillRegistry) -> None:
    with pytest.raises(SkillExecutionError):
        registry.get("goto_point").execute(x=0.0)


def test_invalid_color_rejected(registry: SkillRegistry) -> None:
    with pytest.raises(SkillExecutionError):
        registry.get("push_cube").execute(color="purple", x=0.0, y=0.0)


def test_execute_returns_timing(registry: SkillRegistry) -> None:
    result = registry.get("get_pose").execute(target="robot")
    assert result.ok
    assert result.latency_ms >= 0


def test_push_moves_cube(registry: SkillRegistry, world: MockWorld) -> None:
    registry.get("push_cube").execute(color="red", x=0.5, y=0.5)
    assert world.cubes["red"] == (0.5, 0.5)


def test_duplicate_registration_rejected(registry: SkillRegistry) -> None:
    with pytest.raises(ValueError):
        registry.register(registry.get("goto_point"))


def test_override_allowed_explicitly(registry: SkillRegistry) -> None:
    original = registry.get("goto_point")
    registry.register(original, override=True)
    assert registry.get("goto_point") is original


def test_filter_returns_subset() -> None:
    registry, _ = build_mock_registry()
    subset = registry.filter(["goto_point"])
    assert subset.names() == ["goto_point"]
    assert registry.filter([]) is registry


def test_skill_exception_normalized() -> None:
    class BoomParams(SkillParams):
        pass

    class BoomSkill(Skill):
        name = "boom"
        description = "总是失败"
        params_model = BoomParams

        def run(self, **kwargs):  # type: ignore[override]
            raise RuntimeError("硬件掉线")

    with pytest.raises(SkillExecutionError) as exc:
        BoomSkill().execute()
    assert "硬件掉线" in str(exc.value)


def test_skill_without_description_rejected() -> None:
    with pytest.raises(TypeError):

        class NoDesc(Skill):
            name = "nodesc"

            def run(self, **kwargs):  # type: ignore[override]
                return SkillResult(ok=True)


def test_registry_len(registry: SkillRegistry) -> None:
    # 下限而不是等值：技能库会继续长，但「缩水」必须被立刻发现。
    # 21 对应 README 首屏对外给出的技能数——低于它就不是随便加减技能的问题了。
    assert len(registry) >= 21


def test_stop_skill_zeroes_speed(registry: SkillRegistry, world) -> None:
    """急停技能必须把速度真的清零，并留下可观测的命令记录。"""
    registry.get("goto_point").execute(x=0.5, y=0.0, speed=0.2)
    assert world.last_speed > 0

    result = registry.get("stop").execute()

    assert result.ok is True
    assert world.last_speed == 0.0
    assert world.command_history[-1][0] == "stop"


def test_stop_skill_takes_no_params(registry: SkillRegistry) -> None:
    """急停不接受参数：需要填参数的急停，现场根本来不及用。"""
    from robot_agent.errors import SkillExecutionError

    with pytest.raises(SkillExecutionError):
        registry.get("stop").execute(speed=0.0)  # type: ignore[call-arg]
