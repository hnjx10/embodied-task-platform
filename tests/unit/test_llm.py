"""LLM 规划器测试：不联网，用假客户端驱动全部关键分支。

重点不是「能不能解析正确 JSON」，而是**出错时会发生什么**：
可重试错误是否重试、不可重试错误是否立刻放弃、连续失败是否熔断、
模型幻觉出的技能是否被拒、解析失败是否降级到规则。
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from robot_agent.config import LLMSettings, Settings
from robot_agent.errors import (
    LLMError,
    PlanningError,
    PlanValidationError,
)
from robot_agent.planner.llm import CircuitBreaker, LLMPlanner
from robot_agent.planner.pipeline import Planner
from robot_agent.skills.mock import build_mock_registry


def _response(content: str, prompt_tokens: int = 100, completion_tokens: int = 30) -> Any:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        usage=SimpleNamespace(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens),
    )


class FakeCompletions:
    """按顺序返回预置响应；元素是异常就抛出。"""

    def __init__(self, script: list[Any]) -> None:
        self.script = script
        self.calls = 0
        self.last_kwargs: dict[str, Any] = {}

    def create(self, **kwargs: Any) -> Any:
        self.last_kwargs = kwargs
        item = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        if isinstance(item, Exception):
            raise item
        return item


def make_planner(script: list[Any], **llm_kwargs: Any) -> tuple[LLMPlanner, FakeCompletions]:
    registry, _ = build_mock_registry()
    settings = LLMSettings(api_key="sk-test", max_attempts=2, **llm_kwargs)
    planner = LLMPlanner(settings, registry)
    fake = FakeCompletions(script)
    planner._client = SimpleNamespace(chat=SimpleNamespace(completions=fake))
    return planner, fake


GOOD = json.dumps(
    {"steps": [{"skill": "push_cube", "args": {"color": "red", "x": -1.2, "y": 0.0}}]}
)


# ------------------------------------------------------------------ 正常路径


def test_parses_valid_plan() -> None:
    planner, fake = make_planner([_response(GOOD)])
    plan = planner.plan("把红色方块推到左边")
    assert plan.source == "llm"
    assert len(plan.steps) == 1
    assert plan.steps[0].skill == "push_cube"
    assert plan.steps[0].args["x"] == -1.2
    assert plan.latency_ms > 0
    assert fake.calls == 1


def test_prompt_includes_skill_signatures() -> None:
    planner, fake = make_planner([_response(GOOD)])
    planner.plan("把红色方块推到左边")
    system_prompt = fake.last_kwargs["messages"][0]["content"]
    assert "push_cube" in system_prompt
    assert "red|green|blue" in system_prompt


def test_accepts_fenced_json() -> None:
    fenced = "```json\n" + GOOD + "\n```"
    planner, _ = make_planner([_response(fenced)])
    assert planner.plan("把红色方块推到左边").steps[0].skill == "push_cube"


def test_accepts_json_with_prose() -> None:
    noisy = "好的，计划如下：\n" + GOOD + "\n希望有帮助。"
    planner, _ = make_planner([_response(noisy)])
    assert len(planner.plan("把红色方块推到左边").steps) == 1


# ------------------------------------------------------------------ 输出校验


def test_rejects_unknown_skill() -> None:
    bad = json.dumps({"steps": [{"skill": "launch_missile", "args": {}}]})
    planner, _ = make_planner([_response(bad)])
    with pytest.raises(PlanValidationError) as exc:
        planner.plan("发射导弹")
    assert "未注册技能" in str(exc.value)


def test_rejects_empty_steps() -> None:
    planner, _ = make_planner([_response(json.dumps({"steps": []}))])
    with pytest.raises(PlanValidationError):
        planner.plan("把红色方块推到左边")


def test_rejects_non_json() -> None:
    planner, _ = make_planner([_response("我不知道该怎么规划")])
    with pytest.raises(PlanValidationError):
        planner.plan("把红色方块推到左边")


def test_rejects_missing_skill_field() -> None:
    planner, _ = make_planner([_response(json.dumps({"steps": [{"args": {}}]}))])
    with pytest.raises(PlanValidationError):
        planner.plan("x")


def test_rejects_non_dict_args() -> None:
    bad = json.dumps({"steps": [{"skill": "goto_point", "args": [1, 2]}]})
    planner, _ = make_planner([_response(bad)])
    with pytest.raises(PlanValidationError):
        planner.plan("x")


def test_empty_content_raises() -> None:
    planner, _ = make_planner([_response("   ")])
    with pytest.raises(LLMError):
        planner.plan("x")


# ------------------------------------------------------------------ 重试与熔断


def test_retries_transient_error() -> None:
    planner, fake = make_planner([RuntimeError("timeout 500"), _response(GOOD)])
    plan = planner.plan("把红色方块推到左边")
    assert fake.calls == 2
    assert plan.steps


def test_no_retry_on_auth_error() -> None:
    planner, fake = make_planner([RuntimeError("401 invalid api key"), _response(GOOD)])
    with pytest.raises(LLMError):
        planner.plan("把红色方块推到左边")
    assert fake.calls == 1, "鉴权错误重试没有意义，应立即失败"


def test_gives_up_after_max_attempts() -> None:
    planner, fake = make_planner([RuntimeError("timeout")])
    with pytest.raises(LLMError):
        planner.plan("把红色方块推到左边")
    assert fake.calls == 2


def test_circuit_opens_after_repeated_failures() -> None:
    breaker = CircuitBreaker(failure_threshold=2, cooldown_s=60)
    planner, fake = make_planner([RuntimeError("timeout")])
    planner.breaker = breaker
    for _ in range(2):
        with pytest.raises(LLMError):
            planner.plan("x")
    assert breaker.state == "open"

    # 熔断期间不再发起调用
    called_before = fake.calls
    with pytest.raises(LLMError) as exc:
        planner.plan("x")
    assert "熔断" in str(exc.value)
    assert fake.calls == called_before


def test_circuit_half_open_allows_one_probe() -> None:
    breaker = CircuitBreaker(failure_threshold=1, cooldown_s=0.0)
    breaker.record_failure()
    assert breaker.allow() is True
    assert breaker.state == "half_open"
    assert breaker.allow() is False, "半开状态只允许一个探针，避免雪崩"
    breaker.record_success()
    assert breaker.state == "closed"
    assert breaker.allow() is True


def test_circuit_recovers_after_success() -> None:
    breaker = CircuitBreaker(failure_threshold=1, cooldown_s=0.0)
    breaker.record_failure()
    breaker.record_success()
    assert breaker.state == "closed"


# ------------------------------------------------------------------ 降级


class FailingLLMPlanner(LLMPlanner):
    def plan(self, instruction: str):  # type: ignore[no-untyped-def]
        raise LLMError("上游不可用")


def test_pipeline_falls_back_to_rules() -> None:
    registry, _ = build_mock_registry()
    settings = Settings(llm={"api_key": "sk-test", "fallback_to_rules": True})
    planner = Planner(settings, registry)
    planner.llm = FailingLLMPlanner(settings.llm, registry)  # type: ignore[assignment]

    plan = planner.plan("把红色方块推到左边")
    assert plan.source == "rules"
    assert plan.warnings and "降级" in plan.warnings[0]


def test_pipeline_raises_when_fallback_disabled() -> None:
    registry, _ = build_mock_registry()
    settings = Settings(llm={"api_key": "sk-test", "fallback_to_rules": False})
    planner = Planner(settings, registry)
    planner.llm = FailingLLMPlanner(settings.llm, registry)  # type: ignore[assignment]

    with pytest.raises(LLMError):
        planner.plan("把红色方块推到左边")


def test_pipeline_uses_rules_without_api_key() -> None:
    registry, _ = build_mock_registry()
    settings = Settings(llm={"api_key": None})
    planner = Planner(settings, registry)
    assert planner.llm is None
    assert planner.plan("把红色方块推到左边").source == "rules"


def test_pipeline_rejects_empty_instruction() -> None:
    registry, _ = build_mock_registry()
    planner = Planner(Settings(), registry)
    with pytest.raises(PlanningError):
        planner.plan("  ")


def test_pipeline_rejects_overlong_instruction() -> None:
    registry, _ = build_mock_registry()
    planner = Planner(Settings(), registry)
    with pytest.raises(PlanningError) as exc:
        planner.plan("推" * 501)
    assert "过长" in str(exc.value)


def test_force_rules_skips_llm() -> None:
    registry, _ = build_mock_registry()
    settings = Settings(llm={"api_key": "sk-test"})
    planner = Planner(settings, registry, force_rules=True)
    assert planner.llm is None
    assert planner.plan("把红色方块推到左边").source == "rules"
