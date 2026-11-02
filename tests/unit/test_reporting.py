"""报告渲染的单元测试。

报告是给人看的东西，但它的分支逻辑（成功/拦截/待审批/规划失败）必须准确——
写错一个分支，值班的人就会照着一份「结论：任务成功」的报告去排查真实故障。
"""

from __future__ import annotations

from robot_agent.reporting import render_report


def _ok_step(index: int = 0, skill: str = "push_cube", detail: str = "已推到 (-1.2, 0.5)") -> dict:
    return {
        "index": index,
        "skill": skill,
        "outcome": "ok",
        "latency_ms": 812.4,
        "attempts": 1,
        "detail": detail,
    }


def _report(**overrides):
    base = {
        "task_id": "task-1",
        "instruction": "把红色方块推到左边",
        "status": "succeeded",
        "outcomes": [_ok_step()],
        "verifications": [],
        "plan_source": "rules",
        "step_count": 1,
    }
    base.update(overrides)
    return render_report(**base)


def test_success_report_lists_steps_and_conclusion() -> None:
    out = _report()
    assert "任务: task-1" in out
    assert "指令: 把红色方块推到左边" in out
    assert "规划来源: rules｜步数: 1" in out
    assert "[PASS] 第 1 步 push_cube (812ms, 1 次)" in out
    assert out.endswith("结论: 任务成功")


def test_failed_step_flips_conclusion() -> None:
    failed = {**_ok_step(), "outcome": "timeout", "attempts": 2}
    out = _report(outcomes=[failed])
    assert "[TIMEOUT] 第 1 步 push_cube" in out
    assert "结论: 任务未完成" in out


def test_failed_verification_flips_conclusion() -> None:
    """步骤都成功但核验没过，也必须报未完成——这才是「不信执行器自述」的落地。"""
    out = _report(verifications=[{"target": "red", "ok": False, "detail": "距目标 0.42m"}])
    assert "[FAIL] 核验 red" in out
    assert "结论: 任务未完成" in out


def test_passing_verification_is_shown() -> None:
    out = _report(verifications=[{"target": "robot", "ok": True, "detail": "距目标 0.01m"}])
    assert "[PASS] 核验 robot" in out
    assert "结论: 任务成功" in out


def test_blocked_report_stops_early() -> None:
    out = _report(status="blocked", block_reason="超出场地边界", outcomes=[])
    assert "状态: 已拦截 — 超出场地边界" in out
    assert "结论" not in out, "被拦截的任务不该给出成功/失败结论"


def test_awaiting_approval_report_lists_reasons() -> None:
    out = _report(
        status="awaiting_approval",
        approval_reasons=["push_cube 属于高风险动作", "操作员未确认"],
        outcomes=[],
    )
    assert "等待人工审批" in out
    assert "push_cube 属于高风险动作; 操作员未确认" in out


def test_planning_failure_report_shows_message() -> None:
    out = _report(
        status="failed",
        error={"code": "plan_validation_error", "message": "无法解析目标坐标"},
        outcomes=[],
    )
    assert "状态: 规划失败 — 无法解析目标坐标" in out


def test_corrections_are_summarised_not_dumped() -> None:
    """安全修正可能有几十条，报告里只给数量，细节留给审计。"""
    out = _report(corrections=[{"i": 1}, {"i": 2}], status="succeeded")
    assert "安全修正: 2 处" in out
    assert "详见审计日志" in out


def test_empty_outcomes_never_claims_success() -> None:
    """没有执行任何步骤却被判成功，是最危险的误报。"""
    out = _report(outcomes=[])
    assert "结论: 任务未完成" in out
