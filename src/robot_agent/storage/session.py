"""仓储的打开与关闭。

CLI 的 ``tasks list/show/purge`` 只需要「开一个仓库、用完关掉」，不需要知道
具体实现是不是 SQLite、要不要建表。这段样板原本在三个子命令里各抄了一遍，
并且把 ``SqliteTaskRepository`` 的 ``isinstance`` 判断泄漏到了交付层。

这里把它收进存储层：调用方只表达「我要一个可用的仓库」。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from robot_agent.storage.base import TaskRepository
from robot_agent.storage.sqlite import build_repository


@asynccontextmanager
async def open_repository(url: str) -> AsyncIterator[TaskRepository]:
    """按配置 URL 打开仓库，退出时保证关闭。

    ``sqlite://`` 会顺带建表并开启 WAL；``memory`` 则是空操作。
    异常路径同样会 ``close()``——运维脚本最怕的是把库锁着退出。
    """
    repo = build_repository(url)
    await repo.open()
    try:
        yield repo
    finally:
        await repo.close()
