"""LLM 规划器：Function Calling + 超时 + 重试 + 熔断。

主路径是 **Function Calling**：把技能注册表导出成 OpenAI 兼容的 ``tools``，
模型不再「写一段 JSON 给我解析」，而是直接声明要调用哪个函数、传什么参数。
两者差别不在代码优雅，而在**错误的形状**：

* 纯文本模式：模型可能返回少一层括号、多一段解释、把数组写成字符串，
  解析器得写一堆容错，容错越多越容易把幻觉「修」成一个看着正常的危险动作。
* Function Calling：参数由 provider 按 schema 校验后才交给我们，能错的地方
  只剩「语义错」（该用 goto 用了 move_relative），而这类错误由安全层兜。

四个生产环境必须处理、而 demo 通常忽略的问题：

1. **超时**：模型网关抖动时不能让机械臂无限期等待，必须有硬超时。
2. **熔断**：连续失败说明上游已不可用，继续每单都重试只会放大故障与费用。
   熔断期间直接走规则降级，冷却后自动半开试探。
3. **输出不可信**：模型给出的调用必须逐层校验（函数名已注册 → 参数是对象 →
   步数不超上限），任何一环不过就整条降级，不做「尽量修补」。
4. **网关能力参差**：不是所有 OpenAI 兼容网关都实现 ``tools``。不支持时自动
   退回纯文本 JSON 模式，而不是让整条链路挂掉——降级要记日志，不能静默。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from robot_agent import metrics
from robot_agent.config import LLMSettings
from robot_agent.domain.plan import Plan, PlanStep
from robot_agent.errors import (
    LLMBudgetExceededError,
    LLMError,
    LLMTimeoutError,
    PlanValidationError,
)
from robot_agent.logging import get_logger
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

# tools 模式下技能清单由 tools 数组提供，prompt 里不再重复罗列——
# 重复一份不仅浪费 token，还会和 tools 的定义漂移成两份「事实」。
TOOL_SYSTEM_PROMPT = """你是机器人任务规划器。把用户的中文指令拆解成对已提供函数的调用序列。

场地为正方形，坐标原点在中心，x、y 的合法范围是 [-{half}, {half}]，单位米。
方位参考：左边=(-{half}, 0)，右边=({half}, 0)，中间=(0, 0)。

规则：
1. 只能调用已提供的函数，禁止编造函数名。
2. 按执行顺序调用；推箱前若目标与方块不在同一直线，先调用 goto_point 对准。
3. 调用总数不超过 {max_steps} 个。
4. 用户要求的速度即使不合理也要如实填写，由安全层决定是否降速。
5. 无法完成任务时不要调用任何函数。
"""

_FENCE_RE = r"```(?:json)?\s*(\{.*?\})\s*```"

# 网关不认识 tools 参数时通常会抛这些词；用它判断该不该降级而不是盲目重试
_TOOLS_UNSUPPORTED_HINTS = ("tools", "tool_choice", "function", "not supported", "unsupported")


@dataclass(frozen=True)
class ModelOutput:
    """一次模型调用的归一化结果。

    ``mode`` 记录这条计划是「模型主动调用函数」还是「模型写 JSON 被解析」得来的。
    出问题复盘时这个字段很关键：同样是解析失败，两种模式的排查方向完全不同。
    """

    steps: list[PlanStep]
    raw: str
    usage: dict[str, int]
    mode: str = "tool_calls"


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
            output = self._call_with_retry(instruction)
        except PlanValidationError:
            # 上游是健康的，只是这条计划不合规：不计入熔断，否则几条离谱指令
            # 就能把整个 LLM 通道打成熔断，之后全部降级到规则规划。
            metrics.PLANNING_TOTAL.labels(source=self.source, outcome="invalid").inc()
            metrics.PLANNING_DURATION.labels(source=self.source).observe(
                time.perf_counter() - started
            )
            raise
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
            steps=len(output.steps),
            mode=output.mode,
            latency_ms=round(elapsed, 1),
            prompt_tokens=output.usage.get("prompt_tokens"),
            completion_tokens=output.usage.get("completion_tokens"),
        )
        return Plan(
            steps=output.steps,
            source="llm",
            model=self.settings.model,
            latency_ms=elapsed,
            raw=output.raw[:4000],
        )

    # ------------------------------------------------------------------ 调用

    def _call_with_retry(self, instruction: str) -> ModelOutput:
        """指数退避重试。只重试瞬时错误；鉴权/参数错误立即失败。"""
        last_exc: Exception | None = None
        for attempt in range(1, self.settings.max_attempts + 1):
            try:
                return self._invoke_model(instruction)
            except LLMBudgetExceededError:
                raise
            except PlanValidationError:
                # 模型答了但不合规：这是「计划不合法」，不是「调用失败」。
                # 包成 LLMError 会让上层误判成上游故障（甚至触发熔断计数），
                # 而真正该做的是把这条计划拒掉、交给规则规划降级。
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

    def _invoke_model(self, instruction: str) -> ModelOutput:
        client = self._ensure_client()
        tools_mode = self.settings.tool_calling
        messages = _messages(self._build_prompt(with_skills=not tools_mode), instruction)

        if not tools_mode:
            return self._from_content(self._request(client, messages))

        try:
            response = self._request(client, messages, tools=self.registry.tool_schemas())
        except Exception as exc:
            if not _tools_unsupported(exc):
                raise
            # 网关没实现 tools：退成纯文本 JSON。降级必须留痕，否则「为什么
            # 生产上从没见过 tool_calls」这个问题会永远查不出来。
            logger.warning(
                "llm_tools_unsupported",
                model=self.settings.model,
                error=str(exc)[:200],
            )
            self._record_tool_mode("unsupported")
            fallback = _messages(self._build_prompt(with_skills=True), instruction)
            return self._from_content(self._request(client, fallback))

        message = response.choices[0].message
        tool_calls = getattr(message, "tool_calls", None)
        if tool_calls:
            self._record_tool_mode("tool_calls")
            return self._from_tool_calls(tool_calls, response)

        # 网关接受了 tools 却仍用 content 回答：部分模型只在 tool_choice="required"
        # 时才调函数。这里不重试、不强制，直接按文本解析——多打一次请求不值当。
        content = getattr(message, "content", None) or ""
        if str(content).strip():
            logger.info("llm_tool_calls_absent", model=self.settings.model)
            self._record_tool_mode("content")
            return self._from_content(response)
        raise LLMError("LLM 既未返回 tool_calls 也未返回内容")

    def _request(
        self,
        client: Any,
        messages: list[dict[str, str]],
        *,
        tools: list[dict[str, Any]] | None = None,
    ) -> Any:
        """发起一次补全请求。tools 与 response_format 互斥使用。"""
        kwargs: dict[str, Any] = {
            "model": self.settings.model,
            "messages": messages,
            "temperature": self.settings.temperature,
            "max_tokens": self.settings.max_output_tokens,
            "timeout": self.settings.timeout_s,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        else:
            kwargs["response_format"] = {"type": "json_object"}

        try:
            response = client.chat.completions.create(**kwargs)
        except Exception as exc:
            # 部分兼容网关不支持 response_format，退化为纯文本提示后重试一次
            if tools is None and _json_mode_unsupported(exc):
                logger.info("llm_json_mode_unsupported", model=self.settings.model)
                kwargs.pop("response_format", None)
                response = client.chat.completions.create(**kwargs)
            else:
                raise
        self._record_usage(response)
        return response

    def _record_usage(self, response: Any) -> None:
        usage_obj = getattr(response, "usage", None)
        prompt_tokens = int(getattr(usage_obj, "prompt_tokens", 0) or 0)
        completion_tokens = int(getattr(usage_obj, "completion_tokens", 0) or 0)
        metrics.LLM_TOKENS_TOTAL.labels(model=self.settings.model, direction="in").inc(
            prompt_tokens
        )
        metrics.LLM_TOKENS_TOTAL.labels(model=self.settings.model, direction="out").inc(
            completion_tokens
        )
        metrics.LLM_REQUESTS_TOTAL.labels(model=self.settings.model, outcome="ok").inc()

    def _record_tool_mode(self, mode: str) -> None:
        """记录本次走的是哪条调用路径，用来看网关能力是否与预期一致。

        返回一个可直接画图的计数：如果 ``mode=unsupported`` 持续增长，
        说明这个网关的 Function Calling 是「配置开了但实际没生效」。
        """
        metrics.LLM_TOOL_CALLS_TOTAL.labels(model=self.settings.model, mode=mode).inc()

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

    def _build_prompt(self, *, with_skills: bool) -> str:
        """构造 system prompt。

        ``with_skills`` 与调用方式绑定：走 tools 时不罗列技能（tools 里已有），
        降级到纯文本时必须罗列，否则模型根本不知道有哪些技能可用，只能编。
        """
        if with_skills:
            return SYSTEM_PROMPT.format(
                skills=self.registry.prompt_description(),
                half=self.arena_half_size,
                max_steps=self.max_steps,
            )
        return TOOL_SYSTEM_PROMPT.format(
            half=self.arena_half_size,
            max_steps=self.max_steps,
        )

    # ------------------------------------------------------------------ 解析

    def _from_tool_calls(self, tool_calls: Any, response: Any) -> ModelOutput:
        """把模型返回的 ``tool_calls`` 转成计划。

        这里是 Function Calling 唯一的「信任边界」：函数名必须在注册表里，
        参数必须是对象。schema 校验由 provider 做了一部分，但**不能依赖**——
        不同网关对 ``strict`` 的支持程度不一样，把校验完全外包给上游等于把
        安全边界交给第三方。
        """
        steps: list[PlanStep] = []
        for index, call in enumerate(tool_calls, start=1):
            function = getattr(call, "function", None)
            name = str(getattr(function, "name", "") or "")
            arguments = getattr(function, "arguments", "") if function is not None else ""

            if not name:
                raise PlanValidationError(f"第 {index} 个 tool_call 缺少函数名")
            if not self.registry.has(name):
                raise PlanValidationError(
                    f"第 {index} 步调用了未注册技能 '{name}'，可用: {self.registry.names()}",
                    details={"arguments": str(arguments)[:300]},
                )

            args = _loads_arguments(arguments, index)
            try:
                steps.append(PlanStep(skill=name, args=args))
            except Exception as exc:
                raise PlanValidationError(f"第 {index} 步结构非法: {exc}") from exc

        if not steps:
            raise PlanValidationError("LLM 未选择任何技能")
        if len(steps) > self.max_steps:
            raise PlanValidationError(f"计划 {len(steps)} 步，超过上限 {self.max_steps} 步")
        return ModelOutput(
            steps=steps,
            raw=_dump_tool_calls(tool_calls),
            usage=_usage_of(response),
            mode="tool_calls",
        )

    def _from_content(self, response: Any) -> ModelOutput:
        """纯文本模式：从 ``message.content`` 里解析 JSON。"""
        message = response.choices[0].message
        content = getattr(message, "content", None) or ""
        if not str(content).strip():
            raise LLMError("LLM 返回空内容")
        text = str(content)
        return ModelOutput(
            steps=self._parse_steps(text),
            raw=text,
            usage=_usage_of(response),
            mode="json",
        )

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


def _messages(system: str, instruction: str) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": instruction},
    ]


def _loads_arguments(arguments: Any, index: int) -> dict[str, Any]:
    """解析 tool_call 的 ``arguments``。

    正常形态是 JSON 字符串；部分 SDK 已代为解析成 dict。空字符串按「无参数」处理
    ——无参数技能（``stop`` / ``open_gripper``）在很多模型上就返回 ``""``，
    这里若当成错误，急停反而会失败。
    """
    if isinstance(arguments, dict):
        return dict(arguments)
    text = str(arguments or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PlanValidationError(
            f"第 {index} 步的参数不是合法 JSON: {exc}",
            details={"arguments": text[:300]},
        ) from exc
    if not isinstance(parsed, dict):
        raise PlanValidationError(
            f"第 {index} 步的参数应为对象，实际为 {type(parsed).__name__}",
            details={"arguments": text[:300]},
        )
    return parsed


def _dump_tool_calls(tool_calls: Any) -> str:
    """把 tool_calls 序列化成可存审计的 JSON，供事后回放。"""
    items = []
    for call in tool_calls:
        function = getattr(call, "function", None)
        items.append(
            {
                "id": str(getattr(call, "id", "") or ""),
                "name": str(getattr(function, "name", "") or ""),
                "arguments": str(getattr(function, "arguments", "") or ""),
            }
        )
    return json.dumps({"tool_calls": items}, ensure_ascii=False)


def _usage_of(response: Any) -> dict[str, int]:
    usage_obj = getattr(response, "usage", None)
    return {
        "prompt_tokens": int(getattr(usage_obj, "prompt_tokens", 0) or 0),
        "completion_tokens": int(getattr(usage_obj, "completion_tokens", 0) or 0),
    }


def _tools_unsupported(exc: Exception) -> bool:
    """判断异常是否属于「网关不支持 tools 参数」。

    必须窄：把网络错误误判成「不支持」会导致每条请求都降级成纯文本模式，
    Function Calling 静默失效，而日志里只留下一句「unsupported」。
    """
    text = str(exc).lower()
    if _is_retryable(exc):
        return False
    has_hint = any(hint in text for hint in _TOOLS_UNSUPPORTED_HINTS)
    # 「不认识 tool_choice」这类明确信号；避免 401/404 被误伤
    return has_hint and any(k in text for k in ("unsupport", "not support", "unknown", "invalid"))


def _json_mode_unsupported(exc: Exception) -> bool:
    text = str(exc).lower()
    return "response_format" in text or "json_object" in text


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
