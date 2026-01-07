"""终端渲染的单元测试。

这些函数是纯字符串变换，所以可以直接断言「对齐宽度」「截断不超宽」，
不需要捕获 stdout，也不依赖终端是不是彩色。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from robot_agent import console
from robot_agent.domain.state import TaskRecord
from robot_agent.errors import TaskNotFoundError


def _record(
    instruction: str = "把红色方块推到左边",
    *,
    status: str = "succeeded",
    task_id: str = "t-1",
) -> TaskRecord:
    return TaskRecord(
        id=task_id,
        instruction=instruction,
        status=status,  # type: ignore[arg-type]
        created_at=datetime(2026, 11, 2, 10, 0, tzinfo=timezone.utc),
        report="已把红色方块推到 (-1.2, 0.5)",
    )


def test_display_width_counts_wide_chars_as_two() -> None:
    """中文字符占两列；用 len() 对齐会让表格在中英混排时歪掉。"""
    assert console.display_width("abc") == 3
    assert console.display_width("任务") == 4
    assert console.display_width("任务 abc") == 8


@pytest.mark.parametrize("text", ["a", "任务", "abc", "红色方块"])
def test_pad_reaches_exact_display_width(text: str) -> None:
    assert console.display_width(console.pad(text, 10)) == 10


def test_pad_never_shrinks_content() -> None:
    """内容比列宽还长时原样返回，绝不切割（切中文会切出半个字）。"""
    text = "很长的中文指令内容"
    assert console.pad(text, 4) == text


def test_truncate_keeps_within_limit() -> None:
    text = "把红色方块推到左边再推回来"
    out = console.truncate(text, 10)
    assert console.display_width(out) <= 10
    assert out.endswith("…")


def test_truncate_leaves_short_text_alone() -> None:
    assert console.truncate("把方块推走", 20) == "把方块推走"


def test_task_table_is_empty_message_when_no_records() -> None:
    assert console.task_table([]) == "没有任务记录"


def test_task_table_aligns_instruction_column() -> None:
    """每行指令列的起点必须一致——这正是 pad 按显示宽度补空格的用处。"""
    records = [
        _record("把红色方块推到左边", task_id="task-short"),
        _record("把绿色方块推到右边再回原点", task_id="task-much-longer-id", status="failed"),
    ]
    lines = console.task_table(records, instruction_width=8).splitlines()
    rows = lines[2:]  # 前两行是表头与分隔线

    expected = [console.truncate(r.instruction, 8) for r in records]
    offsets = {
        console.display_width(line) - console.display_width(text)
        for line, text in zip(rows, expected, strict=True)
    }
    assert len(offsets) == 1, f"指令列起点不一致：{offsets}"


def test_task_table_header_has_all_columns() -> None:
    header = console.task_table([_record()]).splitlines()[0]
    for column in ("任务 ID", "状态", "创建时间", "指令"):
        assert column in header


def test_task_json_matches_api_shape() -> None:
    """CLI 的 --json 与 HTTP 响应同构，脚本才能复用同一套 jq 表达式。"""
    import json

    payload = json.loads(console.task_json(_record()))
    assert payload["id"] == "t-1"
    assert payload["status"] == "succeeded"
    assert "instruction" in payload


def test_plan_preview_lists_steps() -> None:
    record = _record(status="awaiting_approval")
    record.plan = []
    record.plan_warnings = ["push_cube 属于高风险动作"]
    out = console.plan_preview(record)
    assert "待审批" in out
    assert "高风险" in out


def test_task_report_mentions_status_and_error() -> None:
    record = _record(status="failed")
    record.error = "skill_timeout: push_cube"
    out = console.task_report(record)
    assert "已把红色方块推到" in out
    assert "failed" in out
    assert "skill_timeout" in out


def test_audit_table_empty_points_at_the_file() -> None:
    out = console.audit_table([], path="/var/log/audit.jsonl")
    assert "/var/log/audit.jsonl" in out


def test_audit_table_renders_one_line_per_entry() -> None:
    entries = [
        {"ts": "2026-11-02T10:00:00", "event": "task_submitted", "task_id": "t-1", "data": {}},
        {"ts": "2026-11-02T10:00:01", "event": "task_succeeded", "task_id": None, "data": {}},
    ]
    lines = console.audit_table(entries).splitlines()
    assert len(lines) == 2
    assert "task_submitted" in lines[0]
    assert "task_succeeded" in lines[1]


def test_purge_message_distinguishes_dry_run() -> None:
    assert "未删除" in console.purge_message(3, retention_hours=72, dry_run=True)
    assert "已清理 3 条" in console.purge_message(3, retention_hours=72, dry_run=False)


def test_error_helpers() -> None:
    exc = TaskNotFoundError("任务不存在", details={"task_id": "t-9"})
    assert console.error_line(exc).startswith("[task_not_found]")
    import json

    payload = json.loads(console.error_json(exc))
    assert payload["ok"] is False
    assert payload["error"]["code"] == "task_not_found"
    assert json.loads(console.unknown_error_json(RuntimeError("boom")))["error"]["code"] == (
        "internal_error"
    )
