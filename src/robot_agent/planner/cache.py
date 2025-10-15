"""规划结果缓存：高频指令跳过模型调用。

## 为什么缓存的是「计划」而不是「答案」

指令到计划的映射在给定技能库与场地配置后是确定的——「把红色方块推到左边」今天
规划成什么，一小时后还是同一步。真正贵的是一次 LLM 往返（数百毫秒到数秒），
而计划本身只有几十字节。缓存计划等于把这条路径从「网络 + 推理」降到「查表」。

## 两个必须做对的细节

1. **进出都深拷贝**。安全闸门会**就地修改**步骤参数（``speed_limit`` 把 0.22 m/s
   之外的线速度改写掉）。如果缓存与调用方共享同一个 ``Plan`` 对象，第一次执行就把
   修正后的速度写回了缓存，后续所有命中都带着上一次的修正值。这类污染不会报错，
   只会在某天表现为「同一个指令两次结果不一样」。
2. **key 里带配置指纹**。技能库增删、场地尺寸变化都会让旧计划失效。只按指令文本
   做 key 的话，换了配置还在返回老计划——这在演示时完全看不出来。

## 没有做什么

不做主动失效通知，只靠 TTL。做缓存失效总线需要所有写入方（上传技能包、改配置、
热重载）都发事件，任何一处漏发就会留下永远不失效的脏数据。TTL 到期自然过期，
代价是最多一个 TTL 周期的不一致，这个代价明确、可解释。
"""

from __future__ import annotations

import hashlib
import threading
import time
from collections import OrderedDict

from robot_agent.domain.plan import Plan


#: 规范化指令：去掉首尾空白、压缩内部空白、英文字母统一小写。
#: 不能更激进（比如去标点）——「开到头」和「开到 (0,0)」去掉标点后仍然不同，
#: 而「把灯调成 蓝色」与「把灯调成蓝色」应该命中同一条。
def normalize_instruction(text: str) -> str:
    return " ".join((text or "").split()).casefold()


class PlanCache:
    """带 TTL 的 LRU 计划缓存。线程安全（Planner 被并发任务共享）。"""

    def __init__(
        self,
        *,
        max_entries: int = 512,
        ttl_s: float = 300.0,
        clock=time.monotonic,
    ) -> None:
        self.max_entries = max(0, max_entries)
        self.ttl_s = max(0.0, ttl_s)
        self._clock = clock
        self._lock = threading.Lock()
        self._items: OrderedDict[str, tuple[float, Plan]] = OrderedDict()

    @property
    def enabled(self) -> bool:
        return self.max_entries > 0 and self.ttl_s > 0

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    def get(self, key: str) -> Plan | None:
        if not self.enabled:
            return None
        now = self._clock()
        with self._lock:
            entry = self._items.get(key)
            if entry is None:
                return None
            stored_at, plan = entry
            if now - stored_at > self.ttl_s:
                # 过期即删，不做惰性保留——保留只会让「命中率」这个指标虚高
                del self._items[key]
                return None
            self._items.move_to_end(key)
        return plan.model_copy(deep=True)

    def put(self, key: str, plan: Plan) -> None:
        if not self.enabled or not plan.steps:
            return
        snapshot = plan.model_copy(deep=True)
        with self._lock:
            self._items[key] = (self._clock(), snapshot)
            self._items.move_to_end(key)
            while len(self._items) > self.max_entries:
                self._items.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


def make_key(instruction: str, fingerprint: str) -> str:
    """指令 + 配置指纹的短哈希。

    用 sha256 而不是 ``hash()``：内置 hash 在每个进程加盐，重启后全量失效，
    多进程部署时各进程各有一套缓存——命中率会莫名其妙地低。
    """
    payload = f"{fingerprint}\x00{normalize_instruction(instruction)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
