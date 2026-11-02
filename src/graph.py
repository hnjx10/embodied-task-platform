"""LangGraph 编排：plan → validate → execute → verify → report。

- 校验不通过的步骤直接拦截，写入审计日志并终止（宁可不动，不可误动）
- build_graph(need_approval=True) 时在 execute 前挂起，等待人工确认后继续
"""
import operator
from typing import Annotated, Any, Dict, List, TypedDict

from langgraph.graph import END, START, StateGraph

from audit import AuditLog
from planner import plan as do_plan
from safety import check_step


class GraphState(TypedDict):
    instruction: str
    steps: List[Dict[str, Any]]          # 规划出的技能序列
    idx: int                             # 当前执行到第几步
    current: Dict[str, Any]              # 当前待执行步骤
    results: Annotated[List[Dict[str, Any]], operator.add]  # 各步执行结果
    blocked: bool
    report: str


def build_graph(registry, audit: AuditLog, need_approval: bool = False):
    """registry: SkillRegistry（ros 或 mock 执行器均可）。"""

    # ---------- 节点 ----------
    def plan_node(state: GraphState) -> Dict[str, Any]:
        steps = do_plan(state["instruction"], registry.prompt_description())
        audit.record("plan", {"instruction": state["instruction"], "steps": steps})
        return {"steps": steps, "idx": 0, "results": [], "blocked": False}

    def validate_node(state: GraphState) -> Dict[str, Any]:
        step = state["steps"][state["idx"]]
        skill_name, args = step["skill"], step.get("args", {})
        try:
            registry.get(skill_name).validate_params(args)  # schema 完整性
        except (KeyError, ValueError) as e:
            audit.record("blocked", {"step": step, "reason": str(e)})
            return {"current": step, "blocked": True,
                    "results": [{"step": skill_name, "ok": False, "detail": f"schema 校验失败: {e}"}]}
        verdict = check_step(skill_name, args, state["instruction"])
        if not verdict.ok:
            audit.record("blocked", {"step": step, "reason": verdict.reason})
            return {"current": step, "blocked": True,
                    "results": [{"step": skill_name, "ok": False, "detail": f"安全拦截: {verdict.reason}"}]}
        # 限速场景自动降速而非拦截（固定 args 优先，风险可控）
        if verdict.fixed_args:
            step = {**step, "args": verdict.fixed_args}
        audit.record("validated", {"step": step})
        return {"current": step, "blocked": False}

    def execute_node(state: GraphState) -> Dict[str, Any]:
        step = state["current"]
        skill = registry.get(step["skill"])
        try:
            result = skill.execute(**step.get("args", {}))
            entry = {"step": step["skill"], "ok": result.ok, "detail": result.detail}
        except Exception as e:  # noqa: BLE001 —— 执行异常不能拖垮整条链路
            entry = {"step": step["skill"], "ok": False, "detail": f"执行异常: {e}"}
        audit.record("executed", entry)
        return {"results": [entry], "idx": state["idx"] + 1}

    def verify_node(state: GraphState) -> Dict[str, Any]:
        """推箱任务的结果核验：用 get_pose 实测方块位置是否达标。"""
        details = []
        for i, step in enumerate(state["steps"]):
            if step["skill"] != "push_cube":
                continue
            args = step.get("args", {})
            try:
                r = registry.get("get_pose").execute(target=args["color"])
                if r.ok:
                    dist = ((r.data["x"] - args["x"]) ** 2 + (r.data["y"] - args["y"]) ** 2) ** 0.5
                    ok = dist < 0.25
                    details.append(f"{args['color']} 方块距目标 {dist:.2f}m → {'达标' if ok else '未达标'}")
                    if not ok:
                        state["results"].append({"step": "verify", "ok": False,
                                                 "detail": f"{args['color']} 未达标"})
                else:
                    details.append(f"{args['color']} 位姿读取失败: {r.detail}")
            except Exception as e:  # noqa: BLE001
                details.append(f"核验异常: {e}")
        audit.record("verified", {"details": details})
        return {}

    def report_node(state: GraphState) -> Dict[str, Any]:
        lines = [f"指令: {state['instruction']}",
                 f"规划步骤: {len(state['steps'])}"]
        for r in state["results"]:
            mark = "PASS" if r["ok"] else "FAIL"
            lines.append(f"  [{mark}] {r['step']}: {r['detail']}")
        all_ok = all(r["ok"] for r in state["results"]) and state["results"]
        lines.append("结论: " + ("任务完成" if all_ok else "任务未完成（存在拦截或失败步骤）"))
        report = "\n".join(lines)
        audit.record("report", {"report": report})
        return {"report": report}

    # ---------- 连线 ----------
    def route_after_validate(state: GraphState) -> str:
        return "report" if state["blocked"] else "execute"

    def route_after_execute(state: GraphState) -> str:
        return "validate" if state["idx"] < len(state["steps"]) else "verify"

    g = StateGraph(GraphState)
    g.add_node("plan", plan_node)
    g.add_node("validate", validate_node)
    g.add_node("execute", execute_node)
    g.add_node("verify", verify_node)
    g.add_node("report", report_node)

    g.add_edge(START, "plan")
    g.add_edge("plan", "validate")
    g.add_conditional_edges("validate", route_after_validate,
                            {"execute": "execute", "report": "report"})
    g.add_conditional_edges("execute", route_after_execute,
                            {"validate": "validate", "verify": "verify"})
    g.add_edge("verify", "report")
    g.add_edge("report", END)

    if need_approval:  # 高风险动作执行前挂起，人工确认后 invoke(None, config) 继续
        from langgraph.checkpoint.memory import MemorySaver
        return g.compile(checkpointer=MemorySaver(), interrupt_before=["execute"])
    return g.compile()
