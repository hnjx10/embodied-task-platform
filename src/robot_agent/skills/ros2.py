"""ROS2 执行器：把技能映射到 TurtleBot3 + Gazebo 的真实控制。

**导入即安全**：``rclpy`` 只在 ROS2 环境存在，模块在缺少依赖时仍可导入
（``ROS_AVAILABLE=False``），只有真正构造 :class:`RosBridge` 才会失败并抛出
:class:`ExecutorUnavailableError`。这样 CI 与 mock 环境不会因为 import 就崩。

控制要点（相对 demo 的改进）：

* ``goto`` 用 odom 闭环比例控制，带超时与到达容差，不再靠 sleep 猜时间；
* 推箱分两段：先绕到方块背面，再低速顶推，每 0.1s 检查一次方块位姿；
* 所有循环都检查 ``stop_event``，急停能在 100ms 内生效；
* 退出时确保发布零速，避免机器人带着最后一条 cmd_vel 继续走。
"""

from __future__ import annotations

import math
import threading
import time
from typing import Any, ClassVar, Literal

from pydantic import Field

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

logger = get_logger(__name__)

try:  # pragma: no cover - 取决于是否在 ROS2 环境
    import rclpy
    from gazebo_msgs.msg import ModelStates
    from geometry_msgs.msg import Twist
    from nav_msgs.msg import Odometry

    ROS_AVAILABLE = True
except ImportError:  # pragma: no cover
    rclpy = None  # type: ignore[assignment]
    Twist = Odometry = ModelStates = None  # type: ignore[assignment,misc]
    ROS_AVAILABLE = False


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
    """急停无参数。"""


def _yaw_from_quat(x: float, y: float, z: float, w: float) -> float:
    return math.atan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z))


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

        rclpy.init()
        self.node = rclpy.create_node("robot_agent_skill_bridge")
        self.cmd_pub = self.node.create_publisher(Twist, "/cmd_vel", 10)
        self._odom: tuple[float, float, float] | None = None
        self._models: dict[str, tuple[float, float]] = {}
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

    def goto(
        self,
        tx: float,
        ty: float,
        speed: float = 0.15,
        tolerance: float = 0.12,
        timeout: float | None = None,
    ) -> SkillResult:
        """比例控制导航：先转向目标，再前进，odom 闭环。"""
        deadline = time.time() + (timeout or self.goto_timeout)
        while time.time() < deadline:
            if self._stop_event.is_set():
                return SkillResult(ok=False, detail="导航被急停中断")
            x, y, yaw = self.robot_pose()
            dx, dy = tx - x, ty - y
            if math.hypot(dx, dy) < tolerance:
                self.stop()
                return SkillResult(
                    ok=True, detail=f"已到达 ({tx:.2f}, {ty:.2f})", data={"x": tx, "y": ty}
                )
            err = math.atan2(dy, dx) - yaw
            err = math.atan2(math.sin(err), math.cos(err))
            angular = max(-1.5, min(1.5, 2.0 * err))
            linear = speed if abs(err) < 0.35 else 0.0
            self.drive(linear, angular, 0.1)
        self.stop()
        return SkillResult(ok=False, detail=f"导航超时，未能到达 ({tx:.2f}, {ty:.2f})")

    def shutdown(self) -> None:
        """确保停车后再销毁节点。"""
        try:
            self.stop()
        finally:
            self.node.destroy_node()
            rclpy.try_shutdown()
            logger.info("ros_bridge_stopped")


class RosGotoSkill(Skill):
    name: ClassVar[str] = "goto_point"
    description: ClassVar[str] = "把机器人导航到场内坐标 (x, y)，速度 speed m/s"
    params_model: ClassVar[type[SkillParams]] = GotoParams
    is_motion: ClassVar[bool] = True

    def __init__(self, bridge: RosBridge) -> None:
        self.bridge = bridge

    def run(self, x: float, y: float, speed: float = 0.15) -> SkillResult:  # type: ignore[override]
        return self.bridge.goto(x, y, speed)


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


def build_ros_registry(settings: Settings | None = None) -> tuple[SkillRegistry, RosBridge]:
    """装配 ROS2 执行器。返回 (registry, bridge)；调用方负责 ``bridge.shutdown()``。"""
    bridge = RosBridge(settings)
    registry = SkillRegistry()
    registry.register_all(
        [
            RosGotoSkill(bridge),
            RosPushSkill(bridge),
            RosGetPoseSkill(bridge),
            RosStopSkill(bridge),
        ]
    )
    return registry, bridge
