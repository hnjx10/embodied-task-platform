"""安全策略集合。

每个策略是一个独立可测的小函数：输入「步骤 + 上下文」，输出「放行 / 修正 / 拒绝」。
新增约束（例：禁止夜间作业、限制单次位移距离）只需加一个类并在
:class:`~robot_agent.safety.gate.SafetyGate` 的默认链路里注册，主流程零改动。

策略的返回语义：

* ``allowed=True``  —— 放行，``fixed_args`` 可携带修正后的参数（自动降速）
* ``allowed=False`` —— 拒绝，``reason`` 会原样写进审计与用户可见的错误信息
* ``requires_approval=True`` —— 动作本身合法，但必须人工确认后才执行
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Protocol

from robot_agent.config import ArenaSettings, SafetySettings
from robot_agent.planner.base import PlanStep
from robot_agent.skills.base import SkillRegistry


@dataclass(frozen=True)
class PolicyContext:
    """策略求值所需的只读上下文。"""

    instruction: str
    safety: SafetySettings
    arena: ArenaSettings
    registry: SkillRegistry
    task_id: str = ""


@dataclass
class PolicyDecision:
    """策略结论。"""

    allowed: bool = True
    policy: str = ""
    reason: str = ""
    fixed_args: dict[str, Any] | None = None
    requires_approval: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def deny(cls, policy: str, reason: str, **metadata: Any) -> PolicyDecision:
        return cls(allowed=False, policy=policy, reason=reason, metadata=metadata)

    @classmethod
    def pass_(
        cls, policy: str, fixed_args: dict[str, Any] | None = None, **metadata: Any
    ) -> PolicyDecision:
        return cls(allowed=True, policy=policy, fixed_args=fixed_args, metadata=metadata)


class Policy(Protocol):
    """策略协议。"""

    name: str

    def evaluate(self, step: PlanStep, ctx: PolicyContext) -> PolicyDecision: ...


# --------------------------------------------------------------------- 策略


class SkillAllowlistPolicy:
    """技能必须存在且在白名单内。防止 LLM 幻觉出未注册/被禁用的能力。"""

    name = "skill_allowlist"

    def evaluate(self, step: PlanStep, ctx: PolicyContext) -> PolicyDecision:
        if not ctx.registry.has(step.skill):
            return PolicyDecision.deny(
                self.name,
                f"技能 '{step.skill}' 不存在，可用技能: {ctx.registry.names()}",
                skill=step.skill,
            )
        allowed = ctx.safety.allowed_skills
        if allowed and step.skill not in allowed:
            return PolicyDecision.deny(
                self.name, f"技能 '{step.skill}' 不在允许列表中", skill=step.skill
            )
        return PolicyDecision.pass_(self.name)


class BlockedKeywordPolicy:
    """指令级关键词黑名单。命中即拒绝整条指令，属于「人话层面的红线」。"""

    name = "blocked_keyword"

    def evaluate(self, step: PlanStep, ctx: PolicyContext) -> PolicyDecision:
        hits = [w for w in ctx.safety.blocked_keywords if w and w in ctx.instruction]
        if hits:
            return PolicyDecision.deny(
                self.name,
                f"指令包含高风险表述 {hits}，拒绝执行",
                keywords=hits,
            )
        return PolicyDecision.pass_(self.name)


class ArenaBoundsPolicy:
    """目标点必须落在场地内。"""

    name = "arena_bounds"

    def evaluate(self, step: PlanStep, ctx: PolicyContext) -> PolicyDecision:
        coords = _extract_xy(step)
        if coords is None:
            return PolicyDecision.pass_(self.name)
        x, y = coords
        half = ctx.arena.half_size
        if not (-half <= x <= half and -half <= y <= half):
            return PolicyDecision.deny(
                self.name,
                f"目标点 ({x:.2f}, {y:.2f}) 超出场地边界 ±{half}m",
                x=x,
                y=y,
            )
        return PolicyDecision.pass_(self.name)


class GeofencePolicy:
    """目标点不得落在禁区内。"""

    name = "geofence"

    def evaluate(self, step: PlanStep, ctx: PolicyContext) -> PolicyDecision:
        coords = _extract_xy(step)
        if coords is None:
            return PolicyDecision.pass_(self.name)
        x, y = coords
        for fence in ctx.safety.geofences:
            if fence.contains(x, y):
                return PolicyDecision.deny(
                    self.name,
                    f"目标点 ({x:.2f}, {y:.2f}) 位于禁区 '{fence.name}' 内",
                    x=x,
                    y=y,
                    geofence=fence.name,
                )
        return PolicyDecision.pass_(self.name)


class SpeedLimitPolicy:
    """线速度上限。默认自动降速而非直接拒绝——风险可控时保留任务可用性。"""

    name = "speed_limit"

    def evaluate(self, step: PlanStep, ctx: PolicyContext) -> PolicyDecision:
        if "speed" not in step.args:
            return PolicyDecision.pass_(self.name)
        raw = step.args["speed"]
        try:
            speed = float(raw)
        except (TypeError, ValueError):
            return PolicyDecision.deny(self.name, f"速度参数非法: {raw!r}", speed=raw)

        if speed <= 0:
            return PolicyDecision.deny(self.name, f"速度必须为正数，收到 {speed}", speed=speed)

        limit = ctx.safety.max_linear_speed
        if speed > limit:
            if ctx.safety.clamp_overspeed:
                clamped = {**step.args, "speed": limit}
                return PolicyDecision.pass_(
                    self.name,
                    fixed_args=clamped,
                    clamped_from=speed,
                    clamped_to=limit,
                )
            return PolicyDecision.deny(
                self.name, f"请求速度 {speed:.2f} m/s 超过上限 {limit:.2f} m/s", speed=speed
            )

        if speed < ctx.safety.min_linear_speed:
            # 过低速度会导致底盘死区不动作，抬到最小值而不是让任务静默卡住
            return PolicyDecision.pass_(
                self.name,
                fixed_args={**step.args, "speed": ctx.safety.min_linear_speed},
                clamped_from=speed,
                clamped_to=ctx.safety.min_linear_speed,
            )
        return PolicyDecision.pass_(self.name)


class StepDistancePolicy:
    """单次位移不得超过配置上限（默认场地对角线，可按现场收紧）。"""

    name = "step_distance"

    def __init__(self, max_distance: float | None = None) -> None:
        self._max = max_distance

    def evaluate(self, step: PlanStep, ctx: PolicyContext) -> PolicyDecision:
        coords = _extract_xy(step)
        if coords is None or self._max is None:
            return PolicyDecision.pass_(self.name)
        dist = math.hypot(*coords)
        if dist > self._max:
            return PolicyDecision.deny(
                self.name,
                f"单步位移 {dist:.2f}m 超过上限 {self._max:.2f}m，请拆分为多步",
                distance=round(dist, 3),
            )
        return PolicyDecision.pass_(self.name)


class ApprovalPolicy:
    """高风险技能/高风险动作需要人工确认。不是拒绝，而是挂起等待审批。"""

    name = "approval"

    def evaluate(self, step: PlanStep, ctx: PolicyContext) -> PolicyDecision:
        must_approve = (
            step.skill in ctx.safety.require_approval_for or _skill_risk(ctx, step.skill) == "high"
        )
        if not must_approve:
            return PolicyDecision.pass_(self.name)
        return PolicyDecision(
            allowed=True,
            policy=self.name,
            reason=f"技能 '{step.skill}' 属于高风险动作，需要人工确认",
            requires_approval=True,
        )


# ----------------------------------------------------------------- 辅助函数


def _extract_xy(step: PlanStep) -> tuple[float, float] | None:
    """从参数里取坐标；非坐标型技能返回 None（由其它策略负责）。"""
    x, y = step.args.get("x"), step.args.get("y")
    if x is None or y is None:
        return None
    try:
        return float(x), float(y)
    except (TypeError, ValueError):
        return None


def _skill_risk(ctx: PolicyContext, name: str) -> str:
    if not ctx.registry.has(name):
        return "high"
    return ctx.registry.get(name).risk


def default_policies() -> list[Policy]:
    """默认策略链。顺序有意义：先查技能合法性，再查物理约束，最后看是否需要审批。"""
    return [
        SkillAllowlistPolicy(),
        BlockedKeywordPolicy(),
        ArenaBoundsPolicy(),
        GeofencePolicy(),
        SpeedLimitPolicy(),
        ApprovalPolicy(),
    ]
