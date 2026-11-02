"""CLI 端到端：通过真实命令入口验证退出码与 JSON 输出契约。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from robot_agent.cli import main

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ROBOT_AGENT_STORAGE__URL", f"sqlite:///{tmp_path / 'tasks.db'}")
    monkeypatch.setenv("ROBOT_AGENT_AUDIT__PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("ROBOT_AGENT_LOG_JSON", "false")


def test_run_success_exit_zero(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["run", "把红色方块推到左边", "--json", "--no-approve"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "succeeded"


def test_run_blocked_exit_one(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["run", "把绿色方块推到 (9, 9)", "--json", "--no-approve"])
    assert code == 1, "被安全拦截的任务必须返回非零退出码，供 CI/脚本判断"
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "blocked"


def test_config_command_outputs_json(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["config", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["service_name"] == "robot-agent"


def test_version_command(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["version"]) == 0
    assert "1.1.0" in capsys.readouterr().out


def test_audit_verify_without_log(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    assert main(["audit", "verify", "--path", str(tmp_path / "empty.jsonl")]) == 0


def test_doctor_exits_zero_in_mock_mode(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["doctor"]) == 0
    assert "配置加载成功" in capsys.readouterr().out


def test_estop_exit_zero_and_audited(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    """急停命令不需要 task_id，且必须留审计——它绕过了编排层。"""
    from robot_agent.audit import tail_entries

    code = main(["estop", "--json", "--reason", "e2e test"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["stopped"] is True
    assert payload["reason"] == "e2e test"

    entries = tail_entries(tmp_path / "audit.jsonl", limit=5)
    assert any(e.get("event") == "emergency_stop" for e in entries)


def test_tasks_purge_json(capsys: pytest.CaptureFixture[str]) -> None:
    """手动清理入口：只清终态，空库应返回 0 条且可被脚本消费。"""
    assert main(["tasks", "purge", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["retention_hours"] >= 1
    assert payload["count"] == 0


def test_tasks_purge_dry_run(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["tasks", "purge", "--dry-run"]) == 0
    assert "未删除" in capsys.readouterr().out
