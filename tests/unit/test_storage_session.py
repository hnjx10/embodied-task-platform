"""仓储会话（``open_repository``）的行为。

这段逻辑的价值在于「打开即可用、退出必释放」——运维脚本把库锁着退出，
下一次服务启动就会卡在 WAL 上。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from robot_agent.domain.state import TaskRecord
from robot_agent.errors import StorageError
from robot_agent.storage.memory import InMemoryTaskRepository
from robot_agent.storage.session import open_repository


async def test_memory_repository_is_usable_inside_scope() -> None:
    async with open_repository("memory") as repo:
        assert isinstance(repo, InMemoryTaskRepository)
        task = await repo.create(TaskRecord(instruction="把红色方块推到左边"))
        assert (await repo.get(task.id)) is not None


async def test_sqlite_repository_creates_file_and_schema(tmp_path: Path) -> None:
    db = tmp_path / "nested" / "tasks.db"
    async with open_repository(f"sqlite:///{db.as_posix()}") as repo:
        await repo.create(TaskRecord(instruction="走到角落"))

    assert db.exists(), "open() 应当建好父目录与库文件"


async def test_sqlite_repository_is_closed_after_scope(tmp_path: Path) -> None:
    """出作用域后连接必须已关闭：再操作应当报「未初始化」而不是静默成功。"""
    db = tmp_path / "tasks.db"
    async with open_repository(f"sqlite:///{db.as_posix()}") as repo:
        task = await repo.create(TaskRecord(instruction="走一步"))

    with pytest.raises(StorageError):
        await repo.get(task.id)


async def test_close_runs_even_when_body_raises(tmp_path: Path) -> None:
    """异常路径同样要释放连接，否则一次失败就会把库锁住。"""
    db = tmp_path / "tasks.db"
    with pytest.raises(RuntimeError):
        async with open_repository(f"sqlite:///{db.as_posix()}") as repo:
            await repo.open()
            raise RuntimeError("业务代码炸了")

    # 还能重新打开并写入，说明上一个连接确实释放了
    async with open_repository(f"sqlite:///{db.as_posix()}") as repo:
        assert await repo.create(TaskRecord(instruction="重开后仍可写")) is not None


async def test_open_is_idempotent_for_memory() -> None:
    """内存实现的 open() 是空操作，重复调用不该报错（工厂与 CLI 都会调）。"""
    repo = InMemoryTaskRepository()
    await repo.open()
    await repo.open()
    assert await repo.list() == []
