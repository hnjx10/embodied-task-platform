"""设备锁测试：用 fakeredis 跑真实 Redis 命令语义，不依赖 Redis 服务。

重点不是「能不能加锁」，而是三个**出错时才会暴露**的点：

1. 释放时 token 不匹配绝不能删——这是分布式锁最经典的 bug，
   删错锁不会报错，只会让互斥在某次超时之后静默失效；
2. TTL 必须真的生效，否则进程崩溃后锁永远不释放；
3. 加锁失败要抛 ``DeviceBusyError`` 而不是静默继续执行。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from robot_agent.config import LockSettings, Settings
from robot_agent.errors import DeviceBusyError
from robot_agent.orchestration.events import EventBus
from robot_agent.orchestration.execution_lock import (
    NullLock,
    RedisLock,
    build_lock,
)
from robot_agent.orchestration.runner import TaskRunner

fakeredis = pytest.importorskip("fakeredis")
from fakeredis.aioredis import FakeRedis  # noqa: E402


def _lock(client=None, **kwargs) -> RedisLock:
    client = client or FakeRedis(decode_responses=True)
    return RedisLock("redis://localhost:6379/0", client=client, **kwargs)


@pytest.fixture
async def client():
    """支持 Lua 的 fakeredis 客户端。

    Lua 需要 ``fakeredis[lua]``（底层是 lupa）。没装时跳过依赖释放语义的用例，
    而不是让整个测试套件红掉——测不了不等于测失败了。
    """
    fake = FakeRedis(decode_responses=True)
    try:
        await fake.eval("return 1", 0)
    except Exception:  # pragma: no cover - 取决于环境是否装了 lupa
        pytest.skip("fakeredis 未启用 Lua 支持（pip install 'fakeredis[lua]'）")
    return fake


# ------------------------------------------------------------------ 互斥语义


async def test_second_acquire_fails_while_held() -> None:
    lock = _lock()
    first = await lock.acquire("robot-a")
    assert first is not None

    second = await lock.acquire("robot-a")
    assert second is None, "同一 key 被两个执行者同时拿到，互斥失效"

    # 不同设备互不影响——锁是按 robot_id 分区的
    assert await lock.acquire("robot-b") is not None


async def test_release_allows_reacquire(client) -> None:
    lock = _lock(client)
    token = await lock.acquire("robot-a")
    assert token is not None
    assert await lock.release("robot-a", token) is True
    assert await lock.acquire("robot-a") is not None


async def test_ttl_expires_the_lock() -> None:
    """进程崩溃后靠 TTL 兜底。TTL 不生效等于永久死锁。"""
    lock = _lock()
    assert await lock.acquire("robot-a", ttl_s=0.05) is not None
    await asyncio.sleep(0.12)
    assert await lock.acquire("robot-a") is not None


async def test_release_does_not_delete_foreign_lock(client) -> None:
    """A 的锁过期、B 拿到锁，此时 A 执行完来释放——绝不能删掉 B 的锁。

    这是分布式锁最隐蔽的失效方式：没有任何报错，互斥只是「有时候不管用」。
    """
    lock = _lock(client)
    stolen = await lock.acquire("robot-a")
    assert stolen is not None

    # 模拟：A 的锁超时过期，B 重新加锁拿到新 token
    await client.set("robot_agent:lock:robot-a", "token-of-b")

    released = await lock.release("robot-a", stolen)
    assert released is False
    assert await client.get("robot_agent:lock:robot-a") == "token-of-b"


async def test_key_prefix_applied() -> None:
    client = FakeRedis(decode_responses=True)
    lock = _lock(client, prefix="custom:")
    await lock.acquire("robot-a")
    assert await client.get("custom:robot-a") is not None


# ------------------------------------------------------------------ 装配


async def test_null_lock_always_succeeds() -> None:
    lock = NullLock()
    assert await lock.acquire("robot-a") is not None
    assert await lock.acquire("robot-a") is not None
    assert await lock.release("robot-a", "x") is True


def test_build_lock_defaults_to_null() -> None:
    assert isinstance(build_lock(LockSettings()), NullLock)


def test_build_lock_requires_url_for_redis() -> None:
    """配了 redis 却没给地址，必须响亮失败，不能悄悄退化成不加锁。"""
    with pytest.raises(RuntimeError, match="redis_url"):
        build_lock(LockSettings(backend="redis"))


def test_redis_lock_from_settings() -> None:
    settings = LockSettings(
        backend="redis",
        redis_url="redis://localhost:6379/9",
        key_prefix="ra:",
        ttl_s=12.0,
    )
    lock = build_lock(settings)
    assert isinstance(lock, RedisLock)
    assert lock.ttl_s == 12.0
    assert lock.prefix == "ra:"


# ------------------------------------------------------------------ 运行器集成


def _runner(lock) -> TaskRunner:
    return TaskRunner(
        settings=Settings(env="test"),
        repository=SimpleNamespace(),
        events=EventBus(),
        audit=SimpleNamespace(record=lambda *a, **k: None),
        # 图签名已带 config（thread_id 用于 checkpoint 命名空间）
        graph=SimpleNamespace(invoke=lambda state, config=None: {"status": "success"}),
        resume_graph=SimpleNamespace(invoke=lambda state, config=None: {"status": "success"}),
        device_lock=lock,
    )


async def test_runner_raises_device_busy_when_lock_held() -> None:
    lock = _lock()
    await lock.acquire("default")
    runner = _runner(lock)

    with pytest.raises(DeviceBusyError) as exc:
        await runner._invoke_graph({}, "task-1")
    assert exc.value.code == "device_busy"
    assert exc.value.retryable is True, "占用是暂时的，调度器应当重试而不是判死"


async def test_runner_releases_lock_after_success(client) -> None:
    lock = _lock(client)
    runner = _runner(lock)
    await runner._invoke_graph({}, "task-1")

    # 执行完必须放开：否则下一个任务永远拿不到锁
    assert await lock.acquire("default") is not None
