"""Prometheus 指标。

**可选依赖**：未安装 ``prometheus-client`` 时退化为 no-op，核心编排能力不受影响
（mock 模式与单元测试不需要埋点）。这样「零依赖跑通」与「生产可观测」不冲突。
"""

from __future__ import annotations

import contextlib
from typing import Any

try:  # pragma: no cover - 分支取决于部署环境
    from prometheus_client import (
        CONTENT_TYPE_LATEST,
        REGISTRY,
        Counter,
        Gauge,
        Histogram,
        generate_latest,
    )

    PROMETHEUS_AVAILABLE = True
except ImportError:  # pragma: no cover
    PROMETHEUS_AVAILABLE = False
    CONTENT_TYPE_LATEST = "text/plain; version=0.0.4; charset=utf-8"

    class _Null:
        """最小 no-op 桩，签名与 prometheus_client 对象一致。"""

        def labels(self, *_: Any, **__: Any) -> _Null:
            return self

        def inc(self, *_: Any, **__: Any) -> None:
            return None

        def dec(self, *_: Any, **__: Any) -> None:
            return None

        def observe(self, *_: Any, **__: Any) -> None:
            return None

        def set(self, *_: Any, **__: Any) -> None:
            return None

    class Counter(_Null):  # type: ignore[no-redef]
        pass

    class Gauge(_Null):  # type: ignore[no-redef]
        pass

    class Histogram(_Null):  # type: ignore[no-redef]
        def __init__(self, *_: Any, **__: Any) -> None:
            pass

    class REGISTRY:  # type: ignore[no-redef]
        pass

    def generate_latest(*_: Any) -> bytes:  # type: ignore[misc]
        return b""


_PREFIX = "robot_agent"

# ---------------------------------------------------------------- 任务级
TASKS_TOTAL = Counter(f"{_PREFIX}_tasks_total", "任务总数", ["status"])
TASK_DURATION = Histogram(
    f"{_PREFIX}_task_duration_seconds",
    "任务端到端耗时",
    buckets=(0.5, 1, 2, 5, 10, 30, 60, 120, 300, 600),
)
TASKS_IN_FLIGHT = Gauge(f"{_PREFIX}_tasks_in_flight", "正在执行的任务数")
TASK_QUEUE_DEPTH = Gauge(f"{_PREFIX}_task_queue_depth", "排队中的任务数")

# ---------------------------------------------------------------- 步骤级
STEPS_TOTAL = Counter(f"{_PREFIX}_steps_total", "技能执行次数", ["skill", "outcome"])
STEP_DURATION = Histogram(
    f"{_PREFIX}_step_duration_seconds",
    "单个技能执行耗时",
    ["skill"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 15, 30, 60, 120),
)
SKILL_RETRIES_TOTAL = Counter(f"{_PREFIX}_skill_retries_total", "技能瞬时故障重试次数", ["skill"])

# ---------------------------------------------------------------- 安全与规划
SAFETY_BLOCKED_TOTAL = Counter(
    f"{_PREFIX}_safety_blocked_total", "被安全闸门拦截的步数", ["policy"]
)
SAFETY_CLAMPED_TOTAL = Counter(
    f"{_PREFIX}_safety_clamped_total", "被自动降速修正的步数", ["policy"]
)
PLANNING_TOTAL = Counter(f"{_PREFIX}_planning_total", "规划尝试次数", ["source", "outcome"])
PLANNING_DURATION = Histogram(
    f"{_PREFIX}_planning_duration_seconds",
    "规划耗时",
    ["source"],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 40),
)

# ---------------------------------------------------------------- LLM
LLM_REQUESTS_TOTAL = Counter(f"{_PREFIX}_llm_requests_total", "LLM 调用次数", ["model", "outcome"])
LLM_TOKENS_TOTAL = Counter(f"{_PREFIX}_llm_tokens_total", "LLM token 用量", ["model", "direction"])

# ---------------------------------------------------------------- 审计
AUDIT_ENTRIES_TOTAL = Counter(f"{_PREFIX}_audit_entries_total", "审计条目写入数", ["event"])
AUDIT_WRITE_ERRORS_TOTAL = Counter(f"{_PREFIX}_audit_write_errors_total", "审计写入失败次数")


def render_metrics() -> tuple[bytes, str]:
    """返回 (payload, content_type)，供 /metrics 端点直接写响应。"""
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST


def reset_for_tests() -> None:
    """测试用：清空计数器，避免用例间互相污染。未装 prometheus 时为空操作。"""
    if not PROMETHEUS_AVAILABLE:  # pragma: no cover
        return
    collectors = [
        TASKS_TOTAL,
        TASK_DURATION,
        STEPS_TOTAL,
        STEP_DURATION,
        SKILL_RETRIES_TOTAL,
        SAFETY_BLOCKED_TOTAL,
        SAFETY_CLAMPED_TOTAL,
        PLANNING_TOTAL,
        PLANNING_DURATION,
        LLM_REQUESTS_TOTAL,
        LLM_TOKENS_TOTAL,
        AUDIT_ENTRIES_TOTAL,
        AUDIT_WRITE_ERRORS_TOTAL,
    ]
    for collector in collectors:
        # 老版本 prometheus_client 没有 clear()，忽略即可
        with contextlib.suppress(Exception):  # pragma: no cover
            collector.clear()  # type: ignore[attr-defined]
