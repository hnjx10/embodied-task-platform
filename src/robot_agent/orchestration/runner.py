"""任务运行器：把「提交 → 执行 → 审批 → 收尾」的生命周期收口到一处。

并发模型：

* 任务级并发由 ``max_concurrent_tasks`` 限制（HTTP 层多个请求可同时进来）；
* **物理动作串行**由 :class:`asyncio.Semaphore` 强制为 1——一台机器人同一时刻
  只能执行一个动作序列，这是物理约束而不是性能取舍；
* 图是同步的（技能调用阻塞），用 ``asyncio.to_thread`` 把执行挪出事件循环，
  避免卡住 HTTP 健康检查。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from robot_agent import metrics
from robot_agent.audit import AuditLog
from robot_agent.config import Settings
from robot_agent.domain.plan import PlanStep, StepOutcome
from robot_agent.domain.state import TaskRecord, new_task_id, utcnow
from robot_agent.errors import (
    RobotAgentError,
    TaskConflictError,
    TaskNotFoundError,
)
from robot_agent.logging import get_logger
from robot_agent.orchestration.events import EventBus, TaskEvent
from robot_agent.orchestration.graph import initial_state, record_task_metrics
from robot_agent.storage.base import TaskRepository

logger = get_logger(__name__)


def _normalize_instruction(text: str) -> str:
    """幂等比较时忽略首尾与连续空白：仅排版差异不该算「另一条指令」。"""
    return " ".join(text.split())


class TaskRunner:
    """任务生命周期管理。"""

    def __init__(
        self,
        *,
        settings: Settings,
        repository: TaskRepository,
        events: EventBus,
        audit: AuditLog,
        graph: Any,
        resume_graph: Any,
        executor: Any = None,
    ) -> None:
        self.settings = settings
        self.repo = repository
        self.events = events
        self.audit = audit
        self.graph = graph
        self.resume_graph = resume_graph
        self.executor = executor
        self._motion_semaphore = asyncio.Semaphore(1)
        self._task_semaphore = asyncio.Semaphore(settings.api.max_concurrent_tasks)
        self._running: dict[str, asyncio.Task[Any]] = {}

    # ------------------------------------------------------------------ 提交

    async def submit(
        self,
        instruction: str,
        *,
        idempotency_key: str | None = None,
        trace_id: str | None = None,
        requested_by: str | None = None,
    ) -> TaskRecord:
        """创建任务记录。相同幂等键返回已有任务，不重复执行。"""
        if idempotency_key:
            existing = await self.repo.find_by_idempotency_key(idempotency_key)
            if existing is not None:
                if _normalize_instruction(existing.instruction) != _normalize_instruction(
                    instruction
                ):
                    # 同一个键配不同指令，是客户端 bug 或键复用事故。
                    # 静默返回旧任务会让调用方拿着别人的执行结果当自己的。
                    raise TaskConflictError(
                        f"幂等键 {idempotency_key} 已被另一条指令占用",
                        details={
                            "idempotency_key": idempotency_key,
                            "existing_task_id": existing.id,
                            "existing_instruction": existing.instruction,
                        },
                    )
                logger.info("task_idempotent_hit", task_id=existing.id, key=idempotency_key)
                return existing

        record = TaskRecord(
            id=new_task_id(),
            instruction=instruction,
            idempotency_key=idempotency_key,
            trace_id=trace_id,
            requested_by=requested_by,
        )
        created = await self.repo.create(record)
        self.audit.record(
            "task_submitted",
            {"instruction": instruction, "requested_by": requested_by},
            task_id=created.id,
            trace_id=trace_id,
        )
        self.events.publish(TaskEvent(created.id, "status", {"status": created.status}))
        logger.info("task_submitted", task_id=created.id)
        return created

    # ------------------------------------------------------------------ 执行

    async def execute(self, task_id: str) -> TaskRecord:
        """同步执行（阻塞到终态或待审批）。API 的后台任务与 CLI 都调用它。"""
        record = await self._require(task_id)
        if record.is_terminal:
            return record
        if record.status == "awaiting_approval":
            return record

        acquired = await self.repo.transition(
            task_id,
            expected=record.status,
            new="running",
            patch={"started_at": record.started_at or utcnow()},
        )
        if not acquired:
            # 已被其它执行者接管（例如两个请求同时触发），直接返回当前状态
            return await self._require(task_id)

        record = await self._require(task_id)
        started = time.perf_counter()
        state = initial_state(task_id, record.instruction)
        self.events.bind_loop()
        self._bind_step_callback(task_id)
        self.events.publish(
            TaskEvent(task_id, "status", {"status": "running", "instruction": record.instruction})
        )
        metrics.TASKS_IN_FLIGHT.inc()

        try:
            async with self._task_semaphore, self._motion_semaphore:
                final = await asyncio.to_thread(self.graph.invoke, state)
        except Exception as exc:
            logger.exception("task_crashed", task_id=task_id)
            return await self._finalize(
                record,
                status="failed",
                report=f"任务执行异常: {exc}",
                error={"code": "internal_error", "message": str(exc)},
                duration=time.perf_counter() - started,
            )
        else:
            return await self._apply_final_state(record, final, time.perf_counter() - started)
        finally:
            metrics.TASKS_IN_FLIGHT.dec()
            if self.executor is not None:
                self.executor.unregister_callback(task_id)
            self._running.pop(task_id, None)

    async def execute_background(self, task_id: str) -> asyncio.Task[Any]:
        """提交到后台执行，返回 asyncio.Task 便于测试等待或取消。"""
        task = asyncio.create_task(self.execute(task_id), name=f"task:{task_id}")
        self._running[task_id] = task
        return task

    # ------------------------------------------------------------------ 审批

    async def approve(
        self, task_id: str, *, approved_by: str = "operator", note: str = ""
    ) -> TaskRecord:
        """人工批准后继续执行。CAS 保证并发批准只有一个生效。"""
        record = await self._require(task_id)
        if record.status != "awaiting_approval":
            raise TaskConflictError(
                f"任务 {task_id} 当前状态为 {record.status}，不可批准",
                details={"status": record.status},
            )
        approval = {
            "approved_by": approved_by,
            "approved_at": utcnow().isoformat(),
            "note": note,
        }
        ok = await self.repo.transition(
            task_id, expected="awaiting_approval", new="running", patch={"approval": approval}
        )
        if not ok:
            raise TaskConflictError(f"任务 {task_id} 状态已被其它操作修改，批准未生效")
        self.audit.record("task_approved", approval, task_id=task_id)
        logger.info("task_approved", task_id=task_id, approved_by=approved_by)

        record = await self._require(task_id)
        started = time.perf_counter()
        state = initial_state(task_id, record.instruction)
        state["plan"] = [s.model_dump() for s in record.plan]
        state["plan_source"] = record.plan_source
        state["status"] = "running"
        state["approved"] = True
        self.events.bind_loop()
        self._bind_step_callback(task_id)
        metrics.TASKS_IN_FLIGHT.inc()

        try:
            async with self._task_semaphore, self._motion_semaphore:
                final = await asyncio.to_thread(self.resume_graph.invoke, state)
        except Exception as exc:
            logger.exception("task_resume_crashed", task_id=task_id)
            return await self._finalize(
                record,
                status="failed",
                report=f"续跑异常: {exc}",
                error={"code": "internal_error", "message": str(exc)},
                duration=time.perf_counter() - started,
            )
        else:
            return await self._apply_final_state(record, final, time.perf_counter() - started)
        finally:
            metrics.TASKS_IN_FLIGHT.dec()
            if self.executor is not None:
                self.executor.unregister_callback(task_id)

    async def reject(self, task_id: str, *, reason: str = "operator rejected") -> TaskRecord:
        """人工驳回：任务直接进入 blocked，不执行任何动作。"""
        record = await self._require(task_id)
        if record.status != "awaiting_approval":
            raise TaskConflictError(f"任务 {task_id} 当前状态为 {record.status}，不可驳回")
        ok = await self.repo.transition(
            task_id,
            expected="awaiting_approval",
            new="blocked",
            patch={"finished_at": utcnow(), "report": f"人工驳回: {reason}"},
        )
        if not ok:
            raise TaskConflictError(f"任务 {task_id} 状态已被其它操作修改")
        self.audit.record("task_rejected", {"reason": reason}, task_id=task_id)
        self.events.publish(TaskEvent(task_id, "status", {"status": "blocked", "reason": reason}))
        self.events.close(task_id)
        return await self._require(task_id)

    async def cancel(self, task_id: str, *, reason: str = "cancelled") -> TaskRecord:
        """取消排队中/待审批的任务。执行中的任务只标记取消请求，不中断动作。"""
        record = await self._require(task_id)
        if record.is_terminal:
            return record
        for expected in ("queued", "awaiting_approval"):
            if await self.repo.transition(
                task_id,
                expected=expected,
                new="cancelled",
                patch={"finished_at": utcnow(), "report": f"已取消: {reason}"},
            ):
                self.audit.record("task_cancelled", {"reason": reason}, task_id=task_id)
                self.events.publish(TaskEvent(task_id, "status", {"status": "cancelled"}))
                self.events.close(task_id)
                return await self._require(task_id)
        raise TaskConflictError(
            f"任务 {task_id} 当前状态为 {record.status}，无法取消（执行中的任务请通过急停处理）"
        )

    # ------------------------------------------------------------------ 查询

    async def get(self, task_id: str) -> TaskRecord:
        return await self._require(task_id)

    async def list(
        self, *, status: str | None = None, limit: int = 50, offset: int = 0
    ) -> list[TaskRecord]:
        return await self.repo.list(status=status, limit=limit, offset=offset)

    async def count(self, *, status: str | None = None) -> int:
        return await self.repo.count(status=status)

    async def purge(self) -> int:
        return await self.repo.purge_expired(self.settings.storage.retention_hours)

    async def shutdown(self, timeout: float = 10.0) -> None:
        """优雅关闭：等待在飞任务收尾，超时才取消。

        直接关连接会让还在写库的后台任务抛 ``Cannot operate on a closed database``，
        任务状态停在 running 成为「僵尸任务」。宁可多等几秒，也要让状态落到终态。
        """
        pending = [t for t in self._running.values() if not t.done()]
        if not pending:
            return
        logger.info("shutdown_waiting_for_tasks", count=len(pending), timeout_s=timeout)
        _done, still = await asyncio.wait(pending, timeout=timeout)
        for task in still:
            task.cancel()
        if still:
            await asyncio.gather(*still, return_exceptions=True)
            logger.warning("shutdown_force_cancelled", count=len(still))
        self._running.clear()

    # ------------------------------------------------------------------ 内部

    async def _require(self, task_id: str) -> TaskRecord:
        record = await self.repo.get(task_id)
        if record is None:
            raise TaskNotFoundError(f"任务不存在: {task_id}", details={"task_id": task_id})
        return record

    def _bind_step_callback(self, task_id: str) -> None:
        if self.executor is None:
            return

        def _on_step(outcome: StepOutcome) -> None:
            self.events.publish_threadsafe(TaskEvent(task_id, "step", outcome.to_dict()))

        self.executor.register_callback(task_id, _on_step)

    async def _apply_final_state(
        self, record: TaskRecord, final: dict[str, Any], duration: float
    ) -> TaskRecord:
        status = str(final.get("status") or "failed")
        plan = [PlanStep.model_validate(s) for s in final.get("plan", [])]
        outcomes = [StepOutcome.model_validate(o) for o in final.get("outcomes", [])]
        corrections = list(final.get("corrections", []))
        verifications = list(final.get("verifications", []))
        report = str(final.get("report", ""))
        error = final.get("error")

        if status == "awaiting_approval":
            target = "awaiting_approval"
        elif status == "blocked":
            target = "blocked"
        elif status == "failed" or error:
            target = "failed"
        elif (
            outcomes
            and all(o.ok for o in outcomes)
            and all(v.get("ok", True) for v in verifications)
        ):
            target = "succeeded"
        else:
            target = "failed"

        patch: dict[str, Any] = {
            "plan": plan,
            "plan_source": final.get("plan_source", record.plan_source),
            "plan_warnings": list(final.get("plan_warnings", [])),
            "outcomes": outcomes,
            "verifications": verifications,
            "corrections": corrections,
            "report": report,
            "requires_approval": bool(final.get("requires_approval", False)),
            "error": error,
        }
        if target != "awaiting_approval":
            patch["finished_at"] = utcnow()

        return await self._finalize(record, status=target, duration=duration, patch=patch)

    async def _finalize(
        self,
        record: TaskRecord,
        *,
        status: str,
        duration: float,
        report: str | None = None,
        error: dict[str, Any] | None = None,
        patch: dict[str, Any] | None = None,
    ) -> TaskRecord:
        """统一收尾：落库、审计、指标、事件。"""
        updates = dict(patch or {})
        updates["status"] = status
        if report is not None:
            updates["report"] = report
        if error is not None:
            updates["error"] = error
        if status != "awaiting_approval":
            updates.setdefault("finished_at", utcnow())

        updated = record.model_copy(update=updates)
        try:
            await self.repo.save(updated)
        except RobotAgentError:
            logger.exception("task_persist_failed", task_id=record.id)

        record_task_metrics(status, duration)
        self.audit.record(
            "task_finished",
            {
                "status": status,
                "duration_s": round(duration, 3),
                "steps": len(updated.outcomes),
            },
            task_id=record.id,
        )
        self.events.publish(
            TaskEvent(
                record.id,
                "status",
                {"status": status, "report": updated.report, "duration_s": round(duration, 3)},
            )
        )
        if status != "awaiting_approval":
            self.events.close(record.id)
        logger.info(
            "task_finished", task_id=record.id, status=status, duration_s=round(duration, 3)
        )
        return updated
