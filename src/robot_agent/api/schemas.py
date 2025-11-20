"""HTTP 接口的请求/响应模型。

对外契约与内部模型分离：内部 ``TaskRecord`` 改动不会直接破坏 API，
响应只暴露 ``to_public()`` 里明确列出的字段。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class TaskCreateRequest(BaseModel):
    """提交任务。"""

    model_config = ConfigDict(extra="forbid")

    instruction: str = Field(
        min_length=1,
        max_length=500,
        description="自然语言指令，例如「把红色方块推到左边」",
        examples=["把红色方块推到左边"],
    )
    wait: bool = Field(
        default=False,
        description="true 时同步等待到终态（受超时限制）；false 立即返回 202，后台执行",
    )
    idempotency_key: str | None = Field(
        default=None,
        max_length=128,
        description="幂等键。相同键重复提交不会重复执行，直接返回已有任务",
    )


class ApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approved_by: str = Field(default="api-operator", max_length=64)
    note: str = Field(default="", max_length=500)


class RejectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(default="operator rejected", max_length=500)


class CancelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(default="cancelled by operator", max_length=500)


class TaskListResponse(BaseModel):
    """分页结果。

    ``count`` 是本次返回的条数，``total`` 是满足过滤条件的总数——
    客户端靠后者渲染「第 1-20 条，共 340 条」，也才知道还要不要翻页。
    """

    items: list[dict[str, Any]]
    count: int
    total: int
    limit: int
    offset: int
    has_more: bool


class StopRequest(BaseModel):
    """急停请求。字段全部可选——停车不该因为少填一个参数而被拒。"""

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(default="operator emergency stop", max_length=200)


class ErrorBody(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class ErrorResponse(BaseModel):
    error: ErrorBody


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded", "fail"]
    version: str
    env: str
    checks: dict[str, Any] = Field(default_factory=dict)
