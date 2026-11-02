"""规划结果的数据模型。

``PlanStep`` 是编排层、安全层、执行层之间唯一的契约对象。用它替代裸 ``dict``
带来三个直接收益：

* 安全策略可以按字段做类型化判断，不必到处 ``args.get("x")`` 猜类型；
* 审计与 API 序列化有稳定结构，回放旧日志不会因为字段顺序漂移而失败；
* ``source`` 字段让「LLM 规划」与「规则兜底」在指标里可区分，便于评估降级率。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

PlanSource = Literal["llm", "rules", "manual", "replay"]
Outcome = Literal["ok", "rejected", "blocked", "error", "timeout", "skipped"]


class PlanStep(BaseModel):
    """一个技能调用。"""

    model_config = ConfigDict(extra="ignore")

    skill: str = Field(min_length=1, description="技能名，必须存在于注册表")
    args: dict[str, Any] = Field(default_factory=dict, description="技能参数")
    # 可选：期望达成的状态，供 verifier 做结果核验（例如 {"color": "red", "x": -1.2}）
    expect: dict[str, Any] | None = None
    # 该步是否允许自动重试（默认继承技能的风险等级）
    retryable: bool = True

    @field_validator("skill")
    @classmethod
    def _strip_skill(cls, value: str) -> str:
        return value.strip()

    def label(self) -> str:
        """日志里的一行摘要，参数过长时截断，避免刷屏。"""
        args = ", ".join(f"{k}={v}" for k, v in sorted(self.args.items()))
        if len(args) > 120:
            args = args[:117] + "..."
        return f"{self.skill}({args})"


class Plan(BaseModel):
    """一次规划的完整产出。"""

    steps: list[PlanStep]
    source: PlanSource = "rules"
    model: str | None = None
    latency_ms: float = 0.0
    warnings: list[str] = Field(default_factory=list)
    # 原始模型输出，仅用于排障；默认不落审计（体积大且可能含用户指令）
    raw: str | None = Field(default=None, repr=False)

    def __len__(self) -> int:
        return len(self.steps)

    def motion_steps(self) -> list[PlanStep]:
        return [s for s in self.steps if s.expect is not None]


class StepOutcome(BaseModel):
    """单步执行结果，进入任务状态与最终报告。"""

    index: int
    skill: str
    outcome: Outcome
    detail: str = ""
    latency_ms: float = 0.0
    attempts: int = 1
    data: dict[str, Any] = Field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.outcome == "ok"

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")
