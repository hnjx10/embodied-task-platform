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
from datetime import datetime, timezone
from pathlib import Path
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
from robot_agent.skills.ros2_action import NavActionClient, NavOutcome

logger = get_logger(__name__)

try:  # pragma: no cover - 取决于是否在 ROS2 环境
    import rclpy
    from gazebo_msgs.msg import ModelStates
    from geometry_msgs.msg import Twist
    from nav_msgs.msg import Odometry
    from sensor_msgs.msg import BatteryState, Image
    from std_msgs.msg import ColorRGBA, Float64
    from std_srvs.srv import Empty

    ROS_AVAILABLE = True
except ImportError:  # pragma: no cover
    rclpy = None  # type: ignore[assignment]
    Twist = Odometry = ModelStates = Float64 = None  # type: ignore[assignment,misc]
    BatteryState = Image = ColorRGBA = Empty = None  # type: ignore[assignment,misc]
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
        self.settings = settings
        self.goto_timeout = settings.executor.step_timeout_s
        self._stop_event = threading.Event()
        self._speed_cap: float | None = None

        rclpy.init()
        self.node = rclpy.create_node("robot_agent_skill_bridge")
        self.cmd_pub = self.node.create_publisher(Twist, "/cmd_vel", 10)
        self.gripper_pub = self.node.create_publisher(Float64, "/gripper/position", 10)
        self.led_pub = self.node.create_publisher(ColorRGBA, "/led/color", 10)
        self._odom: tuple[float, float, float] | None = None
        #: 最近一次 /odom 回调的时刻（monotonic）。安全层用它判断位姿是否过期——
        #: 订阅读着却没有时间戳，等于「有数据但不知道是不是三分钟前的」。
        self._odom_at: float | None = None
        self._models: dict[str, tuple[float, float]] = {}
        self._battery_pct: float | None = None
        self._frame: tuple[int, int, str, bytes] | None = None
        self._gripper_open = True
        self._lock = threading.Lock()
        self.node.create_subscription(Odometry, "/odom", self._odom_cb, 10)
        self.node.create_subscription(ModelStates, "/gazebo/model_states", self._models_cb, 10)
        self.node.create_subscription(BatteryState, "/battery_state", self._battery_cb, 10)
        self.node.create_subscription(Image, "/camera/image_raw", self._frame_cb, 10)
        self.reset_odom_client = self.node.create_client(Empty, "/reset_odom")
        # Action 导航是可选路径：Nav2 存在时用它（有 feedback 与取消语义），
        # 不存在时 RosGotoSkill 回退到 /cmd_vel 闭环。
        self._nav: NavActionClient | None = None
        if settings.executor.use_nav_action:
            self._nav = NavActionClient(
                self.node,
                action_name=settings.executor.nav_action_name,
                frame_id=settings.executor.nav_frame_id,
            )
        self._spin = threading.Thread(target=rclpy.spin, args=(self.node,), daemon=True)
        self._spin.start()
        logger.info("ros_bridge_started", node=self.node.get_name())

    # ------------------------------------------------------------------ 回调

    def _odom_cb(self, msg: Any) -> None:
        p = msg.pose.pose.position
        q = msg.pose.pose.orientation
        with self._lock:
            self._odom = (p.x, p.y, _yaw_from_quat(q.x, q.y, q.z, q.w))
            self._odom_at = time.monotonic()

    def pose_age_s(self) -> float | None:
        """距最近一次 /odom 回调过去了多少秒。从未收到过则返回 None。

        返回 None 而不是 ``inf``：调用方需要区分「还没开始收」和「收了但很旧」，
        这两种情况在排查时的下一步动作完全不同（等节点起来 / 查 DDS 与话题）。
        """
        with self._lock:
            if self._odom_at is None:
                return None
            return max(0.0, time.monotonic() - self._odom_at)

    def _models_cb(self, msg: Any) -> None:
        poses = {
            name: (pose.position.x, pose.position.y)
            for name, pose in zip(msg.name, msg.pose, strict=False)
        }
        with self._lock:
            self._models = poses

    def _battery_cb(self, msg: Any) -> None:
        """BatteryState.percentage 按规范是 0~1，未实现时给 -1。"""
        raw = float(getattr(msg, "percentage", -1.0))
        pct = raw * 100.0 if 0.0 <= raw <= 1.0 else -1.0
        with self._lock:
            self._battery_pct = pct

    def _frame_cb(self, msg: Any) -> None:
        with self._lock:
            self._frame = (
                int(msg.width),
                int(msg.height),
                str(msg.encoding),
                bytes(msg.data),
            )

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

    # ------------------------------------------------------------------ Action

    def nav_available(self) -> bool:
        """Nav2 的 NavigateToPose 是否可用。不可用时调用方回退 Topic 闭环。"""
        return self._nav is not None and self._nav.available()

    def navigate_to_pose(self, x: float, y: float, yaw: float | None = None) -> NavOutcome:
        """通过 Action 导航到点位。未启用时返回明确的失败原因，不静默。"""
        if self._nav is None:
            return NavOutcome(
                False,
                "未启用 Action 导航（ROBOT_AGENT_EXECUTOR__USE_NAV_ACTION=true 可开启）",
                status="disabled",
            )

        def _progress(remaining: float) -> None:
            # 进度只打 debug：导航 feedback 通常 1~10Hz，info 级别会把日志刷满
            logger.debug("nav_feedback", target_x=x, target_y=y, remaining_m=round(remaining, 3))

        return self._nav.navigate(
            x=x,
            y=y,
            yaw=yaw,
            timeout=self.goto_timeout,
            stop_event=self._stop_event,
            on_feedback=_progress,
        )

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

    # ------------------------------------------------------------------ 能源

    def battery(self) -> float | None:
        """返回电量百分比；驱动未发布 ``/battery_state`` 时为 None。"""
        with self._lock:
            return None if self._battery_pct is None else round(self._battery_pct, 1)

    def dock(self) -> SkillResult:
        """导航到充电桩前方停靠点。

        停靠点是"距桩心 clearance 米"的位置，不是桩心本身：桩本身在禁区里，
        直接开向桩心等于让机器人往禁区里钻，安全闸门会（正确地）拦下它。
        """
        dock_x, dock_y = self.settings.arena.dock_pose
        clearance = self.settings.arena.dock_clearance
        try:
            x, y, _ = self.robot_pose()
        except TimeoutError as exc:
            return SkillResult(ok=False, detail=str(exc))

        dx, dy = dock_x - x, dock_y - y
        dist = math.hypot(dx, dy)
        if dist <= clearance:
            return SkillResult(
                ok=True,
                detail=f"已在充电位（距桩心 {dist:.2f}m）",
                data={"x": x, "y": y, "distance_to_dock_m": round(dist, 3)},
            )

        ratio = (dist - clearance) / dist
        target_x, target_y = x + dx * ratio, y + dy * ratio
        moved = self.goto(target_x, target_y, speed=0.08, tolerance=0.05)
        if not moved.ok:
            return moved
        return SkillResult(
            ok=True,
            detail=f"已停靠 ({target_x:.2f}, {target_y:.2f})，距桩心 {clearance:.2f}m",
            data={"x": target_x, "y": target_y, "distance_to_dock_m": clearance},
        )

    def undock(self, distance: float = 0.3) -> SkillResult:
        dock_x, dock_y = self.settings.arena.dock_pose
        try:
            x, y, _ = self.robot_pose()
        except TimeoutError as exc:
            return SkillResult(ok=False, detail=str(exc))

        dx, dy = dock_x - x, dock_y - y
        norm = math.hypot(dx, dy)
        if norm < 1e-6:
            dx, dy, norm = -1.0, -1.0, math.sqrt(2.0)
        target_x = x - dx / norm * distance
        target_y = y - dy / norm * distance
        moved = self.goto(target_x, target_y, speed=0.08, tolerance=0.05)
        if not moved.ok:
            return moved
        return SkillResult(
            ok=True,
            detail=f"已离开充电桩 {distance:.2f}m，当前 ({target_x:.2f}, {target_y:.2f})",
            data={"x": target_x, "y": target_y},
        )

    # ------------------------------------------------------------------ 外设

    _LED_RGB: ClassVar[dict[str, tuple[float, float, float]]] = {
        "red": (1.0, 0.0, 0.0),
        "green": (0.0, 1.0, 0.0),
        "blue": (0.0, 0.0, 1.0),
        "off": (0.0, 0.0, 0.0),
    }

    def set_led(self, color: str, brightness: float = 1.0) -> SkillResult:
        rgb = self._LED_RGB.get(color)
        if rgb is None:
            return SkillResult(ok=False, detail=f"不支持的灯色: {color}")
        message = ColorRGBA()
        message.r = rgb[0] * brightness
        message.g = rgb[1] * brightness
        message.b = rgb[2] * brightness
        message.a = 1.0
        self.led_pub.publish(message)
        return SkillResult(
            ok=True,
            detail=f"灯带已设为 {color}（亮度 {brightness:.2f}）",
            data={"color": color, "brightness": brightness},
        )

    def capture(self, label: str) -> SkillResult:
        """抓拍最近一帧相机图像，写成 PPM。

        只接受 ``rgb8``：PPM 的 P6 格式就是 RGB 三通道原始字节，编码不符时
        必须明确失败——把 bgr8 当 rgb8 写出去，图像红蓝互换，肉眼未必看出来，
        下游算法会算出错误的颜色判定。
        """
        with self._lock:
            frame = self._frame
        if frame is None:
            return SkillResult(ok=False, detail="未收到 /camera/image_raw，无法抓拍")

        width, height, encoding, data = frame
        if encoding != "rgb8":
            return SkillResult(ok=False, detail=f"仅支持 rgb8 编码，当前为 {encoding}")

        payload = f"P6\n{width} {height}\n255\n".encode("ascii") + data
        info: dict[str, object] = {
            "label": label,
            "width": width,
            "height": height,
            "encoding": encoding,
            "format": "ppm",
            "bytes": len(payload),
            "persisted": False,
            "path": "",
        }
        image_dir = self.settings.executor.image_dir
        if not image_dir:
            return SkillResult(ok=True, detail="已抓拍（未配置 image_dir，不落盘）", data=info)

        directory = Path(image_dir)
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        path = directory / f"{stamp}-{label}.ppm"
        path.write_bytes(payload)
        info["persisted"] = True
        info["path"] = str(path)
        return SkillResult(ok=True, detail=f"已抓拍并保存 {path}", data=info)

    def reset_odom(self) -> SkillResult:
        """把里程计归零。物理位置不变，只是基准点变了。"""
        if not self.reset_odom_client.service_is_ready():
            return SkillResult(
                ok=False,
                detail="未发现 /reset_odom 服务，请确认机器人驱动已启动",
            )
        self.reset_odom_client.call_async(Empty.Request())
        with self._lock:
            self._odom = (0.0, 0.0, 0.0)
            # 归零也是一次「位姿刚更新」，否则紧接着的移动动作会被判成位姿过期
            self._odom_at = time.monotonic()
        return SkillResult(ok=True, detail="里程计已归零，当前位置定义为原点")

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
        """优先走 Nav2 Action，不可用时回退 ``/cmd_vel`` 闭环。

        注意回退只发生在「服务不可用」这一种情况。如果 Action 明确返回
        ``aborted`` / ``rejected``，说明规划器认为这个目标到不了——此时再用
        比例控制硬冲一遍，等于把「规划失败」掩盖成「好像到了」，事故复盘时
        最难查的就是这种。
        """
        outcome = self.bridge.navigate_to_pose(x, y)
        if outcome.ok:
            return SkillResult(ok=True, detail=outcome.detail, data=outcome.data)
        if outcome.status not in {"unavailable", "disabled"}:
            return SkillResult(ok=False, detail=outcome.detail, data=outcome.data)

        logger.info("nav_action_fallback_to_topic", reason=outcome.status)
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


# ------------------------------------------------------------ 能源 / 外设


class RosDockSkill(Skill):
    name: ClassVar[str] = "dock"
    description: ClassVar[str] = "返回充电桩并停靠（停在桩前安全距离处）"
    params_model: ClassVar[type[SkillParams]] = DockParams
    risk: ClassVar[RiskLevel] = "medium"
    is_motion: ClassVar[bool] = True

    def __init__(self, bridge: RosBridge) -> None:
        self.bridge = bridge

    def run(self) -> SkillResult:  # type: ignore[override]
        return self.bridge.dock()


class RosUndockSkill(Skill):
    name: ClassVar[str] = "undock"
    description: ClassVar[str] = "离开充电桩，朝远离桩心方向移动指定距离"
    params_model: ClassVar[type[SkillParams]] = UndockParams
    is_motion: ClassVar[bool] = True

    def __init__(self, bridge: RosBridge) -> None:
        self.bridge = bridge

    def run(self, distance: float = 0.3) -> SkillResult:  # type: ignore[override]
        return self.bridge.undock(distance)


class RosGetBatterySkill(Skill):
    name: ClassVar[str] = "get_battery"
    description: ClassVar[str] = "读取电池剩余电量百分比"
    params_model: ClassVar[type[SkillParams]] = GetBatteryParams

    def __init__(self, bridge: RosBridge) -> None:
        self.bridge = bridge

    def run(self) -> SkillResult:  # type: ignore[override]
        level = self.bridge.battery()
        if level is None:
            return SkillResult(
                ok=False,
                detail="未收到 /battery_state，电量未知（不要把它当成满电继续跑）",
            )
        return SkillResult(
            ok=True,
            detail=f"电量 {level:.1f}%" + ("（偏低，建议回充）" if level < 20.0 else ""),
            data={"battery_pct": level, "low": level < 20.0},
        )


class RosSetLedSkill(Skill):
    name: ClassVar[str] = "set_led"
    description: ClassVar[str] = "设置机身灯带颜色与亮度，用于向现场人员表达状态"
    params_model: ClassVar[type[SkillParams]] = SetLedParams

    def __init__(self, bridge: RosBridge) -> None:
        self.bridge = bridge

    def run(self, color: str, brightness: float = 1.0) -> SkillResult:  # type: ignore[override]
        return self.bridge.set_led(color, brightness)


class RosCaptureImageSkill(Skill):
    name: ClassVar[str] = "capture_image"
    description: ClassVar[str] = "抓拍当前视野图像并归档，标签用于后续检索"
    params_model: ClassVar[type[SkillParams]] = CaptureImageParams

    def __init__(self, bridge: RosBridge) -> None:
        self.bridge = bridge

    def run(self, label: str = "snapshot") -> SkillResult:  # type: ignore[override]
        return self.bridge.capture(label)


class RosResetOdomSkill(Skill):
    name: ClassVar[str] = "reset_odom"
    description: ClassVar[str] = "把当前位置设为里程计原点（物理位置不变）"
    params_model: ClassVar[type[SkillParams]] = ResetOdomParams
    risk: ClassVar[RiskLevel] = "medium"

    def __init__(self, bridge: RosBridge) -> None:
        self.bridge = bridge

    def run(self) -> SkillResult:  # type: ignore[override]
        return self.bridge.reset_odom()


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
    RosDockSkill,
    RosUndockSkill,
    RosGetBatterySkill,
    RosSetLedSkill,
    RosCaptureImageSkill,
    RosResetOdomSkill,
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
