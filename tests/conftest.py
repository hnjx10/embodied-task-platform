"""共享测试夹具。

原则：测试用**真实组件**装配（真 SQLite、真审计文件、真图），只把「物理世界」
替换成 mock 执行器。mock 掉自己写的业务代码会让测试变成自我确认。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from robot_agent.config import ArenaSettings, SafetySettings, Settings
from robot_agent.skills.base import SkillRegistry
from robot_agent.skills.mock import MockWorld, build_mock_registry


@pytest.fixture
def tmp_settings(tmp_path: Path) -> Settings:
    """隔离到临时目录的配置：每个用例独立审计文件与数据库。"""
    return Settings(
        env="test",
        log_json=True,
        arena=ArenaSettings(),
        safety=SafetySettings(),
        executor={"backend": "mock", "step_timeout_s": 5.0, "max_attempts": 2},
        audit={"enabled": True, "path": tmp_path / "audit" / "audit.jsonl"},
        storage={"url": f"sqlite:///{tmp_path / 'tasks.db'}"},
    )


@pytest.fixture
def registry_and_world() -> tuple[SkillRegistry, MockWorld]:
    return build_mock_registry(ArenaSettings())


@pytest.fixture
def registry(registry_and_world: tuple[SkillRegistry, MockWorld]) -> SkillRegistry:
    return registry_and_world[0]


@pytest.fixture
def world(registry_and_world: tuple[SkillRegistry, MockWorld]) -> MockWorld:
    return registry_and_world[1]


@pytest.fixture
async def assembly(tmp_settings: Settings) -> Any:
    """完整装配的运行时，退出时自动释放。"""
    from robot_agent.factory import build_assembly

    built = await build_assembly(tmp_settings, force_rules=True)
    try:
        yield built
    finally:
        await built.aclose()
