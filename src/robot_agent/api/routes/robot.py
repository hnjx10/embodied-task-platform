"""机器人本体操作。

急停是这里唯一的接口，但它的设计约束值得写清楚：

* **不挂任务、不排队**：等拿到 task_id 或排在物理动作信号量后面，事故已经发生了。
* **尽力而为但要说实话**：执行器没有 stop 技能时返回 503，而不是回一个「已急停」的
  200——报平安比报错危险得多。
* **必须留痕**：它绕过了编排层，所以审计由这层自己补，附上操作者与原因。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from robot_agent.api.deps import AuthDep
from robot_agent.api.schemas import StopRequest
from robot_agent.domain.state import utcnow
from robot_agent.errors import ExecutorUnavailableError
from robot_agent.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/v1/robot", tags=["robot"])

STOP_SKILL = "stop"


@router.post("/stop", summary="急停（立即停车）")
async def emergency_stop(
    request: Request,
    _auth: AuthDep,
    payload: StopRequest | None = None,
) -> dict[str, Any]:
    assembly = request.app.state.assembly
    reason = payload.reason if payload else "operator"
    registry = assembly.registry

    if not registry.has(STOP_SKILL):
        # mock / ros2 后端都实现了 stop；缺失说明装配有问题，不能假装停下来了
        assembly.audit.record("emergency_stop_failed", {"reason": reason})
        raise ExecutorUnavailableError(
            "当前执行器没有 stop 技能，急停指令未下发",
            details={"backend": assembly.settings.executor.backend},
        )

    assembly.components.executor.emergency_stop(reason=reason)
    at = utcnow().isoformat(timespec="milliseconds")
    assembly.audit.record("emergency_stop", {"reason": reason, "at": at})
    logger.warning("emergency_stop_requested", reason=reason, via="http")

    return {
        "stopped": True,
        "reason": reason,
        "backend": assembly.settings.executor.backend,
        "at": at,
        "note": "只停车、不改任务状态；执行中的任务会因当前步骤失败而停在 failed",
    }
