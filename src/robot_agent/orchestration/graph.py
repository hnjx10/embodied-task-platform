"""LangGraph 状态机。

    plan ──► screen ──► execute ──► verify ──► report
              │            ▲
              │            └── 逐步执行，失败即停
              ├──► report（安全拒绝）
              └──► END（需要人工审批）

**审批挂起有两种实现，由 ``approval_mode`` 选择**

``persist`` 模式（默认）：闸门判定需要审批后，状态置 ``awaiting_approval`` 并
结束本次图执行。审批通过时**重建状态重新入图**，从 screen 再跑一遍。

``interrupt`` 模式：在 screen 节点内调用 LangGraph 的 ``interrupt()``，图原地
挂起，调用方拿到中断载荷；恢复时用 ``Command(resume=...)`` 从断点继续。

为什么默认是前者：``interrupt()`` 依赖 checkpointer，而本项目的 MemorySaver
活在进程内存里。审批是人的动作，可能等几小时——进程一旦重启，checkpoint 就
没了，任务卡在「已挂起但无法恢复」。持久化重建把状态放在 SQLite 里，重启后
照样能批准续跑。interrupt 模式更适合单进程长驻、审批秒回的场景（例如产线上
工位终端），它的优点是挂起/恢复完全交给框架，不必自己维护状态机。

两种模式的**安全语义一致**：批准之后都要重跑一次策略链，人工批准不等于绕过
闸门。

为什么保留 LangGraph 而不是写一个 for 循环？

* 五个阶段的状态迁移是显式的、可断点续跑的，审批挂起与恢复不需要另写一套控制流；
* 每个节点只依赖 ``GraphState``，新增阶段（例如「重规划」）是加节点+加边，
  而不是在主流程里插 ``if``；
* 状态可被 checkpoint 序列化，为将来接 SqliteSaver / 分布式执行留好接口。
"""

from __future__ import annotations

import operator
from dataclasses import dataclass
from typing import Annotated, Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph

from robot_agent import metrics
from robot_agent.audit import AuditLog
from robot_agent.config import Settings
from robot_agent.domain.plan import Plan, PlanStep, StepOutcome
from robot_agent.errors import RobotAgentError
from robot_agent.logging import get_logger
from robot_agent.orchestration.executor import StepExecutor
from robot_agent.planner.pipeline import Planner
from robot_agent.planner.router import classify
from robot_agent.reporting import render_report
from robot_agent.safety.gate import SafetyGate
from robot_agent.skills.base import SkillRegistry

try:  # pragma: no cover - 取决于 langgraph 版本
    from langgraph.types import Command, interrupt

    NATIVE_INTERRUPT = True
except ImportError:  # pragma: no cover
    Command = None  # type: ignore[assignment,misc]
    interrupt = None  # type: ignore[assignment]
    NATIVE_INTERRUPT = False

logger = get_logger(__name__)

#: 审批挂起方式。见模块 docstring 里的取舍说明。
ApprovalMode = Literal["persist", "interrupt"]


class GraphState(TypedDict, total=False):
    """图状态。``outcomes`` 用 ``operator.add`` 累加，便于多节点增量写入。"""

    task_id: str
    instruction: str

    #: 意图识别的结果（move / manipulate / query / system / unknown）
    intent: str
    intent_keyword: str

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


def _make_nodes(c: AgentComponents, *, approval_mode: ApprovalMode = "persist") -> dict[str, Any]:
    audit = c.audit
    native = approval_mode == "interrupt" and NATIVE_INTERRUPT

    # ---------------------------------------------------------------- router
    def router_node(state: GraphState) -> dict[str, Any]:
        """意图识别。只记录、不拦截——规则漏词时仍交给模型判断。

        拦在这里会让「挪一下」「怼过去」这类词表外的合法指令直接失败，
        而它们本来是可以被模型正确解析的。
        """
        task_id = state["task_id"]
        match = classify(state["instruction"])
        audit.record("intent_routed", match.to_dict(), task_id=task_id)
        logger.info(
            "intent_routed",
            task_id=task_id,
            intent=match.intent,
            keyword=match.keyword,
        )
        return {"intent": match.intent, "intent_keyword": match.keyword}

    # ------------------------------------------------------------------ plan
    def plan_node(state: GraphState) -> dict[str, Any]:
        task_id = state["task_id"]
        try:
            plan: Plan = c.planner.plan(state["instruction"])
        except RobotAgentError as exc:
            # 把意图一起带进错误里：同样是「规划失败」，intent=unknown 说明这句
            # 话压根不像指令，intent=move 则说明指令像样但句式没覆盖到——
            # 两种情况的修复方向完全不同，事后看日志必须能区分。
            payload = exc.to_dict()
            details = dict(payload.get("details") or {})
            details.setdefault("intent", state.get("intent", "unknown"))
            payload["details"] = details
            audit.record("plan_failed", payload, task_id=task_id)
            return {"status": "failed", "error": payload, "outcomes": []}
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
                mode="interrupt" if native else "persist",
            )
            if not native:
                return {**base, "status": "awaiting_approval"}

            # 原生挂起：图在这里停下，控制权交回调用方。
            # 恢复时**整个 screen 节点会从头重跑**（LangGraph 的重放语义），
            # 所以这里不能有副作用——审计已经写过了，不能再写一遍。
            decision = interrupt(
                {
                    "type": "approval_required",
                    "task_id": task_id,
                    "instruction": state["instruction"],
                    "reasons": screening.approval_reasons,
                    "plan": [s.model_dump() for s in screening.steps],
                }
            )
            if not _decision_approved(decision):
                audit.record(
                    "approval_rejected",
                    {"mode": "interrupt", "decision": decision},
                    task_id=task_id,
                )
                return {
                    **base,
                    "status": "blocked",
                    "block_reason": "人工驳回（LangGraph interrupt 恢复）",
                    "outcomes": [],
                }

            # 批准后重新校验：人工批准不等于绕过策略链。与 persist 模式的
            # 续跑图保持同一语义，否则两种模式的安全强度会悄悄不同。
            recheck = c.gate.screen(plan, instruction=state["instruction"], task_id=task_id)
            audit.record(
                "approval_granted",
                {"mode": "interrupt", "recheck": recheck.to_dict()},
                task_id=task_id,
            )
            if not recheck.ok:
                return {
                    **base,
                    "status": "blocked",
                    "block_reason": f"批准后复检未通过: {recheck.first_reason()}",
                    "outcomes": [],
                }
            return {
                "plan": [s.model_dump() for s in recheck.steps],
                "corrections": recheck.corrections,
                "requires_approval": False,
                "approval_reasons": recheck.approval_reasons,
                "approved": True,
                "status": "ready",
            }
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
        report = render_report(
            task_id=task_id,
            instruction=state.get("instruction", ""),
            status=state.get("status", ""),
            plan_source=state.get("plan_source", "-"),
            step_count=len(state.get("plan", [])),
            outcomes=list(state.get("outcomes", [])),
            verifications=list(state.get("verifications", [])),
            corrections=list(state.get("corrections", [])),
            block_reason=state.get("block_reason", ""),
            approval_reasons=list(state.get("approval_reasons", [])),
            error=state.get("error"),
        )
        audit.record("reported", {"report": report, "status": state.get("status")}, task_id=task_id)
        return {"report": report}

    return {
        "router": router_node,
        "plan": plan_node,
        "screen": screen_node,
        "execute": execute_node,
        "verify": verify_node,
        "report": report_node,
    }


def build_graph(
    c: AgentComponents,
    *,
    approval_mode: ApprovalMode = "persist",
    checkpointer: Any = None,
) -> Any:
    """完整图：用于新任务。"""
    nodes = _make_nodes(c, approval_mode=approval_mode)
    g: StateGraph = StateGraph(GraphState)
    for name, fn in nodes.items():
        g.add_node(name, fn)

    g.add_edge(START, "router")
    g.add_edge("router", "plan")
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
    return g.compile(checkpointer=_ensure_checkpointer(approval_mode, checkpointer))


def build_resume_graph(
    c: AgentComponents,
    *,
    approval_mode: ApprovalMode = "persist",
    checkpointer: Any = None,
) -> Any:
    """审批通过后的续跑图：跳过 plan，**重跑 screen**再执行。

    重跑 screen 是为了覆盖「审批期间计划被人为修改」的情况——人工修改同样受策略约束。
    ``interrupt`` 模式下这张图用不到（恢复走 ``Command(resume=...)``），
    保留它是为了让 persist 路径与测试共用同一套装配。
    """
    nodes = _make_nodes(c, approval_mode=approval_mode)
    g: StateGraph = StateGraph(GraphState)
    for name, fn in nodes.items():
        if name in {"router", "plan"}:
            # 续跑时意图已经路由过、计划也已确定，重跑这两步只会覆盖人工意图
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
    return g.compile(checkpointer=_ensure_checkpointer(approval_mode, checkpointer))


def _ensure_checkpointer(approval_mode: ApprovalMode, checkpointer: Any) -> Any:
    """interrupt 模式必须有 checkpointer：没有它就无处保存中断点，恢复会报错。

    这里兜底成 MemorySaver，而不是让调用方自己去配——忘了配的人只会在
    第一次需要审批时才发现问题，那时任务已经卡住了。
    """
    if checkpointer is not None:
        return checkpointer
    if approval_mode == "interrupt" and NATIVE_INTERRUPT:
        from langgraph.checkpoint.memory import MemorySaver

        return MemorySaver()
    return None


def _decision_approved(decision: Any) -> bool:
    """解析人工决定。默认拒绝——只有显式 approved=True 才算批准。"""
    if isinstance(decision, dict):
        return decision.get("approved") is True
    return decision is True


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
