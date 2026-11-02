"""LangGraph 状态机。

    plan ──► screen ──► execute ──► verify ──► report
              │            ▲
              │            └── 逐步执行，失败即停
              ├──► report（安全拒绝）
              └──► END（需要人工审批，状态置 awaiting_approval）

为什么保留 LangGraph 而不是写一个 for 循环？

* 五个阶段的状态迁移是显式的、可断点续跑的，审批挂起与恢复不需要另写一套控制流；
* 每个节点只依赖 ``GraphState``，新增阶段（例如「重规划」）是加节点+加边，
  而不是在主流程里插 ``if``；
* 状态可被 checkpoint 序列化，为将来接 SqliteSaver / 分布式执行留好接口。
"""

from __future__ import annotations

import operator
from dataclasses import dataclass
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph

from robot_agent import metrics
from robot_agent.audit import AuditLog
from robot_agent.config import Settings
from robot_agent.errors import RobotAgentError
from robot_agent.logging import get_logger
from robot_agent.orchestration.executor import StepExecutor
from robot_agent.planner.base import Plan, PlanStep, StepOutcome
from robot_agent.planner.pipeline import Planner
from robot_agent.safety.gate import SafetyGate
from robot_agent.skills.base import SkillRegistry

logger = get_logger(__name__)


class GraphState(TypedDict, total=False):
    """图状态。``outcomes`` 用 ``operator.add`` 累加，便于多节点增量写入。"""

    task_id: str
    instruction: str

    plan: list[dict[str, Any]]
    plan_source: str
    plan_warnings: list[str]
    index: int

    outcomes: Annotated[list[dict[str, Any]], operator.add]
    verifications: list[dict[str, Any]]
    corrections: list[dict[str, Any]]

    status: str
    block_reason: str
    requires_approval: bool
    approval_reasons: list[str]
    approved: bool
    report: str
    error: dict[str, Any] | None


@dataclass
class AgentComponents:
    """图所需依赖的装配容器（显式依赖注入，便于测试替换任意一层）。"""

    settings: Settings
    registry: SkillRegistry
    gate: SafetyGate
    planner: Planner
    executor: StepExecutor
    audit: AuditLog


def initial_state(task_id: str, instruction: str) -> GraphState:
    return {
        "task_id": task_id,
        "instruction": instruction,
        "plan": [],
        "plan_source": "",
        "plan_warnings": [],
        "index": 0,
        "outcomes": [],
        "verifications": [],
        "corrections": [],
        "status": "queued",
        "block_reason": "",
        "requires_approval": False,
        "approval_reasons": [],
        "approved": False,
        "report": "",
        "error": None,
    }


def _make_nodes(c: AgentComponents) -> dict[str, Any]:
    audit = c.audit

    # ------------------------------------------------------------------ plan
    def plan_node(state: GraphState) -> dict[str, Any]:
        task_id = state["task_id"]
        try:
            plan: Plan = c.planner.plan(state["instruction"])
        except RobotAgentError as exc:
            audit.record("plan_failed", exc.to_dict(), task_id=task_id)
            return {"status": "failed", "error": exc.to_dict(), "outcomes": []}
        except Exception as exc:
            logger.exception("plan_unexpected_error", task_id=task_id)
            err = {"code": "internal_error", "message": str(exc)}
            audit.record("plan_failed", err, task_id=task_id)
            return {"status": "failed", "error": err, "outcomes": []}

        audit.record(
            "planned",
            {
                "source": plan.source,
                "model": plan.model,
                "latency_ms": round(plan.latency_ms, 2),
                "steps": [s.model_dump() for s in plan.steps],
                "warnings": plan.warnings,
            },
            task_id=task_id,
        )
        logger.info("plan_ready", task_id=task_id, steps=len(plan.steps), source=plan.source)
        return {
            "plan": [s.model_dump() for s in plan.steps],
            "plan_source": plan.source,
            "plan_warnings": plan.warnings,
            "index": 0,
            "status": "running",
            "outcomes": [],
        }

    # ---------------------------------------------------------------- screen
    def screen_node(state: GraphState) -> dict[str, Any]:
        task_id = state["task_id"]
        steps = [PlanStep.model_validate(s) for s in state.get("plan", [])]
        plan = Plan(steps=steps, source=state.get("plan_source", "rules"))  # type: ignore[arg-type]
        try:
            screening = c.gate.screen(plan, instruction=state["instruction"], task_id=task_id)
        except Exception as exc:
            logger.exception("screening_error", task_id=task_id)
            return {
                "status": "blocked",
                "block_reason": f"安全校验内部错误，按拒绝处理: {exc}",
                "outcomes": [],
            }

        audit.record("screened", screening.to_dict(), task_id=task_id)
        base: dict[str, Any] = {
            "plan": [s.model_dump() for s in screening.steps] if screening.ok else state["plan"],
            "corrections": screening.corrections,
            "requires_approval": screening.requires_approval,
            "approval_reasons": screening.approval_reasons,
        }
        if not screening.ok:
            return {
                **base,
                "status": "blocked",
                "block_reason": screening.first_reason(),
                "outcomes": [],
            }
        # 只要闸门判定「需要审批」就一律挂起。绝不因为配置项为空而静默放行高风险动作。
        # 已获批准的任务（approved=True）直接放行，避免续跑图再次挂起形成死循环。
        if screening.requires_approval and not state.get("approved"):
            logger.info(
                "awaiting_approval",
                task_id=task_id,
                reasons=screening.approval_reasons,
            )
            return {**base, "status": "awaiting_approval"}
        return {**base, "status": "ready"}

    # --------------------------------------------------------------- execute
    def execute_node(state: GraphState) -> dict[str, Any]:
        task_id = state["task_id"]
        steps = [PlanStep.model_validate(s) for s in state.get("plan", [])]
        outcomes = c.executor.execute_plan(steps, task_id=task_id)
        failed = [o for o in outcomes if not o.ok]
        status = "running" if not failed else "failed"
        return {
            "outcomes": [o.to_dict() for o in outcomes],
            "index": len(outcomes),
            "status": status,
        }

    # ---------------------------------------------------------------- verify
    def verify_node(state: GraphState) -> dict[str, Any]:
        """对声明了 ``expect`` 的步骤做结果核验（用实测位姿，不信执行器的自述）。"""
        task_id = state["task_id"]
        checks: list[dict[str, Any]] = []
        if not c.registry.has("get_pose"):
            return {"verifications": checks}

        for step_dict in state.get("plan", []):
            step = PlanStep.model_validate(step_dict)
            if not step.expect:
                continue
            target = step.expect.get("color") or "robot"
            tolerance = float(step.expect.get("tolerance", 0.25))
            try:
                pose = c.registry.get("get_pose").execute(target=target)
            except Exception as exc:
                checks.append({"target": target, "ok": False, "detail": f"位姿读取失败: {exc}"})
                continue
            if not pose.ok:
                checks.append({"target": target, "ok": False, "detail": pose.detail})
                continue
            dx = float(pose.data["x"]) - float(step.expect.get("x", 0.0))
            dy = float(pose.data["y"]) - float(step.expect.get("y", 0.0))
            distance = (dx * dx + dy * dy) ** 0.5
            ok = distance <= tolerance
            checks.append(
                {
                    "target": target,
                    "ok": ok,
                    "distance_m": round(distance, 3),
                    "tolerance_m": tolerance,
                    "detail": f"{target} 距目标 {distance:.2f}m（容差 {tolerance:.2f}m）",
                }
            )

        audit.record("verified", {"checks": checks}, task_id=task_id)
        return {"verifications": checks}

    # ---------------------------------------------------------------- report
    def report_node(state: GraphState) -> dict[str, Any]:
        task_id = state["task_id"]
        report = _render_report(state)
        audit.record("reported", {"report": report, "status": state.get("status")}, task_id=task_id)
        return {"report": report}

    return {
        "plan": plan_node,
        "screen": screen_node,
        "execute": execute_node,
        "verify": verify_node,
        "report": report_node,
    }


def build_graph(c: AgentComponents) -> Any:
    """完整图：用于新任务。"""
    nodes = _make_nodes(c)
    g: StateGraph = StateGraph(GraphState)
    for name, fn in nodes.items():
        g.add_node(name, fn)

    g.add_edge(START, "plan")
    g.add_conditional_edges(
        "plan",
        _route_after_plan,
        {"screen": "screen", "report": "report"},
    )
    g.add_conditional_edges(
        "screen",
        _route_after_screen,
        {"execute": "execute", "report": "report", "end": END},
    )
    g.add_edge("execute", "verify")
    g.add_edge("verify", "report")
    g.add_edge("report", END)
    return g.compile()


def build_resume_graph(c: AgentComponents) -> Any:
    """审批通过后的续跑图：跳过 plan，**重跑 screen**再执行。

    重跑 screen 是为了覆盖「审批期间计划被人为修改」的情况——人工修改同样受策略约束。
    """
    nodes = _make_nodes(c)
    g: StateGraph = StateGraph(GraphState)
    for name, fn in nodes.items():
        if name == "plan":
            continue
        g.add_node(name, fn)

    g.add_edge(START, "screen")
    g.add_conditional_edges(
        "screen",
        _route_after_screen,
        {"execute": "execute", "report": "report", "end": END},
    )
    g.add_edge("execute", "verify")
    g.add_edge("verify", "report")
    g.add_edge("report", END)
    return g.compile()


# --------------------------------------------------------------------- 路由


def _route_after_plan(state: GraphState) -> str:
    return "report" if state.get("status") == "failed" else "screen"


def _route_after_screen(state: GraphState) -> str:
    status = state.get("status")
    if status == "blocked":
        return "report"
    if status == "awaiting_approval":
        return "end"
    return "execute"


# --------------------------------------------------------------------- 报告


def _render_report(state: GraphState) -> str:
    status = state.get("status", "")
    lines: list[str] = [
        f"任务: {state.get('task_id', '-')}",
        f"指令: {state.get('instruction', '')}",
        f"规划来源: {state.get('plan_source', '-')}｜步数: {len(state.get('plan', []))}",
    ]
    if status == "awaiting_approval":
        reasons = "; ".join(state.get("approval_reasons", []))
        lines.append(f"状态: 等待人工审批（{reasons}）")
        return "\n".join(lines)
    if status == "blocked":
        lines.append(f"状态: 已拦截 — {state.get('block_reason', '')}")
        return "\n".join(lines)
    if state.get("error"):
        lines.append(f"状态: 规划失败 — {(state.get('error') or {}).get('message', '')}")
        return "\n".join(lines)

    for outcome in state.get("outcomes", []):
        mark = "PASS" if outcome["outcome"] == "ok" else outcome["outcome"].upper()
        lines.append(
            f"  [{mark}] {outcome['index']}.{outcome['skill']} "
            f"({outcome['latency_ms']:.0f}ms, {outcome.get('attempts', 1)} 次): {outcome['detail']}"
        )
    for check in state.get("verifications", []):
        mark = "PASS" if check["ok"] else "FAIL"
        lines.append(f"  [{mark}] 核验 {check.get('target')}: {check.get('detail')}")

    corrections = state.get("corrections", [])
    if corrections:
        lines.append(f"安全修正: {len(corrections)} 处（详见审计日志）")

    outcomes = state.get("outcomes", [])
    checks = state.get("verifications", [])
    all_ok = bool(outcomes) and all(o["outcome"] == "ok" for o in outcomes)
    verify_ok = all(ch["ok"] for ch in checks) if checks else True
    if all_ok and verify_ok:
        lines.append("结论: 任务成功")
    else:
        lines.append("结论: 任务未完成（存在失败步骤或核验未通过）")
    return "\n".join(lines)


def record_task_metrics(status: str, duration_s: float) -> None:
    """统一的任务级指标埋点，供 runner 在收尾时调用。"""
    metrics.TASKS_TOTAL.labels(status=status).inc()
    metrics.TASK_DURATION.observe(max(duration_s, 0.0))


__all__ = [
    "AgentComponents",
    "GraphState",
    "PlanStep",
    "StepOutcome",
    "build_graph",
    "build_resume_graph",
    "initial_state",
    "record_task_metrics",
]
