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


def _tool_call(name: str, args: Any = None, call_id: str = "call_1") -> Any:
    """构造一个 OpenAI 形态的 tool_call。args 传 dict 会序列化成字符串。"""
    raw = args if isinstance(args, str) else json.dumps(args if args is not None else {})
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name=name, arguments=raw),
    )


def _tool_response(*calls: Any, content: str | None = None) -> Any:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=content, tool_calls=list(calls) or None)
            )
        ],
        usage=SimpleNamespace(prompt_tokens=120, completion_tokens=45),
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
    planner, fake = make_planner([_response(GOOD)], tool_calling=False)
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


# ------------------------------------------------------------ Function Calling


def test_parses_tool_calls_in_order() -> None:
    """多个 tool_call 的顺序就是执行顺序——这是 Function Calling 相对 JSON 的硬优势。"""
    planner, fake = make_planner(
        [
            _tool_response(
                _tool_call("goto_point", {"x": 0.0, "y": 0.5}, "c1"),
                _tool_call("push_cube", {"color": "red", "x": -1.2, "y": 0.0}, "c2"),
            )
        ]
    )
    plan = planner.plan("把红色方块推到左边")
    assert [s.skill for s in plan.steps] == ["goto_point", "push_cube"]
    assert plan.steps[1].args["color"] == "red"
    assert "tools" in fake.last_kwargs


def test_tools_array_carries_full_schema() -> None:
    planner, fake = make_planner([_tool_response(_tool_call("get_pose", {"target": "robot"}))])
    planner.plan("机器人在哪")
    tools = fake.last_kwargs["tools"]
    names = [t["function"]["name"] for t in tools]
    assert "goto_point" in names and "patrol" in names
    assert all(t["type"] == "function" for t in tools)

    push = next(t for t in tools if t["function"]["name"] == "push_cube")
    params = push["function"]["parameters"]
    assert params["type"] == "object"
    assert params["properties"]["color"]["enum"] == ["red", "green", "blue"]
    assert set(params["required"]) == {"color", "x", "y"}
    assert "$ref" not in json.dumps(params)


def test_tool_mode_prompt_does_not_repeat_skill_list() -> None:
    """技能清单只该有一份事实来源。prompt 与 tools 各写一份必然漂移。"""
    planner, fake = make_planner([_tool_response(_tool_call("get_pose", {"target": "robot"}))])
    planner.plan("机器人在哪")
    system_prompt = fake.last_kwargs["messages"][0]["content"]
    assert "push_cube(" not in system_prompt
    assert "red|green|blue" not in system_prompt


def test_rejects_unknown_tool_name() -> None:
    planner, _ = make_planner([_tool_response(_tool_call("launch_missile", {}))])
    with pytest.raises(PlanValidationError) as exc:
        planner.plan("发射导弹")
    assert "未注册技能" in str(exc.value)


def test_rejects_tool_args_that_are_not_json() -> None:
    planner, _ = make_planner([_tool_response(_tool_call("goto_point", "{not json"))])
    with pytest.raises(PlanValidationError) as exc:
        planner.plan("去中间")
    assert "参数" in str(exc.value)


def test_empty_argument_string_means_no_params() -> None:
    """无参数技能在多数模型上返回空字符串。这里若报错，急停就按不下去。"""
    planner, _ = make_planner([_tool_response(_tool_call("stop", ""))])
    plan = planner.plan("停下")
    assert plan.steps[0].skill == "stop"
    assert plan.steps[0].args == {}


def test_rejects_plan_longer_than_limit() -> None:
    calls = [_tool_call("get_pose", {"target": "robot"}, f"c{i}") for i in range(12)]
    planner, _ = make_planner([_tool_response(*calls)])
    with pytest.raises(PlanValidationError) as exc:
        planner.plan("反复查询")
    assert "上限" in str(exc.value)


def test_falls_back_to_json_when_gateway_lacks_tools() -> None:
    """网关不认 tools 参数时必须退回纯文本，而不是整条链路失败。

    且降级后的 prompt 必须重新带上技能清单——用 tools 版 prompt 去解析纯文本，
    模型根本不知道有哪些技能。
    """
    planner, fake = make_planner(
        [RuntimeError("unsupported parameter: tools"), _response(GOOD)],
    )
    plan = planner.plan("把红色方块推到左边")
    assert plan.steps[0].skill == "push_cube"
    assert fake.calls == 2
    assert "push_cube(" in fake.last_kwargs["messages"][0]["content"]


def test_tool_calls_recorded_for_replay() -> None:
    """raw 里要留下模型到底调了什么，否则事后无法解释「它为什么这么干」。"""
    planner, _ = make_planner([_tool_response(_tool_call("goto_point", {"x": 0.5, "y": 0.5}))])
    plan = planner.plan("去右上角")
    assert "tool_calls" in plan.raw
    assert "goto_point" in plan.raw


def test_content_used_when_tools_accepted_but_ignored() -> None:
    """部分模型只在 tool_choice=required 时才调函数，仍用 content 回答。"""
    planner, _ = make_planner([_tool_response(content=GOOD)])
    assert planner.plan("把红色方块推到左边").steps[0].skill == "push_cube"


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
