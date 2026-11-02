"""审计日志测试：哈希链、篡改检测、重启续链、关闭状态。"""

from __future__ import annotations

import json
from pathlib import Path

from robot_agent.audit import AuditLog, NullAuditLog, tail_entries
from robot_agent.config import AuditSettings


def make_log(path: Path, **kwargs) -> AuditLog:
    return AuditLog(AuditSettings(enabled=True, path=path), **kwargs)


def test_record_and_verify_chain(tmp_path: Path) -> None:
    log = make_log(tmp_path / "a.jsonl")
    log.record("planned", {"steps": 2}, task_id="t1")
    log.record("executed", {"ok": True}, task_id="t1")
    result = log.verify_chain()
    assert result.ok
    assert result.entries == 2


def test_chain_detects_tampering(tmp_path: Path) -> None:
    path = tmp_path / "a.jsonl"
    log = make_log(path)
    log.record("planned", {"steps": 2}, task_id="t1")
    log.record("executed", {"ok": False}, task_id="t1")

    lines = path.read_text(encoding="utf-8").splitlines()
    entry = json.loads(lines[1])
    entry["data"]["ok"] = True  # 篡改：把失败改成成功
    lines[1] = json.dumps(entry, ensure_ascii=False)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = log.verify_chain()
    assert not result.ok
    assert result.broken_at == 2


def test_chain_detects_deleted_entry(tmp_path: Path) -> None:
    path = tmp_path / "a.jsonl"
    log = make_log(path)
    for i in range(3):
        log.record("step", {"i": i}, task_id="t")
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text("\n".join([lines[0], lines[2]]) + "\n", encoding="utf-8")
    assert not log.verify_chain().ok


def test_seq_continues_after_restart(tmp_path: Path) -> None:
    path = tmp_path / "a.jsonl"
    first = make_log(path)
    first.record("a", {}, task_id="t")
    first.record("b", {}, task_id="t")

    second = make_log(path)  # 模拟进程重启
    second.record("c", {}, task_id="t")
    entries = tail_entries(path, 10)
    assert [e["seq"] for e in entries] == [1, 2, 3]
    assert second.verify_chain().ok


def test_disabled_audit_writes_nothing(tmp_path: Path) -> None:
    path = tmp_path / "a.jsonl"
    log = AuditLog(AuditSettings(enabled=False, path=path))
    assert log.record("x", {}) is None
    assert not path.exists()


def test_null_audit_log() -> None:
    assert NullAuditLog().record("x", {}) is None


def test_write_failure_fail_open_by_default(tmp_path: Path) -> None:
    """写不进去时默认不阻塞主流程（fail-open），但会打错误日志并返回 None。"""
    target = tmp_path / "as_dir"
    target.mkdir()  # 把目录当文件写，必然 OSError（跨平台一致）
    log = make_log(target)
    assert log.record("x", {}) is None


def test_write_failure_fail_closed_raises(tmp_path: Path) -> None:
    from robot_agent.errors import StorageError

    target = tmp_path / "as_dir2"
    target.mkdir()
    log = AuditLog(AuditSettings(enabled=True, path=target), fail_closed=True)
    try:
        log.record("x", {})
    except StorageError as exc:
        assert "审计写入失败" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("fail_closed=True 时应当抛 StorageError")


def test_tail_entries_limit(tmp_path: Path) -> None:
    path = tmp_path / "a.jsonl"
    log = make_log(path)
    for i in range(5):
        log.record("e", {"i": i}, task_id="t")
    assert len(tail_entries(path, 2)) == 2


def test_trace_id_captured(tmp_path: Path) -> None:
    from robot_agent.logging import request_context

    path = tmp_path / "a.jsonl"
    log = make_log(path)
    with request_context() as tid:
        log.record("x", {}, task_id="t")
    entry = tail_entries(path, 1)[0]
    assert entry["trace_id"] == tid
