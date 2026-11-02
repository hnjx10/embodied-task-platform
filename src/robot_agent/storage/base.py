"""任务存储抽象。

编排层与 API 层只依赖 :class:`TaskRepository` 协议，内存实现用于测试，
SQLite 实现用于单机生产，未来换 PostgreSQL 只需新增一个实现。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from robot_agent.domain.state import TaskRecord


@runtime_checkable
class TaskRepository(Protocol):
    """任务仓库协议。全部方法为异步，避免阻塞事件循环。"""

    async def create(self, task: TaskRecord) -> TaskRecord: ...

    async def get(self, task_id: str) -> TaskRecord | None: ...

    async def save(self, task: TaskRecord) -> None: ...

    async def list(
        self, *, status: str | None = None, limit: int = 50, offset: int = 0
    ) -> list[TaskRecord]: ...

    async def find_by_idempotency_key(self, key: str) -> TaskRecord | None: ...

    async def transition(
        self, task_id: str, *, expected: str, new: str, patch: dict | None = None
    ) -> bool: ...

    async def purge_expired(self, retention_hours: int) -> int: ...

    async def close(self) -> None: ...
