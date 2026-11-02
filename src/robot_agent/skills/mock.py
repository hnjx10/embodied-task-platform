"""Mock 执行器：不依赖 ROS2 的确定性世界模型。

用途有三：CI 全链路回归、本地开发、以及给面试/评审演示「同一套编排可换执行器」。

**参数范围刻意放宽**（±100 m）：物理边界属于安全策略的职责，不在参数类型层拦截，
否则安全闸门就永远没有机会拒绝一个越界指令，测试也就测不到真正的防线。
"""

from __future__ import annotations

import math
import threading
from typing import ClassVar, Literal

from pydantic import Field

from robot_agent.config import ArenaSettings
from robot_agent.skills.base import (
    RiskLevel,
    Skill,
    SkillParams,
    SkillRegistry,
    SkillResult,
)


class GotoParams(SkillParams):
    x: float = Field(description="目标 x 坐标（米）")
    y: float = Field(description="目标 y 坐标（米）")
    speed: float = Field(default=0.15, description="线速度 m/s")


class PushCubeParams(SkillParams):
    color: Literal["red", "green", "blue"] = Field(description="方块颜色")
    x: float = Field(description="目标 x 坐标（米）")
    y: float = Field(description="目标 y 坐标（米）")


class GetPoseParams(SkillParams):
    target: Literal["robot", "red", "green", "blue"] = Field(description="查询对象")


class StopParams(SkillParams):
    """急停无参数——需要参数才能停下来的急停不叫急停。"""


class MockWorld:
    """极简世界模型：记录机器人与方块位姿。带锁，可被多线程调用。"""

    def __init__(self, arena: ArenaSettings | None = None) -> None:
        arena = arena or ArenaSettings()
        self.arena = arena
        self._lock = threading.RLock()
        self.robot: tuple[float, float, float] = (0.0, 0.0, 0.0)
        self.cubes: dict[str, tuple[float, float]] = dict(arena.cube_layout)
        self.last_speed: float = 0.0
        self.command_history: list[tuple[str, dict[str, float]]] = []

    def move_robot(self, x: float, y: float, speed: float) -> None:
        with self._lock:
            yaw = (
                math.atan2(y - self.robot[1], x - self.robot[0])
                if (x, y) != self.robot[:2]
                else self.robot[2]
            )
            self.robot = (x, y, yaw)
            self.last_speed = speed
            self.command_history.append(("goto", {"x": x, "y": y, "speed": speed}))

    def set_cube(self, color: str, x: float, y: float) -> None:
        with self._lock:
            self.cubes[color] = (x, y)
            self.robot = (x, y, self.robot[2])
            self.command_history.append(("push", {"x": x, "y": y}))

    def pose_of(self, target: str) -> tuple[float, float] | None:
        with self._lock:
            if target == "robot":
                return (self.robot[0], self.robot[1])
            return self.cubes.get(target)

    def stop(self) -> None:
        """急停：速度归零。位姿保持不变——急停不是「撤回」，是把动势消掉。"""
        with self._lock:
            self.last_speed = 0.0
            self.command_history.append(("stop", {"speed": 0.0}))

    def reset(self) -> None:
        with self._lock:
            self.robot = (0.0, 0.0, 0.0)
            self.cubes = dict(self.arena.cube_layout)
            self.last_speed = 0.0
            self.command_history.clear()


class MockGotoSkill(Skill):
    name: ClassVar[str] = "goto_point"
    description: ClassVar[str] = "把机器人平移导航到场内坐标 (x, y)，速度 speed m/s"
    params_model: ClassVar[type[SkillParams]] = GotoParams
    risk: ClassVar[RiskLevel] = "low"
    is_motion: ClassVar[bool] = True

    def __init__(self, world: MockWorld) -> None:
        self.world = world

    def run(self, x: float, y: float, speed: float = 0.15) -> SkillResult:  # type: ignore[override]
        self.world.move_robot(x, y, speed)
        return SkillResult(ok=True, detail=f"已到达 ({x:.2f}, {y:.2f})", data={"x": x, "y": y})


class MockPushSkill(Skill):
    name: ClassVar[str] = "push_cube"
    description: ClassVar[str] = "绕到指定颜色方块后方，将其推到目标点 (x, y)"
    params_model: ClassVar[type[SkillParams]] = PushCubeParams
    risk: ClassVar[RiskLevel] = "medium"
    is_motion: ClassVar[bool] = True

    def __init__(self, world: MockWorld) -> None:
        self.world = world

    def run(self, color: str, x: float, y: float) -> SkillResult:  # type: ignore[override]
        if color not in self.world.cubes:
            return SkillResult(ok=False, detail=f"场上没有 {color} 方块")
        self.world.set_cube(color, x, y)
        return SkillResult(
            ok=True,
            detail=f"{color} 方块已推至 ({x:.2f}, {y:.2f})",
            data={"color": color, "x": x, "y": y},
        )


class MockGetPoseSkill(Skill):
    name: ClassVar[str] = "get_pose"
    description: ClassVar[str] = "读取机器人或某个方块当前位姿"
    params_model: ClassVar[type[SkillParams]] = GetPoseParams
    risk: ClassVar[RiskLevel] = "low"

    def __init__(self, world: MockWorld) -> None:
        self.world = world

    def run(self, target: str) -> SkillResult:  # type: ignore[override]
        pose = self.world.pose_of(target)
        if pose is None:
            return SkillResult(ok=False, detail=f"未知目标 {target}")
        x, y = pose
        return SkillResult(
            ok=True, detail=f"{target} 位于 ({x:.2f}, {y:.2f})", data={"x": x, "y": y}
        )


class MockStopSkill(Skill):
    name: ClassVar[str] = "stop"
    description: ClassVar[str] = "紧急停止：立即把线速度清零"
    params_model: ClassVar[type[SkillParams]] = StopParams
    risk: ClassVar[RiskLevel] = "low"
    is_motion: ClassVar[bool] = False

    def __init__(self, world: MockWorld) -> None:
        self.world = world

    def run(self) -> SkillResult:  # type: ignore[override]
        self.world.stop()
        return SkillResult(ok=True, detail="已急停（速度归零）", data={"speed": 0.0})


def build_mock_registry(arena: ArenaSettings | None = None) -> tuple[SkillRegistry, MockWorld]:
    """装配 mock 执行器。返回 (registry, world)，world 便于测试断言状态。"""
    world = MockWorld(arena)
    registry = SkillRegistry()
    registry.register_all(
        [
            MockGotoSkill(world),
            MockPushSkill(world),
            MockGetPoseSkill(world),
            MockStopSkill(world),
        ]
    )
    return registry, world
