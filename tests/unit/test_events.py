"""事件总线测试：投递、背压、终态哨兵、订阅上限、已结束集合的边界。"""

from __future__ import annotations

import asyncio
import threading

import pytest

from robot_agent import metrics
from robot_agent.errors import TooManySubscribersError
from robot_agent.orchestration.events import END_SENTINEL, EventBus, TaskEvent


def _event(task_id: str = "t1", seq: int = 0) -> TaskEvent:
    return TaskEvent(task_id=task_id, type="step_ok", data={"seq": seq})


async def test_publish_delivers_to_all_subscribers() -> None:
    bus = EventBus()
    first = bus.subscribe("t1")
    second = bus.subscribe("t1")

    bus.publish(_event(seq=1))

    assert (await first.get()).data["seq"] == 1
    assert (await second.get()).data["seq"] == 1


async def test_other_task_does_not_receive() -> None:
    bus = EventBus()
    queue = bus.subscribe("t1")

    bus.publish(_event(task_id="t2"))

    assert queue.empty()


async def test_backpressure_drops_oldest_keeps_newest() -> None:
    """客户端消费不过来时，宁可丢旧的也要保证最新状态可达。"""
    bus = EventBus(max_queue=2)
    queue = bus.subscribe("t1")

    for seq in range(5):
        bus.publish(_event(seq=seq))

    assert queue.qsize() == 2
    remaining = [queue.get_nowait().data["seq"] for _ in range(2)]  # type: ignore[union-attr]
    assert remaining == [3, 4]


async def test_close_sends_sentinel_and_cleans_up() -> None:
    bus = EventBus()
    queue = bus.subscribe("t1")

    bus.close("t1")

    assert await queue.get() is None
    assert "t1" not in bus._subscribers


async def test_subscribe_after_close_gets_sentinel_immediately() -> None:
    """任务在客户端订阅前就结束了，不该让连接挂着等一个永不到来的事件。"""
    bus = EventBus()
    bus.close("t1")

    queue = bus.subscribe("t1")

    assert queue.get_nowait() is None


async def test_subscriber_cap_enforced() -> None:
    bus = EventBus(max_subscribers_per_task=2)
    bus.subscribe("t1")
    bus.subscribe("t1")

    before = metrics.EVENT_SUBSCRIBER_REJECTED_TOTAL._value.get()  # type: ignore[attr-defined]
    with pytest.raises(TooManySubscribersError) as exc:
        bus.subscribe("t1")
    after = metrics.EVENT_SUBSCRIBER_REJECTED_TOTAL._value.get()  # type: ignore[attr-defined]

    assert exc.value.http_status == 429
    assert exc.value.retryable is True
    assert exc.value.details == {"task_id": "t1", "limit": 2}
    assert after == before + 1


async def test_cap_is_per_task_not_global() -> None:
    bus = EventBus(max_subscribers_per_task=1)
    bus.subscribe("t1")
    bus.subscribe("t2")  # 另一个任务不受影响


async def test_closed_set_is_bounded() -> None:
    """跑一整天不能把「已结束任务」攒成一张永不释放的表。"""
    bus = EventBus(closed_task_limit=3)
    for i in range(10):
        bus.close(f"t{i}")

    assert len(bus._closed_set) == 3
    assert bus._closed_set == {"t7", "t8", "t9"}
    # 被淘汰的老任务不再被当作「已结束」，重新订阅会走正常路径
    queue = bus.subscribe("t0")
    assert queue.empty()


async def test_stream_ends_on_close() -> None:
    bus = EventBus()
    received: list[str] = []

    async def consume() -> None:
        async for event in bus.stream("t1"):
            received.append(event.type)

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)  # 让 stream 先订阅上
    bus.publish(_event())
    bus.close("t1")
    await asyncio.wait_for(task, timeout=2.0)

    assert received == ["step_ok"]
    assert "t1" not in bus._subscribers


async def test_publish_threadsafe_from_worker_thread() -> None:
    """技能跑在线程池里，事件必须能跨线程安全投递。"""
    bus = EventBus()
    bus.bind_loop()
    queue = bus.subscribe("t1")

    thread = threading.Thread(target=bus.publish_threadsafe, args=(_event(seq=7),))
    thread.start()
    thread.join()

    event = await asyncio.wait_for(queue.get(), timeout=2.0)
    assert event is not None
    assert event.data["seq"] == 7


def test_end_sentinel_constant() -> None:
    assert END_SENTINEL == "__end__"
