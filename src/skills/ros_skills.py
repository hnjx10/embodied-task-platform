"""ROS2 执行器：把 goto / push / get_pose 实现为对 TurtleBot3 + Gazebo 的真实控制。

依赖 rclpy，仅在 ROS2 Humble 环境可导入；graph.py 按运行环境选择执行器。
"""
import math
import threading
import time
from typing import Optional, Tuple

from .base import Skill, SkillResult

try:
    import rclpy
    from geometry_msgs.msg import Twist
    from nav_msgs.msg import Odometry
    from gazebo_msgs.msg import ModelStates
    ROS_AVAILABLE = True
except ImportError:  # pragma: no cover
    ROS_AVAILABLE = False


def _yaw_from_quat(x: float, y: float, z: float, w: float) -> float:
    return math.atan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z))


class RosBridge:
    """单节点后台 spin，向所有技能共享 /cmd_vel、/odom、/gazebo/model_states。"""

    def __init__(self) -> None:
        if not ROS_AVAILABLE:
            raise RuntimeError("未检测到 rclpy，请使用 --mock 模式或在 ROS2 环境运行")
        rclpy.init()
        self.node = rclpy.create_node("langgraph_skill_bridge")
        self.cmd_pub = self.node.create_publisher(Twist, "/cmd_vel", 10)
        self._odom: Optional[Tuple[float, float, float]] = None
        self._models: dict = {}
        self.node.create_subscription(Odometry, "/odom", self._odom_cb, 10)
        self.node.create_subscription(ModelStates, "/gazebo/model_states", self._models_cb, 10)
        self._spin = threading.Thread(target=rclpy.spin, args=(self.node,), daemon=True)
        self._spin.start()
        time.sleep(0.5)

    def _odom_cb(self, msg) -> None:
        p = msg.pose.pose.position
        q = msg.pose.pose.orientation
        self._odom = (p.x, p.y, _yaw_from_quat(q.x, q.y, q.z, q.w))

    def _models_cb(self, msg) -> None:
        self._models = dict(zip(msg.name, zip(msg.pose.position.x, msg.pose.position.y)))

    def robot_pose(self) -> Tuple[float, float, float]:
        for _ in range(20):
            if self._odom:
                return self._odom
            time.sleep(0.1)
        raise TimeoutError("未收到 /odom，请确认 TurtleBot3 已启动")

    def model_pose(self, name: str) -> Optional[Tuple[float, float]]:
        return self._models.get(name)

    def drive(self, linear: float, angular: float, seconds: float) -> None:
        twist = Twist()
        twist.linear.x = linear
        twist.angular.z = angular
        end = time.time() + seconds
        while time.time() < end:
            self.cmd_pub.publish(twist)
            time.sleep(0.05)
        self.cmd_pub.publish(Twist())  # 停车

    def goto(self, tx: float, ty: float, speed: float = 0.15,
             tolerance: float = 0.12, timeout: float = 90.0) -> SkillResult:
        """比例控制导航：先对准航向，再前进，odom 闭环。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            x, y, yaw = self.robot_pose()
            dx, dy = tx - x, ty - y
            dist = math.hypot(dx, dy)
            if dist < tolerance:
                self.drive(0.0, 0.0, 0.1)
                return SkillResult(True, f"已到达 ({tx:.2f}, {ty:.2f})")
            err = math.atan2(dy, dx) - yaw
            err = math.atan2(math.sin(err), math.cos(err))
            angular = max(-1.5, min(1.5, 2.0 * err))
            linear = speed if abs(err) < 0.35 else 0.0
            self.drive(linear, angular, 0.1)
        return SkillResult(False, f"导航超时，未能到达 ({tx:.2f}, {ty:.2f})")

    def shutdown(self) -> None:
        self.node.destroy_node()
        rclpy.try_shutdown()


class RosGotoSkill(Skill):
    name = "goto_point"
    description = "将机器人导航到场内坐标 (x, y)，速度 speed m/s"
    params_schema = {"x": "float [-1.5, 1.5]", "y": "float [-1.5, 1.5]", "speed": "float (0, 0.22]"}

    def __init__(self, bridge: RosBridge) -> None:
        self.bridge = bridge

    def execute(self, x: float, y: float, speed: float = 0.15) -> SkillResult:
        return self.bridge.goto(x, y, speed)


class RosPushSkill(Skill):
    name = "push_cube"
    description = "导航到指定颜色方块后方并将其推到目标点 (x, y)"
    params_schema = {"color": "red|green|blue", "x": "float [-1.5, 1.5]", "y": "float [-1.5, 1.5]"}

    def __init__(self, bridge: RosBridge) -> None:
        self.bridge = bridge

    def execute(self, color: str, x: float, y: float) -> SkillResult:
        model = f"{color}_cube"
        cube = self.bridge.model_pose(model)
        if cube is None:
            return SkillResult(False, f"Gazebo 场景中未找到 {model}")
        cx, cy = cube
        # 1) 绕到方块后方（目标方向的反向延长线上，留 0.35m 车身距）
        dx, dy = x - cx, y - cy
        norm = math.hypot(dx, dy) or 1.0
        ux, uy = dx / norm, dy / norm
        r = self.bridge.goto(cx - 0.35 * ux, cy - 0.35 * uy, 0.15)
        if not r.ok:
            return r
        # 2) 低速顶推，方块进入目标 0.2m 圈即停
        deadline = time.time() + 60.0
        while time.time() < deadline:
            cube = self.bridge.model_pose(model)
            if cube and math.hypot(cube[0] - x, cube[1] - y) < 0.20:
                self.bridge.drive(0.0, 0.0, 0.1)
                return SkillResult(True, f"{color} 方块已推至目标附近")
            self.bridge.drive(0.06, 0.0, 0.1)  # 直行顶推
        return SkillResult(False, "推箱超时")

    def _unused(self):  # 占位：保持接口一致性
        pass


class RosGetPoseSkill(Skill):
    name = "get_pose"
    description = "读取机器人或方块当前位姿"
    params_schema = {"target": "robot|red|green|blue"}

    def __init__(self, bridge: RosBridge) -> None:
        self.bridge = bridge

    def execute(self, target: str) -> SkillResult:
        if target == "robot":
            x, y, _ = self.bridge.robot_pose()
        else:
            p = self.bridge.model_pose(f"{target}_cube")
            if p is None:
                return SkillResult(False, f"未找到 {target}_cube")
            x, y = p
        return SkillResult(True, f"{target} 位于 ({x:.2f}, {y:.2f})", data={"x": x, "y": y})


def build_ros_registry():
    from .base import SkillRegistry

    bridge = RosBridge()
    registry = SkillRegistry()
    for s in (RosGotoSkill(bridge), RosPushSkill(bridge), RosGetPoseSkill(bridge)):
        registry.register(s)
    return registry, bridge
