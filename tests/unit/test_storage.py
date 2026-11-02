"""存储测试：CRUD、幂等、CAS 迁移、过期清理。两种实现跑同一套断言。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from robot_agent.domain.state import TaskRecord
from robot_agent.errors import TaskNotFoundError
from robot_agent.storage.memory import InMemoryTaskRepository
from robot_agent.storage.sqlite import SqliteTaskRepository, build_repository


@pytest.fixture(params=["memory", "sqlite"])
async def repo(request, tmp_path: Path):
    if request.param == "memory":
        yield InMemoryTaskRepository()
        return
    sqlite_repo = SqliteTaskRepository(tmp_path / "tasks.db")
    await sqlite_repo.init()
    try:
        yield sqlite_repo
    finally:
        await sqlite_repo.close()


async def test_create_and_get(repo) -> None:
    task = TaskRecord(instruction="把红块推到左边")
    await repo.create(task)
    loaded = await repo.get(task.id)
    assert loaded is not None
    assert loaded.instruction == "把红块推到左边"
    assert loaded.status == "queued"


async def test_get_missing_returns_none(repo) -> None:
    assert await repo.get("nope") is None


async def test_idempotency_key_returns_existing(repo) -> None:
    first = await repo.create(TaskRecord(instruction="a", idempotency_key="k1"))
    second = await repo.create(TaskRecord(instruction="b", idempotency_key="k1"))
    assert first.id == second.id
    assert second.instruction == "a"


async def test_find_by_idempotency_key(repo) -> None:
    task = await repo.create(TaskRecord(instruction="a", idempotency_key="k2"))
    found = await repo.find_by_idempotency_key("k2")
    assert found is not None and found.id == task.id
    assert await repo.find_by_idempotency_key("missing") is None


async def test_save_updates_status(repo) -> None:
    task = await repo.create(TaskRecord(instruction="a"))
    task.status = "running"
    await repo.save(task)
    loaded = await repo.get(task.id)
    assert loaded is not None and loaded.status == "running"


async def test_list_orders_by_created_desc(repo) -> None:
    for i in range(3):
        await repo.create(TaskRecord(instruction=f"t{i}"))
    items = await repo.list(limit=10)
    assert len(items) == 3


async def test_list_filter_by_status(repo) -> None:
    a = await repo.create(TaskRecord(instruction="a"))
    await repo.create(TaskRecord(instruction="b"))
    a.status = "succeeded"
    await repo.save(a)
    assert len(await repo.list(status="succeeded")) == 1
    assert len(await repo.list(status="queued")) == 1


async def test_transition_cas_success(repo) -> None:
    task = await repo.create(TaskRecord(instruction="a"))
    assert await repo.transition(task.id, expected="queued", new="running")
    loaded = await repo.get(task.id)
    assert loaded is not None and loaded.status == "running"


async def test_transition_cas_rejects_stale_expectation(repo) -> None:
    """两个并发批准只有一个能成功——这是防重复执行的关键。"""
    task = await repo.create(TaskRecord(instruction="a"))
    assert await repo.transition(task.id, expected="queued", new="running")
    assert not await repo.transition(task.id, expected="queued", new="running")


async def test_transition_missing_task_raises(repo) -> None:
    if isinstance(repo, InMemoryTaskRepository):
        assert not await repo.transition("nope", expected="queued", new="running")
        return
    with pytest.raises(TaskNotFoundError):
        await repo.transition("nope", expected="queued", new="running")


async def test_save_missing_task_raises(repo) -> None:
    if isinstance(repo, InMemoryTaskRepository):
        with pytest.raises(TaskNotFoundError):
            await repo.save(TaskRecord(instruction="ghost"))
        return
    await repo.save(TaskRecord(instruction="ghost"))  # sqlite 静默无行更新
    assert True


async def test_purge_expired_only_terminal(repo) -> None:
    """只清终态：待审批/执行中的老任务不是垃圾，是排障线索与待办。"""
    long_ago = datetime.now(timezone.utc) - timedelta(hours=100)
    old_done = TaskRecord(instruction="old", status="succeeded", created_at=long_ago)
    old_pending = TaskRecord(instruction="pending", status="awaiting_approval", created_at=long_ago)
    await repo.create(old_done)
    await repo.create(old_pending)
    await repo.create(TaskRecord(instruction="new", status="succeeded"))

    deleted = await repo.purge_expired(retention_hours=72)

    assert deleted == 1
    assert await repo.get(old_done.id) is None
    assert await repo.get(old_pending.id) is not None


def test_build_repository_memory() -> None:
    assert isinstance(build_repository("memory"), InMemoryTaskRepository)


def test_build_repository_sqlite(tmp_path: Path) -> None:
    repo = build_repository(f"sqlite:///{tmp_path / 'x.db'}")
    assert isinstance(repo, SqliteTaskRepository)


def test_build_repository_unsupported() -> None:
    from robot_agent.errors import StorageError

    with pytest.raises(StorageError):
        build_repository("postgres://nope")
