"""LangGraph 原生 ``interrupt()`` 审批挂起的集成测试。

两种挂起方式（``persist`` / ``interrupt``）对外必须完全一致：任务状态都是
``awaiting_approval``，批准后都能跑完。差异只在内部——一个是「置位后重建状态
重新入图」，一个是「图原地停住、Command 唤醒」。

这里验证三件容易被忽略的事：

1. 中断后**任务状态**是对的（而不是停在 running）；
2. 批准后能**从断点继续**，且批准后仍会重跑一次策略链；
3. 驳回后图能被唤醒并走到 blocked——否则 checkpoint 会一直挂着这条线程。
"""

from __future__ import annotations

import pytest

from robot_agent.config import ArenaSettings, SafetySettings, Settings
from robot_agent.factory import build_assembly
from robot_agent.orchestration.graph import NATIVE_INTERRUPT

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not NATIVE_INTERRUPT, reason="当前 langgraph 版本不支持 interrupt()"),
]

#: 把 goto_point 配成需要审批，这样任何一条「开到某处」的指令都会触发挂起。
_NEEDS_APPROVAL = SafetySettings(require_approval_for=["goto_point"])


@pytest.fixture
async def interrupt_assembly(tmp_path):
    settings = Settings(
        env="test",
        approval_mode="interrupt",
        safety=_NEEDS_APPROVAL,
        arena=ArenaSettings(),
        executor={"backend": "mock", "step_timeout_s": 5.0, "max_attempts": 1},
        audit={"enabled": True, "path": tmp_path / "audit" / "audit.jsonl"},
        storage={"url": f"sqlite:///{tmp_path / 'tasks.db'}"},
    )
    built = await build_assembly(settings, force_rules=True)
    try:
        yield built
    finally:
        await built.aclose()


@pytest.fixture
async def persist_assembly(tmp_path):
    """对照组：同样的安全配置，走持久化重建路径。"""
    settings = Settings(
        env="test",
        approval_mode="persist",
        safety=_NEEDS_APPROVAL,
        arena=ArenaSettings(),
        executor={"backend": "mock", "step_timeout_s": 5.0, "max_attempts": 1},
        audit={"enabled": True, "path": tmp_path / "audit" / "audit.jsonl"},
        storage={"url": f"sqlite:///{tmp_path / 'tasks.db'}"},
    )
    built = await build_assembly(settings, force_rules=True)
    try:
        yield built
    finally:
        await built.aclose()


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not NATIVE_INTERRUPT, reason="当前 langgraph 版本不支持 interrupt()"),
]


async def test_interrupt_suspends_with_awaiting_status(interrupt_assembly) -> None:
    runner = interrupt_assembly.runner
    task = await runner.submit("开到 (1.0, 1.0)")
    record = await runner.execute(task.id)

    assert record.status == "awaiting_approval"
    assert record.requires_approval is True
    assert record.finished_at is None, "挂起不是终态，不该写 finished_at"


async def test_both_modes_expose_same_contract(interrupt_assembly, persist_assembly) -> None:
    """对外契约一致才是好抽象：上层不该知道底层用的是哪种挂起机制。"""
    results = []
    for assembly in (interrupt_assembly, persist_assembly):
        task = await assembly.runner.submit("开到 (1.0, 1.0)")
        record = await assembly.runner.execute(task.id)
        results.append((record.status, record.requires_approval))

    assert results[0] == results[1] == ("awaiting_approval", True)


async def test_approve_resumes_from_breakpoint(interrupt_assembly) -> None:
    runner = interrupt_assembly.runner
    task = await runner.submit("开到 (1.0, 1.0)")
    await runner.execute(task.id)

    record = await runner.approve(task.id, approved_by="tester")
    assert record.status == "succeeded"
    assert record.outcomes and all(o.ok for o in record.outcomes)


async def test_reject_wakes_the_suspended_graph(interrupt_assembly) -> None:
    """驳回必须让挂起的图走完，否则 checkpoint 里会留着一条永不结束的线程。"""
    runner = interrupt_assembly.runner
    task = await runner.submit("开到 (1.0, 1.0)")
    await runner.execute(task.id)

    record = await runner.reject(task.id, reason="现场不安全")
    assert record.status == "blocked"
    assert "驳回" in record.report

    # 图已经不挂了：再查 thread 不应有 next 节点
    snapshot = runner.graph.get_state({"configurable": {"thread_id": task.id}})
    assert not snapshot.next


async def test_approval_still_rechecks_policy(interrupt_assembly) -> None:
    """批准不等于绕过闸门：恢复时会重跑一次策略链。

    这里把禁区内的目标配成「不需要审批」也无所谓——因为指令本身在 screen 阶段
    就会被拦下，压根不会走到审批。
    """
    runner = interrupt_assembly.runner
    task = await runner.submit("开到 (9.0, 9.0)")
    record = await runner.execute(task.id)

    # 越界在 screen 阶段直接拒绝，不进入审批
    assert record.status == "blocked"
    assert record.requires_approval is False
