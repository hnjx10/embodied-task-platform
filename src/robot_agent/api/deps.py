"""FastAPI 依赖：鉴权、运行时对象获取、追踪上下文。"""

from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import Depends, Header, Request

from robot_agent.config import Settings
from robot_agent.errors import UnauthorizedError
from robot_agent.orchestration.runner import TaskRunner

_BEARER_PREFIX = "bearer "


def get_settings(request: Request) -> Settings:
    return request.app.state.settings  # type: ignore[no-any-return]


def get_runner(request: Request) -> TaskRunner:
    return request.app.state.runner  # type: ignore[no-any-return]


async def require_auth(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    """Bearer Token 鉴权。

    * 未配置 ``auth_token`` 时仅允许本机访问（生产环境启动校验会拦住无 token 的
      对外监听，所以这里放行是安全的）；
    * 比较用 :func:`secrets.compare_digest`，避免按字节比较泄露 token 前缀。
    """
    settings: Settings = get_settings(request)
    expected = settings.api.auth_token

    if expected is None:
        client = request.client.host if request.client else "unknown"
        if settings.env == "prod" and client not in {"127.0.0.1", "::1", "localhost"}:
            raise UnauthorizedError("该实例要求 Bearer Token")
        return None

    if not authorization or not authorization.lower().startswith(_BEARER_PREFIX):
        raise UnauthorizedError("缺少 Authorization: Bearer <token>")

    provided = authorization[len(_BEARER_PREFIX) :].strip()
    if not secrets.compare_digest(provided, expected.get_secret_value()):
        raise UnauthorizedError("Bearer Token 无效")
    return None


# Annotated 别名：FastAPI 只认「类型注解 + Depends 元数据」这种形式，
# 直接把 Depends(...) 赋给变量当类型用会让它把 Depends 对象当成真实类型去解析。
AuthDep = Annotated[None, Depends(require_auth)]
RunnerDep = Annotated[TaskRunner, Depends(get_runner)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
