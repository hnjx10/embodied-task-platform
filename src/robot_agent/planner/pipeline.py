"""规划门面：LLM 优先、规则兜底的降级链。

降级是显式的、有记录的、可度量的：每次降级都会写 warning 并累加
``planning_total{source, outcome}``，运维能直接回答「今天有多少比例走了兜底」。
"""

from __future__ import annotations

from robot_agent import metrics
from robot_agent.config import Settings
from robot_agent.domain.plan import Plan
from robot_agent.errors import LLMError, PlanningError
from robot_agent.logging import get_logger
from robot_agent.planner.llm import LLMPlanner
from robot_agent.planner.rules import RulePlanner
from robot_agent.skills.base import SkillRegistry

logger = get_logger(__name__)


class Planner:
    """按配置装配规划器。同一实例可被多任务并发复用（无状态）。"""

    def __init__(
        self,
        settings: Settings,
        registry: SkillRegistry,
        *,
        force_rules: bool = False,
    ) -> None:
        self.settings = settings
        self.registry = registry
        self.rules = RulePlanner(
            arena_half_size=settings.arena.half_size,
            default_speed=settings.safety.default_speed,
        )
        self.llm: LLMPlanner | None = None
        self.force_rules = force_rules

        if not force_rules and settings.llm.configured:
            try:
                self.llm = LLMPlanner(
                    settings.llm,
                    registry,
                    arena_half_size=settings.arena.half_size,
                    max_steps=settings.safety.max_steps,
                )
            except LLMError as exc:  # pragma: no cover - 构造期失败直接降级
                logger.warning("llm_disabled", error=str(exc))

    @property
    def llm_available(self) -> bool:
        return self.llm is not None

    def plan(self, instruction: str) -> Plan:
        """生成计划。LLM 失败且允许降级时返回规则计划，并在 warnings 里留痕。"""
        instruction = (instruction or "").strip()
        if not instruction:
            raise PlanningError("指令为空")
        if len(instruction) > 500:
            raise PlanningError("指令过长（上限 500 字符）", details={"length": len(instruction)})

        if self.llm is not None:
            try:
                return self.llm.plan(instruction)
            except (LLMError, PlanningError) as exc:
                if not self.settings.llm.fallback_to_rules:
                    raise
                logger.warning(
                    "planning_fallback_to_rules",
                    reason=str(exc),
                    error_code=getattr(exc, "code", ""),
                )
                plan = self._rule_plan(instruction)
                plan.warnings.append(f"LLM 规划失败已降级规则解析: {exc}")
                return plan

        return self._rule_plan(instruction)

    # ------------------------------------------------------------------ 内部

    def _rule_plan(self, instruction: str) -> Plan:
        steps = self.rules.plan(instruction)
        metrics.PLANNING_TOTAL.labels(source="rules", outcome="ok").inc()
        return Plan(steps=steps, source="rules", model=None, latency_ms=0.0)
