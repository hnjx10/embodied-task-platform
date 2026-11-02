"""内存实现：单元测试与本地体验用。进程退出即丢数据。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from robot_agent.domain.state import TaskRecord
from robot_agent.errors import TaskNotFoundError


class InMemoryTaskRepository:
    """用字典保存任务。``transition`` 通过检查当前状态实现 CAS 语义。"""

    def __init__(self) -> None:
        self._tasks: dict[str, TaskRecord] = {}
        self._by_idem: dict[str, str] = {}
        self._lock = asyncio.Lock()

    async def create(self, task: TaskRecord) -> TaskRecord:
        async with self._lock:
            if task.idempotency_key:
                existing = self._by_idem.get(task.idempotency_key)
                if existing:
                    return self._tasks[existing].model_copy(deep=True)
                self._by_idem[task.idempotency_key] = task.id
            self._tasks[task.id] = task.model_copy(deep=True)
            return task.model_copy(deep=True)

    async def get(self, task_id: str) -> TaskRecord | None:
        async with self._lock:
            task = self._tasks.get(task_id)
            return task.model_copy(deep=True) if task else None

    async def save(self, task: TaskRecord) -> None:
        async with self._lock:
            if task.id not in self._tasks:
                raise TaskNotFoundError(f"任务不存在: {task.id}")
            task.updated_at = datetime.now(timezone.utc)
            self._tasks[task.id] = task.model_copy(deep=True)

    async def list(
        self, *, status: str | None = None, limit: int = 50, offset: int = 0
    ) -> list[TaskRecord]:
        async with self._lock:
            items = [t for t in self._tasks.values() if status is None or t.status == status]
        items.sort(key=lambda t: t.created_at, reverse=True)
        return [t.model_copy(deep=True) for t in items[offset : offset + limit]]

    async def find_by_idempotency_key(self, key: str) -> TaskRecord | None:
        async with self._lock:
            task_id = self._by_idem.get(key)
            task = self._tasks.get(task_id) if task_id else None
            return task.model_copy(deep=True) if task else None

    async def transition(
        self, task_id: str, *, expected: str, new: str, patch: dict | None = None
    ) -> bool:
        """仅当当前状态等于 ``expected`` 时才迁移，返回是否成功。"""
        async with self._lock:
            task = self._tasks.get(task_id)
            if task is None or task.status != expected:
                return False
            updated = task.model_copy(
                update={**(patch or {}), "status": new, "updated_at": datetime.now(timezone.utc)}
            )
            self._tasks[task_id] = updated
            return True

    async def purge_expired(self, retention_hours: int) -> int:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=retention_hours)
        async with self._lock:
            # 只清终态：未完成的任务是排障线索，不能当成过期垃圾
            expired = [
                tid for tid, t in self._tasks.items() if t.is_terminal and t.created_at < cutoff
            ]
            for tid in expired:
                self._tasks.pop(tid, None)
        return len(expired)

    async def close(self) -> None:
        return None
