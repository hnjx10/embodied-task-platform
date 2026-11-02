"""后台维护任务：按保留期清理过期任务。

配置里的 ``storage.retention_hours`` 只有真的被周期性调用才有意义——否则它就只是
一行「我们打算清理」的注释，数据库照样无限增长。这里把它变成一个有起停边界的
后台协程：

* 启动在应用的 lifespan 里，随进程退出一起取消，不留孤儿任务；
* 清理失败只记日志并继续下一轮——一次锁冲突不该让保留策略永久停摆；
* 单轮异常不影响后续轮次，因此 ``while not stopped`` 包住整轮而不是包住单次清理。
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING

from robot_agent.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from robot_agent.orchestration.runner import TaskRunner

logger = get_logger(__name__)


class PurgeScheduler:
    """周期性调用 ``TaskRunner.purge()``。"""

    def __init__(self, runner: TaskRunner, interval_s: float) -> None:
        self.runner = runner
        self.interval_s = interval_s
        self._task: asyncio.Task[None] | None = None
        self._stopped = asyncio.Event()

    async def run_once(self) -> int:
        """清理一轮，返回删除条数。异常不外抛，避免拖垮后台循环。"""
        try:
            deleted = await self.runner.purge()
        except Exception:
            logger.warning("purge_failed", exc_info=True)
            return 0
        if deleted:
            logger.info("purge_done", deleted=deleted)
        return deleted

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stopped.clear()
        self._task = asyncio.create_task(self._loop(), name="purge-scheduler")
        logger.info("purge_scheduler_started", interval_s=self.interval_s)

    async def stop(self) -> None:
        self._stopped.set()
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        logger.info("purge_scheduler_stopped")

    async def _loop(self) -> None:
        while not self._stopped.is_set():
            try:
                # 用 wait_for 而不是 sleep，停下时不必等满一个周期
                await asyncio.wait_for(self._stopped.wait(), timeout=self.interval_s)
                return  # 收到停止信号
            except TimeoutError:
                await self.run_once()
