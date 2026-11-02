"""ROS2 执行器：把技能映射到 TurtleBot3 + Gazebo 的真实控制。

**导入即安全**：``rclpy`` 只在 ROS2 环境存在，模块在缺少依赖时仍可导入
（``ROS_AVAILABLE=False``），只有真正构造 :class:`RosBridge` 才会失败并抛出
:class:`ExecutorUnavailableError`。这样 CI 与 mock 环境不会因为 import 就崩。

控制要点：

* ``goto`` 用 odom 闭环比例控制，带超时与到达容差，不再靠 sleep 猜时间；
* ``rotate`` / ``look_at`` 用角速度闭环，而不是「发一个角速度等固定时长」——
  后者在电池电压下降、地面摩擦变化时一定会转过头；
* 推箱分两段：先绕到方块背面，再低速顶推，每 0.1s 检查一次方块位姿；
* 所有循环都检查 ``stop_event``，急停能在 100ms 内生效；
* 退出时确保发布零速，避免机器人带着最后一条 cmd_vel 继续走。

**关于夹爪与抓放**：本模块把夹爪接口约定为 ``/gripper/position``（``Float64``，
0 闭合、1 张开）。真机上换成 ``control_msgs/action/GripperCommand`` 只需改
:meth:`RosBridge.set_gripper` 一个方法——技能层不感知这个差异。
抓放没有引入 MoveIt2 的运动规划，走的是「导航到方块旁 → 闭合夹爪 → 依赖物理
吸附」的降级路径；这在固定场景的仿真里可跑通，在杂乱场景里不成立，见
``docs/adr`` 中关于「先做安全、后做规划」的记录。
"""

from __future__ import annotations

import math
import threading
import time
from typing import Any, ClassVar

from robot_agent.config import Settings
from robot_agent.errors import ExecutorUnavailableError
from robot_agent.logging import get_logger
from robot_agent.skills.base import (
    RiskLevel,
    Skill,
    SkillParams,
    SkillRegistry,
    SkillResult,
)
from robot_agent.skills.params import (
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
    RotateParams,
    SetSpeedParams,
    StopParams,
    WaitParams,
)

logger = get_logger(__name__)

try:  # pragma: no cover - 取决于是否在 ROS2 环境
    import rclpy
    from gazebo_msgs.msg import ModelStates
    from geometry_msgs.msg import Twist
    from nav_msgs.msg import Odometry
    from std_msgs.msg import Float64

    ROS_AVAILABLE = True
except ImportError:  # pragma: no cover
    rclpy = None  # type: ignore[assignment]
    Twist = Odometry = ModelStates = Float64 = None  # type: ignore[assignment,misc]
    ROS_AVAILABLE = False

#: 抓取时允许的最大距离（米）。超出就先导航到方块旁。
_PICK_REACH_M = 0.35


def _yaw_from_quat(x: float, y: float, z: float, w: float) -> float:
    return math.atan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z))


def _wrap(angle: float) -> float:
    return math.atan2(math.sin(angle), math.cos(angle))


class RosBridge:
    """单节点后台 spin，向所有技能共享 /cmd_vel、/odom、/gazebo/model_states。"""

    def __init__(self, settings: Settings | None = None) -> None:
        if not ROS_AVAILABLE:
            raise ExecutorUnavailableError(
                "未检测到 rclpy，请使用 mock 模式（--mock）或在 ROS2 环境运行"
            )
        settings = settings or Settings()
        self.goto_timeout = settings.executor.step_timeout_s
        self._stop_event = threading.Event()
        self._speed_cap: float | None = None

        rclpy.init()
        self.node = rclpy.create_node("robot_agent_skill_bridge")
        self.cmd_pub = self.node.create_publisher(Twist, "/cmd_vel", 10)
        self.gripper_pub = self.node.create_publisher(Float64, "/gripper/position", 10)
        self._odom: tuple[float, float, float] | None = None
        self._models: dict[str, tuple[float, float]] = {}
        self._gripper_open = True
        self._lock = threading.Lock()
        self.node.create_subscription(Odometry, "/odom", self._odom_cb, 10)
        self.node.create_subscription(ModelStates, "/gazebo/model_states", self._models_cb, 10)
        self._spin = threading.Thread(target=rclpy.spin, args=(self.node,), daemon=True)
        self._spin.start()
        logger.info("ros_bridge_started", node=self.node.get_name())

    # ------------------------------------------------------------------ 回调

    def _odom_cb(self, msg: Any) -> None:
        p = msg.pose.pose.position
        q = msg.pose.pose.orientation
        with self._lock:
            self._odom = (p.x, p.y, _yaw_from_quat(q.x, q.y, q.z, q.w))

    def _models_cb(self, msg: Any) -> None:
        poses = {
            name: (pose.position.x, pose.position.y)
            for name, pose in zip(msg.name, msg.pose, strict=False)
        }
        with self._lock:
            self._models = poses

    # ------------------------------------------------------------------ 读取

    def robot_pose(self, wait_s: float = 2.0) -> tuple[float, float, float]:
        deadline = time.time() + wait_s
        while time.time() < deadline:
            with self._lock:
                if self._odom is not None:
                    return self._odom
            time.sleep(0.05)
        raise TimeoutError("未收到 /odom，请确认 TurtleBot3 已启动")

    def model_pose(self, name: str) -> tuple[float, float] | None:
        with self._lock:
            return self._models.get(name)

    # ------------------------------------------------------------------ 控制

    def drive(self, linear: float, angular: float, seconds: float) -> None:
        """持续发布速度指令 ``seconds`` 秒，期间响应急停。"""
        twist = Twist()
        twist.linear.x = float(linear)
        twist.angular.z = float(angular)
        end = time.time() + seconds
        while time.time() < end:
            if self._stop_event.is_set():
                break
            self.cmd_pub.publish(twist)
            time.sleep(0.05)
        self.stop()

    def stop(self) -> None:
        self.cmd_pub.publish(Twist())

    def emergency_stop(self) -> None:
        """置位急停标志并立刻发零速。必须在任何超时/异常处理里优先调用。"""
        self._stop_event.set()
        self.stop()

    def clear_emergency_stop(self) -> None:
        self._stop_event.clear()

    def set_speed_cap(self, value: float) -> None:
        self._speed_cap = value

    def _capped(self, speed: float) -> float:
        return min(speed, self._speed_cap) if self._speed_cap else speed

    def goto(
        self,
        tx: float,
        ty: float,
        speed: float = 0.15,
        tolerance: float = 0.12,
        timeout: float | None = None,
    ) -> SkillResult:
        """比例控制导航：先转向目标，再前进，odom 闭环。"""
        speed = self._capped(speed)
        deadline = time.time() + (timeout or self.goto_timeout)
        while time.time() < deadline:
            if self._stop_event.is_set():
                return SkillResult(ok=False, detail="导航被急停中断")
            try:
                x, y, yaw = self.robot_pose()
            except TimeoutError as exc:
                return SkillResult(ok=False, detail=str(exc))
            dx, dy = tx - x, ty - y
            if math.hypot(dx, dy) < tolerance:
                self.stop()
                return SkillResult(
                    ok=True, detail=f"已到达 ({tx:.2f}, {ty:.2f})", data={"x": tx, "y": ty}
                )
            err = _wrap(math.atan2(dy, dx) - yaw)
            angular = max(-1.5, min(1.5, 2.0 * err))
            linear = speed if abs(err) < 0.35 else 0.0
            self.drive(linear, angular, 0.1)
        self.stop()
        return SkillResult(ok=False, detail=f"导航超时，未能到达 ({tx:.2f}, {ty:.2f})")

    def rotate_to(
        self,
        target_yaw: float,
        *,
        tolerance_deg: float = 3.0,
        timeout: float = 15.0,
    ) -> SkillResult:
        """角速度闭环转到目标朝向。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._stop_event.is_set():
                return SkillResult(ok=False, detail="旋转被急停中断")
            try:
                _, _, yaw = self.robot_pose()
            except TimeoutError as exc:
                return SkillResult(ok=False, detail=str(exc))
            err = _wrap(target_yaw - yaw)
            if abs(math.degrees(err)) < tolerance_deg:
                self.stop()
                return SkillResult(
                    ok=True,
                    detail=f"已转到 {math.degrees(yaw):.1f}°",
                    data={"yaw_deg": round(math.degrees(yaw), 1)},
                )
            angular = max(-1.0, min(1.0, 2.0 * err))
            self.drive(0.0, angular, 0.1)
        self.stop()
        return SkillResult(ok=False, detail="旋转超时")

    def wait(self, seconds: float) -> SkillResult:
        """可被急停打断的等待。"""
        deadline = time.time() + seconds
        while time.time() < deadline:
            if self._stop_event.is_set():
                return SkillResult(ok=False, detail="等待被急停中断")
            time.sleep(0.05)
        return SkillResult(ok=True, detail=f"已等待 {seconds:.2f}s", data={"waited_s": seconds})

    def set_gripper(self, *, open_: bool) -> SkillResult:
        """0 = 闭合，1 = 张开。换成 GripperCommand action 只需改这里。"""
        self.gripper_pub.publish(Float64(data=1.0 if open_ else 0.0))
        self._gripper_open = open_
        return SkillResult(
            ok=True,
            detail="夹爪已张开" if open_ else "夹爪已闭合",
            data={"gripper_open": open_},
        )

    def shutdown(self) -> None:
        """确保停车后再销毁节点。"""
        try:
            self.stop()
        finally:
            self.node.destroy_node()
            rclpy.try_shutdown()
            logger.info("ros_bridge_stopped")


# --------------------------------------------------------------------- 技能


class RosGotoSkill(Skill):
    name: ClassVar[str] = "goto_point"
    description: ClassVar[str] = "把机器人导航到场内坐标 (x, y)，速度 speed m/s"
    params_model: ClassVar[type[SkillParams]] = GotoParams
    is_motion: ClassVar[bool] = True

    def __init__(self, bridge: RosBridge) -> None:
        self.bridge = bridge

    def run(self, x: float, y: float, speed: float = 0.15) -> SkillResult:  # type: ignore[override]
        return self.bridge.goto(x, y, speed)


class RosMoveRelativeSkill(Skill):
    name: ClassVar[str] = "move_relative"
    description: ClassVar[str] = "相对当前位置移动 (dx, dy)，dx 右为正、dy 前为正，单位米"
    params_model: ClassVar[type[SkillParams]] = MoveRelativeParams
    is_motion: ClassVar[bool] = True

    def __init__(self, bridge: RosBridge) -> None:
        self.bridge = bridge

    def run(self, dx: float, dy: float, speed: float = 0.15) -> SkillResult:  # type: ignore[override]
        try:
            x, y, _ = self.bridge.robot_pose()
        except TimeoutError as exc:
            return SkillResult(ok=False, detail=str(exc))
        return self.bridge.goto(x + dx, y + dy, speed)


class RosRotateSkill(Skill):
    name: ClassVar[str] = "rotate"
    description: ClassVar[str] = "原地旋转指定角度（度），逆时针为正"
    params_model: ClassVar[type[SkillParams]] = RotateParams
    is_motion: ClassVar[bool] = True

    def __init__(self, bridge: RosBridge) -> None:
        self.bridge = bridge

    def run(self, yaw_deg: float) -> SkillResult:  # type: ignore[override]
        try:
            _, _, yaw = self.bridge.robot_pose()
        except TimeoutError as exc:
            return SkillResult(ok=False, detail=str(exc))
        return self.bridge.rotate_to(yaw + math.radians(yaw_deg))


class RosLookAtSkill(Skill):
    name: ClassVar[str] = "look_at"
    description: ClassVar[str] = "原地转向，使机器人朝向场内坐标 (x, y)"
    params_model: ClassVar[type[SkillParams]] = LookAtParams
    is_motion: ClassVar[bool] = True

    def __init__(self, bridge: RosBridge) -> None:
        self.bridge = bridge

    def run(self, x: float, y: float) -> SkillResult:  # type: ignore[override]
        try:
            rx, ry, _ = self.bridge.robot_pose()
        except TimeoutError as exc:
            return SkillResult(ok=False, detail=str(exc))
        return self.bridge.rotate_to(math.atan2(y - ry, x - rx))


class RosPatrolSkill(Skill):
    name: ClassVar[str] = "patrol"
    description: ClassVar[str] = "按给定巡逻点序列循环移动 loops 圈"
    params_model: ClassVar[type[SkillParams]] = PatrolParams
    risk: ClassVar[RiskLevel] = "medium"
    is_motion: ClassVar[bool] = True

    def __init__(self, bridge: RosBridge) -> None:
        self.bridge = bridge

    def run(self, waypoints: list[GotoParams], loops: int = 1) -> SkillResult:  # type: ignore[override]
        visited = 0
        for _ in range(loops):
            for point in waypoints:
                result = self.bridge.goto(point.x, point.y, point.speed)
                if not result.ok:
                    return SkillResult(
                        ok=False,
                        detail=f"巡逻在第 {visited + 1} 个点位中断: {result.detail}",
                        data={"visited": visited},
                    )
                visited += 1
        last = waypoints[-1]
        return SkillResult(
            ok=True,
            detail=f"完成巡逻：{visited} 个点位（{loops} 圈）",
            data={"visited": visited, "x": last.x, "y": last.y},
        )


class RosPushSkill(Skill):
    name: ClassVar[str] = "push_cube"
    description: ClassVar[str] = "绕到指定颜色方块后方，将其推到目标点 (x, y)"
    params_model: ClassVar[type[SkillParams]] = PushCubeParams
    risk: ClassVar[RiskLevel] = "medium"
    is_motion: ClassVar[bool] = True

    def __init__(self, bridge: RosBridge) -> None:
        self.bridge = bridge

    def run(self, color: str, x: float, y: float) -> SkillResult:  # type: ignore[override]
        model = f"{color}_cube"
        cube = self.bridge.model_pose(model)
        if cube is None:
            return SkillResult(ok=False, detail=f"Gazebo 场景中未找到 {model}")

        cx, cy = cube
        dx, dy = x - cx, y - cy
        norm = math.hypot(dx, dy) or 1.0
        ux, uy = dx / norm, dy / norm

        # 1) 绕到方块背面（沿目标方向反向偏 0.35m，留出车身）
        approach = self.bridge.goto(cx - 0.35 * ux, cy - 0.35 * uy, speed=0.15)
        if not approach.ok:
            return approach

        # 2) 低速顶推，方块进入目标 0.20m 圈即停
        deadline = time.time() + 60.0
        while time.time() < deadline:
            if self.bridge._stop_event.is_set():
                return SkillResult(ok=False, detail="推箱被急停中断")
            current = self.bridge.model_pose(model)
            if current and math.hypot(current[0] - x, current[1] - y) < 0.20:
                self.bridge.stop()
                return SkillResult(
                    ok=True,
                    detail=f"{color} 方块已推至目标 {0.20:.2f}m 内",
                    data={"x": current[0], "y": current[1]},
                )
            self.bridge.drive(0.06, 0.0, 0.1)
        self.bridge.stop()
        return SkillResult(ok=False, detail="推箱超时，方块未到达目标区域")


class RosPickSkill(Skill):
    name: ClassVar[str] = "pick_cube"
    description: ClassVar[str] = "抓取指定颜色方块（先导航到方块旁，再闭合夹爪）"
    params_model: ClassVar[type[SkillParams]] = PickCubeParams
    risk: ClassVar[RiskLevel] = "high"
    is_motion: ClassVar[bool] = True

    def __init__(self, bridge: RosBridge) -> None:
        self.bridge = bridge

    def run(self, color: str) -> SkillResult:  # type: ignore[override]
        model = f"{color}_cube"
        cube = self.bridge.model_pose(model)
        if cube is None:
            return SkillResult(ok=False, detail=f"Gazebo 场景中未找到 {model}")

        try:
            rx, ry, _ = self.bridge.robot_pose()
        except TimeoutError as exc:
            return SkillResult(ok=False, detail=str(exc))

        dist = math.hypot(cube[0] - rx, cube[1] - ry)
        if dist > _PICK_REACH_M:
            # 停在方块前 0.25m，不要压上去——真机上这一步压上去就是撞
            approach_x = cube[0] - 0.25 * (cube[0] - rx) / (dist or 1.0)
            approach_y = cube[1] - 0.25 * (cube[1] - ry) / (dist or 1.0)
            moved = self.bridge.goto(approach_x, approach_y, speed=0.1)
            if not moved.ok:
                return moved

        closed = self.bridge.set_gripper(open_=False)
        if not closed.ok:
            return closed
        return SkillResult(
            ok=True,
            detail=f"已闭合夹爪抓取 {color} 方块（接近距离 {dist:.2f}m）",
            data={"color": color, "approach_distance_m": round(dist, 3)},
        )


class RosPlaceSkill(Skill):
    name: ClassVar[str] = "place_cube"
    description: ClassVar[str] = "把夹爪中的方块放置到目标点 (x, y)"
    params_model: ClassVar[type[SkillParams]] = PlaceCubeParams
    risk: ClassVar[RiskLevel] = "high"
    is_motion: ClassVar[bool] = True

    def __init__(self, bridge: RosBridge) -> None:
        self.bridge = bridge

    def run(self, x: float, y: float) -> SkillResult:  # type: ignore[override]
        moved = self.bridge.goto(x, y, speed=0.1)
        if not moved.ok:
            return moved
        opened = self.bridge.set_gripper(open_=True)
        if not opened.ok:
            return opened
        return SkillResult(
            ok=True, detail=f"已在 ({x:.2f}, {y:.2f}) 松开夹爪", data={"x": x, "y": y}
        )


class RosOpenGripperSkill(Skill):
    name: ClassVar[str] = "open_gripper"
    description: ClassVar[str] = "张开夹爪"
    params_model: ClassVar[type[SkillParams]] = GripperParams

    def __init__(self, bridge: RosBridge) -> None:
        self.bridge = bridge

    def run(self) -> SkillResult:  # type: ignore[override]
        return self.bridge.set_gripper(open_=True)


class RosCloseGripperSkill(Skill):
    name: ClassVar[str] = "close_gripper"
    description: ClassVar[str] = "闭合夹爪"
    params_model: ClassVar[type[SkillParams]] = GripperParams
    risk: ClassVar[RiskLevel] = "medium"

    def __init__(self, bridge: RosBridge) -> None:
        self.bridge = bridge

    def run(self) -> SkillResult:  # type: ignore[override]
        return self.bridge.set_gripper(open_=False)


class RosGetPoseSkill(Skill):
    name: ClassVar[str] = "get_pose"
    description: ClassVar[str] = "读取机器人或某个方块当前位姿"
    params_model: ClassVar[type[SkillParams]] = GetPoseParams

    def __init__(self, bridge: RosBridge) -> None:
        self.bridge = bridge

    def run(self, target: str) -> SkillResult:  # type: ignore[override]
        if target == "robot":
            try:
                x, y, _ = self.bridge.robot_pose()
            except TimeoutError as exc:
                return SkillResult(ok=False, detail=str(exc))
        else:
            pose = self.bridge.model_pose(f"{target}_cube")
            if pose is None:
                return SkillResult(ok=False, detail=f"未找到 {target}_cube")
            x, y = pose
        return SkillResult(
            ok=True, detail=f"{target} 位于 ({x:.2f}, {y:.2f})", data={"x": x, "y": y}
        )


class RosReportStatusSkill(Skill):
    name: ClassVar[str] = "report_status"
    description: ClassVar[str] = "上报机器人当前状态快照（位姿、夹爪、场上方块）"
    params_model: ClassVar[type[SkillParams]] = ReportStatusParams

    def __init__(self, bridge: RosBridge) -> None:
        self.bridge = bridge

    def run(self) -> SkillResult:  # type: ignore[override]
        try:
            x, y, yaw = self.bridge.robot_pose()
        except TimeoutError as exc:
            return SkillResult(ok=False, detail=str(exc))
        with self.bridge._lock:
            models = dict(self.bridge._models)
        return SkillResult(
            ok=True,
            detail=f"机器人位于 ({x:.2f}, {y:.2f})，朝向 {math.degrees(yaw):.1f}°",
            data={
                "robot": {
                    "x": round(x, 3),
                    "y": round(y, 3),
                    "yaw_deg": round(math.degrees(yaw), 1),
                },
                "gripper_open": self.bridge._gripper_open,
                "models": {k: [round(v[0], 3), round(v[1], 3)] for k, v in models.items()},
            },
        )


class RosWaitSkill(Skill):
    name: ClassVar[str] = "wait"
    description: ClassVar[str] = "等待指定时长（秒）后继续，用于给物理动作留出稳定时间"
    params_model: ClassVar[type[SkillParams]] = WaitParams

    def __init__(self, bridge: RosBridge) -> None:
        self.bridge = bridge

    def run(self, duration_s: float) -> SkillResult:  # type: ignore[override]
        return self.bridge.wait(duration_s)


class RosSetSpeedSkill(Skill):
    name: ClassVar[str] = "set_speed"
    description: ClassVar[str] = "设置后续动作的线速度上限（m/s）"
    params_model: ClassVar[type[SkillParams]] = SetSpeedParams
    risk: ClassVar[RiskLevel] = "medium"

    def __init__(self, bridge: RosBridge) -> None:
        self.bridge = bridge

    def run(self, max_speed: float) -> SkillResult:  # type: ignore[override]
        self.bridge.set_speed_cap(max_speed)
        return SkillResult(
            ok=True, detail=f"速度上限已设为 {max_speed:.2f} m/s", data={"max_speed": max_speed}
        )


class RosStopSkill(Skill):
    """急停。执行器超时后会自动调用它，也可由人工通过 API 触发。"""

    name: ClassVar[str] = "stop"
    description: ClassVar[str] = "立即停车（零速指令），用于异常与人工急停"
    params_model: ClassVar[type[SkillParams]] = StopParams

    def __init__(self, bridge: RosBridge) -> None:
        self.bridge = bridge

    def run(self) -> SkillResult:  # type: ignore[override]
        self.bridge.emergency_stop()
        return SkillResult(ok=True, detail="已发送急停指令")


#: ROS2 后端提供的全部技能。顺序即注册顺序，与 mock 后端必须一一对应。
ROS_SKILL_CLASSES: tuple[type[Skill], ...] = (
    RosGotoSkill,
    RosMoveRelativeSkill,
    RosRotateSkill,
    RosLookAtSkill,
    RosPatrolSkill,
    RosPushSkill,
    RosPickSkill,
    RosPlaceSkill,
    RosOpenGripperSkill,
    RosCloseGripperSkill,
    RosGetPoseSkill,
    RosReportStatusSkill,
    RosWaitSkill,
    RosSetSpeedSkill,
    RosStopSkill,
)


def ros2_skill_names() -> list[str]:
    """ROS2 后端的技能名。不需要 rclpy 即可调用，供跨后端一致性测试使用。"""
    return sorted(cls.name for cls in ROS_SKILL_CLASSES)


def build_ros_registry(settings: Settings | None = None) -> tuple[SkillRegistry, RosBridge]:
    """装配 ROS2 执行器。返回 (registry, bridge)；调用方负责 ``bridge.shutdown()``。"""
    bridge = RosBridge(settings)
    registry = SkillRegistry()
    registry.register_all([cls(bridge) for cls in ROS_SKILL_CLASSES])
    return registry, bridge
