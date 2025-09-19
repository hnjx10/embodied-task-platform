"""技能参数契约。

所有技能的参数模型集中在这里，而不是 mock 与 ROS2 各写一份。因为同一份 schema
同时喂给三个消费者：

1. **LLM 规划器**——从它生成函数描述，模型据此填参数；
2. **安全闸门**——用它做执行前的参数校验（``ParamSchemaPolicy``）；
3. **执行器**——用它把 ``dict`` 转成类型安全的入参。

三处一旦不一致，就会出现「模型按 schema 生成的调用被闸门拒绝」这类最难查的问题。
集中定义保证它们看到的是同一份契约。

关于取值范围，有一条刻意的分界：

* **物理安全边界**（速度上限、场地范围、禁区）不放这里，交给安全策略。否则非法值
  在 schema 层就被拒，安全闸门永远没有机会表现，也没法做「降速后继续执行」这种修正。
* **技能自身的约束**（等待时长、巡逻点数量、必填参数）放这里，因为它们是能力边界，
  不是安全策略——问一个「等 100 小时」的请求，本身就是无效请求。
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from robot_agent.skills.base import SkillParams

CubeColor = Literal["red", "green", "blue"]

# ---------------------------------------------------------------- 移动


class GotoParams(SkillParams):
    x: float = Field(description="目标 x 坐标（米）")
    y: float = Field(description="目标 y 坐标（米）")
    # 不给上下限：超速由 SpeedLimitPolicy 降速修正，而不是在 schema 层拒绝
    speed: float = Field(default=0.15, description="线速度 m/s")


class MoveRelativeParams(SkillParams):
    dx: float = Field(description="相对当前位置的 x 偏移（米），右为正")
    dy: float = Field(description="相对当前位置的 y 偏移（米），前为正")
    speed: float = Field(default=0.15, description="线速度 m/s")


class RotateParams(SkillParams):
    yaw_deg: float = Field(description="原地旋转角度（度），逆时针为正")


class LookAtParams(SkillParams):
    x: float = Field(description="要朝向的点的 x 坐标（米）")
    y: float = Field(description="要朝向的点的 y 坐标（米）")


class PatrolParams(SkillParams):
    waypoints: list[GotoParams] = Field(
        min_length=1,
        max_length=8,
        description="巡逻点序列，按顺序循环",
    )
    loops: int = Field(default=1, ge=1, le=5, description="循环圈数")


# ---------------------------------------------------------------- 操作


class PushCubeParams(SkillParams):
    color: CubeColor = Field(description="方块颜色")
    x: float = Field(description="目标 x 坐标（米）")
    y: float = Field(description="目标 y 坐标（米）")


class PickCubeParams(SkillParams):
    color: CubeColor = Field(description="要抓取的方块颜色")


class PlaceCubeParams(SkillParams):
    x: float = Field(description="放置点 x 坐标（米）")
    y: float = Field(description="放置点 y 坐标（米）")


class GripperParams(SkillParams):
    """夹爪开关无参数——需要参数才能开合的夹爪，应急时用不了。"""


# ---------------------------------------------------------------- 系统


class GetPoseParams(SkillParams):
    target: Literal["robot", "red", "green", "blue"] = Field(description="查询对象")


class WaitParams(SkillParams):
    duration_s: float = Field(ge=0.1, le=30.0, description="等待时长（秒）")


class SetSpeedParams(SkillParams):
    max_speed: float = Field(gt=0.0, description="把线速度上限设为该值（m/s）")


class StopParams(SkillParams):
    """急停无参数——需要参数才能停下来的急停不叫急停。"""


class ReportStatusParams(SkillParams):
    """状态上报无参数。"""


# ---------------------------------------------------------------- 能源 / 外设


class DockParams(SkillParams):
    """回充无参数。

    停到哪儿由场地配置决定（``arena.dock_pose``），不该让模型指定——模型没有
    充电桩坐标的可靠来源，让它填等于给它一个编错坐标的机会。
    """


class UndockParams(SkillParams):
    distance: float = Field(
        default=0.3, ge=0.05, le=2.0, description="离开充电桩的距离（米），朝场地中心方向"
    )


class GetBatteryParams(SkillParams):
    """电量查询无参数。"""


class SetLedParams(SkillParams):
    color: Literal["red", "green", "blue", "off"] = Field(description="灯带颜色，off 为关闭")
    brightness: float = Field(default=1.0, ge=0.0, le=1.0, description="亮度，0~1")


class CaptureImageParams(SkillParams):
    """抓拍一张图并归档。

    ``label`` 会被用作文件名的一部分，因此必须限制字符集：不限制就等于把
    ``../../etc/passwd`` 这种路径交给模型去填。这里用白名单而不是黑名单——
    黑名单永远漏一个没被想到的变体。
    """

    label: str = Field(
        default="snapshot",
        pattern=r"^[A-Za-z0-9_\u4e00-\u9fa5-]{1,64}$",
        description="图像标签，仅允许字母、数字、下划线、连字符与中文，用于归档检索",
    )


class ResetOdomParams(SkillParams):
    """重置里程计无参数：把当前位姿当作原点，重置本身不需要目标值。"""
