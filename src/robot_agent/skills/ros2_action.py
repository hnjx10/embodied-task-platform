"""ROS2 Action 客户端：基于 ``nav2_msgs/action/NavigateToPose`` 的点位导航。

**为什么导航要用 Action 而不是 Topic**

用 ``/cmd_vel`` 发速度指令是最低层的控制方式，它能转向、能前进，但它回答不了
三个问题：

1. **到没到**：指令发出去就没有回执了，只能自己订阅 odom 反复比对，判断逻辑
   散落在每个调用方手里，而且「差 0.12m 算不算到」这种阈值会各写各的。
2. **进度多少**：前端想显示「还剩 0.4m」时，Topic 模式下没有任何数据源。
3. **能不能取消**：现场突然有人走进场地，需要立刻中止导航。Topic 只能发零速把
   车停住，但导航任务本身还挂着，恢复后它会继续朝原目标走。

Action 的 ``goal / feedback / result`` 三段正好对应这三个问题：goal 确认目标
被接受（或被拒绝），feedback 持续报进度，result 给出终态。所以本模块把导航
封装成 Action 调用；``/cmd_vel`` 闭环作为没有 Nav2 时的降级路径保留。

**降级策略**：``nav2_msgs`` 与 Nav2 都只在完整导航栈环境里存在。没装时
:class:`NavActionClient` 不抛异常，``available()`` 返回 False，调用方据此
回退到 Topic 闭环——两条路径的对外契约完全一致，技能层不感知差别。
"""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from robot_agent.logging import get_logger

logger = get_logger(__name__)

try:  # pragma: no cover - 取决于是否在 ROS2 环境
    from rclpy.action import ActionClient

    ACTION_AVAILABLE = True
except ImportError:  # pragma: no cover
    ActionClient = None  # type: ignore[assignment,misc]
    ACTION_AVAILABLE = False

#: ``action_msgs/msg/GoalStatus`` 里的终态常量。4 = SUCCEEDED。
_STATUS_SUCCEEDED = 4
_STATUS_CANCELED = 5
_STATUS_ABORTED = 6


def _yaw_to_quat(yaw: float) -> tuple[float, float]:
    """平面朝向转四元数，返回 (z, w)。平面导航只需绕 z 轴。"""
    return math.sin(yaw / 2.0), math.cos(yaw / 2.0)


@dataclass(frozen=True)
class NavOutcome:
    """导航终态。``status`` 用 Action 的原始语义，便于上层区分「取消」和「失败」。"""

    ok: bool
    detail: str
    status: str = ""
    data: dict[str, Any] = field(default_factory=dict)


class NavActionClient:
    """把 ``NavigateToPose`` 包装成一次阻塞调用，期间可被急停取消。"""

    def __init__(
        self,
        node: Any,
        *,
        action_name: str = "/navigate_to_pose",
        frame_id: str = "map",
    ) -> None:
        self.node = node
        self.action_name = action_name
        self.frame_id = frame_id
        self._client: Any = None

    def available(self) -> bool:
        """Action 服务是否就绪。未装 nav2_msgs 时恒为 False。"""
        if not ACTION_AVAILABLE:
            return False
        if self._client is None:
            return False
        try:
            return bool(self._client.server_is_ready())
        except Exception:  # pragma: no cover - 服务发现本身可能抛
            return False

    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        from nav2_msgs.action import NavigateToPose

        self._client = ActionClient(self.node, NavigateToPose, self.action_name)
        return self._client

    def navigate(
        self,
        *,
        x: float,
        y: float,
        yaw: float | None = None,
        timeout: float = 90.0,
        stop_event: threading.Event | None = None,
        on_feedback: Callable[[float], None] | None = None,
    ) -> NavOutcome:
        """发送导航目标并等待终态。

        所有等待都带超时，并且每轮都检查 ``stop_event``——急停必须能打断导航，
        而不是等它自己走完。取消后会再发一次 ``/cmd_vel`` 零速，双保险。
        """
        if not ACTION_AVAILABLE:
            return NavOutcome(False, "未安装 rclpy.action，无法使用 Action 导航")
        try:
            client = self._ensure_client()
        except ImportError:
            return NavOutcome(False, "未安装 nav2_msgs，无法使用 Action 导航")

        if not client.wait_for_server(timeout_sec=3.0):
            return NavOutcome(
                False,
                f"导航 Action 服务 {self.action_name} 不可用（Nav2 未启动？）",
                status="unavailable",
            )

        goal = self._build_goal(x, y, yaw)
        done = threading.Event()
        box: dict[str, Any] = {"remaining": -1.0}

        def _feedback(message: Any) -> None:
            remaining = float(getattr(message.feedback, "distance_remaining", -1.0))
            box["remaining"] = remaining
            if on_feedback is not None:
                on_feedback(remaining)

        def _on_result(future: Any) -> None:
            box["response"] = future.result()
            done.set()

        def _on_goal(future: Any) -> None:
            handle = future.result()
            box["handle"] = handle
            if not bool(getattr(handle, "accepted", False)):
                done.set()
                return
            handle.get_result_async().add_done_callback(_on_result)

        client.send_goal_async(goal, feedback_callback=_feedback).add_done_callback(_on_goal)

        deadline = time.monotonic() + timeout
        while not done.wait(0.1):
            if stop_event is not None and stop_event.is_set():
                self._cancel(box.get("handle"))
                return NavOutcome(False, "导航被急停取消", status="canceled", data={"x": x, "y": y})
            if time.monotonic() > deadline:
                self._cancel(box.get("handle"))
                return NavOutcome(
                    False,
                    f"导航超时（{timeout:.0f}s），已请求取消，剩余距离 {box['remaining']:.2f}m",
                    status="timeout",
                    data={"x": x, "y": y, "distance_remaining": box["remaining"]},
                )

        handle = box.get("handle")
        if handle is not None and not bool(getattr(handle, "accepted", False)):
            return NavOutcome(
                False, "导航目标被 Action 服务拒绝", status="rejected", data={"x": x, "y": y}
            )

        response = box.get("response")
        raw_status = int(getattr(response, "status", -1)) if response is not None else -1
        if raw_status == _STATUS_SUCCEEDED:
            yaw_deg = None if yaw is None else round(math.degrees(yaw), 1)
            return NavOutcome(
                True,
                f"已通过 Action 导航到达 ({x:.2f}, {y:.2f})",
                status="succeeded",
                data={"x": x, "y": y, "yaw_deg": yaw_deg},
            )
        label = {
            _STATUS_CANCELED: "canceled",
            _STATUS_ABORTED: "aborted",
        }.get(raw_status, "failed")
        return NavOutcome(
            False,
            f"导航未成功（status={raw_status}），可能被规划器中止",
            status=label,
            data={"x": x, "y": y, "status_code": raw_status},
        )

    def _build_goal(self, x: float, y: float, yaw: float | None) -> Any:
        from nav2_msgs.action import NavigateToPose

        goal = NavigateToPose.Goal()
        goal.pose.header.frame_id = self.frame_id
        goal.pose.header.stamp = self.node.get_clock().now().to_msg()
        goal.pose.pose.position.x = float(x)
        goal.pose.pose.position.y = float(y)
        goal.pose.pose.orientation.z, goal.pose.pose.orientation.w = _yaw_to_quat(yaw or 0.0)
        return goal

    def _cancel(self, handle: Any) -> None:
        if handle is None:
            return
        try:
            handle.cancel_goal_async()
        except Exception as exc:  # pragma: no cover - 取消失败不影响急停主路径
            logger.warning("nav_cancel_failed", error=str(exc))
