"""健康检查与版本信息。

区分 liveness 与 readiness 是容器编排的基本要求：

* ``/healthz``（liveness）—— 进程还活着就返回 200。**不查外部依赖**，
  否则数据库抖动会导致 k8s 把正常的 Pod 反复重启。
* ``/readyz``（readiness）—— 能否接业务。检查存储、执行器、审计目录，
  不满足返回 503，让流量切走但不重启进程。
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from robot_agent.api.deps import SettingsDep
from robot_agent.version import __version__

router = APIRouter(tags=["health"])

_STARTED_AT = time.time()


@router.get("/healthz", summary="存活探针")
async def healthz(settings: SettingsDep) -> dict[str, Any]:
    return {
        "status": "ok",
        "version": __version__,
        "env": settings.env,
        "uptime_s": round(time.time() - _STARTED_AT, 1),
    }


@router.get("/readyz", summary="就绪探针")
async def readyz(request: Request, settings: SettingsDep) -> JSONResponse:
    checks: dict[str, Any] = {}
    ok = True

    # 存储可读
    try:
        await request.app.state.runner.repo.list(limit=1)
        checks["storage"] = {"ok": True, "backend": settings.storage.url.split("://")[0]}
    except Exception as exc:
        ok = False
        checks["storage"] = {"ok": False, "error": str(exc)}

    # 执行器已装配
    registry = request.app.state.registry
    if len(registry) == 0:
        ok = False
        checks["executor"] = {"ok": False, "error": "没有可用技能"}
    else:
        checks["executor"] = {
            "ok": True,
            "backend": settings.executor.backend,
            "skills": registry.names(),
        }

    # 审计目录可写（只判断父目录，不真的写文件污染审计链）
    audit_path = settings.audit.path
    if settings.audit.enabled:
        writable = audit_path.parent.exists()
        if not writable:
            ok = False
        checks["audit"] = {"ok": writable, "path": str(audit_path)}

    body = {
        "status": "ok" if ok else "fail",
        "version": __version__,
        "env": settings.env,
        "checks": checks,
    }
    return JSONResponse(status_code=200 if ok else 503, content=body)


@router.get("/version", summary="版本信息")
async def version() -> dict[str, str]:
    return {"name": "robot-agent", "version": __version__}
