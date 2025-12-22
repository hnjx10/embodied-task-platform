"""安全闸门测试：每条策略的边界行为，以及 fail-closed 语义。"""

from __future__ import annotations

import pytest

from robot_agent.config import ArenaSettings, Geofence, SafetySettings
from robot_agent.domain.plan import Plan, PlanStep
from robot_agent.safety.gate import SafetyGate
from robot_agent.safety.policies import PolicyContext, PolicyDecision
from robot_agent.skills.base import SkillRegistry
from robot_agent.skills.mock import build_mock_registry


def make_gate(registry: SkillRegistry | None = None, **safety_kwargs) -> SafetyGate:
    registry = registry or build_mock_registry()[0]
    return SafetyGate(SafetySettings(**safety_kwargs), ArenaSettings(), registry)


def plan_of(*steps: PlanStep) -> Plan:
    return Plan(steps=list(steps), source="rules")


def test_valid_plan_passes() -> None:
    gate = make_gate()
    result = gate.screen(
        plan_of(PlanStep(skill="push_cube", args={"color": "red", "x": -1.2, "y": 0.0}))
    )
    assert result.ok
    assert result.steps[0].args["x"] == pytest.approx(-1.2)


def test_out_of_bounds_blocked() -> None:
    gate = make_gate()
    result = gate.screen(plan_of(PlanStep(skill="goto_point", args={"x": 5.0, "y": 0.0})))
    assert not result.ok
    assert "超出场地边界" in result.first_reason()
    assert result.violations[0].policy == "arena_bounds"


def test_geofence_blocked() -> None:
    gate = make_gate(geofences=[Geofence(name="dock", x=1.0, y=1.0, radius=0.5)])
    result = gate.screen(plan_of(PlanStep(skill="goto_point", args={"x": 1.1, "y": 1.1})))
    assert not result.ok
    assert "禁区" in result.first_reason()


# --------------------------------------------------- 相对位移与嵌套点参数
#
# 这两类参数的共同点是「坐标不在顶层 x/y 上」。策略若只读扁平字段，
# 就会安静地漏过整类指令——演示时看不出来，撞墙时才看出来。


def test_relative_move_beyond_arena_diagonal_blocked() -> None:
    """位移超过场地对角线：无论起点在哪都必然出界，因此不需要位姿也能拒。"""
    gate = make_gate()
    result = gate.screen(
        plan_of(PlanStep(skill="move_relative", args={"dx": 0.0, "dy": 10.0, "speed": 0.15}))
    )
    assert not result.ok
    assert result.violations[0].policy == "arena_bounds"
    assert "对角线" in result.first_reason()


def test_relative_move_within_diagonal_allowed() -> None:
    """对角线以内不越权拒绝——精确判定需要位姿，那是编排层的事，不是闸门猜。"""
    gate = make_gate()
    result = gate.screen(
        plan_of(PlanStep(skill="move_relative", args={"dx": 0.0, "dy": 3.0, "speed": 0.15}))
    )
    assert result.ok


def test_patrol_waypoint_out_of_bounds_blocked() -> None:
    gate = make_gate()
    result = gate.screen(
        plan_of(
            PlanStep(
                skill="patrol",
                args={"waypoints": [{"x": 0.5, "y": 0.5}, {"x": 3.0, "y": 3.0}]},
            )
        )
    )
    assert not result.ok
    assert result.violations[0].policy == "arena_bounds"


def test_patrol_waypoint_in_geofence_blocked() -> None:
    gate = make_gate(geofences=[Geofence(name="dock", x=1.0, y=1.0, radius=0.5)])
    result = gate.screen(
        plan_of(PlanStep(skill="patrol", args={"waypoints": [{"x": 1.1, "y": 1.0}]}))
    )
    assert not result.ok
    assert result.violations[0].policy == "geofence"


def test_patrol_waypoints_in_bounds_allowed() -> None:
    gate = make_gate()
    result = gate.screen(
        plan_of(PlanStep(skill="patrol", args={"waypoints": [{"x": 0.5, "y": 0.5}]}))
    )
    assert result.ok


def test_overspeed_clamped_by_default() -> None:
    gate = make_gate()
    result = gate.screen(
        plan_of(PlanStep(skill="goto_point", args={"x": 0.0, "y": 0.0, "speed": 0.9}))
    )
    assert result.ok
    assert result.steps[0].args["speed"] == pytest.approx(0.22)
    assert result.corrections and result.corrections[0]["policy"] == "speed_limit"


def test_overspeed_rejected_when_clamp_disabled() -> None:
    gate = make_gate(clamp_overspeed=False)
    result = gate.screen(
        plan_of(PlanStep(skill="goto_point", args={"x": 0.0, "y": 0.0, "speed": 0.9}))
    )
    assert not result.ok
    assert "超过上限" in result.first_reason()


def test_negative_speed_rejected() -> None:
    gate = make_gate()
    result = gate.screen(
        plan_of(PlanStep(skill="goto_point", args={"x": 0.0, "y": 0.0, "speed": -1}))
    )
    assert not result.ok


def test_too_slow_speed_lifted_to_minimum() -> None:
    gate = make_gate()
    result = gate.screen(
        plan_of(PlanStep(skill="goto_point", args={"x": 0.0, "y": 0.0, "speed": 0.001}))
    )
    assert result.ok
    assert result.steps[0].args["speed"] == pytest.approx(0.02)


def test_unknown_skill_blocked() -> None:
    gate = make_gate()
    result = gate.screen(plan_of(PlanStep(skill="launch_missile", args={})))
    assert not result.ok
    assert result.violations[0].policy == "skill_allowlist"


def test_allowlist_restricts_skills() -> None:
    gate = make_gate(allowed_skills=["goto_point"])
    result = gate.screen(
        plan_of(PlanStep(skill="push_cube", args={"color": "red", "x": 0, "y": 0}))
    )
    assert not result.ok
    assert "不在允许列表" in result.first_reason()


def test_blocked_keyword_rejects_instruction() -> None:
    gate = make_gate()
    result = gate.screen(
        plan_of(PlanStep(skill="goto_point", args={"x": 0.0, "y": 0.0})),
        instruction="冲向墙壁",
    )
    assert not result.ok
    assert result.violations[0].policy == "blocked_keyword"


def test_step_budget_enforced() -> None:
    gate = make_gate(max_steps=2)
    steps = [PlanStep(skill="goto_point", args={"x": 0.0, "y": 0.0}) for _ in range(3)]
    result = gate.screen(plan_of(*steps))
    assert not result.ok
    assert "超过单次指令上限" in result.first_reason()


def test_empty_plan_rejected() -> None:
    gate = make_gate()
    result = gate.screen(plan_of())
    assert not result.ok
    assert "计划为空" in result.first_reason()


def test_high_risk_requires_approval() -> None:
    gate = make_gate(require_approval_for=["push_cube"])
    result = gate.screen(
        plan_of(PlanStep(skill="push_cube", args={"color": "red", "x": 0, "y": 0}))
    )
    assert result.ok
    assert result.requires_approval is True


def test_policy_exception_is_fail_closed() -> None:
    """策略自身崩溃必须按拒绝处理，不能放行。"""

    class ExplodingPolicy:
        name = "exploding"

        def evaluate(self, step: PlanStep, ctx: PolicyContext) -> PolicyDecision:
            raise RuntimeError("boom")

    registry = build_mock_registry()[0]
    gate = SafetyGate(SafetySettings(), ArenaSettings(), registry, policies=[ExplodingPolicy()])
    result = gate.screen(plan_of(PlanStep(skill="goto_point", args={"x": 0.0, "y": 0.0})))
    assert not result.ok
    assert "安全策略内部错误" in result.first_reason()


def test_custom_policy_can_be_injected() -> None:
    class NoGotoPolicy:
        name = "no_goto"

        def evaluate(self, step: PlanStep, ctx: PolicyContext) -> PolicyDecision:
            if step.skill == "goto_point":
                return PolicyDecision.deny(self.name, "本场景禁止导航")
            return PolicyDecision.pass_(self.name)

    registry = build_mock_registry()[0]
    gate = SafetyGate(SafetySettings(), ArenaSettings(), registry, policies=[NoGotoPolicy()])
    result = gate.screen(plan_of(PlanStep(skill="goto_point", args={"x": 0.0, "y": 0.0})))
    assert not result.ok
    assert result.violations[0].policy == "no_goto"


def test_screen_step_returns_corrected_copy() -> None:
    gate = make_gate()
    step, summary = gate.screen_step(
        PlanStep(skill="goto_point", args={"x": 0.0, "y": 0.0, "speed": 0.5})
    )
    assert summary.allowed
    assert summary.corrected
    assert step.args["speed"] == pytest.approx(0.22)


def test_no_motion_plan_without_coords_passes() -> None:
    gate = make_gate()
    result = gate.screen(plan_of(PlanStep(skill="get_pose", args={"target": "robot"})))
    assert result.ok
