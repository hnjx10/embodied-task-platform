"""领域层：跨层共享的数据契约，不依赖任何外部设施。

这一层只有 pydantic 模型与少量纯函数，没有 I/O、没有配置、没有日志。
好处是它可以被任何一层依赖而不引入耦合——``PlanStep`` 同时被规划、安全、
编排、存储使用，如果它长在某一层内部，其余层就必须反向依赖那一层。
"""

from robot_agent.domain.plan import Outcome, Plan, PlanSource, PlanStep, StepOutcome
from robot_agent.domain.state import (
    TERMINAL_STATUSES,
    TaskRecord,
    TaskStatus,
    new_task_id,
    utcnow,
)

__all__ = [
    "TERMINAL_STATUSES",
    "Outcome",
    "Plan",
    "PlanSource",
    "PlanStep",
    "StepOutcome",
    "TaskRecord",
    "TaskStatus",
    "new_task_id",
    "utcnow",
]
