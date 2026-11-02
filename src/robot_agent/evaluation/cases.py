"""评测用例的数据契约与加载。

一个安全系统最容易被质疑的地方不是「有没有做校验」，而是「你说拦得住，凭什么」。
评测集就是把「凭什么」写成可执行文件：每条用例声明一句自然语言指令、一个期望
结论、以及为什么这样标注。

两种用例形态，对应两类完全不同的风险：

* **自然语言用例**（``instruction``）——走完整链路：规则解析 → 安全闸门。
  它检验的是「用户说什么」与「系统判定什么」之间的契约。
* **注入用例**（``plan``）——跳过解析器，直接给出一串技能调用。
  它模拟的是 **LLM 规划器幻觉**：技能名不存在、参数缺字段、步数超限。
  这类输入永远无法用自然语言稳定复现，只能注入。

用例的 ``expect`` 只有三种取值，刻意保持粗糙：``allow`` / ``block`` / ``approval``。
更细的区分（被哪条策略拦、降速到多少）用 ``expect_clamped`` 单独表达，
避免把「结论」和「理由」混在同一个字段里。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from robot_agent.errors import EvaluationError

try:
    import yaml
except ImportError:  # pragma: no cover - pyyaml 是核心依赖，这里只做兜底提示
    yaml = None  # type: ignore[assignment]

Expectation = Literal["allow", "block", "approval"]

DATA_DIR = Path(__file__).resolve().parent / "data"
DEFAULT_SUITE = DATA_DIR / "safety_cases.yaml"


class ClampExpectation(BaseModel):
    """期望某一步的某个参数被策略修正成指定值（例：超速被降到硬件上限）。"""

    model_config = ConfigDict(extra="forbid")

    index: int = Field(default=0, ge=0, description="第几步（从 0 开始）")
    field: str = Field(min_length=1, description="被修正的参数名")
    value: float = Field(description="期望修正后的值")


class SafetyCase(BaseModel):
    """一条评测用例。"""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, description="全局唯一标识，出现在失败明细里")
    category: str = Field(default="general", description="风险类别，用于分类统计")
    expect: Expectation
    why: str = Field(default="", description="标注理由：为什么这条应该被拦/放行")
    # 用 None 表示「未提供」，与「提供了空字符串」区分开——空指令本身就是一条用例
    instruction: str | None = Field(default=None, description="自然语言指令")
    plan: list[dict[str, Any]] | None = Field(
        default=None, description="注入的计划步骤；用于模拟 LLM 幻觉"
    )
    expect_clamped: list[ClampExpectation] = Field(default_factory=list)
    # 标注「当前实现明确做不到、但理想上应该做到」的用例。
    # 它们计入「已知缺口」而不是「失败」——一个从不失败的评测集，只有两种可能：
    # 系统完美，或者出题的人只会出自己会做的题。
    known_gap: bool = Field(default=False, description="已知能力缺口，不计入意外失败")

    @model_validator(mode="after")
    def _has_input(self) -> SafetyCase:
        """至少要有一种输入。

        允许二者共存：``plan`` 存在时以注入计划为准，``instruction`` 只作人类可读的
        意图说明——读数据集的人不必先读懂 JSON 才能知道这条用例在测什么。
        """
        if self.instruction is None and not self.plan:
            raise ValueError(f"用例 {self.id}: 必须提供 instruction 或 plan")
        return self

    @property
    def injected(self) -> bool:
        """是否为注入用例（跳过解析器）。"""
        return bool(self.plan)

    @property
    def input_text(self) -> str:
        """用于报告展示的输入摘要。"""
        if self.plan:
            return " | ".join(
                f"{step.get('skill', '?')}({_compact_args(step.get('args'))})" for step in self.plan
            )
        return (self.instruction or "").strip() or "<空输入>"


class SafetySuite(BaseModel):
    """一份评测集。"""

    model_config = ConfigDict(extra="forbid")

    name: str = "safety-suite"
    version: int = 1
    description: str = ""
    cases: list[SafetyCase] = Field(min_length=1)
    source_path: str = ""

    @model_validator(mode="after")
    def _unique_ids(self) -> SafetySuite:
        seen: set[str] = set()
        duplicates: list[str] = []
        for case in self.cases:
            if case.id in seen:
                duplicates.append(case.id)
            seen.add(case.id)
        if duplicates:
            raise ValueError(f"用例 id 重复：{sorted(set(duplicates))}")
        return self

    def categories(self) -> list[str]:
        return sorted({c.category for c in self.cases})


def load_suite(path: str | Path | None = None) -> SafetySuite:
    """从 YAML 加载评测集。不给路径时加载包内默认数据集。"""
    if yaml is None:  # pragma: no cover
        raise EvaluationError("缺少 pyyaml，无法读取评测集：pip install pyyaml")

    target = Path(path) if path is not None else DEFAULT_SUITE
    if not target.is_file():
        raise EvaluationError(
            f"评测集不存在: {target}",
            details={"path": str(target), "default": str(DEFAULT_SUITE)},
        )

    try:
        raw = yaml.safe_load(target.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise EvaluationError(f"评测集 YAML 解析失败: {exc}", cause=exc) from exc

    if not isinstance(raw, dict):
        raise EvaluationError(f"评测集顶层必须是映射，实际是 {type(raw).__name__}")

    try:
        suite = SafetySuite.model_validate(raw)
    except ValueError as exc:  # pydantic.ValidationError 是其子类
        raise EvaluationError(f"评测集字段非法: {exc}", details={"path": str(target)}) from exc

    return suite.model_copy(update={"source_path": str(target)})


def _compact_args(args: Any) -> str:
    if not isinstance(args, dict):
        return ""
    return ", ".join(f"{k}={v}" for k, v in sorted(args.items()))
