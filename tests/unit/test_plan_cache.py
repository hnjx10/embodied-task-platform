"""规划缓存与下发前二次校验。

这两条都是「文档里承诺了、代码里必须真的做到」的能力，因此测试的重点不是覆盖率，
而是把**容易错的那一面**钉死：

* 缓存进出必须深拷贝——安全闸门会就地改步骤参数，共享对象会把修正写回缓存；
* 缓存 key 必须带配置指纹——换了技能库还命中旧计划，演示时看不出来；
* 下发前校验必须使用**修正后**的步骤——校验完仍按原参数下发等于没校验。
"""

from __future__ import annotations

import pytest

from robot_agent.config import Settings
from robot_agent.domain.plan import Plan, PlanStep
from robot_agent.orchestration.executor import StepExecutor
from robot_agent.planner.cache import PlanCache, make_key, normalize_instruction
from robot_agent.planner.pipeline import Planner
from robot_agent.safety.gate import SafetyGate
from robot_agent.skills.base import Skill, SkillParams, SkillRegistry, SkillResult
from robot_agent.skills.mock import build_mock_registry


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def make_plan(speed: float = 0.15) -> Plan:
    return Plan(
        steps=[PlanStep(skill="goto_point", args={"x": 0.3, "y": 0.3, "speed": speed})],
        source="rules",
    )


# ------------------------------------------------------------------ 缓存本体


def test_cache_hit_returns_equal_but_distinct_object() -> None:
    """命中的必须是副本：调用方改一个字段不能影响下一次命中。"""
    cache = PlanCache(max_entries=4, ttl_s=60.0)
    cache.put("k", make_plan())
    first = cache.get("k")
    assert first is not None
    first.steps[0].args["speed"] = 0.99
    second = cache.get("k")
    assert second is not None
    assert second.steps[0].args["speed"] == pytest.approx(0.15)


def test_cache_put_is_isolated_from_caller_mutation() -> None:
    """存入之后再改原对象，缓存里不能跟着变——闸门就地修正参数正是这个场景。"""
    cache = PlanCache(max_entries=4, ttl_s=60.0)
    plan = make_plan()
    cache.put("k", plan)
    plan.steps[0].args["speed"] = 0.05
    hit = cache.get("k")
    assert hit is not None
    assert hit.steps[0].args["speed"] == pytest.approx(0.15)


def test_cache_expires_after_ttl() -> None:
    clock = FakeClock()
    cache = PlanCache(max_entries=4, ttl_s=10.0, clock=clock)
    cache.put("k", make_plan())
    clock.now += 9.0
    assert cache.get("k") is not None
    clock.now += 2.0
    assert cache.get("k") is None, "过期条目必须真的取不到，否则命中率指标会虚高"


def test_cache_evicts_least_recently_used() -> None:
    cache = PlanCache(max_entries=2, ttl_s=60.0)
    cache.put("a", make_plan())
    cache.put("b", make_plan())
    assert cache.get("a") is not None  # a 变成最近使用
    cache.put("c", make_plan())  # 淘汰 b
    assert cache.get("a") is not None
    assert cache.get("b") is None
    assert cache.get("c") is not None


def test_cache_disabled_when_zero_entries() -> None:
    cache = PlanCache(max_entries=0, ttl_s=60.0)
    cache.put("k", make_plan())
    assert cache.get("k") is None
    assert not cache.enabled


def test_empty_plan_is_not_cached() -> None:
    """空计划缓存在了下一次也还是空——缓存它只会让失败看起来更稳定。"""
    cache = PlanCache(max_entries=4, ttl_s=60.0)
    cache.put("k", Plan(steps=[], source="rules"))
    assert cache.get("k") is None


def test_key_ignores_whitespace_and_case() -> None:
    assert normalize_instruction("  把 红色方块  推到左边 ") == "把 红色方块 推到左边"
    assert make_key("Push Red", "fp") == make_key("push red", "fp")


def test_key_changes_with_fingerprint() -> None:
    assert make_key("往前开一米", "skills=A") != make_key("往前开一米", "skills=B")


# ------------------------------------------------------------- 与 Planner 集成


def _planner(**cache_overrides) -> Planner:
    settings = Settings(
        env="test",
        storage={"url": "memory"},
        plan_cache={"enabled": True, "max_entries": 8, "ttl_s": 60.0, **cache_overrides},
    )
    registry, _ = build_mock_registry(settings.arena)
    return Planner(settings, registry, force_rules=True)


def test_second_identical_instruction_is_served_from_cache() -> None:
    planner = _planner()
    first = planner.plan("把红色方块推到左边")
    second = planner.plan("  把红色方块推到左边  ")

    assert first.cached is False
    assert second.cached is True, "第二次必须走缓存，否则高频指令仍然每次付一次模型调用"
    assert second.steps[0].skill == first.steps[0].skill
    assert second.source == first.source


def test_cache_can_be_disabled() -> None:
    """评测与单测要能关掉缓存：否则同一个用例跑两遍，第二遍测的是缓存不是逻辑。"""
    planner = _planner(enabled=False)
    planner.plan("把红色方块推到左边")
    again = planner.plan("把红色方块推到左边")
    assert again.cached is False


# --------------------------------------------------------- 下发前二次校验


class SpeedParams(SkillParams):
    speed: float = 0.15


class SpySkill(Skill):
    """记录实际下发的参数，用来验证「修正后的参数真的生效了」。"""

    name = "spy"
    description = "测试用技能：把收到的参数记下来"
    params_model = SpeedParams

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def run(self, speed: float = 0.15) -> SkillResult:
        self.calls.append({"speed": speed})
        return SkillResult(ok=True, detail="ran")


def _registry_with_spy() -> tuple[SkillRegistry, SpySkill]:
    spy = SpySkill()
    registry = SkillRegistry()
    registry.register(spy)
    return registry, spy


def test_pre_dispatch_denial_never_reaches_the_skill() -> None:
    """被拒的步骤不能有任何物理后果——技能一次都不该被执行。"""
    registry, spy = _registry_with_spy()
    settings = Settings(env="test", executor={"backend": "mock"})
    executor = StepExecutor(
        registry,
        settings.executor,
        pre_dispatch=lambda step: (step, "目标点位于禁区"),
    )

    outcomes = executor.execute_plan([PlanStep(skill="spy", args={"speed": 0.15})], task_id="t1")

    assert spy.calls == [], "被拦下的步骤绝不能落到硬件上"
    assert outcomes[0].outcome == "blocked"
    assert "禁区" in outcomes[0].detail


def test_pre_dispatch_uses_corrected_step() -> None:
    """限速策略把 0.9 改成 0.22，执行器必须用 0.22 下发，而不是原参数。"""
    registry, spy = _registry_with_spy()
    settings = Settings(env="test", executor={"backend": "mock"})
    gate = SafetyGate(settings.safety, settings.arena, registry)

    def check(step: PlanStep) -> tuple[PlanStep, str | None]:
        corrected, summary = gate.screen_step(step)
        return corrected, (None if summary.allowed else summary.reason)

    executor = StepExecutor(registry, settings.executor, pre_dispatch=check)
    executor.execute_plan([PlanStep(skill="spy", args={"speed": 0.9})], task_id="t2")

    assert spy.calls, "步骤应当被执行"
    assert spy.calls[0]["speed"] == pytest.approx(settings.safety.max_linear_speed)
