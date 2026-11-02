"""步骤执行器：超时、重试、急停、事件回调。

三个容易被忽略但生产必踩的坑：

1. **超时不可强杀**：Python 线程无法被强制终止，``future.result(timeout)`` 超时后
   技能线程仍在跑。因此超时处理必须「放弃等待 + 触发急停」，而不是假设线程已死。
2. **重试边界**：只有 :class:`SkillExecutionError` 这类瞬时故障才重试；安全拒绝
   （``ok=False`` 的返回）与参数错误永不重试，否则重试就成了绕过策略的后门。
3. **急停优先**：任何超时/异常后先发停车指令，再谈重试。机器人多动一秒的代价
   远高于任务失败。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Any

from robot_agent import metrics
from robot_agent.config import ExecutorSettings
from robot_agent.errors import SkillExecutionError, SkillTimeoutError
from robot_agent.logging import get_logger
from robot_agent.planner.base import PlanStep, StepOutcome
from robot_agent.skills.base import SkillRegistry

logger = get_logger(__name__)

StepCallback = Callable[[StepOutcome], None]
EMERGENCY_STOP_SKILL = "stop"


class StepExecutor:
    """执行一串已通过安全校验的步骤。"""

    def __init__(
        self,
        registry: SkillRegistry,
        settings: ExecutorSettings,
        *,
        on_step: StepCallback | None = None,
    ) -> None:
        self.registry = registry
        self.settings = settings
        self.on_step = on_step
        # 按 task_id 隔离回调：并发任务不能共用同一个进度回调，否则事件会串台
        self._callbacks: dict[str, StepCallback] = {}
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="skill-exec")

    # ------------------------------------------------------------------ 回调

    def register_callback(self, task_id: str, callback: StepCallback) -> None:
        self._callbacks[task_id] = callback

    def unregister_callback(self, task_id: str) -> None:
        self._callbacks.pop(task_id, None)

    def _resolve_callback(self, task_id: str, override: StepCallback | None) -> StepCallback | None:
        return override or self._callbacks.get(task_id) or self.on_step

    # ------------------------------------------------------------------ 单步

    def execute_step(self, index: int, step: PlanStep, task_id: str = "") -> StepOutcome:
        skill = self.registry.get(step.skill)
        attempts = 0
        last_error: str = ""
        started = time.perf_counter()

        while attempts < self.settings.max_attempts:
            attempts += 1
            try:
                result = self._run_with_timeout(skill, step, task_id)
            except SkillTimeoutError as exc:
                # 超时不重试：先急停，再把失败交回上层
                self.emergency_stop(task_id, reason=f"step_timeout:{step.skill}")
                return StepOutcome(
                    index=index,
                    skill=step.skill,
                    outcome="timeout",
                    detail=str(exc),
                    latency_ms=(time.perf_counter() - started) * 1000.0,
                    attempts=attempts,
                )
            except SkillExecutionError as exc:
                last_error = str(exc)
                retryable = getattr(exc, "retryable", False) and step.retryable
                if attempts >= self.settings.max_attempts or not retryable:
                    return StepOutcome(
                        index=index,
                        skill=step.skill,
                        outcome="error",
                        detail=last_error,
                        latency_ms=(time.perf_counter() - started) * 1000.0,
                        attempts=attempts,
                    )
                metrics.SKILL_RETRIES_TOTAL.labels(skill=step.skill).inc()
                logger.warning(
                    "skill_retry",
                    task_id=task_id,
                    skill=step.skill,
                    attempt=attempts,
                    error=last_error,
                )
                time.sleep(self.settings.retry_backoff_s * attempts)
                continue

            return StepOutcome(
                index=index,
                skill=step.skill,
                outcome="ok" if result.ok else "rejected",
                detail=result.detail,
                latency_ms=(time.perf_counter() - started) * 1000.0,
                attempts=attempts,
                data=result.data,
            )

        return StepOutcome(
            index=index,
            skill=step.skill,
            outcome="error",
            detail=last_error or "未知错误",
            latency_ms=(time.perf_counter() - started) * 1000.0,
            attempts=attempts,
        )

    def _run_with_timeout(self, skill: Any, step: PlanStep, task_id: str) -> Any:
        future = self._pool.submit(skill.execute, **step.args)
        try:
            return future.result(timeout=self.settings.step_timeout_s)
        except FutureTimeout as exc:
            raise SkillTimeoutError(
                f"技能 {step.skill} 执行超过 {self.settings.step_timeout_s:.0f}s",
                details={"skill": step.skill, "task_id": task_id},
            ) from exc

    # ------------------------------------------------------------------ 整串

    def execute_plan(
        self, steps: list[PlanStep], task_id: str = "", on_step: StepCallback | None = None
    ) -> list[StepOutcome]:
        """顺序执行整串步骤。**一旦某步失败立即停止**，不再执行后续动作。

        为什么不是「跑完看结果」：机器人是物理系统，前一步没到位时后续动作可能
        撞到未预期的物体。fail-fast 是这里唯一合理的选择。
        """
        outcomes: list[StepOutcome] = []
        callback = self._resolve_callback(task_id, on_step)
        for index, step in enumerate(steps):
            logger.info("step_start", task_id=task_id, index=index, step=step.label())
            outcome = self.execute_step(index, step, task_id)
            outcomes.append(outcome)
            if callback is not None:
                try:
                    callback(outcome)
                except Exception:
                    logger.exception("step_callback_failed")
            if not outcome.ok:
                logger.warning(
                    "step_failed_stop",
                    task_id=task_id,
                    index=index,
                    skill=step.skill,
                    outcome=outcome.outcome,
                    detail=outcome.detail,
                )
                break
        return outcomes

    # ------------------------------------------------------------------ 急停

    def emergency_stop(self, task_id: str = "", reason: str = "") -> None:
        """尽最大努力停车。急停本身失败只记日志，不能掩盖原始错误。"""
        if not self.registry.has(EMERGENCY_STOP_SKILL):
            logger.warning("emergency_stop_skill_missing", task_id=task_id, reason=reason)
            return
        try:
            self.registry.get(EMERGENCY_STOP_SKILL).execute()
            logger.info("emergency_stop_sent", task_id=task_id, reason=reason)
        except Exception as exc:
            logger.error("emergency_stop_failed", task_id=task_id, error=str(exc), exc_info=True)

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
