"""统一异常体系。

约定：

* 所有对外抛出的异常都继承 :class:`RobotAgentError`，携带稳定的 ``code``
  与 ``http_status``，HTTP 层与 CLI 层无需再维护一张映射表。
* ``retryable`` 标记区分「瞬时故障」与「确定性失败」。技能执行器与 LLM
  客户端据此决定是否重试——安全拦截（:class:`SafetyViolation`）永远不可重试，
  否则重试会变成绕过安全策略的后门。
"""

from __future__ import annotations

from typing import Any


class RobotAgentError(Exception):
    """全部业务异常的基类。"""

    code: str = "internal_error"
    http_status: int = 500
    retryable: bool = False

    def __init__(
        self,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}
        self.__cause__ = cause

    def to_dict(self) -> dict[str, Any]:
        """序列化为 API 错误体 / 审计条目。"""
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            payload["details"] = self.details
        return payload

    def __str__(self) -> str:  # pragma: no cover - 透传给日志
        return self.message


# ---------------------------------------------------------------- 配置与启动


class ConfigurationError(RobotAgentError):
    """配置缺失或非法，进程不应启动。"""

    code = "configuration_error"
    http_status = 500


# ------------------------------------------------------------------ 规划阶段


class PlanningError(RobotAgentError):
    """自然语言无法被解析为合法技能序列。"""

    code = "planning_error"
    http_status = 422


class PlanValidationError(PlanningError):
    """LLM 返回了结构非法的计划（缺字段、未知技能、参数类型错误）。"""

    code = "plan_validation_error"


class LLMError(RobotAgentError):
    """LLM 调用失败（鉴权、限流、服务端错误）。可降级到规则规划。"""

    code = "llm_error"
    http_status = 502
    retryable = True


class LLMTimeoutError(LLMError):
    code = "llm_timeout"


class LLMBudgetExceededError(LLMError):
    """单次规划超出 token 预算，直接降级，避免成本失控。"""

    code = "llm_budget_exceeded"
    retryable = False


# ------------------------------------------------------------------ 安全阶段


class SafetyViolation(RobotAgentError):  # noqa: N818 —— 领域术语，改叫 *Error 反而别扭
    """安全闸门拒绝执行。

    刻意设 ``retryable = False``：任何自动重试都可能被用来试探策略边界。
    """

    code = "safety_violation"
    http_status = 422
    retryable = False

    def __init__(
        self,
        reason: str,
        *,
        step: dict[str, Any] | None = None,
        policy: str = "",
        details: dict[str, Any] | None = None,
    ) -> None:
        merged = dict(details or {})
        merged.setdefault("policy", policy)
        if step is not None:
            merged.setdefault("step", step)
        super().__init__(reason, details=merged)
        self.policy = policy


# ------------------------------------------------------------------ 执行阶段


class SkillError(RobotAgentError):
    code = "skill_error"
    http_status = 500


class SkillNotFoundError(SkillError):
    """请求的技能不在注册表中——通常是 LLM 幻觉出的技能名。"""

    code = "skill_not_found"
    http_status = 400


class SkillExecutionError(SkillError):
    """技能执行失败（超时、硬件拒绝、轨迹不可达）。"""

    code = "skill_execution_error"
    http_status = 502
    retryable = True


class SkillTimeoutError(SkillExecutionError):
    code = "skill_timeout"


class ExecutorUnavailableError(SkillError):
    """执行器不可用（rclpy 缺失、Gazebo 未启动）。"""

    code = "executor_unavailable"
    http_status = 503
    retryable = True


# ------------------------------------------------------------------ 存储与任务


class StorageError(RobotAgentError):
    code = "storage_error"
    http_status = 500
    retryable = True


class TaskNotFoundError(RobotAgentError):
    code = "task_not_found"
    http_status = 404


class TaskConflictError(RobotAgentError):
    """重复提交（幂等键冲突且请求体不同）。"""

    code = "task_conflict"
    http_status = 409


class TaskCancelledError(RobotAgentError):
    code = "task_cancelled"
    http_status = 409


class UnauthorizedError(RobotAgentError):
    code = "unauthorized"
    http_status = 401


class TooManySubscribersError(RobotAgentError):
    """单任务的事件订阅者超过上限：客户端可重试（退避后换个连接再订）。"""

    code = "too_many_subscribers"
    http_status = 429
    retryable = True


class DeviceBusyError(RobotAgentError):
    """设备被另一个执行者占用（另一副本 / CLI / 另一个任务正在驱动它）。

    可重试：占用通常是暂时的，退避后重新提交即可。不可重试会让调度器直接
    把任务判死，而实际上它只是排在别人后面。
    """

    code = "device_busy"
    http_status = 409
    retryable = True


# -------------------------------------------------------------------- 评测


class EvaluationError(RobotAgentError):
    """评测集本身有问题（文件缺失、字段非法、用例重复）。

    刻意与「用例未通过」区分：数据集坏了是工程错误，用例如实失败是正常结果。
    """

    code = "evaluation_error"
    http_status = 500
