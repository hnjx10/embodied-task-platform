"""任务相关接口。

语义约定：

* ``POST /v1/tasks`` 默认**异步**：立即返回 202 与任务 ID，客户端订阅 events 或轮询；
  ``wait=true`` 时同步等结果，但受 ``api.request_timeout_s`` 限制，超时也返回 202
  （任务仍在后台跑），避免 HTTP 连接长时间挂住。
* 状态迁移是 CAS 的：并发批准同一任务，只有一个拿到 200，另一个得到 409。
"""

from __future__ import annotations

import asyncio
import json
from typing import Annotated, Any

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from robot_agent.api.deps import AuthDep, RunnerDep, SettingsDep
from robot_agent.api.schemas import (
    ApprovalRequest,
    CancelRequest,
    RejectionRequest,
    TaskCreateRequest,
)
from robot_agent.logging import get_logger
from robot_agent.orchestration.state import TaskRecord

logger = get_logger(__name__)

router = APIRouter(prefix="/v1/tasks", tags=["tasks"])

TRACE_HEADER = "X-Trace-Id"
IDEM_HEADER = "Idempotency-Key"


@router.post("", status_code=202, summary="提交任务")
async def create_task(
    payload: TaskCreateRequest,
    request: Request,
    runner: RunnerDep,
    settings: SettingsDep,
    _auth: AuthDep,
    idempotency_header: Annotated[str | None, Header(alias=IDEM_HEADER)] = None,
) -> Any:
    trace_id = getattr(request.state, "trace_id", None)
    key = payload.idempotency_key or idempotency_header

    record = await runner.submit(
        payload.instruction,
        idempotency_key=key,
        trace_id=trace_id,
    )

    if payload.wait:
        try:
            record = await asyncio.wait_for(
                runner.execute(record.id), timeout=settings.api.request_timeout_s
            )
        except asyncio.TimeoutError:
            # 3.10 里 asyncio 的超时与内置 TimeoutError 不是同一个类，写错会漏接
            logger.warning("sync_wait_timeout", task_id=record.id)
            body = record.to_public()
            body["note"] = (
                f"等待超过 {settings.api.request_timeout_s:.0f}s，任务已转入后台执行，"
                "请通过 GET /v1/tasks/{id} 或 events 查询"
            )
            return JSONResponse(
                status_code=202, content=body, headers={TRACE_HEADER: trace_id or ""}
            )
    else:
        await runner.execute_background(record.id)

    return JSONResponse(
        status_code=202,
        content=record.to_public(),
        headers={TRACE_HEADER: trace_id or ""},
    )


@router.get("", summary="列出任务")
async def list_tasks(
    runner: RunnerDep,
    _auth: AuthDep,
    status: Annotated[str | None, Query(description="按状态过滤")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    records = await runner.list(status=status, limit=limit, offset=offset)
    return {"items": [r.to_public() for r in records], "count": len(records)}


@router.get("/{task_id}", summary="查询任务")
async def get_task(task_id: str, runner: RunnerDep, _auth: AuthDep) -> dict[str, Any]:
    record = await runner.get(task_id)
    return record.to_public()


@router.get("/{task_id}/events", summary="订阅任务事件（SSE）")
async def stream_events(
    task_id: str, request: Request, runner: RunnerDep, _auth: AuthDep
) -> StreamingResponse:
    record = await runner.get(task_id)
    bus = request.app.state.events
    queue = bus.subscribe(task_id)

    async def event_stream():
        try:
            # 先补一条当前状态，避免订阅晚于事件导致客户端一直等
            yield _sse("status", {"status": record.status, "snapshot": True})
            if record.is_terminal:
                yield _sse("end", {"status": record.status})
                return

            while True:
                if await request.is_disconnected():
                    break
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                if item is None:
                    yield _sse("end", {"status": "closed"})
                    break
                yield _sse(item.type, item.to_dict())
        finally:
            bus.unsubscribe(task_id, queue)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # 关掉 nginx 缓冲，否则 SSE 会被攒着发
        },
    )


@router.post("/{task_id}/approve", summary="批准待审批任务")
async def approve_task(
    task_id: str, payload: ApprovalRequest, runner: RunnerDep, _auth: AuthDep
) -> dict[str, Any]:
    record: TaskRecord = await runner.approve(
        task_id, approved_by=payload.approved_by, note=payload.note
    )
    return record.to_public()


@router.post("/{task_id}/reject", summary="驳回待审批任务")
async def reject_task(
    task_id: str, payload: RejectionRequest, runner: RunnerDep, _auth: AuthDep
) -> dict[str, Any]:
    record = await runner.reject(task_id, reason=payload.reason)
    return record.to_public()


@router.post("/{task_id}/cancel", summary="取消任务")
async def cancel_task(
    task_id: str, payload: CancelRequest, runner: RunnerDep, _auth: AuthDep
) -> dict[str, Any]:
    record = await runner.cancel(task_id, reason=payload.reason)
    return record.to_public()


def _sse(event: str, data: dict[str, Any]) -> str:
    """SSE 帧。``event:`` 便于客户端按类型分发，``data:`` 必须是单行 JSON。"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
