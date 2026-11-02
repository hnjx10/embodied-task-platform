"""Mock 执行器：无 ROS 环境时用同样的 Skill 接口模拟机器人，供 CI 与演示。"""
import math
from typing import Dict, Tuple

from .base import Skill, SkillResult

# 场地 3m x 3m，原点居中
ARENA_HALF = 1.5


class MockWorld:
    """极简物理世界：记录机器人与方块位姿，push 直接把方块挪到目标附近。"""

    def __init__(self) -> None:
        self.robot: Tuple[float, float, float] = (0.0, 0.0, 0.0)  # x, y, yaw
        self.cubes: Dict[str, Tuple[float, float]] = {
            "red": (0.8, 0.5),
            "green": (0.9, -0.4),
            "blue": (-0.7, 0.6),
        }
        self.last_speed: float = 0.0


class MockGotoSkill(Skill):
    name = "goto_point"
    description = "将机器人平移导航到场内坐标 (x, y)，速度 speed m/s"
    params_schema = {"x": "float [-1.5, 1.5]", "y": "float [-1.5, 1.5]", "speed": "float (0, 0.22]"}

    def __init__(self, world: MockWorld) -> None:
        self.world = world

    def execute(self, x: float, y: float, speed: float = 0.15) -> SkillResult:
        self.world.robot = (x, y, self.world.robot[2])
        self.world.last_speed = speed
        return SkillResult(True, f"已到达 ({x:.2f}, {y:.2f})")


class MockPushSkill(Skill):
    name = "push_cube"
    description = "导航到指定颜色方块后方并将其推到目标点 (x, y)"
    params_schema = {"color": "red|green|blue", "x": "float [-1.5, 1.5]", "y": "float [-1.5, 1.5]"}

    def __init__(self, world: MockWorld) -> None:
        self.world = world

    def execute(self, color: str, x: float, y: float) -> SkillResult:
        if color not in self.world.cubes:
            return SkillResult(False, f"场上没有 {color} 方块")
        self.world.cubes[color] = (x, y)
        self.world.robot = (x, y, self.world.robot[2])
        return SkillResult(True, f"{color} 方块已推至 ({x:.2f}, {y:.2f})")


class MockGetPoseSkill(Skill):
    name = "get_pose"
    description = "读取机器人或方块当前位姿"
    params_schema = {"target": "robot|red|green|blue"}

    def __init__(self, world: MockWorld) -> None:
        self.world = world

    def execute(self, target: str) -> SkillResult:
        if target == "robot":
            x, y, _ = self.world.robot
        elif target in self.world.cubes:
            x, y = self.world.cubes[target]
        else:
            return SkillResult(False, f"未知目标 {target}")
        return SkillResult(True, f"{target} 位于 ({x:.2f}, {y:.2f})", data={"x": x, "y": y})


def build_mock_registry() -> Tuple[object, object]:
    """返回 (registry, world)。graph.py 据此装配执行器。"""
    from .base import SkillRegistry

    world = MockWorld()
    registry = SkillRegistry()
    for s in (MockGotoSkill(world), MockPushSkill(world), MockGetPoseSkill(world)):
        registry.register(s)
    return registry, world
