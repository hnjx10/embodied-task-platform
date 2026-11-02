"""FastAPI 应用装配。

三个刻意为之的细节：

1. **用纯 ASGI 中间件而不是 ``BaseHTTPMiddleware``**：后者会包一层任务与缓冲，
   对 SSE 这种长连接流式响应有已知的延迟/断流问题。这里直接包装 ``send``，
   只加一个响应头，不碰消息体。
2. **异常到 HTTP 的映射只写一次**：业务异常自带 ``http_status``，处理器统一转换；
   未预期异常一律 500 且**不回显内部信息**，只给 trace_id 让运维去查日志。
3. **lifespan 里装配、关闭时释放**：执行器线程池、SQLite 连接、ROS 节点的
   生命周期都跟着应用走，避免 reload 时句柄泄漏。
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from robot_agent.api.routes import health, tasks
from robot_agent.api.routes import metrics as metrics_routes
from robot_agent.config import Settings, load_settings
from robot_agent.errors import RobotAgentError
from robot_agent.logging import (
    configure_logging,
    get_logger,
    new_trace_id,
    request_context,
)
from robot_agent.version import __version__

logger = get_logger(__name__)

Scope = dict[str, Any]
Message = dict[str, Any]
ASGIApp = Callable[
    [Scope, Callable[[], Awaitable[Message]], Callable[[Message], Awaitable[None]]], Awaitable[None]
]

TRACE_HEADER_LOWER = b"x-trace-id"


class TraceMiddleware:
    """注入 trace_id、记录访问日志、回写响应头。纯 ASGI，不缓冲流式响应。"""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        trace_id = headers.get(TRACE_HEADER_LOWER, b"").decode("latin-1") or new_trace_id()
        scope.setdefault("state", {})["trace_id"] = trace_id

        method = scope.get("method", "")
        path = scope.get("path", "")
        started = time.perf_counter()
        status_holder = {"code": 0}

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_holder["code"] = int(message.get("status", 0))
                message.setdefault("headers", [])
                message["headers"].append((TRACE_HEADER_LOWER, trace_id.encode("latin-1")))
            await send(message)

        with request_context(trace_id=trace_id):
            try:
                await self.app(scope, receive, send_wrapper)
            except Exception:
                # 让上层异常处理器/服务器感知，但先留下访问日志便于排障
                duration = (time.perf_counter() - started) * 1000
                logger.error(
                    "http_request_failed",
                    method=method,
                    path=path,
                    duration_ms=round(duration, 2),
                    exc_info=True,
                )
                raise
            else:
                duration = (time.perf_counter() - started) * 1000
                # 健康探针和指标抓取量大且噪音高，降级为 debug
                quiet = path in {"/healthz", "/readyz", "/metrics"}
                log = logger.debug if quiet else logger.info
                log(
                    "http_request",
                    method=method,
                    path=path,
                    status=status_holder["code"],
                    duration_ms=round(duration, 2),
                )


def create_app(settings: Settings | None = None) -> FastAPI:
    """构建应用实例。测试可直接传入 Settings 而不读环境变量。"""
    settings = settings or load_settings()
    configure_logging(
        settings.log_level,
        json_output=settings.log_json,
        service=settings.service_name,
        env=settings.env,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        from robot_agent.factory import build_assembly

        assembly = await build_assembly(settings)
        app.state.settings = settings
        app.state.assembly = assembly
        app.state.runner = assembly.runner
        app.state.registry = assembly.registry
        app.state.events = assembly.events
        logger.info(
            "api_started",
            version=__version__,
            env=settings.env,
            backend=settings.executor.backend,
            storage=settings.storage.url.split("://")[0],
            auth=settings.api.auth_token is not None,
        )
        try:
            yield
        finally:
            logger.info("api_stopping")
            # 先让在飞任务收尾，再关数据库/执行器，避免任务状态卡在 running
            await assembly.runner.shutdown(timeout=10.0)
            await assembly.aclose()

    app = FastAPI(
        title="robot-agent",
        version=__version__,
        summary="LLM 驱动的机器人任务编排服务",
        description=(
            "把自然语言指令编译为受约束的技能序列：规划 → 安全闸门 → 执行 → 核验 → 报告。\n\n"
            "所有任务可审计（哈希链）、可观测（Prometheus）、可人工接管（审批）。"
        ),
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url=None,
        openapi_url="/openapi.json",
        openapi_tags=[
            {"name": "tasks", "description": "任务提交、查询、审批与事件订阅"},
            {"name": "health", "description": "存活/就绪探针与版本"},
            {"name": "observability", "description": "指标导出"},
        ],
    )

    app.add_middleware(TraceMiddleware)
    if settings.api.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.api.cors_origins,
            allow_credentials=False,
            allow_methods=["GET", "POST"],
            allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "X-Trace-Id"],
            expose_headers=["X-Trace-Id"],
        )

    app.include_router(health.router)
    app.include_router(tasks.router)
    app.include_router(metrics_routes.router)

    _register_exception_handlers(app)
    return app


def _register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(RobotAgentError)
    async def handle_domain_error(request: Request, exc: RobotAgentError) -> JSONResponse:
        """业务异常自带状态码；4xx 不打堆栈（不是故障），5xx 才记 error。"""
        trace_id = getattr(request.state, "trace_id", None)
        log = logger.warning if exc.http_status < 500 else logger.error
        log(
            "request_rejected",
            code=exc.code,
            status=exc.http_status,
            message=exc.message,
            path=request.url.path,
        )
        headers = {"X-Trace-Id": trace_id} if trace_id else {}
        if exc.http_status == 401:
            headers["WWW-Authenticate"] = "Bearer"
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": exc.to_dict(), "trace_id": trace_id},
            headers=headers,
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        trace_id = getattr(request.state, "trace_id", None)
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "validation_error",
                    "message": "请求体校验失败",
                    "details": {"errors": exc.errors()},
                },
                "trace_id": trace_id,
            },
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        """兜底：绝不把内部堆栈或 SQL 泄露给调用方。"""
        trace_id = getattr(request.state, "trace_id", None)
        logger.error("unhandled_exception", path=request.url.path, exc_info=True)
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "internal_error",
                    "message": "服务内部错误，请凭 trace_id 联系运维",
                },
                "trace_id": trace_id,
            },
        )
