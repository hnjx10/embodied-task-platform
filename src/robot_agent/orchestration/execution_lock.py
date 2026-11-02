"""设备级执行锁：防止同一台机器人被两个执行者同时驱动。

**为什么单进程也需要谈这个**

单实例部署时，进程内的 ``asyncio.Semaphore(1)`` 已经把所有物理动作串行化了，
再上一层分布式锁是纯开销。但只要出现下面任一情况，进程内锁就立刻失效：

* 同一套服务起两个副本（滚动发布时新旧 Pod 会短暂共存）；
* CLI 和 HTTP 服务同时对着同一台机器人发指令；
* 一个任务在 A 实例跑，操作员在 B 实例点了「批准续跑」。

这时两个进程各自认为自己持有「唯一的」执行权，同时向 ``/cmd_vel`` 发布指令，
表现为机器人动作互相打断、位姿跳变，而两边日志都显示「执行成功」。
所以锁的粒度不是任务，而是**设备**。

**两个必须做对的地方**

1. ``SET key token NX PX ttl`` 必须是**一条**命令。拆成 ``SETNX`` + ``EXPIRE``
   两步，进程如果在两条之间挂掉，这把锁就永远没有过期时间，只能人工去删。
2. 释放必须**比对 token** 再删。设想 A 的锁超时自动过期、B 随即拿到锁，
   此时 A 执行完去删锁——删掉的是 B 的锁，互斥当场失效。用 Lua 把
   「读-比较-删」做成原子操作，才能避免这个经典错误。

**已知局限（诚实记录）**：本实现不做自动续约。TTL 设得比最长任务短，长任务
执行途中锁会过期；设得太长，进程崩溃后要等很久才解锁。真实生产里通常加一个
watchdog 定期 ``PEXPIRE`` 续约，或改成 Redlock。这里没做，因为本项目的执行
单元有硬超时（``executor.step_timeout_s``），TTL 取该超时加余量即可覆盖——
这个前提一旦变化，就必须补续约。
"""

from __future__ import annotations

import uuid
from typing import Any, Protocol, runtime_checkable

from robot_agent.config import LockSettings
from robot_agent.logging import get_logger

logger = get_logger(__name__)

try:  # pragma: no cover - 取决于是否装了 redis
    from redis.asyncio import Redis

    REDIS_AVAILABLE = True
except ImportError:  # pragma: no cover
    Redis = None  # type: ignore[assignment,misc]
    REDIS_AVAILABLE = False


#: 只有 token 仍然属于自己时才删除。返回 1 表示删掉了，0 表示已被别人持有。
_RELEASE_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""


@runtime_checkable
class ExecutionLock(Protocol):
    """设备锁契约。实现必须是异步的——执行路径全程 await，不能有同步阻塞。"""

    async def acquire(self, key: str, *, ttl_s: float | None = None) -> str | None:
        """尝试加锁。成功返回持有令牌，失败返回 None（不阻塞等待）。"""
        ...

    async def release(self, key: str, token: str) -> bool:
        """释放锁。token 不匹配时返回 False 且不删除。"""
        ...

    async def aclose(self) -> None: ...


class NullLock:
    """不加锁。单实例部署用这个：进程内信号量已经够了。"""

    async def acquire(self, key: str, *, ttl_s: float | None = None) -> str | None:
        return "noop"

    async def release(self, key: str, token: str) -> bool:
        return True

    async def aclose(self) -> None:
        return None


class RedisLock:
    """基于 Redis 的 ``SET NX PX`` 锁。"""

    def __init__(
        self,
        url: str,
        *,
        prefix: str = "robot_agent:lock:",
        ttl_s: float = 30.0,
        client: Any = None,
    ) -> None:
        if client is not None:
            self._client = client
        else:
            if not REDIS_AVAILABLE:
                raise RuntimeError(
                    "未安装 redis 包，无法使用 Redis 锁。请 pip install 'robot-agent[redis]' "
                    "或把 ROBOT_AGENT_LOCK__BACKEND 设回 none"
                )
            self._client = Redis.from_url(url, decode_responses=True)
        self.prefix = prefix
        self.ttl_s = ttl_s

    def _key(self, key: str) -> str:
        return f"{self.prefix}{key}"

    async def acquire(self, key: str, *, ttl_s: float | None = None) -> str | None:
        """原子地「不存在才写入并同时设过期」。

        注意用 ``px``（毫秒）而不是 ``ex``（秒）：执行单元的超时是亚秒级的，
        按秒取整会让短任务拿到一个明显超过预期的 TTL。
        """
        ttl_ms = max(1, round((ttl_s if ttl_s is not None else self.ttl_s) * 1000))
        token = uuid.uuid4().hex
        acquired = await self._client.set(self._key(key), token, nx=True, px=ttl_ms)
        if acquired:
            logger.info("lock_acquired", key=key, ttl_ms=ttl_ms)
            return token
        return None

    async def release(self, key: str, token: str) -> bool:
        try:
            removed = await self._client.eval(_RELEASE_LUA, 1, self._key(key), token)
        except Exception as exc:  # pragma: no cover - 网络异常不应拖垮主流程
            logger.warning("lock_release_failed", key=key, error=str(exc))
            return False

        if not removed:
            # 自己的锁已过期、别人重新持有：此时什么都不能删
            logger.warning("lock_token_mismatch", key=key)
            return False
        logger.info("lock_released", key=key)
        return True

    async def aclose(self) -> None:
        with_open = getattr(self._client, "aclose", None)
        if with_open is not None:
            await with_open()


def build_lock(settings: LockSettings) -> ExecutionLock:
    """按配置装配锁后端。redis 依赖缺失时**响亮失败**，不静默降级。"""
    if settings.backend == "redis":
        url = settings.redis_url.get_secret_value() if settings.redis_url else None
        if not url:
            raise RuntimeError("lock.backend=redis 但未配置 lock.redis_url")
        return RedisLock(url, prefix=settings.key_prefix, ttl_s=settings.ttl_s)

    return NullLock()
