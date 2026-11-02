"""任务报告的文本渲染。

报告是**结果的可读呈现**，不是状态机的一部分：把它留在图节点里会导致
「改一句文案要动编排逻辑，还得跑一遍 LangGraph 才能验证」。

这里做成纯函数——只吃普通值，不依赖 ``GraphState``，也不依赖任何其它模块，
因此可以单独构造各种分支（成功/拦截/待审批/规划失败）来测。
"""

from __future__ import annotations

from typing import Any

#: 单步结果的标记：ok 显示 PASS，其余原样大写（BLOCKED/ERROR/TIMEOUT…）
_OK = "ok"


def render_report(
    *,
    task_id: str,
    instruction: str,
    status: str,
    outcomes: list[dict[str, Any]],
    verifications: list[dict[str, Any]],
    corrections: list[dict[str, Any]] | None = None,
    plan_source: str = "-",
    step_count: int = 0,
    block_reason: str = "",
    approval_reasons: list[str] | None = None,
    error: dict[str, Any] | None = None,
) -> str:
    """渲染一份人读报告。四个分支各自提前返回，避免深层嵌套。"""
    lines: list[str] = [
        f"任务: {task_id or '-'}",
        f"指令: {instruction}",
        f"规划来源: {plan_source or '-'}｜步数: {step_count}",
    ]

    if status == "awaiting_approval":
        reasons = "; ".join(approval_reasons or [])
        lines.append(f"状态: 等待人工审批（{reasons}）")
        return "\n".join(lines)

    if status == "blocked":
        lines.append(f"状态: 已拦截 — {block_reason}")
        return "\n".join(lines)

    if error:
        lines.append(f"状态: 规划失败 — {error.get('message', '')}")
        return "\n".join(lines)

    for outcome in outcomes:
        mark = "PASS" if outcome["outcome"] == _OK else str(outcome["outcome"]).upper()
        lines.append(
            f"  [{mark}] 第 {int(outcome['index']) + 1} 步 {outcome['skill']} "
            f"({outcome['latency_ms']:.0f}ms, {outcome.get('attempts', 1)} 次): {outcome['detail']}"
        )

    for check in verifications:
        mark = "PASS" if check["ok"] else "FAIL"
        lines.append(f"  [{mark}] 核验 {check.get('target')}: {check.get('detail')}")

    if corrections:
        lines.append(f"安全修正: {len(corrections)} 处（详见审计日志）")

    lines.append(f"结论: {_conclusion(outcomes, verifications)}")
    return "\n".join(lines)


def _conclusion(outcomes: list[dict[str, Any]], checks: list[dict[str, Any]]) -> str:
    """结论只看两件事：每一步都成功，且所有核验通过。

    没有核验时按「没被推翻就算通过」处理——不能因为没有可核验的期望位姿
    就把成功任务报成失败。
    """
    steps_ok = bool(outcomes) and all(o["outcome"] == _OK for o in outcomes)
    checks_ok = all(c["ok"] for c in checks) if checks else True
    return "任务成功" if steps_ok and checks_ok else "任务未完成（存在失败步骤或核验未通过）"
