"""规则规划器：确定性、零依赖、可测。

定位不是「LLM 的替代品」，而是**降级路径**：LLM 不可用、超预算、输出非法时，
系统仍能以可预测的方式处理高频指令。因此这里只覆盖常见句式，把复杂表达交给 LLM；
一旦解析不了就明确报错，绝不猜测性执行——这是安全系统与玩具项目的分界线。
"""

from __future__ import annotations

import re

from robot_agent.domain.plan import PlanStep
from robot_agent.errors import PlanningError

# 颜色别名 → 场景中的模型名
_COLOR_ALIASES = {
    "红": "red",
    "红色": "red",
    "绿": "green",
    "绿色": "green",
    "蓝": "blue",
    "蓝色": "blue",
}

# 方位目标相对场地边缘的收缩系数，避免目标点贴在物理围挡上
_DIRECTION_MARGIN = 0.8

# 方位词 → 归一化坐标系数（-1 / 0 / 1），实际坐标 = 系数 × 半边长 × 边距系数
_DIRECTION_ALIASES: dict[str, tuple[float, float]] = {
    "左边": (-1.0, 0.0),
    "左侧": (-1.0, 0.0),
    "左": (-1.0, 0.0),
    "右边": (1.0, 0.0),
    "右侧": (1.0, 0.0),
    "右": (1.0, 0.0),
    "中间": (0.0, 0.0),
    "中间位置": (0.0, 0.0),
    "中心": (0.0, 0.0),
    "原点": (0.0, 0.0),
    "前方": (0.0, 1.0),
    "前面": (0.0, 1.0),
    "后面": (0.0, -1.0),
    "后方": (0.0, -1.0),
}

#: 方向词 → 单位向量。注意 move_relative 的坐标系：dx 右为正、dy 前为正。
_DIRECTION_VECTORS: dict[str, tuple[float, float]] = {
    "前": (0.0, 1.0),
    "后": (0.0, -1.0),
    "左": (-1.0, 0.0),
    "右": (1.0, 0.0),
}

# 分句符：多步指令按顺序拆解。注意 **不能** 用正则简单切分——坐标里的逗号
# 会被误当分隔符，把 "(1.0, 0.5)" 切成两半。这里用括号深度感知的手写扫描。
_CLAUSE_SEPARATORS = ("然后", "接着", "随后", "之后", "并且")
_CLAUSE_CHARS = "，,；;"

_PUSH_RE = re.compile(
    r"把\s*(?P<color>红|绿|蓝)色?\s*方?块\s*(?:推|搬|移|挪|顶)\s*(?:到|至|向|去)?\s*(?P<target>.+)"
)
_GOTO_RE = re.compile(r"(?:开到|走到|移动到|导航到|行驶到|回到|返回|前往|去)\s*(?P<target>.+)")
_COORD_RE = re.compile(r"(-?\d+(?:\.\d+)?)\s*[,，\s]\s*(-?\d+(?:\.\d+)?)")
_EXPLICIT_COORD_RE = re.compile(r"x\s*=\s*(-?\d+(?:\.\d+)?).*?y\s*=\s*(-?\d+(?:\.\d+)?)", re.I)

#: 中文数字。指令里「往前开一米」比「往前开 1 米」更常见，不认这些就是明明能懂却报错。
_CN_NUMERALS = {
    "半": 0.5,
    "一": 1.0,
    "两": 2.0,
    "二": 2.0,
    "三": 3.0,
    "四": 4.0,
    "五": 5.0,
    "六": 6.0,
    "七": 7.0,
    "八": 8.0,
    "九": 9.0,
    "十": 10.0,
}

_DISTANCE = r"(?P<dist>\d+(?:\.\d+)?|[半一两二三四五六七八九十])\s*(?:米|m|M)"

#: 相对位移：方向 + 距离。`往后` / `左移` / `前进` 都要认。
_RELATIVE_RE = re.compile(
    rf"(?P<dir>前|后|左|右)(?:方|边|侧|进|退|移)?[^0-9半一两二三四五六七八九十]{{0,6}}{_DISTANCE}"
)

#: 「充电」单独出现时是动词，但「充电桩」是名词。不加这个负向前瞻，
#: 「开到离充电桩远一点的地方」会命中回充——指令意图正好相反，而且它会兴高采烈地执行。
_DOCK_RE = re.compile(r"(?:回充|回去充电|去充电|回充电桩|补充电量|充会儿电|充电(?!桩))")
_UNDOCK_RE = re.compile(
    rf"(?:离开充电桩|退出充电|离桩|从充电桩出来)(?:[^0-9半一两二三四五六七八九十]{{0,6}}{_DISTANCE})?"
)
_LED_RE = re.compile(r"(?:灯带|指示灯|灯).*?(?P<color>红|绿|蓝)|(?P<off>关)(?:掉)?(?:灯|灯带)")
_CAPTURE_RE = re.compile(r"(?:拍照|拍张照|拍个照|拍一下|抓拍|截图|拍张图)")
_BATTERY_RE = re.compile(r"(?:电量|电池|剩余电|还有多少电)")
_RESET_ODOM_RE = re.compile(
    r"(?:重置里程计|里程计归零|里程计清零|里程计重置|重置?odom|reset\s*odom)", re.I
)


def split_clauses(text: str) -> list[str]:
    """按分句符切分，同时保留括号内的逗号（坐标）。

    例：``"把红块推到 (1.0, 0.5)，然后开到左边"`` → 两个子句，
    而 ``"把红块推到 (1.0, 0.5)"`` 不会被切开。
    """
    clauses: list[str] = []
    buffer: list[str] = []
    depth = 0
    idx = 0
    while idx < len(text):
        ch = text[idx]
        if ch in "(（":
            depth += 1
        elif ch in ")）":
            depth = max(0, depth - 1)

        if depth == 0:
            separator = next((sep for sep in _CLAUSE_SEPARATORS if text.startswith(sep, idx)), None)
            if separator is not None:
                clauses.append("".join(buffer))
                buffer = []
                idx += len(separator)
                continue
            if ch in _CLAUSE_CHARS:
                clauses.append("".join(buffer))
                buffer = []
                idx += 1
                continue
        buffer.append(ch)
        idx += 1
    clauses.append("".join(buffer))
    return [c.strip() for c in clauses if c and c.strip()]


class RulePlanner:
    """基于正则与词典的规划器。线程安全（无状态）。"""

    source = "rules"

    def __init__(self, arena_half_size: float = 1.5, default_speed: float = 0.15) -> None:
        self.arena_half_size = arena_half_size
        self.default_speed = default_speed

    # ------------------------------------------------------------------ 入口

    def plan(self, instruction: str) -> list[PlanStep]:
        text = (instruction or "").strip()
        if not text:
            raise PlanningError("指令为空")

        clauses = split_clauses(text)
        steps: list[PlanStep] = []
        unparsed: list[str] = []
        for clause in clauses:
            step = self.parse_clause(clause)
            if step is None:
                unparsed.append(clause)
            else:
                steps.append(step)

        if not steps:
            raise PlanningError(
                f"无法解析指令: {instruction!r}。支持的句式示例："
                "「把红色方块推到左边」「开到 (1.0, 0.5)」",
                details={"instruction": instruction},
            )
        if unparsed:
            # 部分解析成功时宁可整条失败，也不执行「半条指令」——半条往往最危险
            raise PlanningError(
                f"指令中有无法解析的片段: {unparsed}",
                details={"instruction": instruction, "parsed": [s.label() for s in steps]},
            )
        return steps

    # ------------------------------------------------------------------ 单句

    def parse_clause(self, clause: str) -> PlanStep | None:
        # 顺序有意义：越具体的句式越先匹配。
        # 「离开充电桩」同时含「充电」，不先判 undock 就会被当成回充；
        # 「把充电桩旁的方块推过来」含「充电」，不先判 push 就会被当成回充。
        if _RESET_ODOM_RE.search(clause):
            return PlanStep(skill="reset_odom", args={})

        if _CAPTURE_RE.search(clause):
            return PlanStep(skill="capture_image", args={"label": "snapshot"})

        undock = _UNDOCK_RE.search(clause)
        if undock:
            distance = self._distance(undock.group("dist")) if undock.group("dist") else 0.3
            return PlanStep(skill="undock", args={"distance": distance})

        push = _PUSH_RE.search(clause)
        if push:
            color = _COLOR_ALIASES.get(push.group("color"))
            coords = self.parse_target(push.group("target"))
            if color and coords:
                return PlanStep(
                    skill="push_cube",
                    args={"color": color, "x": coords[0], "y": coords[1]},
                    expect={"color": color, "x": coords[0], "y": coords[1], "tolerance": 0.25},
                )
            return None

        relative = _RELATIVE_RE.search(clause)
        if relative:
            distance = self._distance(relative.group("dist"))
            nx, ny = _DIRECTION_VECTORS[relative.group("dir")]
            return PlanStep(
                skill="move_relative",
                args={
                    "dx": round(nx * distance, 4),
                    "dy": round(ny * distance, 4),
                    "speed": self._infer_speed(clause),
                },
            )

        led = _LED_RE.search(clause)
        if led:
            if led.group("off"):
                return PlanStep(skill="set_led", args={"color": "off", "brightness": 0.0})
            color = _COLOR_ALIASES.get(led.group("color") or "")
            if color:
                return PlanStep(skill="set_led", args={"color": color, "brightness": 1.0})

        if _DOCK_RE.search(clause):
            return PlanStep(skill="dock", args={})

        # 电池查询放在回充之后：「补充电量」既含「电量」也含「充电」，
        # 先判查询就会把一条充电指令当成只读查询放过去——语义完全相反。
        if _BATTERY_RE.search(clause):
            return PlanStep(skill="get_battery", args={})

        goto = _GOTO_RE.search(clause)
        if goto:
            coords = self.parse_target(goto.group("target"))
            if coords:
                speed = self._infer_speed(clause)
                return PlanStep(
                    skill="goto_point",
                    args={"x": coords[0], "y": coords[1], "speed": speed},
                    expect={"x": coords[0], "y": coords[1], "tolerance": 0.15},
                )
            return None
        return None

    def _distance(self, raw: str) -> float:
        """把「半」「一」「3」统一成米数。"""
        if raw in _CN_NUMERALS:
            return _CN_NUMERALS[raw]
        return float(raw)

    def parse_target(self, raw: str) -> tuple[float, float] | None:
        """把「左边」「(1.2, -0.3)」「x=1.2 y=0」统一成坐标。"""
        text = raw.strip().rstrip("。.！!？?")
        explicit = _EXPLICIT_COORD_RE.search(text)
        if explicit:
            return float(explicit.group(1)), float(explicit.group(2))

        for alias, (nx, ny) in _DIRECTION_ALIASES.items():
            if alias in text:
                # 留 20% 边距：贴边目标会让方块/车身顶到围挡，实际不可达
                reach = self.arena_half_size * _DIRECTION_MARGIN
                return round(nx * reach, 4), round(ny * reach, 4)

        numeric = _COORD_RE.search(text)
        if numeric:
            return float(numeric.group(1)), float(numeric.group(2))
        return None

    def _infer_speed(self, clause: str) -> float:
        """只有显式要求快/慢才改速度；非法速度交给安全策略处理（会被自动降速）。"""
        if any(w in clause for w in ("全速", "最快", "尽量快")):
            return 0.5
        if any(w in clause for w in ("慢", "小心", "缓缓")):
            return 0.08
        return self.default_speed
