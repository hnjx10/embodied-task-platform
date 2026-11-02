"""配置层测试：优先级、校验、脱敏、生产护栏。"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from robot_agent.config import Settings, load_settings
from robot_agent.errors import ConfigurationError


def test_defaults_are_safe() -> None:
    settings = Settings()
    assert settings.executor.backend == "mock"
    assert settings.safety.max_linear_speed == pytest.approx(0.22)
    assert settings.audit.enabled is True
    assert settings.llm.configured is False  # 没配 key 就不能声称能用 LLM


def test_env_override_nested(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ROBOT_AGENT_SAFETY__MAX_LINEAR_SPEED", "0.18")
    monkeypatch.setenv("ROBOT_AGENT_EXECUTOR__BACKEND", "ros2")
    settings = load_settings()
    assert settings.safety.max_linear_speed == pytest.approx(0.18)
    assert settings.executor.backend == "ros2"


def test_speed_ordering_validated() -> None:
    with pytest.raises(ValidationError):
        Settings(safety={"min_linear_speed": 0.5, "max_linear_speed": 0.2})


def test_default_speed_must_be_within_limits() -> None:
    with pytest.raises(ValidationError):
        Settings(safety={"default_speed": 1.0, "max_linear_speed": 0.22})


def test_secrets_masked_in_safe_dump() -> None:
    settings = Settings()
    dumped = settings.safe_dump()
    settings2 = Settings(llm={"api_key": "sk-super-secret"}, api={"auth_token": "tok-123"})
    dumped2 = settings2.safe_dump()
    assert "sk-super-secret" not in str(dumped2)
    assert dumped2["llm"]["api_key"] == "***"
    assert dumped2["api"]["auth_token"] == "***"
    assert isinstance(dumped, dict)


def test_prod_requires_auth_token_for_public_bind() -> None:
    with pytest.raises(ValidationError) as exc:
        Settings(
            env="prod",
            api={"host": "0.0.0.0", "auth_token": None},  # noqa: S104
            storage={"url": "sqlite:///tmp/x.db"},
        )
    assert "AUTH_TOKEN" in str(exc.value)


def test_prod_allows_localhost_without_token() -> None:
    settings = Settings(
        env="prod", api={"host": "127.0.0.1"}, storage={"url": "sqlite:///tmp/x.db"}
    )
    assert settings.env == "prod"


def test_prod_rejects_memory_storage() -> None:
    with pytest.raises(Exception) as exc:
        Settings(env="prod", api={"host": "127.0.0.1"}, storage={"url": "memory"})
    assert "持久化存储" in str(exc.value)


def test_prod_rejects_disabled_audit() -> None:
    with pytest.raises(Exception) as exc:
        Settings(
            env="prod",
            audit={"enabled": False},
            api={"host": "127.0.0.1"},
            storage={"url": "sqlite:///tmp/x.db"},
        )
    assert "审计" in str(exc.value)


def test_load_settings_wraps_validation_error() -> None:
    with pytest.raises(ConfigurationError):
        load_settings(safety={"max_linear_speed": -1.0})


def test_blank_api_key_treated_as_missing() -> None:
    settings = Settings(llm={"api_key": "   "})
    assert settings.llm.api_key is None
    assert settings.llm.configured is False


def test_yaml_config_is_applied(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = tmp_path / "custom.yaml"
    config.write_text("safety:\n  max_linear_speed: 0.18\n", encoding="utf-8")
    monkeypatch.setenv("ROBOT_AGENT_CONFIG", str(config))

    settings = load_settings()
    assert settings.safety.max_linear_speed == pytest.approx(0.18)


def test_env_wins_over_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = tmp_path / "custom.yaml"
    config.write_text("safety:\n  max_linear_speed: 0.18\n", encoding="utf-8")
    monkeypatch.setenv("ROBOT_AGENT_CONFIG", str(config))
    monkeypatch.setenv("ROBOT_AGENT_SAFETY__MAX_LINEAR_SPEED", "0.20")

    settings = load_settings()
    assert settings.safety.max_linear_speed == pytest.approx(0.20)


def test_explicit_missing_config_file_fails_loudly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """显式指定的配置文件读不到必须报错——静默退回默认值等于悄悄关掉安全策略。"""
    monkeypatch.setenv("ROBOT_AGENT_CONFIG", str(tmp_path / "absent.yaml"))

    with pytest.raises(ConfigurationError) as exc:
        load_settings()
    assert "不存在" in str(exc.value)


def test_non_mapping_yaml_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = tmp_path / "bad.yaml"
    config.write_text("- 1\n- 2\n", encoding="utf-8")
    monkeypatch.setenv("ROBOT_AGENT_CONFIG", str(config))

    with pytest.raises(ConfigurationError) as exc:
        load_settings()
    assert "映射" in str(exc.value)
