"""下发前的位姿新鲜度校验。

这条链是「ROS2 状态回调 → 安全层 → 执行器」的唯一交叉点。它存在的理由是
一种**不报错的故障**：``/odom`` 断了以后，ROS2 桥里的位姿会安静地停在最后
一次收到的值上，之后的「再往前 3 米」就拿一条几分钟前的位姿去算目标点。
没有任何异常，只有位置慢慢偏离——所以只能靠时间戳发现。

测试要钉住四件事：

* 位姿过期 / 从未收到 → 移动动作必须拒；
* 位姿新鲜 → 不能误拦（安全系统只报召回不报误拦，等于鼓励「一律拒绝」）；
* **急停 `stop` 永不被这条规则拦**——位姿过期时最该能执行的恰恰是停下来；
* 被拒的步骤一次都不能落到技能上。
"""

from __future__ import annotations

import pytest

from robot_agent.config import Settings
from robot_agent.domain.plan import PlanStep
from robot_agent.factory import MOTION_SKILLS, _pre_dispatch
from robot_agent.orchestration.executor import StepExecutor
from robot_agent.safety.gate import SafetyGate
from robot_agent.skills.mock import build_mock_registry


def _executor(age: float | None, **overrides: object) -> StepExecutor:
    settings = Settings(env="test", executor={"backend": "mock"}, **overrides)  # type: ignore[arg-type]
    registry, _world = build_mock_registry(settings.arena)
    gate = SafetyGate(settings.safety, settings.arena, registry)
    check = _pre_dispatch(
        gate,
        pose_age=lambda: age,
        stale_s=settings.safety.pose_stale_s,
    )
    return StepExecutor(registry, settings.executor, pre_dispatch=check)


def _move() -> PlanStep:
    return PlanStep(skill="move_relative", args={"dx": 0.0, "dy": 0.3, "speed": 0.15})


# ------------------------------------------------------------------ 拦截


def test_stale_pose_blocks_motion() -> None:
    """位姿 5 秒没更新，默认阈值 2 秒——不能下发，否则在用过期的自己。"""
    executor = _executor(age=5.0)
    outcomes = executor.execute_plan([_move()], task_id="stale")

    assert outcomes[0].outcome == "blocked"
    assert outcomes[0].attempts == 0, "被拦的步骤不该有任何一次尝试"
    assert "过期" in outcomes[0].detail


def test_never_received_pose_blocks_motion() -> None:
    """从未收到过位姿（None）和「收到了但很旧」是两回事，报错要能区分。"""
    executor = _executor(age=None)
    outcomes = executor.execute_plan([_move()], task_id="never")

    assert outcomes[0].outcome == "blocked"
    assert "未收到" in outcomes[0].detail


def test_blocked_step_never_reaches_the_skill() -> None:
    """被拒之后世界状态必须一动不动——这是「阻断下发」的字面意思。"""
    settings = Settings(env="test", executor={"backend": "mock"})
    registry, world = build_mock_registry(settings.arena)
    gate = SafetyGate(settings.safety, settings.arena, registry)
    executor = StepExecutor(
        registry,
        settings.executor,
        pre_dispatch=_pre_dispatch(gate, pose_age=lambda: 9.0),
    )
    before = world.robot

    executor.execute_plan([_move()], task_id="no-move")

    assert world.robot == before


# ------------------------------------------------------------------ 放行


def test_fresh_pose_is_not_blocked() -> None:
    """新鲜位姿必须放行。误拦率 0 不是锦上添花，是这条规则能上线的前提。"""
    executor = _executor(age=0.05)
    outcomes = executor.execute_plan([_move()], task_id="fresh")

    assert outcomes[0].outcome == "ok", outcomes[0].detail


def test_threshold_comes_from_settings() -> None:
    """阈值可配：调小到 0.1s 后，0.5s 的位姿就该拒。"""
    executor = _executor(age=0.5, safety={"pose_stale_s": 0.1})
    outcomes = executor.execute_plan([_move()], task_id="tight")

    assert outcomes[0].outcome == "blocked"


def test_emergency_stop_survives_stale_pose() -> None:
    """急停不受位姿新鲜度限制。

    这是整条规则最容易被写反的地方：把 ``stop`` 也塞进 MOTION_SKILLS，看起来
    「更严格」，实际是在位姿过期时同时剥夺了停车能力——安全规则制造了危险。
    """
    assert "stop" not in MOTION_SKILLS

    executor = _executor(age=999.0)
    outcomes = executor.execute_plan([PlanStep(skill="stop", args={})], task_id="estop")

    assert outcomes[0].outcome == "ok", outcomes[0].detail


def test_query_skills_are_not_gated_by_pose() -> None:
    """查电量不需要位姿，位姿过期时也应该能查——不然连「为什么不动」都问不出来。"""
    executor = _executor(age=999.0)
    outcomes = executor.execute_plan([PlanStep(skill="get_battery", args={})], task_id="batt")

    assert outcomes[0].outcome == "ok", outcomes[0].detail


# ------------------------------------------------------------------ 与闸门共存


def test_pose_check_runs_before_the_gate() -> None:
    """位姿过期时先报位姿，不要报一个「参数越界」——否则排查方向会被带偏。"""
    executor = _executor(age=999.0)
    step = PlanStep(skill="goto_point", args={"x": 99.0, "y": 99.0, "speed": 0.15})
    outcomes = executor.execute_plan([step], task_id="order")

    assert outcomes[0].outcome == "blocked"
    assert "过期" in outcomes[0].detail


def test_gate_corrections_still_apply_when_pose_is_fresh() -> None:
    """位姿校验只是加在前面，闸门的限速修正照旧生效，顺序不能互相顶掉。"""
    executor = _executor(age=0.0)
    step = PlanStep(skill="move_relative", args={"dx": 0.0, "dy": 0.3, "speed": 0.9})
    outcomes = executor.execute_plan([step], task_id="clamp")

    assert outcomes[0].outcome == "ok", outcomes[0].detail
    assert outcomes[0].attempts == 1


@pytest.mark.parametrize("skill", sorted(MOTION_SKILLS))
def test_every_motion_skill_is_covered(skill: str) -> None:
    """名单里的每个技能都真的会被拦——避免出现「加进常量但拼错名字」。"""
    executor = _executor(age=999.0)
    # 参数留空：这一步在闸门那层会因为 schema 报错，但位姿检查必须在它之前命中
    outcomes = executor.execute_plan([PlanStep(skill=skill, args={})], task_id=f"cov-{skill}")

    assert outcomes[0].outcome == "blocked"
    assert "过期" in outcomes[0].detail
