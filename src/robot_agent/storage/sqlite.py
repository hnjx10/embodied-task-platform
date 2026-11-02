"""SQLite 实现：单机生产可用，零外部依赖。

* 开启 WAL，读写不互相阻塞；
* 任务整体序列化为 JSON 存 ``payload``，同时把 ``status`` / ``created_at`` 冗余成列
  以保证列表与过期清理走索引，不必反序列化全表；
* ``transition`` 用 ``UPDATE ... WHERE status = ?`` 实现 CAS，避免并发审批导致重复执行。
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosqlite

from robot_agent.domain.state import TERMINAL_STATUSES, TaskRecord
from robot_agent.errors import StorageError, TaskNotFoundError
from robot_agent.logging import get_logger
from robot_agent.storage.base import TaskRepository

logger = get_logger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id              TEXT PRIMARY KEY,
    status          TEXT NOT NULL,
    instruction     TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    idempotency_key TEXT,
    trace_id        TEXT,
    payload         TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_idem
    ON tasks(idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_tasks_status_created
    ON tasks(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tasks_created
    ON tasks(created_at);
"""


class SqliteTaskRepository:
    """基于 aiosqlite 的任务仓库。一个实例一个连接，写操作串行化。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._db: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ 生命周期

    async def init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA synchronous=NORMAL")
        await self._db.execute("PRAGMA busy_timeout=5000")
        await self._db.executescript(_SCHEMA)
        await self._db.commit()
        logger.info("storage_ready", backend="sqlite", path=str(self.path))

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    def _conn(self) -> aiosqlite.Connection:
        if self._db is None:
            raise StorageError("存储未初始化，请先 await repository.init()")
        return self._db

    # ------------------------------------------------------------------ CRUD

    async def create(self, task: TaskRecord) -> TaskRecord:
        async with self._lock:
            db = self._conn()
            if task.idempotency_key:
                existing = await self._find_idem_unlocked(task.idempotency_key)
                if existing is not None:
                    return existing
            try:
                await db.execute(
                    "INSERT INTO tasks (id, status, instruction, created_at, updated_at,"
                    " idempotency_key, trace_id, payload) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        task.id,
                        task.status,
                        task.instruction,
                        task.created_at.isoformat(),
                        task.updated_at.isoformat(),
                        task.idempotency_key,
                        task.trace_id,
                        _dump(task),
                    ),
                )
                await db.commit()
            except aiosqlite.IntegrityError as exc:
                raise StorageError(f"任务写入冲突: {exc}", cause=exc) from exc
            return task.model_copy(deep=True)

    async def get(self, task_id: str) -> TaskRecord | None:
        db = self._conn()
        async with db.execute("SELECT payload FROM tasks WHERE id = ?", (task_id,)) as cur:
            row = await cur.fetchone()
        return _load(row["payload"]) if row else None

    async def save(self, task: TaskRecord) -> None:
        async with self._lock:
            db = self._conn()
            task.updated_at = datetime.now(timezone.utc)
            await db.execute(
                "UPDATE tasks SET status = ?, updated_at = ?, payload = ? WHERE id = ?",
                (task.status, task.updated_at.isoformat(), _dump(task), task.id),
            )
            await db.commit()

    async def list(
        self, *, status: str | None = None, limit: int = 50, offset: int = 0
    ) -> list[TaskRecord]:
        db = self._conn()
        if status:
            query = (
                "SELECT payload FROM tasks WHERE status = ? "
                "ORDER BY created_at DESC LIMIT ? OFFSET ?"
            )
            params: tuple = (status, limit, offset)
        else:
            query = "SELECT payload FROM tasks ORDER BY created_at DESC LIMIT ? OFFSET ?"
            params = (limit, offset)
        async with db.execute(query, params) as cur:
            rows = await cur.fetchall()
        return [_load(r["payload"]) for r in rows]

    async def find_by_idempotency_key(self, key: str) -> TaskRecord | None:
        async with self._lock:
            return await self._find_idem_unlocked(key)

    async def _find_idem_unlocked(self, key: str) -> TaskRecord | None:
        db = self._conn()
        async with db.execute("SELECT payload FROM tasks WHERE idempotency_key = ?", (key,)) as cur:
            row = await cur.fetchone()
        return _load(row["payload"]) if row else None

    # ------------------------------------------------------------------ CAS

    async def transition(
        self, task_id: str, *, expected: str, new: str, patch: dict | None = None
    ) -> bool:
        """比较并交换：仅当库中状态仍是 ``expected`` 时才改为 ``new``。"""
        async with self._lock:
            db = self._conn()
            current = await self.get(task_id)
            if current is None:
                raise TaskNotFoundError(f"任务不存在: {task_id}")
            if current.status != expected:
                return False
            updated = current.model_copy(
                update={**(patch or {}), "status": new, "updated_at": datetime.now(timezone.utc)}
            )
            await db.execute(
                "UPDATE tasks SET status = ?, updated_at = ?, payload = ? "
                "WHERE id = ? AND status = ?",
                (new, updated.updated_at.isoformat(), _dump(updated), task_id, expected),
            )
            await db.commit()
            return True

    # ------------------------------------------------------------------ 维护

    async def purge_expired(self, retention_hours: int) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=retention_hours)).isoformat()
        # 只清终态：删掉 awaiting_approval 等于把待人工决策的记录藏起来，
        # 删掉 running 等于在机器人还在动的时候销毁现场。
        placeholders = ", ".join("?" * len(TERMINAL_STATUSES))
        params = (cutoff, *sorted(TERMINAL_STATUSES))
        async with self._lock:
            db = self._conn()
            cur = await db.execute(
                f"DELETE FROM tasks WHERE created_at < ? AND status IN ({placeholders})",  # noqa: S608 - 占位符由常量长度生成
                params,
            )
            await db.commit()
            deleted = cur.rowcount or 0
        if deleted:
            logger.info("storage_purged", deleted=deleted, retention_hours=retention_hours)
        return deleted


# --------------------------------------------------------------------- 工具


def _dump(task: TaskRecord) -> str:
    return json.dumps(task.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":"))


def _load(payload: str) -> TaskRecord:
    return TaskRecord.model_validate(json.loads(payload))


def build_repository(settings_url: str) -> TaskRepository:
    """按配置 URL 装配仓库。``sqlite://`` 前缀解析为文件路径。"""
    if settings_url in {"memory", "memory://", ""}:
        from robot_agent.storage.memory import InMemoryTaskRepository

        return InMemoryTaskRepository()
    if settings_url.startswith("sqlite://"):
        raw = settings_url[len("sqlite://") :]
        # sqlite:////abs/path.db → /abs/path.db；sqlite:///rel.db → rel.db
        path = raw[1:] if raw.startswith("/") and len(raw) > 1 else raw
        return SqliteTaskRepository(Path(path))
    raise StorageError(f"不支持的存储 URL: {settings_url}（支持 memory / sqlite:///path/to.db）")
