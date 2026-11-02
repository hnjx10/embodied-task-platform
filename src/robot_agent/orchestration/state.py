"""任务状态模型：API、存储、编排三处共用的一份契约。

状态机::

    queued ──► running ──► succeeded
       │          │
       │          ├──► blocked            （安全闸门拒绝）
       │          ├──► failed             （执行失败且重试耗尽）
       │          ├──► awaiting_approval ──► running（人工批准）
       │          │                     └──► blocked（人工驳回）
       └──────────┴──► cancelled

``awaiting_approval`` 是唯一可以从非终态「倒回」运行态的状态，因此审批动作
在存储层是 CAS（比较并交换）语义，防止两次并发批准导致重复执行。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from robot_agent.planner.base import PlanStep, StepOutcome

TaskStatus = Literal[
    "queued",
    "running",
    "awaiting_approval",
    "succeeded",
    "failed",
    "blocked",
    "cancelled",
]

TERMINAL_STATUSES: frozenset[str] = frozenset({"succeeded", "failed", "blocked", "cancelled"})


def new_task_id() -> str:
    """任务 ID：带前缀便于日志检索，26 位可读性优于纯 UUID。"""
    return f"task_{uuid.uuid4().hex[:20]}"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TaskRecord(BaseModel):
    """任务的持久化形态。"""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=new_task_id)
    instruction: str
    status: TaskStatus = "queued"

    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    started_at: datetime | None = None
    finished_at: datetime | None = None

    plan: list[PlanStep] = Field(default_factory=list)
    plan_source: str = "rules"
    plan_warnings: list[str] = Field(default_factory=list)
    corrections: list[dict[str, Any]] = Field(default_factory=list)
    outcomes: list[StepOutcome] = Field(default_factory=list)
    verifications: list[dict[str, Any]] = Field(default_factory=list)
    report: str = ""

    requires_approval: bool = False
    approval: dict[str, Any] | None = None

    error: dict[str, Any] | None = None
    trace_id: str | None = None
    idempotency_key: str | None = None
    requested_by: str | None = None

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def duration_s(self) -> float | None:
        if self.started_at is None:
            return None
        end = self.finished_at or utcnow()
        return (end - self.started_at).total_seconds()

    def to_public(self) -> dict[str, Any]:
        """对外视图：隐藏内部字段，时间统一 ISO8601。"""
        return {
            "id": self.id,
            "instruction": self.instruction,
            "status": self.status,
            "created_at": self.created_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "duration_s": round(self.duration_s, 3) if self.duration_s is not None else None,
            "plan": [s.model_dump() for s in self.plan],
            "plan_source": self.plan_source,
            "plan_warnings": self.plan_warnings,
            "corrections": self.corrections,
            "outcomes": [o.to_dict() for o in self.outcomes],
            "verifications": self.verifications,
            "report": self.report,
            "requires_approval": self.requires_approval,
            "approval": self.approval,
            "error": self.error,
            "trace_id": self.trace_id,
        }
