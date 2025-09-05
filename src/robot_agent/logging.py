"""结构化日志与请求上下文。

* **统一出口**：structlog 与标准库 logging 都经过同一个 ``ProcessorFormatter``，
  uvicorn / fastapi / 第三方库的日志同样输出为 JSON，便于采集到 Loki / ELK。
* **请求上下文**：``trace_id`` / ``task_id`` 存在 ``contextvars`` 里，异步任务
  之间天然隔离，日志里无需手工传参就能串起一次请求的全部日志。
* **异常不吞**：``exc_info=True`` 时完整堆栈进日志，业务异常同时保留机器可读的 ``code``。
"""

from __future__ import annotations

import json
import logging
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import structlog
from structlog.types import Processor

TRACE_HEADER = "X-Trace-Id"
_NOISY_LOGGERS = ("httpx", "httpcore", "urllib3", "asyncio", "aiosqlite")


def new_trace_id() -> str:
    """生成 16 位十六进制 trace id（足够单机排障，且不泄露时间信息）。"""
    return uuid.uuid4().hex[:16]


def _json_serializer(obj: Any, **kwargs: Any) -> str:
    kwargs.pop("default", None)
    return json.dumps(obj, ensure_ascii=False, default=str, **kwargs)


def _shared_processors() -> list[Processor]:
    return [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True, key="timestamp"),
        structlog.processors.StackInfoRenderer(),
    ]


def configure_logging(
    level: str = "INFO",
    *,
    json_output: bool = True,
    service: str = "robot-agent",
    env: str = "dev",
) -> None:
    """进程启动时调用一次。重复调用是安全的（会替换已有 handler）。"""
    log_level = logging.getLevelName(level.upper())
    if not isinstance(log_level, int):
        log_level = logging.INFO

    shared = _shared_processors()
    renderer: Processor = (
        structlog.processors.JSONRenderer(serializer=_json_serializer)
        if json_output
        else structlog.dev.ConsoleRenderer(colors=sys.stdout.isatty())
    )

    structlog.configure(
        processors=[
            *shared,
            structlog.processors.format_exc_info,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,
            structlog.processors.UnicodeDecoder(),
            renderer,
        ],
    )

    # 日志走 stderr：stdout 留给程序的结构化输出（CLI --json、指标导出），
    # 混在一起会让 `robot-agent run --json | jq` 直接解析失败。
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(log_level)

    # 让 uvicorn 复用 root handler，避免它自带的纯文本格式混进 JSON 流
    for name in ("uvicorn", "uvicorn.error"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True
    logging.getLogger("uvicorn.access").handlers.clear()
    logging.getLogger("uvicorn.access").propagate = False

    for noisy in _NOISY_LOGGERS:
        logging.getLogger(noisy).setLevel(logging.WARNING)

    structlog.contextvars.bind_contextvars(service=service, env=env)


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """获取绑定好上下文的 logger。"""
    return structlog.stdlib.get_logger(name or "robot_agent")


def bind_context(**values: Any) -> None:
    """把键值绑到当前异步上下文的全部后续日志。"""
    structlog.contextvars.bind_contextvars(**{k: v for k, v in values.items() if v is not None})


def unbind_context(*keys: str) -> None:
    structlog.contextvars.unbind_contextvars(*keys)


def clear_context() -> None:
    structlog.contextvars.clear_contextvars()


@contextmanager
def request_context(
    *,
    trace_id: str | None = None,
    task_id: str | None = None,
    **extra: Any,
) -> Iterator[str]:
    """为一段逻辑（一次 HTTP 请求 / 一个任务）建立隔离的日志上下文。

    退出时清理，避免 contextvars 在连接复用时串味。
    """
    tid = trace_id or new_trace_id()
    values = {"trace_id": tid, "task_id": task_id, **extra}
    bound = {k: v for k, v in values.items() if v is not None}
    with structlog.contextvars.bound_contextvars(**bound):
        yield tid


def current_trace_id() -> str | None:
    ctx = structlog.contextvars.get_contextvars()
    value = ctx.get("trace_id")
    return str(value) if value is not None else None
