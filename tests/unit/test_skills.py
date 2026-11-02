"""技能层测试：参数校验、注册表约束、异常归一化。"""

from __future__ import annotations

import pytest

from robot_agent.errors import SkillExecutionError, SkillNotFoundError
from robot_agent.skills.base import Skill, SkillParams, SkillRegistry, SkillResult
from robot_agent.skills.mock import MockWorld, build_mock_registry


def test_registry_lists_skills(registry: SkillRegistry) -> None:
    assert registry.names() == ["get_pose", "goto_point", "push_cube"]
    assert "goto_point" in registry
    assert "launch_missile" not in registry


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
    assert len(registry) == 3
