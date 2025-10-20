"""安全闸门：把策略链组合成一次「计划级 + 步骤级」的原子校验。

编排层只与 :meth:`SafetyGate.screen` 打交道，拿到的要么是「可执行且已修正的步骤
列表」，要么是「拒绝原因」。它不关心具体是哪条策略拦的，但原因会完整写进审计。

关键约定：**校验必须发生在执行之前，且一次失败的校验绝不允许部分执行**。
这是本模块存在的全部理由——宁可不动，不可误动。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from robot_agent import metrics
from robot_agent.config import ArenaSettings, SafetySettings
from robot_agent.domain.plan import Plan, PlanStep
from robot_agent.logging import get_logger
from robot_agent.safety.policies import (
    Policy,
    PolicyContext,
    PolicyDecision,
    default_policies,
)
from robot_agent.skills.base import SkillRegistry

logger = get_logger(__name__)


@dataclass
class Violation:
    """一条拒绝记录。``index`` 为步骤下标，-1 表示计划级约束（如步数超限）。"""

    index: int
    policy: str
    reason: str
    step: PlanStep | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "policy": self.policy,
            "reason": self.reason,
            "step": self.step.label() if self.step else None,
        }


@dataclass
class ScreeningResult:
    """校验结果。``ok=False`` 时 ``steps`` 仍返回已通过部分，仅用于审计展示。"""

    ok: bool
    steps: list[PlanStep] = field(default_factory=list)
    violations: list[Violation] = field(default_factory=list)
    corrections: list[dict[str, Any]] = field(default_factory=list)
    requires_approval: bool = False
    approval_reasons: list[str] = field(default_factory=list)

    def first_reason(self) -> str:
        return self.violations[0].reason if self.violations else ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "violations": [v.to_dict() for v in self.violations],
            "corrections": self.corrections,
            "requires_approval": self.requires_approval,
        }


class SafetyGate:
    """策略链执行器。"""

    def __init__(
        self,
        settings: SafetySettings,
        arena: ArenaSettings,
        registry: SkillRegistry,
        policies: list[Policy] | None = None,
    ) -> None:
        self.settings = settings
        self.arena = arena
        self.registry = registry
        self.policies: list[Policy] = policies if policies is not None else default_policies()

    # ------------------------------------------------------------------ 计划级

    def screen(self, plan: Plan, *, instruction: str = "", task_id: str = "") -> ScreeningResult:
        """整计划校验。任何一步被拒则整体拒绝（不做部分执行）。"""
        result = ScreeningResult(ok=True)
        ctx = PolicyContext(
            instruction=instruction,
            safety=self.settings,
            arena=self.arena,
            registry=self.registry,
            task_id=task_id,
        )

        # 0) 计划级约束：非空 + 步数上限
        if not plan.steps:
            result.ok = False
            result.violations.append(Violation(-1, "plan_shape", "计划为空，没有可执行的技能步骤"))
            return result
        if len(plan.steps) > self.settings.max_steps:
            result.ok = False
            result.violations.append(
                Violation(
                    -1,
                    "plan_shape",
                    f"计划包含 {len(plan.steps)} 步，超过单次指令上限 {self.settings.max_steps} 步",
                )
            )
            metrics.SAFETY_BLOCKED_TOTAL.labels(policy="plan_shape").inc()
            return result

        # 1) 逐步过策略链
        screened: list[PlanStep] = []
        for idx, step in enumerate(plan.steps):
            current = step
            for policy in self.policies:
                decision = self._safe_evaluate(policy, current, ctx)
                if decision is None:
                    continue

                if not decision.allowed:
                    result.ok = False
                    result.violations.append(
                        Violation(idx, decision.policy or policy.name, decision.reason, current)
                    )
                    metrics.SAFETY_BLOCKED_TOTAL.labels(policy=decision.policy).inc()
                    logger.warning(
                        "safety_blocked",
                        task_id=task_id,
                        step_index=idx,
                        skill=current.skill,
                        policy=decision.policy,
                        reason=decision.reason,
                    )
                    return result

                if decision.requires_approval:
                    result.requires_approval = True
                    if decision.reason not in result.approval_reasons:
                        result.approval_reasons.append(decision.reason)

                if decision.fixed_args is not None and decision.fixed_args != current.args:
                    result.corrections.append(
                        {
                            "index": idx,
                            "policy": decision.policy,
                            "before": dict(current.args),
                            "after": dict(decision.fixed_args),
                            **decision.metadata,
                        }
                    )
                    metrics.SAFETY_CLAMPED_TOTAL.labels(policy=decision.policy).inc()
                    current = current.model_copy(update={"args": decision.fixed_args})

            screened.append(current)

        result.steps = screened
        return result

    # ------------------------------------------------------------------ 步骤级

    def screen_step(
        self, step: PlanStep, *, instruction: str = "", task_id: str = ""
    ) -> tuple[PlanStep, StrategySummary]:
        """执行前的二次校验。

        为什么执行前还要再查一遍？因为步骤可能在审批环节被人为修改（人工接管），
        而人工修改同样必须过策略链——否则审批就成了一条绕开安全层的通道。
        """
        ctx = PolicyContext(
            instruction=instruction,
            safety=self.settings,
            arena=self.arena,
            registry=self.registry,
            task_id=task_id,
        )
        summary = StrategySummary()
        current = step
        for policy in self.policies:
            decision = self._safe_evaluate(policy, current, ctx)
            if decision is None:
                continue
            if not decision.allowed:
                summary.allowed = False
                summary.policy = decision.policy or policy.name
                summary.reason = decision.reason
                metrics.SAFETY_BLOCKED_TOTAL.labels(policy=summary.policy).inc()
                return current, summary
            if decision.fixed_args is not None and decision.fixed_args != current.args:
                summary.corrected = True
                summary.corrections.append({"policy": decision.policy, **decision.metadata})
                current = current.model_copy(update={"args": decision.fixed_args})
            if decision.requires_approval:
                summary.requires_approval = True
        return current, summary

    # ------------------------------------------------------------------ 内部

    def _safe_evaluate(
        self, policy: Policy, step: PlanStep, ctx: PolicyContext
    ) -> PolicyDecision | None:
        """策略自身抛异常时按「拒绝」处理（fail-closed）。

        安全组件出故障时选择放行，等于把安全组件变成攻击面。默认拒绝更保守。
        """
        try:
            return policy.evaluate(step, ctx)
        except Exception as exc:
            logger.error(
                "safety_policy_error",
                policy=getattr(policy, "name", type(policy).__name__),
                error=str(exc),
                exc_info=True,
            )
            return PolicyDecision.deny(
                getattr(policy, "name", "unknown"),
                f"安全策略内部错误，按拒绝处理: {exc}",
            )


@dataclass
class StrategySummary:
    """单步校验摘要。"""

    allowed: bool = True
    policy: str = ""
    reason: str = ""
    corrected: bool = False
    requires_approval: bool = False
    corrections: list[dict[str, Any]] = field(default_factory=list)
