"""行为安全层：参数边界 + 速度上限 + 电子围栏，越界指令在执行前拦截。"""
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

ARENA_HALF = 1.5          # 3m x 3m 场地
SPEED_LIMIT = 0.22        # TurtleBot3 Waffle 最大线速度
GEOFENCES: List[Tuple[float, float, float]] = [
    # (x, y, radius) —— 圆形禁区，例如充电桩区域
    (1.3, 1.3, 0.4),
]

HIGH_RISK_HINTS = ("全速", "撞", "冲向", "最快", "destroy")


@dataclass
class SafetyVerdict:
    ok: bool
    reason: str = ""
    fixed_args: Optional[Dict[str, Any]] = None


def _inside_geofence(x: float, y: float) -> bool:
    for gx, gy, r in GEOFENCES:
        if (x - gx) ** 2 + (y - gy) ** 2 < r * r:
            return True
    return False


def check_step(skill_name: str, args: Dict[str, Any], instruction: str = "") -> SafetyVerdict:
    """对单个技能步骤做三层校验。返回 ok=False 时主流程直接拦截。"""
    # 0) 指令级风险词（演示用粗筛，生产应换模型审核器）
    if any(w in instruction for w in HIGH_RISK_HINTS):
        pass  # 风险词不直接拦截，交给下面的参数硬校验兜底

    if skill_name in ("goto_point", "push_cube"):
        xs = [args.get("x"), args.get("y")]
        if any(v is None for v in xs):
            return SafetyVerdict(False, f"缺少坐标参数: {args}")
        x, y = float(xs[0]), float(xs[1])
        # 1) 参数边界
        if not (-ARENA_HALF <= x <= ARENA_HALF and -ARENA_HALF <= y <= ARENA_HALF):
            return SafetyVerdict(False, f"目标点 ({x:.2f}, {y:.2f}) 超出场地边界 ±{ARENA_HALF}m")
        # 2) 电子围栏
        if _inside_geofence(x, y):
            return SafetyVerdict(False, f"目标点 ({x:.2f}, {y:.2f}) 位于禁区（充电桩）内")
        # 3) 速度上限
        if skill_name == "goto_point":
            speed = float(args.get("speed", 0.15))
            if speed > SPEED_LIMIT:
                return SafetyVerdict(
                    False,
                    f"请求速度 {speed} m/s 超过硬件上限 {SPEED_LIMIT} m/s，已拦截",
                    fixed_args={**args, "speed": SPEED_LIMIT},
                )
            if speed <= 0:
                return SafetyVerdict(False, f"非法速度 {speed}")
    return SafetyVerdict(True, "通过")
