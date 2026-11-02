"""集成测试：真实装配下的端到端行为。"""

from __future__ import annotations

import asyncio

import pytest

from robot_agent.errors import TaskConflictError, TaskNotFoundError

pytestmark = pytest.mark.integration


async def test_happy_path(assembly) -> None:
    task = await assembly.runner.submit("把红色方块推到左边")
    record = await assembly.runner.execute(task.id)
    assert record.status == "succeeded"
    assert record.outcomes and all(o.ok for o in record.outcomes)
    assert "任务成功" in record.report
    assert record.finished_at is not None
    assert record.duration_s is not None and record.duration_s >= 0


async def test_out_of_bounds_is_blocked(assembly) -> None:
    task = await assembly.runner.submit("把绿色方块推到 (9.0, 9.0)")
    record = await assembly.runner.execute(task.id)
    assert record.status == "blocked"
    assert "超出场地边界" in record.report
    assert record.outcomes == []


async def test_overspeed_is_clamped_not_fatal(assembly) -> None:
    task = await assembly.runner.submit("全速开到 (1.0, 1.0)")
    record = await assembly.runner.execute(task.id)
    assert record.status == "succeeded"
    assert record.corrections, "超速应被记录为安全修正"
    assert record.outcomes[0].ok


async def test_geofence_blocks(assembly) -> None:
    task = await assembly.runner.submit("把蓝色方块推到 (1.3, 1.3)")
    record = await assembly.runner.execute(task.id)
    assert record.status == "blocked"
    assert "禁区" in record.report


async def test_unparseable_fails(assembly) -> None:
    task = await assembly.runner.submit("给我唱首歌")
    record = await assembly.runner.execute(task.id)
    assert record.status == "failed"
    assert record.error is not None
    assert record.error["code"] == "planning_error"


async def test_idempotency(assembly) -> None:
    first = await assembly.runner.submit("把红色方块推到左边", idempotency_key="dup-1")
    second = await assembly.runner.submit("把红色方块推到左边", idempotency_key="dup-1")
    assert first.id == second.id


async def test_unknown_task_raises(assembly) -> None:
    with pytest.raises(TaskNotFoundError):
        await assembly.runner.get("task_missing")


async def test_cancel_queued_task(assembly) -> None:
    task = await assembly.runner.submit("把红色方块推到左边")
    cancelled = await assembly.runner.cancel(task.id, reason="测试取消")
    assert cancelled.status == "cancelled"


async def test_cannot_cancel_terminal_task(assembly) -> None:
    task = await assembly.runner.submit("把红色方块推到左边")
    done = await assembly.runner.execute(task.id)
    assert done.status == "succeeded"
    assert (await assembly.runner.cancel(task.id)).status == "succeeded"


async def test_approve_flow(assembly) -> None:
    assembly.settings.safety.require_approval_for = ["push_cube"]
    # 图的闸门在装配时已绑定配置对象，直接改配置即可生效（同一对象引用）
    task = await assembly.runner.submit("把红色方块推到左边")
    pending = await assembly.runner.execute(task.id)
    assert pending.status == "awaiting_approval"
    assert pending.outcomes == [], "审批前不得执行任何动作"

    approved = await assembly.runner.approve(task.id, approved_by="qa")
    assert approved.status == "succeeded"
    assert approved.approval is not None
    assert approved.approval["approved_by"] == "qa"


async def test_double_approve_rejected(assembly) -> None:
    assembly.settings.safety.require_approval_for = ["push_cube"]
    task = await assembly.runner.submit("把红色方块推到左边")
    await assembly.runner.execute(task.id)
    await assembly.runner.approve(task.id, approved_by="qa")
    with pytest.raises(TaskConflictError):
        await assembly.runner.approve(task.id, approved_by="qa-2")


async def test_reject_flow(assembly) -> None:
    assembly.settings.safety.require_approval_for = ["push_cube"]
    task = await assembly.runner.submit("把红色方块推到左边")
    await assembly.runner.execute(task.id)
    rejected = await assembly.runner.reject(task.id, reason="不符合安全要求")
    assert rejected.status == "blocked"
    assert "人工驳回" in rejected.report


async def test_events_stream(assembly) -> None:
    task = await assembly.runner.submit("把红色方块推到左边")
    seen: list[str] = []

    async def consume() -> None:
        async for event in assembly.events.stream(task.id):
            seen.append(event.type)

    consumer = asyncio.create_task(consume())
    await asyncio.sleep(0)
    await assembly.runner.execute(task.id)
    await asyncio.wait_for(consumer, timeout=5)
    assert "status" in seen
    assert "step" in seen


async def test_audit_chain_valid_after_run(assembly) -> None:
    task = await assembly.runner.submit("把红色方块推到左边")
    await assembly.runner.execute(task.id)
    result = assembly.audit.verify_chain()
    assert result.ok, result.reason
    assert result.entries > 0


async def test_list_tasks(assembly) -> None:
    task = await assembly.runner.submit("把红色方块推到左边")
    await assembly.runner.execute(task.id)
    records = await assembly.runner.list(limit=10)
    assert any(r.id == task.id for r in records)


async def test_report_contains_verification(assembly) -> None:
    task = await assembly.runner.submit("把红色方块推到左边")
    record = await assembly.runner.execute(task.id)
    assert record.verifications
    assert record.verifications[0]["ok"] is True
    assert "核验" in record.report
