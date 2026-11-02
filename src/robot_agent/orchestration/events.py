"""任务事件总线：把执行进度实时推给 SSE / WebSocket 订阅者。

实现要点：

* **线程安全**：技能执行跑在线程池里（ROS 调用是阻塞的），事件从工作线程
  ``call_soon_threadsafe`` 投递回事件循环，避免跨线程操作 asyncio 队列。
* **背压**：订阅队列满时丢弃最旧事件而不是阻塞生产者——客户端慢不能拖慢机器人。
* **终态关闭**：任务进入终态后投递 ``__end__`` 哨兵，订阅流自然结束，
  不会让 HTTP 连接悬挂。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from robot_agent.logging import get_logger

logger = get_logger(__name__)

END_SENTINEL = "__end__"


@dataclass
class TaskEvent:
    """一条任务事件。``type`` 供客户端做分支处理。"""

    task_id: str
    type: str
    data: dict[str, Any] = field(default_factory=dict)
    at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class EventBus:
    """按 ``task_id`` 分组的内存广播。单进程内有效，多实例需换 Redis Pub/Sub。"""

    def __init__(self, max_queue: int = 256) -> None:
        self._subscribers: dict[str, set[asyncio.Queue[TaskEvent | None]]] = {}
        self._closed: set[str] = set()
        self._max_queue = max_queue
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        """在事件循环线程里调用一次，供工作线程回调使用。"""
        self._loop = loop or asyncio.get_running_loop()

    def subscribe(self, task_id: str) -> asyncio.Queue[TaskEvent | None]:
        queue: asyncio.Queue[TaskEvent | None] = asyncio.Queue(maxsize=self._max_queue)
        self._subscribers.setdefault(task_id, set()).add(queue)
        return queue

    def unsubscribe(self, task_id: str, queue: asyncio.Queue[TaskEvent | None]) -> None:
        subs = self._subscribers.get(task_id)
        if not subs:
            return
        subs.discard(queue)
        if not subs:
            self._subscribers.pop(task_id, None)

    def publish(self, event: TaskEvent) -> None:
        """从任意线程投递事件。"""
        for queue in list(self._subscribers.get(event.task_id, ())):
            self._offer(queue, event)

    def publish_threadsafe(self, event: TaskEvent) -> None:
        """供工作线程调用。"""
        loop = self._loop
        if loop is None or loop.is_closed():
            self.publish(event)
            return
        try:
            loop.call_soon_threadsafe(self.publish, event)
        except RuntimeError:  # pragma: no cover - 循环正在关闭
            logger.debug("eventbus_loop_closed", task_id=event.task_id)

    def close(self, task_id: str) -> None:
        """投递结束哨兵并清理订阅者。"""
        self._closed.add(task_id)
        for queue in list(self._subscribers.get(task_id, ())):
            self._offer(queue, None)
        self._subscribers.pop(task_id, None)

    def _offer(self, queue: asyncio.Queue[TaskEvent | None], item: TaskEvent | None) -> None:
        try:
            queue.put_nowait(item)
        except asyncio.QueueFull:
            # 客户端消费不过来：丢最旧的，保证最新状态可达
            try:
                queue.get_nowait()
                queue.put_nowait(item)
            except (asyncio.QueueEmpty, asyncio.QueueFull):  # pragma: no cover
                pass

    async def stream(self, task_id: str) -> AsyncIterator[TaskEvent]:
        """订阅一个任务的事件流。"""
        queue = self.subscribe(task_id)
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                yield item
        finally:
            self.unsubscribe(task_id, queue)
