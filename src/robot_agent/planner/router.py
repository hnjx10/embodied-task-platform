"""意图识别路由：在调用规划器之前，先判断「这句话想干什么」。

**为什么在规划器前面加一层**

规划器是通用的大模型调用，它能处理任何句式，但代价是：一句明显不属于本系统
能力范围的话（「今天天气怎么样」），也要走一次完整的模型调用、等一次超时、
再报一个「规划失败」。加一层关键词路由的价值不在准确率——它的准确率肯定不如
模型——而在于：

* **省一次调用**：明确的闲聊/无关指令在 0.1ms 内就被识别，不烧 token；
* **错误信息更具体**：讲清楚「你这句话被识别成什么意图」，比一句
  「规划失败」有用得多；
* **留出扩展点**：不同意图可以挂不同的技能子集与提示词，例如 ``query`` 类
  根本不需要 ``push_cube``，收窄候选集能显著降低幻觉选错技能的概率。

**刻意的设计取舍**：路由结果是 ``unknown`` 时**不拦截**，仍然交给规划器。
因为关键词表必然漏词（「挪一下」「怼过去」都是合法指令），拿一个覆盖率有限
的规则去拦截，会把本来能完成的任务变成失败——这比多花一次模型调用糟糕得多。
规则只负责「能确定的时候省一次」，不确定就交给模型。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Intent = Literal["move", "manipulate", "query", "system", "unknown"]

#: 类别 → 关键词。长词在前：`回充` 必须先于 `回` 命中，否则会被当成移动指令。
_KEYWORDS: dict[Intent, tuple[str, ...]] = {
    "manipulate": ("推", "抓", "拿起", "拿", "放下", "放", "夹", "取", "搬", "拾"),
    "system": (
        "回充",
        "充电",
        "灯",
        "拍照",
        "拍张",
        "拍个",
        "抓拍",
        "截图",
        "电量",
        "重置",
        "里程计",
        "急停",
        "停下",
        "停",
    ),
    "query": ("在哪", "位置", "状态", "多少", "查询", "报告", "汇报", "看看"),
    "move": (
        "开到",
        "走到",
        "前往",
        "导航",
        "移动",
        "巡逻",
        "旋转",
        "朝向",
        "回去",
        "回到",
        "开",
        "走",
        "去",
        "到",
        "转",
    ),
}

#: 命中多个类别时的优先级。操作类最具体，优先；移动类最泛，最后兜底。
_PRIORITY: tuple[Intent, ...] = ("manipulate", "system", "query", "move")


@dataclass(frozen=True)
class IntentMatch:
    """一次路由结果。``keyword`` 记录命中的词，便于解释「为什么这么判」。"""

    intent: Intent
    keyword: str = ""
    reason: str = ""

    @property
    def certain(self) -> bool:
        """是否确定。``unknown`` 表示规则没把握，调用方应交给模型而不是拒绝。"""
        return self.intent != "unknown"

    def to_dict(self) -> dict[str, str]:
        return {"intent": self.intent, "keyword": self.keyword, "reason": self.reason}


def classify(instruction: str) -> IntentMatch:
    """按关键词粗分类。纯函数，无副作用，可单独测。"""
    text = instruction.strip()
    if not text:
        return IntentMatch("unknown", reason="空指令")

    hits: dict[Intent, list[str]] = {}
    for intent, words in _KEYWORDS.items():
        found = [word for word in words if word in text]
        if found:
            hits[intent] = found

    if not hits:
        return IntentMatch("unknown", reason="未命中任何意图关键词")

    for intent in _PRIORITY:
        if intent in hits:
            # 同类里取最长的命中词：`回充` 比 `回` 更能说明意图
            keyword = max(hits[intent], key=len)
            return IntentMatch(intent, keyword, f"命中关键词 {keyword!r}")

    return IntentMatch("unknown", reason="未命中任何意图关键词")
