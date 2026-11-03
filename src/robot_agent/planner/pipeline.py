"""规划门面：缓存 → LLM 优先 → 规则兜底的降级链。

三级路径，每一级的耗时差两个数量级：

1. **缓存命中**（亚毫秒）——同一句指令的重复请求，高频意图走这条；
2. **LLM 规划**（数百毫秒到数秒）——首次出现的指令；
3. **规则兜底**（亚毫秒）——模型不可用或输出不合法。

降级是显式的、有记录的、可度量的：每次降级都会写 warning 并累加
``planning_total{source, outcome}``，运维能直接回答「今天有多少比例走了兜底」。
"""

from __future__ import annotations

import time

from robot_agent import metrics
from robot_agent.config import Settings
from robot_agent.domain.plan import Plan
from robot_agent.errors import LLMError, PlanningError
from robot_agent.logging import get_logger
from robot_agent.planner.cache import PlanCache, make_key
from robot_agent.planner.llm import LLMPlanner
from robot_agent.planner.rules import RulePlanner
from robot_agent.skills.base import SkillRegistry

logger = get_logger(__name__)


class Planner:
    """按配置装配规划器。同一实例可被多任务并发复用（缓存本身线程安全）。"""

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

        cache_cfg = settings.plan_cache
        self.cache = PlanCache(
            max_entries=cache_cfg.max_entries if cache_cfg.enabled else 0,
            ttl_s=cache_cfg.ttl_s if cache_cfg.enabled else 0.0,
        )
        # 技能库或场地变了，旧计划就不再有效——把这两者折进 key 的指纹里
        self._fingerprint = "|".join(
            [
                ",".join(sorted(registry.names())),
                f"half={settings.arena.half_size}",
                f"speed={settings.safety.default_speed}",
            ]
        )

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
        """生成计划。命中缓存直接返回，否则走 LLM，失败再降级规则。"""
        instruction = (instruction or "").strip()
        if not instruction:
            raise PlanningError("指令为空")
        if len(instruction) > 500:
            raise PlanningError("指令过长（上限 500 字符）", details={"length": len(instruction)})

        started = time.perf_counter()
        key = make_key(instruction, self._fingerprint)
        hit = self.cache.get(key)
        if hit is not None:
            metrics.PLAN_CACHE_TOTAL.labels(result="hit").inc()
            hit.cached = True
            hit.latency_ms = (time.perf_counter() - started) * 1000.0
            logger.info("plan_cache_hit", steps=len(hit.steps), source=hit.source)
            return hit

        metrics.PLAN_CACHE_TOTAL.labels(result="miss").inc()
        plan = self._plan_uncached(instruction)
        # 存的是深拷贝：闸门会就地修正步骤参数（限速），共享对象会把修正写回缓存
        self.cache.put(key, plan)
        return plan

    # ------------------------------------------------------------------ 内部

    def _plan_uncached(self, instruction: str) -> Plan:
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

    def _rule_plan(self, instruction: str) -> Plan:
        steps = self.rules.plan(instruction)
        metrics.PLANNING_TOTAL.labels(source="rules", outcome="ok").inc()
        return Plan(steps=steps, source="rules", model=None, latency_ms=0.0)
