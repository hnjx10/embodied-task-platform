"""LLM 规划器：结构化输出 + 超时 + 重试 + 熔断。

三个生产环境必须处理、而 demo 通常忽略的问题：

1. **超时**：模型网关抖动时不能让机械臂无限期等待，必须有硬超时。
2. **熔断**：连续失败说明上游已不可用，继续每单都重试只会放大故障与费用。
   熔断期间直接走规则降级，冷却后自动半开试探。
3. **输出不可信**：模型返回的 JSON 必须逐层校验（能解析 → 有 steps → 技能存在 →
   参数类型正确），任何一环不过就整条降级，不做「尽量修补」——修补幻觉容易修出危险动作。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from robot_agent import metrics
from robot_agent.config import LLMSettings
from robot_agent.errors import (
    LLMBudgetExceededError,
    LLMError,
    LLMTimeoutError,
    PlanValidationError,
)
from robot_agent.logging import get_logger
from robot_agent.planner.base import Plan, PlanStep
from robot_agent.skills.base import SkillRegistry

logger = get_logger(__name__)

SYSTEM_PROMPT = """你是机器人任务规划器。把用户的中文指令拆解成技能调用序列。

只输出 JSON，格式严格如下，不要输出任何解释文字：
{{"steps": [{{"skill": "<技能名>", "args": {{...}}}}]}}

可用技能：
{skills}

场地为正方形，坐标原点在中心，x、y 的合法范围是 [-{half}, {half}]，单位米。
方位参考：左边=(-{half}, 0)，右边=({half}, 0)，中间=(0, 0)。

规则：
1. 只能使用上面列出的技能名，禁止编造。
2. 推箱前如果目标与方块不在同一直线，可以先 goto_point 对准。
3. 步数不超过 {max_steps} 步。
4. 用户要求的速度即使不合理也要如实填写，由安全层决定是否降速。
5. 无法完成任务时输出 {{"steps": []}}。
"""

_FENCE_RE = r"```(?:json)?\s*(\{.*?\})\s*```"


@dataclass
class CircuitBreaker:
    """极简熔断器：连续失败 N 次后进入 open，冷却结束后半开一次。"""

    failure_threshold: int = 5
    cooldown_s: float = 60.0
    _failures: int = 0
    _opened_at: float | None = None
    _probe_in_flight: bool = False
    _state: str = "closed"
    _lock: Any = field(default=None, repr=False)

    def __post_init__(self) -> None:
        import threading

        self._lock = threading.Lock()

    @property
    def state(self) -> str:
        with self._lock:
            if (
                self._state == "open"
                and self._opened_at is not None
                and time.monotonic() - self._opened_at >= self.cooldown_s
            ):
                self._state = "half_open"
            return self._state

    def allow(self) -> bool:
        """是否允许发起调用。半开状态只放一个探针，避免雪崩。"""
        with self._lock:
            if self._state == "closed":
                return True
            if self._state == "open":
                if (
                    self._opened_at is not None
                    and time.monotonic() - self._opened_at >= self.cooldown_s
                ):
                    self._state = "half_open"
                    self._probe_in_flight = True
                    return True
                return False
            # half_open
            if self._probe_in_flight:
                return False
            self._probe_in_flight = True
            return True

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._probe_in_flight = False
            if self._state != "closed":
                logger.info("llm_circuit_closed")
            self._state = "closed"
            self._opened_at = None

    def record_failure(self) -> None:
        with self._lock:
            self._probe_in_flight = False
            self._failures += 1
            if self._state in {"half_open", "open"} or self._failures >= self.failure_threshold:
                if self._state != "open":
                    logger.warning("llm_circuit_opened", failures=self._failures)
                self._state = "open"
                self._opened_at = time.monotonic()


class LLMPlanner:
    """OpenAI 兼容接口的规划器。"""

    source = "llm"

    def __init__(
        self,
        settings: LLMSettings,
        registry: SkillRegistry,
        *,
        arena_half_size: float = 1.5,
        max_steps: int = 8,
        breaker: CircuitBreaker | None = None,
    ) -> None:
        if settings.api_key is None:
            raise LLMError("未配置 LLM api_key，无法使用 LLM 规划")
        self.settings = settings
        self.registry = registry
        self.arena_half_size = arena_half_size
        self.max_steps = max_steps
        self.breaker = breaker or CircuitBreaker()
        self._client: Any = None

    # ------------------------------------------------------------------ 对外

    def plan(self, instruction: str) -> Plan:
        if not self.breaker.allow():
            metrics.PLANNING_TOTAL.labels(source=self.source, outcome="circuit_open").inc()
            raise LLMError("LLM 熔断器已打开，跳过调用")

        started = time.perf_counter()
        try:
            raw, usage = self._call_with_retry(instruction)
            steps = self._parse_steps(raw)
        except Exception:
            self.breaker.record_failure()
            metrics.PLANNING_TOTAL.labels(source=self.source, outcome="error").inc()
            metrics.PLANNING_DURATION.labels(source=self.source).observe(
                time.perf_counter() - started
            )
            raise

        self.breaker.record_success()
        elapsed = (time.perf_counter() - started) * 1000.0
        metrics.PLANNING_TOTAL.labels(source=self.source, outcome="ok").inc()
        metrics.PLANNING_DURATION.labels(source=self.source).observe(elapsed / 1000.0)
        logger.info(
            "llm_plan_ok",
            steps=len(steps),
            latency_ms=round(elapsed, 1),
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
        )
        return Plan(
            steps=steps,
            source="llm",
            model=self.settings.model,
            latency_ms=elapsed,
            raw=raw[:4000],
        )

    # ------------------------------------------------------------------ 调用

    def _call_with_retry(self, instruction: str) -> tuple[str, dict[str, int]]:
        """指数退避重试。只重试瞬时错误；鉴权/参数错误立即失败。"""
        last_exc: Exception | None = None
        for attempt in range(1, self.settings.max_attempts + 1):
            try:
                return self._invoke_model(instruction)
            except LLMBudgetExceededError:
                raise
            except Exception as exc:
                last_exc = exc
                retryable = _is_retryable(exc)
                logger.warning(
                    "llm_attempt_failed",
                    attempt=attempt,
                    max_attempts=self.settings.max_attempts,
                    retryable=retryable,
                    error=str(exc),
                )
                if not retryable or attempt >= self.settings.max_attempts:
                    break
                time.sleep(min(0.5 * 2 ** (attempt - 1), 4.0))
        raise _wrap_error(last_exc)

    def _invoke_model(self, instruction: str) -> tuple[str, dict[str, int]]:
        client = self._ensure_client()
        prompt = self._build_prompt(instruction)

        try:
            response = client.chat.completions.create(
                model=self.settings.model,
                messages=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": instruction},
                ],
                temperature=self.settings.temperature,
                max_tokens=self.settings.max_output_tokens,
                timeout=self.settings.timeout_s,
                response_format={"type": "json_object"},
            )
        except Exception as exc:
            # 部分兼容网关不支持 response_format，退化为纯文本提示后重试一次
            if "response_format" in str(exc).lower() or "json_object" in str(exc).lower():
                logger.info("llm_json_mode_unsupported", model=self.settings.model)
                response = client.chat.completions.create(
                    model=self.settings.model,
                    messages=[
                        {"role": "system", "content": prompt},
                        {"role": "user", "content": instruction},
                    ],
                    temperature=self.settings.temperature,
                    max_tokens=self.settings.max_output_tokens,
                    timeout=self.settings.timeout_s,
                )
            else:
                raise

        usage_obj = getattr(response, "usage", None)
        usage = {
            "prompt_tokens": int(getattr(usage_obj, "prompt_tokens", 0) or 0),
            "completion_tokens": int(getattr(usage_obj, "completion_tokens", 0) or 0),
        }
        metrics.LLM_TOKENS_TOTAL.labels(model=self.settings.model, direction="in").inc(
            usage["prompt_tokens"]
        )
        metrics.LLM_TOKENS_TOTAL.labels(model=self.settings.model, direction="out").inc(
            usage["completion_tokens"]
        )
        metrics.LLM_REQUESTS_TOTAL.labels(model=self.settings.model, outcome="ok").inc()

        content = response.choices[0].message.content or ""
        if not content.strip():
            raise LLMError("LLM 返回空内容")
        return content, usage

    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover
            raise LLMError(
                "未安装 openai 包，请 pip install 'robot-agent[llm]'", cause=exc
            ) from exc

        self._client = OpenAI(
            api_key=self.settings.api_key.get_secret_value() if self.settings.api_key else None,
            base_url=self.settings.base_url,
            timeout=self.settings.timeout_s,
            max_retries=0,  # 重试由本模块控制，避免与 SDK 内部重试叠加
        )
        return self._client

    def _build_prompt(self, _instruction: str) -> str:
        return SYSTEM_PROMPT.format(
            skills=self.registry.prompt_description(),
            half=self.arena_half_size,
            max_steps=self.max_steps,
        )

    # ------------------------------------------------------------------ 解析

    def _parse_steps(self, raw: str) -> list[PlanStep]:
        payload = _loads_json_object(raw)
        steps_raw = payload.get("steps")
        if not isinstance(steps_raw, list):
            raise PlanValidationError("LLM 输出缺少 steps 数组", details={"raw": raw[:500]})
        if not steps_raw:
            raise PlanValidationError("LLM 返回空计划，无法执行", details={"raw": raw[:500]})

        steps: list[PlanStep] = []
        for idx, item in enumerate(steps_raw):
            if not isinstance(item, dict):
                raise PlanValidationError(f"第 {idx + 1} 步不是对象: {item!r}")
            skill = item.get("skill")
            args = item.get("args", {})
            if not isinstance(skill, str) or not skill:
                raise PlanValidationError(f"第 {idx + 1} 步缺少 skill 字段")
            if not self.registry.has(skill):
                raise PlanValidationError(
                    f"第 {idx + 1} 步使用了未注册技能 '{skill}'，可用: {self.registry.names()}",
                    details={"raw_step": item},
                )
            if not isinstance(args, dict):
                raise PlanValidationError(
                    f"第 {idx + 1} 步 args 必须是对象，收到 {type(args).__name__}"
                )
            try:
                steps.append(PlanStep(skill=skill, args=args))
            except Exception as exc:
                raise PlanValidationError(f"第 {idx + 1} 步结构非法: {exc}") from exc
        return steps


# --------------------------------------------------------------------- 工具


def _loads_json_object(raw: str) -> dict[str, Any]:
    """容忍模型输出被包在 ```json``` 代码块里，或前后带解释文字。"""
    text = raw.strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        import re

        fenced = re.search(_FENCE_RE, text, re.S)
        if fenced:
            try:
                parsed = json.loads(fenced.group(1))
            except json.JSONDecodeError as exc:
                raise PlanValidationError(f"LLM 输出不是合法 JSON: {exc}") from exc
        else:
            start, end = text.find("{"), text.rfind("}")
            if start == -1 or end <= start:
                raise PlanValidationError("LLM 输出中找不到 JSON 对象") from None
            try:
                parsed = json.loads(text[start : end + 1])
            except json.JSONDecodeError as exc:
                raise PlanValidationError(f"LLM 输出不是合法 JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise PlanValidationError(f"LLM 输出顶层应为对象，实际为 {type(parsed).__name__}")
    return parsed


def _is_retryable(exc: Exception) -> bool:
    """网络/限流/5xx 可重试；鉴权失败、参数错误重试也无意义。"""
    if isinstance(exc, LLMTimeoutError):
        return True
    text = str(exc).lower()
    if any(k in text for k in ("401", "403", "invalid api key", "unauthorized", "permission")):
        return False
    if any(k in text for k in ("400", "invalid_request", "bad request")):
        return False
    return any(
        k in text
        for k in (
            "timeout",
            "timed out",
            "429",
            "rate limit",
            "500",
            "502",
            "503",
            "504",
            "connect",
        )
    ) or isinstance(exc, (TimeoutError, ConnectionError))


def _wrap_error(exc: Exception | None) -> LLMError:
    if exc is None:
        return LLMError("LLM 调用失败，原因未知")
    if isinstance(exc, LLMError):
        return exc
    text = str(exc)
    if "timeout" in text.lower() or isinstance(exc, TimeoutError):
        return LLMTimeoutError(f"LLM 调用超时: {exc}", cause=exc)
    return LLMError(f"LLM 调用失败: {exc}", cause=exc)
