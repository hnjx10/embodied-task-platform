"""Prometheus 指标端点。"""

from __future__ import annotations

from fastapi import APIRouter, Response

from robot_agent import metrics

router = APIRouter(tags=["observability"])


@router.get("/metrics", summary="Prometheus 指标", include_in_schema=False)
async def metrics_endpoint() -> Response:
    payload, content_type = metrics.render_metrics()
    return Response(content=payload, media_type=content_type)
