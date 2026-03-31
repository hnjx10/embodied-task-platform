"""Mock 执行器：不依赖 ROS2 的确定性世界模型。

用途有三：CI 全链路回归、本地开发、以及在不接真机时演示「同一套编排可换执行器」。

**参数范围刻意放宽**：物理边界属于安全策略的职责，不在参数类型层拦截，
否则安全闸门就永远没有机会拒绝一个越界指令，测试也就测不到真正的防线。

**技能行为刻意保真**：能抓多远、夹爪空着能不能放、方块被拿起后还在不在场上，
这些都按真实物理约束建模。一个「什么都能成功」的 mock 会让编排层的错误隐性化——
比如没有 obj 也照样 place 成功，那么流水线就有 bug 也测不出来。
"""

from __future__ import annotations

import math
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import ClassVar

from robot_agent.config import ArenaSettings
from robot_agent.skills.base import (
    RiskLevel,
    Skill,
    SkillParams,
    SkillRegistry,
    SkillResult,
)
from robot_agent.skills.params import (
    CaptureImageParams,
    DockParams,
    GetBatteryParams,
    GetPoseParams,
    GotoParams,
    GripperParams,
    LookAtParams,
    MoveRelativeParams,
    PatrolParams,
    PickCubeParams,
    PlaceCubeParams,
    PushCubeParams,
    ReportStatusParams,
    ResetOdomParams,
    RotateParams,
    SetLedParams,
    SetSpeedParams,
    StopParams,
    UndockParams,
    WaitParams,
)

#: mock 环境下的等待上限。真按 30s 睡会把测试拖成分钟计，这里截断并如实标注。
_WAIT_CAP_S = 0.2

#: 每米行程消耗的电量（百分比）。数值不追求物理精确，只要求「会掉」——
#: 电量永远 100% 的 mock 会让「低电量回充」这类依赖电量的逻辑永远测不到。
_DRAIN_PER_METER = 0.4


class MockWorld:
    """极简世界模型：记录机器人与方块位姿。带锁，可被多线程调用。"""

    def __init__(
        self,
        arena: ArenaSettings | None = None,
        reach: float = 0.35,
        *,
        image_dir: str | None = None,
    ) -> None:
        arena = arena or ArenaSettings()
        self.arena = arena
        self.reach = reach
        #: 抓拍落盘目录。为 None 时只返回元数据——mock 没有相机，假装有才是撒谎。
        self.image_dir = image_dir
        self._lock = threading.RLock()
        self.robot: tuple[float, float, float] = (0.0, 0.0, 0.0)
        self.cubes: dict[str, tuple[float, float]] = dict(arena.cube_layout)
        self.last_speed: float = 0.0
        self.gripper_open: bool = True
        self.holding: str | None = None
        self.speed_cap: float | None = None
        self.battery_pct: float = 100.0
        self.led: tuple[str, float] = ("off", 0.0)
        self.docked: bool = False
        self.odom_origin: tuple[float, float, float] = (0.0, 0.0, 0.0)
        self.command_history: list[tuple[str, dict[str, float]]] = []

    # ------------------------------------------------------------------ 移动

    def move_robot(self, x: float, y: float, speed: float) -> None:
        with self._lock:
            travel = math.hypot(x - self.robot[0], y - self.robot[1])
            yaw = (
                math.atan2(y - self.robot[1], x - self.robot[0])
                if (x, y) != self.robot[:2]
                else self.robot[2]
            )
            self.robot = (x, y, yaw)
            self.last_speed = speed
            self.docked = False
            self.battery_pct = max(0.0, self.battery_pct - travel * _DRAIN_PER_METER)
            self.command_history.append(("goto", {"x": x, "y": y, "speed": speed}))

    def move_relative(self, dx: float, dy: float, speed: float) -> tuple[float, float]:
        with self._lock:
            x, y = self.robot[0] + dx, self.robot[1] + dy
        self.move_robot(x, y, speed)
        return x, y

    def rotate_by(self, yaw_deg: float) -> float:
        with self._lock:
            yaw = self.robot[2] + math.radians(yaw_deg)
            self.robot = (self.robot[0], self.robot[1], math.atan2(math.sin(yaw), math.cos(yaw)))
            self.command_history.append(("rotate", {"yaw_deg": yaw_deg}))
            return math.degrees(self.robot[2])

    def face(self, x: float, y: float) -> float:
        """转向某点，返回转后的朝向（度）。"""
        with self._lock:
            yaw = math.atan2(y - self.robot[1], x - self.robot[0])
            self.robot = (self.robot[0], self.robot[1], yaw)
            self.command_history.append(("face", {"x": x, "y": y}))
            return math.degrees(yaw)

    # ------------------------------------------------------------------ 操作

    def set_cube(self, color: str, x: float, y: float) -> None:
        with self._lock:
            self.cubes[color] = (x, y)
            self.robot = (x, y, self.robot[2])
            self.command_history.append(("push", {"x": x, "y": y}))

    def set_gripper(self, *, open_: bool) -> None:
        with self._lock:
            self.gripper_open = open_
            self.command_history.append(("gripper", {"open": 1.0 if open_ else 0.0}))

    def pick(self, color: str) -> tuple[bool, str]:
        """抓取。距离不够就失败——这条约束是真机最容易忽略、mock 最容易放过的一条。"""
        with self._lock:
            pose = self.cubes.get(color)
            if pose is None:
                return False, f"场上没有 {color} 方块"
            if self.holding is not None:
                return False, f"夹爪已持有 {self.holding} 立方体，请先放置"
            dist = math.hypot(pose[0] - self.robot[0], pose[1] - self.robot[1])
            if dist > self.reach:
                return (
                    False,
                    f"距 {color} 方块 {dist:.2f}m，超出抓取范围 {self.reach:.2f}m",
                )
            self.holding = color
            self.gripper_open = False
            self.cubes.pop(color)
            self.command_history.append(("pick", {"distance": round(dist, 3)}))
            return True, f"已抓起 {color} 方块（距离 {dist:.2f}m）"

    def place(self, x: float, y: float) -> tuple[bool, str]:
        with self._lock:
            if self.holding is None:
                return False, "夹爪里没有物体，无法放置"
            color = self.holding
            self.cubes[color] = (x, y)
            self.holding = None
            self.gripper_open = True
            self.robot = (x, y, self.robot[2])
            self.command_history.append(("place", {"x": x, "y": y}))
            return True, f"{color} 方块已放置到 ({x:.2f}, {y:.2f})"

    # ------------------------------------------------------------------ 能源

    def dock(self) -> tuple[float, float]:
        """朝充电桩移动并停靠，停靠点由 ``arena.dock_clearance`` 决定。

        停靠距离必须大于禁区半径：充电桩本身是禁区，机器人不能停在禁区里，
        所以「停靠」是停在禁区外缘，不是一个点。这条约束若写错，
        回充会被自己的安全策略拦下——这正是策略链值得存在的原因。
        """
        with self._lock:
            cx, cy = self.arena.dock_pose
            clearance = self.arena.dock_clearance
            dx, dy = cx - self.robot[0], cy - self.robot[1]
            dist = math.hypot(dx, dy)
            if dist <= clearance:
                # 已经比停靠距离更近：直接充电，不后退。后退等于朝桩里撞。
                self.docked = True
                self.battery_pct = 100.0
                self.command_history.append(("dock", {"dist": round(dist, 3)}))
                return (self.robot[0], self.robot[1])
            ratio = (dist - clearance) / dist
            target = (self.robot[0] + dx * ratio, self.robot[1] + dy * ratio)

        self.move_robot(target[0], target[1], 0.1)
        with self._lock:
            # move_robot 会把 docked 置回 False，这里重新置位：停靠完成的语义
            # 是「已接触充电触点」，不是「刚位移过」。
            self.docked = True
            self.battery_pct = 100.0
            self.command_history.append(("dock", {"x": target[0], "y": target[1]}))
        return target

    def undock(self, distance: float) -> tuple[float, float]:
        """离开充电桩，朝远离桩心方向移动 ``distance`` 米。"""
        with self._lock:
            cx, cy = self.arena.dock_pose
            dx, dy = cx - self.robot[0], cy - self.robot[1]
            norm = math.hypot(dx, dy)
            if norm < 1e-6:
                dx, dy, norm = -1.0, -1.0, math.sqrt(2.0)
            target = (
                self.robot[0] - dx / norm * distance,
                self.robot[1] - dy / norm * distance,
            )
            was_docked = self.docked

        self.move_robot(target[0], target[1], 0.1)
        with self._lock:
            self.docked = False
            self.command_history.append(("undock", {"was_docked": 1.0 if was_docked else 0.0}))
        return target

    def battery(self) -> float:
        with self._lock:
            return round(self.battery_pct, 1)

    # ------------------------------------------------------------------ 外设

    def set_led(self, color: str, brightness: float) -> tuple[str, float]:
        with self._lock:
            current = (color, brightness)
            self.led = current
            self.command_history.append(("led", {"brightness": brightness}))
            return current

    def capture(self, label: str) -> dict[str, object]:
        """抓拍一张图。

        配了 ``image_dir`` 就写一张合法 PPM（纯文本、无需额外依赖、能被常见
        图像库解析）；没配就只返回元数据并标记 ``persisted=False``——
        mock 没有相机，返回一张假图比返回「没有图」更容易骗过调用方。
        """
        width = height = 8
        pixels = [(i * 3) % 256 for i in range(width * height * 3)]
        body = f"P3\n{width} {height}\n255\n" + " ".join(str(p) for p in pixels) + "\n"

        payload: dict[str, object] = {
            "label": label,
            "width": width,
            "height": height,
            "format": "ppm",
            "bytes": len(body.encode("ascii")),
            "persisted": bool(self.image_dir),
        }
        if self.image_dir:
            directory = Path(self.image_dir)
            directory.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
            path = directory / f"{stamp}-{label}.ppm"
            # newline="" 关掉文本模式的换行转换：Windows 默认把 \n 改写成 \r\n，
            # 会让 PPM 头部的字节数与 Linux 下不一致（格式文件不该受平台影响）。
            path.write_text(body, encoding="ascii", newline="")
            payload["path"] = str(path)
        else:
            payload["path"] = ""

        with self._lock:
            self.command_history.append(("capture", {}))
        return payload

    def reset_odom(self) -> tuple[float, float, float]:
        """把当前位置定义为里程计原点。

        物理位置不变，只是之后 ``pose_of("robot")`` 返回相对坐标——真实机器人
        重置 odom 就是这个语义（编码器计数清零，车没动）。
        """
        with self._lock:
            origin = self.robot
            self.odom_origin = origin
            self.command_history.append(("reset_odom", {}))
            return origin

    # ------------------------------------------------------------------ 状态

    def pose_of(self, target: str) -> tuple[float, float] | None:
        with self._lock:
            if target == "robot":
                return (
                    self.robot[0] - self.odom_origin[0],
                    self.robot[1] - self.odom_origin[1],
                )
            return self.cubes.get(target)

    def pose_age_s(self) -> float:
        """mock 世界的位姿永远是最新的：没有订阅、没有 DDS，也就不存在丢包。

        返回 0.0 而不是 ``None`` 是有意的——``None`` 表示「从未收到位姿」，
        在 mock 下如果返回 None，每次移动下发前都会被安全层拒掉，
        整个演示环境就跑不起来了。这个接口存在的意义是让 ROS2 侧有东西可填。
        """
        return 0.0

    def set_speed_cap(self, value: float) -> None:
        with self._lock:
            self.speed_cap = value
            self.command_history.append(("set_speed", {"max_speed": value}))

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "robot": {
                    "x": round(self.robot[0], 3),
                    "y": round(self.robot[1], 3),
                    "yaw_deg": round(math.degrees(self.robot[2]), 1),
                },
                "cubes": {k: [round(v[0], 3), round(v[1], 3)] for k, v in self.cubes.items()},
                "gripper_open": self.gripper_open,
                "holding": self.holding,
                "last_speed": self.last_speed,
            }

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
            self.gripper_open = True
            self.holding = None
            self.speed_cap = None
            self.battery_pct = 100.0
            self.led = ("off", 0.0)
            self.docked = False
            self.odom_origin = (0.0, 0.0, 0.0)
            self.command_history.clear()


# --------------------------------------------------------------------- 技能


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


class MockMoveRelativeSkill(Skill):
    name: ClassVar[str] = "move_relative"
    description: ClassVar[str] = "相对当前位置移动 (dx, dy)，dx 右为正、dy 前为正，单位米"
    params_model: ClassVar[type[SkillParams]] = MoveRelativeParams
    risk: ClassVar[RiskLevel] = "low"
    is_motion: ClassVar[bool] = True

    def __init__(self, world: MockWorld) -> None:
        self.world = world

    def run(self, dx: float, dy: float, speed: float = 0.15) -> SkillResult:  # type: ignore[override]
        x, y = self.world.move_relative(dx, dy, speed)
        return SkillResult(
            ok=True,
            detail=f"已相对移动 ({dx:+.2f}, {dy:+.2f}) 到 ({x:.2f}, {y:.2f})",
            data={"x": x, "y": y},
        )


class MockRotateSkill(Skill):
    name: ClassVar[str] = "rotate"
    description: ClassVar[str] = "原地旋转指定角度（度），逆时针为正"
    params_model: ClassVar[type[SkillParams]] = RotateParams
    risk: ClassVar[RiskLevel] = "low"
    is_motion: ClassVar[bool] = True

    def __init__(self, world: MockWorld) -> None:
        self.world = world

    def run(self, yaw_deg: float) -> SkillResult:  # type: ignore[override]
        heading = self.world.rotate_by(yaw_deg)
        return SkillResult(
            ok=True,
            detail=f"已旋转 {yaw_deg:+.1f}°，当前朝向 {heading:.1f}°",
            data={"yaw_deg": heading},
        )


class MockLookAtSkill(Skill):
    name: ClassVar[str] = "look_at"
    description: ClassVar[str] = "原地转向，使机器人朝向场内坐标 (x, y)"
    params_model: ClassVar[type[SkillParams]] = LookAtParams
    risk: ClassVar[RiskLevel] = "low"
    is_motion: ClassVar[bool] = True

    def __init__(self, world: MockWorld) -> None:
        self.world = world

    def run(self, x: float, y: float) -> SkillResult:  # type: ignore[override]
        heading = self.world.face(x, y)
        return SkillResult(
            ok=True,
            detail=f"已朝向 ({x:.2f}, {y:.2f})，当前朝向 {heading:.1f}°",
            data={"yaw_deg": heading},
        )


class MockPatrolSkill(Skill):
    name: ClassVar[str] = "patrol"
    description: ClassVar[str] = "按给定巡逻点序列循环移动 loops 圈"
    params_model: ClassVar[type[SkillParams]] = PatrolParams
    risk: ClassVar[RiskLevel] = "medium"
    is_motion: ClassVar[bool] = True

    def __init__(self, world: MockWorld) -> None:
        self.world = world

    def run(self, waypoints: list[GotoParams], loops: int = 1) -> SkillResult:  # type: ignore[override]
        visited = 0
        for _ in range(loops):
            for point in waypoints:
                self.world.move_robot(point.x, point.y, point.speed)
                visited += 1
        last = waypoints[-1]
        return SkillResult(
            ok=True,
            detail=f"完成巡逻：{visited} 个点位（{loops} 圈），终点 ({last.x:.2f}, {last.y:.2f})",
            data={"visited": visited, "x": last.x, "y": last.y},
        )


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


class MockPickSkill(Skill):
    name: ClassVar[str] = "pick_cube"
    description: ClassVar[str] = "抓取指定颜色方块（需先移动到方块附近）"
    params_model: ClassVar[type[SkillParams]] = PickCubeParams
    risk: ClassVar[RiskLevel] = "high"
    is_motion: ClassVar[bool] = True

    def __init__(self, world: MockWorld) -> None:
        self.world = world

    def run(self, color: str) -> SkillResult:  # type: ignore[override]
        ok, detail = self.world.pick(color)
        return SkillResult(
            ok=ok, detail=detail, data={"color": color, "holding": self.world.holding}
        )


class MockPlaceSkill(Skill):
    name: ClassVar[str] = "place_cube"
    description: ClassVar[str] = "把夹爪中的方块放置到目标点 (x, y)"
    params_model: ClassVar[type[SkillParams]] = PlaceCubeParams
    risk: ClassVar[RiskLevel] = "high"
    is_motion: ClassVar[bool] = True

    def __init__(self, world: MockWorld) -> None:
        self.world = world

    def run(self, x: float, y: float) -> SkillResult:  # type: ignore[override]
        ok, detail = self.world.place(x, y)
        return SkillResult(ok=ok, detail=detail, data={"x": x, "y": y})


class MockOpenGripperSkill(Skill):
    name: ClassVar[str] = "open_gripper"
    description: ClassVar[str] = "张开夹爪"
    params_model: ClassVar[type[SkillParams]] = GripperParams
    risk: ClassVar[RiskLevel] = "low"

    def __init__(self, world: MockWorld) -> None:
        self.world = world

    def run(self) -> SkillResult:  # type: ignore[override]
        self.world.set_gripper(open_=True)
        return SkillResult(ok=True, detail="夹爪已张开", data={"gripper_open": True})


class MockCloseGripperSkill(Skill):
    name: ClassVar[str] = "close_gripper"
    description: ClassVar[str] = "闭合夹爪"
    params_model: ClassVar[type[SkillParams]] = GripperParams
    risk: ClassVar[RiskLevel] = "medium"

    def __init__(self, world: MockWorld) -> None:
        self.world = world

    def run(self) -> SkillResult:  # type: ignore[override]
        self.world.set_gripper(open_=False)
        return SkillResult(ok=True, detail="夹爪已闭合", data={"gripper_open": False})


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


class MockReportStatusSkill(Skill):
    name: ClassVar[str] = "report_status"
    description: ClassVar[str] = "上报机器人当前状态快照（位姿、夹爪、持物、场上方块）"
    params_model: ClassVar[type[SkillParams]] = ReportStatusParams
    risk: ClassVar[RiskLevel] = "low"

    def __init__(self, world: MockWorld) -> None:
        self.world = world

    def run(self) -> SkillResult:  # type: ignore[override]
        snapshot = self.world.snapshot()
        robot = snapshot["robot"]
        assert isinstance(robot, dict)
        return SkillResult(
            ok=True,
            detail=f"机器人位于 ({robot['x']}, {robot['y']})，朝向 {robot['yaw_deg']}°",
            data=snapshot,
        )


class MockWaitSkill(Skill):
    name: ClassVar[str] = "wait"
    description: ClassVar[str] = "等待指定时长（秒）后继续，用于给物理动作留出稳定时间"
    params_model: ClassVar[type[SkillParams]] = WaitParams
    risk: ClassVar[RiskLevel] = "low"

    def __init__(self, world: MockWorld | None = None) -> None:
        self.world = world

    def run(self, duration_s: float) -> SkillResult:  # type: ignore[override]
        waited = min(duration_s, _WAIT_CAP_S)
        time.sleep(waited)
        note = f"已等待 {waited:.2f}s" + ("（mock 环境截断）" if waited < duration_s else "")
        return SkillResult(
            ok=True, detail=note, data={"requested_s": duration_s, "waited_s": waited}
        )


class MockSetSpeedSkill(Skill):
    name: ClassVar[str] = "set_speed"
    description: ClassVar[str] = "设置后续动作的线速度上限（m/s）"
    params_model: ClassVar[type[SkillParams]] = SetSpeedParams
    risk: ClassVar[RiskLevel] = "medium"

    def __init__(self, world: MockWorld) -> None:
        self.world = world

    def run(self, max_speed: float) -> SkillResult:  # type: ignore[override]
        self.world.set_speed_cap(max_speed)
        return SkillResult(
            ok=True, detail=f"速度上限已设为 {max_speed:.2f} m/s", data={"max_speed": max_speed}
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


# ------------------------------------------------------------ 能源 / 外设


class MockDockSkill(Skill):
    name: ClassVar[str] = "dock"
    description: ClassVar[str] = "返回充电桩并停靠，充满电量"
    params_model: ClassVar[type[SkillParams]] = DockParams
    # 定级 medium 而不是 low：回充的目标点在充电桩附近，属于靠近禁区的动作，
    # 现场有人时不应该被一条指令就触发。
    risk: ClassVar[RiskLevel] = "medium"
    is_motion: ClassVar[bool] = True

    def __init__(self, world: MockWorld) -> None:
        self.world = world

    def run(self) -> SkillResult:  # type: ignore[override]
        x, y = self.world.dock()
        return SkillResult(
            ok=True,
            detail=f"已停靠充电桩 ({x:.2f}, {y:.2f})，电量 {self.world.battery():.1f}%",
            data={"x": x, "y": y, "battery_pct": self.world.battery(), "docked": True},
        )


class MockUndockSkill(Skill):
    name: ClassVar[str] = "undock"
    description: ClassVar[str] = "离开充电桩，朝远离桩心方向移动指定距离"
    params_model: ClassVar[type[SkillParams]] = UndockParams
    risk: ClassVar[RiskLevel] = "low"
    is_motion: ClassVar[bool] = True

    def __init__(self, world: MockWorld) -> None:
        self.world = world

    def run(self, distance: float = 0.3) -> SkillResult:  # type: ignore[override]
        x, y = self.world.undock(distance)
        return SkillResult(
            ok=True,
            detail=f"已离开充电桩 {distance:.2f}m，当前位于 ({x:.2f}, {y:.2f})",
            data={"x": x, "y": y, "docked": False},
        )


class MockGetBatterySkill(Skill):
    name: ClassVar[str] = "get_battery"
    description: ClassVar[str] = "读取电池剩余电量百分比"
    params_model: ClassVar[type[SkillParams]] = GetBatteryParams
    risk: ClassVar[RiskLevel] = "low"

    def __init__(self, world: MockWorld) -> None:
        self.world = world

    def run(self) -> SkillResult:  # type: ignore[override]
        level = self.world.battery()
        low = level < 20.0
        return SkillResult(
            ok=True,
            detail=f"电量 {level:.1f}%" + ("（偏低，建议回充）" if low else ""),
            data={"battery_pct": level, "low": low},
        )


class MockSetLedSkill(Skill):
    name: ClassVar[str] = "set_led"
    description: ClassVar[str] = "设置机身灯带颜色与亮度，用于向现场人员表达状态"
    params_model: ClassVar[type[SkillParams]] = SetLedParams
    risk: ClassVar[RiskLevel] = "low"

    def __init__(self, world: MockWorld) -> None:
        self.world = world

    def run(self, color: str, brightness: float = 1.0) -> SkillResult:  # type: ignore[override]
        actual, level = self.world.set_led(color, brightness)
        return SkillResult(
            ok=True,
            detail=f"灯带已设为 {actual}（亮度 {level:.2f}）",
            data={"color": actual, "brightness": level},
        )


class MockCaptureImageSkill(Skill):
    name: ClassVar[str] = "capture_image"
    description: ClassVar[str] = "抓拍当前视野图像并归档，标签用于后续检索"
    params_model: ClassVar[type[SkillParams]] = CaptureImageParams
    risk: ClassVar[RiskLevel] = "low"

    def __init__(self, world: MockWorld) -> None:
        self.world = world

    def run(self, label: str = "snapshot") -> SkillResult:  # type: ignore[override]
        payload = self.world.capture(label)
        if not payload["persisted"]:
            return SkillResult(
                ok=True,
                detail=f"已抓拍（mock 后端不落盘，标签 {label}）",
                data=payload,
            )
        return SkillResult(
            ok=True,
            detail=f"已抓拍并保存: {payload['path']}",
            data=payload,
        )


class MockResetOdomSkill(Skill):
    name: ClassVar[str] = "reset_odom"
    description: ClassVar[str] = "把当前位置设为里程计原点（物理位置不变）"
    params_model: ClassVar[type[SkillParams]] = ResetOdomParams
    # medium：重置后所有相对坐标的语义都变了，上层若还在用旧基准会算错位置。
    risk: ClassVar[RiskLevel] = "medium"

    def __init__(self, world: MockWorld) -> None:
        self.world = world

    def run(self) -> SkillResult:  # type: ignore[override]
        origin = self.world.reset_odom()
        return SkillResult(
            ok=True,
            detail=f"里程计已归零，原点对应世界坐标 ({origin[0]:.2f}, {origin[1]:.2f})",
            data={"origin_x": origin[0], "origin_y": origin[1]},
        )


def build_mock_registry(
    arena: ArenaSettings | None = None,
    *,
    image_dir: str | None = None,
) -> tuple[SkillRegistry, MockWorld]:
    """装配 mock 执行器。返回 (registry, world)，world 便于测试断言状态。"""
    world = MockWorld(arena, image_dir=image_dir)
    registry = SkillRegistry()
    registry.register_all(
        [
            MockGotoSkill(world),
            MockMoveRelativeSkill(world),
            MockRotateSkill(world),
            MockLookAtSkill(world),
            MockPatrolSkill(world),
            MockPushSkill(world),
            MockPickSkill(world),
            MockPlaceSkill(world),
            MockOpenGripperSkill(world),
            MockCloseGripperSkill(world),
            MockGetPoseSkill(world),
            MockReportStatusSkill(world),
            MockWaitSkill(world),
            MockSetSpeedSkill(world),
            MockStopSkill(world),
            MockDockSkill(world),
            MockUndockSkill(world),
            MockGetBatterySkill(world),
            MockSetLedSkill(world),
            MockCaptureImageSkill(world),
            MockResetOdomSkill(world),
        ]
    )
    return registry, world
