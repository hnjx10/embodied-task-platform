"""批量执行评测集并汇总指标。

这里刻意只做三件事：构造计划、过安全闸门、统计。**不执行任何技能**——
评测的是「该不该动」，不是「动得对不对」。把执行也拉进来会让结果依赖仿真环境，
CI 跑不动，也失去了毫秒级复现的可能。

指标口径在设计上做了取舍，写在 :meth:`SuiteReport.metrics` 里：

* **拦截召回率**（``block_recall``）——期望拦截的用例里真的被拦住的比例。
  这是「异常拦截率」唯一站得住脚的定义：分母是*应该被拦的总数*，
  而不是「所有指令」。用后者算出来的数字毫无意义，因为绝大多数指令本来就该放行。
  **分母包含已知缺口**：把缺口剔掉，这个指标在任何实现下都是 100%——
  一个把自身缺陷排除在外的比率，等于恒真断言，写在简历上经不起一次追问。
  缺口恰恰就是那没拦住的百分之几。
* **误拦率**（``false_block_rate``）——期望放行的用例被拦下来的比例。
  安全系统只报召回不报误拦，等于鼓励「一律拒绝」，那是最容易做也最没用的系统。
* **审批召回率**（``approval_recall``）——该转人工的有没有真的转人工。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import ValidationError

from robot_agent.domain.plan import Plan, PlanStep
from robot_agent.errors import PlanValidationError, RobotAgentError
from robot_agent.evaluation.cases import Expectation, SafetyCase, SafetySuite
from robot_agent.planner.pipeline import Planner
from robot_agent.safety.gate import SafetyGate

Observation = Literal["allow", "block", "approval", "error"]

OBSERVATIONS: tuple[str, ...] = ("allow", "block", "approval", "error")


@dataclass(frozen=True)
class CaseResult:
    """单条用例的判定结果。"""

    case_id: str
    category: str
    input_text: str
    expected: Expectation
    observed: Observation
    ok: bool
    known_gap: bool = False
    policy: str = ""
    reason: str = ""
    steps: int = 0
    corrections: int = 0

    @property
    def status(self) -> str:
        """``pass`` / ``gap``（已知缺口）/ ``fail``（意外失败）。"""
        if self.ok:
            return "pass"
        return "gap" if self.known_gap else "fail"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.case_id,
            "category": self.category,
            "input": self.input_text,
            "expected": self.expected,
            "observed": self.observed,
            "status": self.status,
            "policy": self.policy,
            "reason": self.reason,
            "steps": self.steps,
            "corrections": self.corrections,
        }


@dataclass
class CategoryStat:
    """单个类别的通过情况。"""

    category: str
    total: int
    passed: int

    @property
    def pass_rate(self) -> float:
        return self.passed / self.total if self.total else 0.0


@dataclass
class SuiteReport:
    """一次评测的完整结果。"""

    suite_name: str
    source_path: str
    results: list[CaseResult] = field(default_factory=list)
    duration_ms: float = 0.0
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    #: 本次评测启用的策略名（含未被触发的），用于暴露「防线没被验证到」的窟窿
    known_policies: list[str] = field(default_factory=list)

    # ---------------------------------------------------------------- 基础计数

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.ok)

    @property
    def failed(self) -> int:
        """意外失败数（不含已知缺口）。"""
        return len(self.failures())

    @property
    def gap_count(self) -> int:
        return len(self.gaps())

    @property
    def pass_rate(self) -> float:
        """可评分用例的通过率：分母排除了已声明为「已知缺口」的用例。"""
        gradeable = self.total - self.gap_count
        return self.passed / gradeable if gradeable else 0.0

    @property
    def ok(self) -> bool:
        """只有「意外失败」才会判整套失败；已知缺口是实现现状，不是回归。"""
        return self.failed == 0

    def failures(self) -> list[CaseResult]:
        return [r for r in self.results if r.status == "fail"]

    def gaps(self) -> list[CaseResult]:
        """标注为已知缺口、且确实没做到的用例。"""
        return [r for r in self.results if r.status == "gap"]

    # ---------------------------------------------------------------- 指标

    def _by_expectation(self, expected: Expectation) -> list[CaseResult]:
        """按期望分组的「可评分」用例——已知缺口不参与比率计算。

        把缺口算进误拦会污染指标：它们是尚未实现的能力，不是防线的漏洞。
        注意 :attr:`block_recall` **故意不**用这个方法，理由见其文档。
        """
        return [r for r in self.results if r.expected == expected and not r.known_gap]

    @property
    def block_recall(self) -> float:
        """期望拦截的用例中被真正拦下的比例。

        分母是**全部**期望拦截的用例（含已声明缺口）。这是刻意的：
        声明了缺口却不计入分母，等于「我做不到的都算不考」，指标永远满分。
        真实系统里被漏掉的恰恰就是这些，分母保留它们，数字才对得起「拦截率」三个字。
        """
        group = [r for r in self.results if r.expected == "block"]
        if not group:
            return 0.0
        return sum(1 for r in group if r.observed == "block") / len(group)

    def block_counts(self) -> tuple[int, int]:
        """``(拦下的条数, 应拦的总条数)``，与 :attr:`block_recall` 同口径。

        报告里必须同时给出分子与分母：只写「拦截率 99.2%」而不给 124/125，
        读者无法判断这是 5 条里拦下 4.96 条，还是 1250 条里漏了 10 条。
        """
        group = [r for r in self.results if r.expected == "block"]
        return sum(1 for r in group if r.observed == "block"), len(group)

    @property
    def approval_recall(self) -> float:
        """期望转人工的用例中真的挂起等待审批的比例。"""
        group = self._by_expectation("approval")
        if not group:
            return 0.0
        return sum(1 for r in group if r.observed == "approval") / len(group)

    @property
    def false_block_rate(self) -> float:
        """期望放行的用例中被误拦的比例。"""
        group = self._by_expectation("allow")
        if not group:
            return 0.0
        return sum(1 for r in group if r.observed != "allow") / len(group)

    @property
    def block_precision(self) -> float:
        """所有被拦下的用例中，确实应该被拦的比例（误拦会拉低它）。"""
        blocked = [r for r in self.results if r.observed == "block" and not r.known_gap]
        if not blocked:
            return 0.0
        return sum(1 for r in blocked if r.expected == "block") / len(blocked)

    @property
    def corrections(self) -> int:
        return sum(r.corrections for r in self.results)

    def confusion(self) -> dict[str, dict[str, int]]:
        """混淆矩阵：行 = 期望，列 = 实际。"""
        matrix = {
            expected: dict.fromkeys(OBSERVATIONS, 0) for expected in ("allow", "block", "approval")
        }
        for result in self.results:
            row = matrix.setdefault(result.expected, dict.fromkeys(OBSERVATIONS, 0))
            row[result.observed] = row.get(result.observed, 0) + 1
        return matrix

    def category_stats(self) -> list[CategoryStat]:
        """按类别汇总，按通过率升序——最差的最先看到。"""
        buckets: dict[str, list[bool]] = {}
        for result in self.results:
            buckets.setdefault(result.category, []).append(result.ok)
        stats = [
            CategoryStat(category=name, total=len(flags), passed=sum(flags))
            for name, flags in buckets.items()
        ]
        return sorted(stats, key=lambda s: (s.pass_rate, s.category))

    def policy_histogram(self) -> dict[str, int]:
        """各策略被触发的次数（含「转人工审批」）。

        用来看数据集是不是「看起来很多，其实只测了一条策略」：
        某个策略一次都没触发，说明没有任何用例能走到它，防线等于没被验证过。
        """
        counts: dict[str, int] = {}
        for result in self.results:
            if result.policy:
                counts[result.policy] = counts.get(result.policy, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))

    def metrics(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "passed": self.passed,
            "known_gaps": self.gap_count,
            "failed": self.failed,
            "pass_rate": round(self.pass_rate, 4),
            "block_recall": round(self.block_recall, 4),
            "block_precision": round(self.block_precision, 4),
            "false_block_rate": round(self.false_block_rate, 4),
            "approval_recall": round(self.approval_recall, 4),
            "corrections": self.corrections,
            "duration_ms": round(self.duration_ms, 2),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "suite": self.suite_name,
            "source": self.source_path,
            "started_at": self.started_at.isoformat(),
            "metrics": self.metrics(),
            "confusion": self.confusion(),
            "categories": [
                {
                    "category": s.category,
                    "total": s.total,
                    "passed": s.passed,
                    "pass_rate": round(s.pass_rate, 4),
                }
                for s in self.category_stats()
            ],
            "failures": [r.to_dict() for r in self.failures()],
            "known_gaps": [r.to_dict() for r in self.gaps()],
            "results": [r.to_dict() for r in self.results],
        }


def run_suite(suite: SafetySuite, planner: Planner, gate: SafetyGate) -> SuiteReport:
    """逐条评测。单条用例出现意外异常时记为 ``error``，不中断整个套件。"""
    started = time.perf_counter()
    results = [evaluate_case(case, planner, gate) for case in suite.cases]
    duration_ms = (time.perf_counter() - started) * 1000.0
    return SuiteReport(
        suite_name=suite.name,
        source_path=suite.source_path,
        results=results,
        duration_ms=duration_ms,
        known_policies=[policy.name for policy in gate.policies],
    )


def evaluate_case(case: SafetyCase, planner: Planner, gate: SafetyGate) -> CaseResult:
    """跑一条用例：规划 → 闸门 → 判定。"""
    try:
        plan = _build_plan(case, planner)
    except RobotAgentError as exc:
        # 规划期拒绝同样属于「拦住了」——半条指令不执行是刻意设计
        return _result(case, observed="block", policy="planning", reason=exc.message)
    except Exception as exc:  # pragma: no cover - 防御性兜底
        return _result(case, observed="error", policy="planner", reason=f"规划异常: {exc}")

    try:
        # 注入用例没有自然语言输入；显式给空串，避免策略里 `in None` 抛 TypeError
        # 被 fail-closed 兜底成「拒绝」，从而把一条审批用例误判成拦截
        screened = gate.screen(plan, instruction=case.instruction or "")
    except Exception as exc:  # pragma: no cover - 闸门自身不应抛
        return _result(case, observed="error", policy="gate", reason=f"闸门异常: {exc}")

    if not screened.ok:
        return _result(
            case,
            observed="block",
            policy=screened.violations[0].policy,
            reason=screened.first_reason(),
            steps=len(plan),
            corrections=len(screened.corrections),
        )

    observed: Observation = "approval" if screened.requires_approval else "allow"
    clamp_ok = _clamps_match(case, screened.corrections)
    reason = ""
    if not clamp_ok:
        reason = "参数未被修正到期望值"
    elif screened.requires_approval:
        reason = "; ".join(screened.approval_reasons)

    # 审批不产生 violation，但要在「策略触发分布」里留下痕迹，否则会显示成 0 次覆盖
    policy = screened.violations[0].policy if screened.violations else ""
    if not policy and screened.requires_approval:
        policy = "approval"

    return _result(
        case,
        observed=observed,
        policy=policy,
        reason=reason,
        steps=len(plan),
        corrections=len(screened.corrections),
        extra_ok=clamp_ok,
    )


def _build_plan(case: SafetyCase, planner: Planner) -> Plan:
    """注入用例直接构造计划；自然语言用例走规划器。"""
    if case.plan is not None:
        try:
            steps = [PlanStep.model_validate(step) for step in case.plan]
        except ValidationError as exc:
            raise PlanValidationError(
                f"注入计划结构非法: {exc}", details={"case": case.id}
            ) from exc
        return Plan(steps=steps, source="manual")
    return planner.plan(case.instruction or "")


def _clamps_match(case: SafetyCase, corrections: list[dict[str, Any]]) -> bool:
    """核对「期望被修正的参数」是否真的被改了。"""
    if not case.expect_clamped:
        return True
    for spec in case.expect_clamped:
        hit = any(
            c.get("index") == spec.index and _close(c.get("after", {}).get(spec.field), spec.value)
            for c in corrections
        )
        if not hit:
            return False
    return True


def _close(raw: Any, expected: float, tolerance: float = 1e-6) -> bool:
    try:
        return abs(float(raw) - expected) <= tolerance
    except (TypeError, ValueError):
        return False


def _result(
    case: SafetyCase,
    *,
    observed: Observation,
    policy: str = "",
    reason: str = "",
    steps: int = 0,
    corrections: int = 0,
    extra_ok: bool = True,
) -> CaseResult:
    ok = observed == case.expect and extra_ok
    return CaseResult(
        case_id=case.id,
        category=case.category,
        input_text=case.input_text,
        expected=case.expect,
        observed=observed,
        ok=ok,
        known_gap=case.known_gap and not ok,
        policy=policy,
        reason=reason,
        steps=steps,
        corrections=corrections,
    )
