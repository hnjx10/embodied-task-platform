"""后台清理调度器测试。

重点不是「能清理」，而是**边界**：停止后不留孤儿协程、单轮失败不中断后续轮次、
关闭链路里先停它再关数据库。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from robot_agent.orchestration.maintenance import PurgeScheduler


class _FakeRunner:
    def __init__(self, results: list[Any] | None = None) -> None:
        self.calls = 0
        self._results = results or [0]

    async def purge(self) -> int:
        self.calls += 1
        result = self._results[min(self.calls - 1, len(self._results) - 1)]
        if isinstance(result, Exception):
            raise result
        return int(result)


async def test_run_once_returns_deleted_count() -> None:
    runner = _FakeRunner([3])
    scheduler = PurgeScheduler(runner, interval_s=60)  # type: ignore[arg-type]

    assert await scheduler.run_once() == 3
    assert runner.calls == 1


async def test_run_once_swallows_errors() -> None:
    """一次锁冲突不该让保留策略永久停摆。"""
    runner = _FakeRunner([RuntimeError("database is locked")])
    scheduler = PurgeScheduler(runner, interval_s=60)  # type: ignore[arg-type]

    assert await scheduler.run_once() == 0


async def test_loop_runs_periodically_and_stops_cleanly() -> None:
    runner = _FakeRunner([1])
    scheduler = PurgeScheduler(runner, interval_s=0.01)  # type: ignore[arg-type]

    await scheduler.start()
    await asyncio.sleep(0.15)
    await scheduler.stop()

    assert runner.calls >= 2, "调度器应当按周期反复清理"
    assert scheduler._task is None


async def test_stop_is_idempotent() -> None:
    scheduler = PurgeScheduler(_FakeRunner(), interval_s=60)  # type: ignore[arg-type]

    await scheduler.stop()  # 从未启动也不该炸
    await scheduler.start()
    await scheduler.stop()
    await scheduler.stop()


async def test_start_is_idempotent() -> None:
    scheduler = PurgeScheduler(_FakeRunner(), interval_s=60)  # type: ignore[arg-type]

    await scheduler.start()
    first = scheduler._task
    await scheduler.start()
    assert scheduler._task is first, "重复 start 不应派生第二个协程"

    await scheduler.stop()


async def test_failure_does_not_kill_the_loop() -> None:
    """第一轮抛错，第二轮必须照常进行。"""
    runner = _FakeRunner([RuntimeError("locked"), 2])
    scheduler = PurgeScheduler(runner, interval_s=0.01)  # type: ignore[arg-type]

    await scheduler.start()
    await asyncio.sleep(0.35)  # 首轮要打一次异常堆栈，窗口留宽些
    await scheduler.stop()

    assert runner.calls >= 2


async def test_interval_must_be_positive() -> None:
    """配置层已经约束 > 0，这里确认 0 不会被静默当成「永不清理」。"""
    from pydantic import ValidationError

    from robot_agent.config import StorageSettings

    with pytest.raises(ValidationError):
        StorageSettings(purge_interval_s=0)
