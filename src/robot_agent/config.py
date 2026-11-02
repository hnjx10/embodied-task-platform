"""分层配置。

优先级（高 → 低）::

    构造参数  >  环境变量 (ROBOT_AGENT_*)  >  .env 文件  >  YAML 配置文件  >  代码默认值

设计要点：

* 所有「业务常量」都从配置来（场地尺寸、速度上限、禁区、超时、并发数），
  代码里不再有散落的魔数，换场地只需改 YAML。
* 嵌套字段用双下划线覆盖，例如 ``ROBOT_AGENT_SAFETY__MAX_LINEAR_SPEED=0.18``。
* 密钥统一用 :class:`~pydantic.SecretStr`，``safe_dump()`` 保证日志/健康检查不会泄露。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from robot_agent.errors import ConfigurationError

try:  # YAML 是可选配置源，未安装时读取配置文件会显式报错（见 YamlConfigSource）
    import yaml
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore[assignment]

ENV_PREFIX = "ROBOT_AGENT_"


class Geofence(BaseModel):
    """圆形禁区（例：充电桩、人员通道）。"""

    name: str = "keep_out"
    x: float
    y: float
    radius: float = Field(gt=0.0)

    def contains(self, px: float, py: float) -> bool:
        return (px - self.x) ** 2 + (py - self.y) ** 2 < self.radius**2


class ArenaSettings(BaseModel):
    """场地与初始场景。"""

    half_size: float = Field(default=1.5, gt=0.0, description="正方形场地半边长，米")
    cube_size: float = Field(default=0.1, gt=0.0)
    # 方块颜色 → 初始坐标；仅 mock 执行器与场景生成脚本使用
    cube_layout: dict[str, tuple[float, float]] = Field(
        default_factory=lambda: {
            "red": (0.8, 0.5),
            "green": (0.9, -0.4),
            "blue": (-0.7, 0.6),
        }
    )

    @property
    def bounds(self) -> tuple[float, float]:
        return (-self.half_size, self.half_size)


class SafetySettings(BaseModel):
    """安全闸门参数。改这里等于改机器人的物理行为边界，需评审。"""

    max_linear_speed: float = Field(default=0.22, gt=0.0, description="硬件线速度上限 m/s")
    min_linear_speed: float = Field(default=0.02, gt=0.0)
    default_speed: float = Field(default=0.15, gt=0.0)
    max_steps: int = Field(default=8, ge=1, le=32, description="单条指令允许的最大技能步数")
    geofences: list[Geofence] = Field(
        default_factory=lambda: [Geofence(name="charging_dock", x=1.3, y=1.3, radius=0.4)]
    )
    # 命中即拒绝的指令级关键词；参数级硬校验始终兜底，不依赖这里
    blocked_keywords: list[str] = Field(default_factory=lambda: ["冲向", "撞", "destroy", "全速冲"])
    # 白名单为空表示放行注册表内全部技能
    allowed_skills: list[str] = Field(default_factory=list)
    # 这些技能在动作执行前需要人工确认（LangGraph interrupt）
    require_approval_for: list[str] = Field(default_factory=list)
    # 是否允许把「超速请求」自动降速后执行，而不是直接拒绝
    clamp_overspeed: bool = True

    @model_validator(mode="after")
    def _check_speed_ordering(self) -> SafetySettings:
        if self.min_linear_speed >= self.max_linear_speed:
            raise ValueError("min_linear_speed 必须小于 max_linear_speed")
        if not self.min_linear_speed <= self.default_speed <= self.max_linear_speed:
            raise ValueError("default_speed 必须落在 [min_linear_speed, max_linear_speed] 区间")
        return self

    @field_validator("geofences")
    @classmethod
    def _unique_geofence_names(cls, value: list[Geofence]) -> list[Geofence]:
        names = [g.name for g in value]
        if len(names) != len(set(names)):
            raise ValueError("geofence 名称必须唯一")
        return value


class LLMSettings(BaseModel):
    """LLM 规划器配置。未配置 api_key 时自动降级为规则规划。"""

    enabled: bool = True
    api_key: SecretStr | None = None
    base_url: str | None = Field(default=None, description="OpenAI 兼容网关地址")
    model: str = "qwen-plus"
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    timeout_s: float = Field(default=20.0, gt=0.0)
    max_attempts: int = Field(default=3, ge=1, le=6)
    max_output_tokens: int = Field(default=1024, ge=64)
    fallback_to_rules: bool = Field(default=True, description="LLM 失败时是否降级规则解析")

    @field_validator("api_key", mode="before")
    @classmethod
    def _blank_to_none(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("base_url", mode="before")
    @classmethod
    def _strip_base_url(cls, value: Any) -> Any:
        if isinstance(value, str) and value.strip():
            return value.strip().rstrip("/")
        return None

    @property
    def configured(self) -> bool:
        """是否具备真实调用条件。"""
        return bool(self.enabled and self.api_key is not None)


class ExecutorSettings(BaseModel):
    """执行器选择与容错参数。"""

    backend: Literal["mock", "ros2"] = "mock"
    step_timeout_s: float = Field(default=90.0, gt=0.0)
    max_attempts: int = Field(default=2, ge=1, le=5, description="瞬时故障重试次数")
    retry_backoff_s: float = Field(default=0.3, ge=0.0)
    ros_domain_id: int | None = Field(default=None, ge=0, le=232)


class AuditSettings(BaseModel):
    """审计日志落地参数。审计是合规要求，生产环境必须开启。"""

    enabled: bool = True
    path: Path = Path("var/audit/audit.jsonl")
    rotate_bytes: int = Field(default=32 * 1024 * 1024, ge=1024)
    fsync: bool = Field(default=False, description="每次写入落盘；开启后吞吐下降但断电不丢")
    hash_chain: bool = Field(default=True, description="条目带前序哈希，可检测篡改")


class StorageSettings(BaseModel):
    """任务持久化。``memory`` 仅用于测试与本地体验。"""

    url: str = Field(default="memory", description="memory | sqlite:////abs/path/tasks.db")
    retention_hours: int = Field(default=72, ge=1)

    @property
    def is_sqlite(self) -> bool:
        return self.url.startswith("sqlite://")


class APISettings(BaseModel):
    """HTTP 控制面。"""

    host: str = "0.0.0.0"  # noqa: S104 —— 容器内需监听全网卡，鉴权由 auth_token 兜底
    port: int = Field(default=8080, ge=1, le=65535)
    auth_token: SecretStr | None = Field(
        default=None, description="Bearer Token；为空则仅允许 localhost 访问"
    )
    cors_origins: list[str] = Field(default_factory=list)
    max_concurrent_tasks: int = Field(default=4, ge=1, le=64)
    max_queue_size: int = Field(default=64, ge=1, le=1024)
    request_timeout_s: float = Field(default=180.0, gt=0.0)
    max_request_bytes: int = Field(
        default=64 * 1024,
        ge=1024,
        le=8 * 1024 * 1024,
        description="单个请求体字节上限；指令只有 500 字符，64 KiB 已足够宽松",
    )

    @field_validator("auth_token", mode="before")
    @classmethod
    def _blank_to_none(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value


class YamlConfigSource(PydanticBaseSettingsSource):
    """把 YAML 文件作为最低优先级的配置源（低于 env 与 .env）。"""

    def __init__(self, settings_cls: type[BaseSettings], path: Path | None) -> None:
        super().__init__(settings_cls)
        self._path = path

    def get_field_value(self, field: Any, field_name: str) -> tuple[Any, str, bool]:
        # 整份 YAML 在 __call__ 里一次性返回，逐字段钩子不需要实现
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        if self._path is None:
            return {}
        # 显式/自动发现的配置文件读不了必须炸：静默退回默认值会让人以为
        # 限速、禁区和审批策略都已生效，实际上一条都没加载。
        if yaml is None:
            raise ConfigurationError(
                f"需要读取配置文件 {self._path}，但未安装 PyYAML（pip install 'robot-agent[api]'）"
            )
        if not self._path.exists():
            raise ConfigurationError(f"配置文件不存在: {self._path}")
        raw = yaml.safe_load(self._path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise ConfigurationError(f"配置文件顶层必须是映射: {self._path}")
        return raw


class Settings(BaseSettings):
    """进程级配置单例（由 :func:`load_settings` 构造）。"""

    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        validate_default=True,
    )

    env: Literal["dev", "staging", "prod", "test"] = "dev"
    service_name: str = "robot-agent"
    log_level: str = "INFO"
    log_json: bool = True

    arena: ArenaSettings = Field(default_factory=ArenaSettings)
    safety: SafetySettings = Field(default_factory=SafetySettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    executor: ExecutorSettings = Field(default_factory=ExecutorSettings)
    audit: AuditSettings = Field(default_factory=AuditSettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    api: APISettings = Field(default_factory=APISettings)

    # 由 load_settings 注入，供 YAML 源读取
    _config_path: Path | None = None

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        path = _resolve_config_path()
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            YamlConfigSource(settings_cls, path),
            file_secret_settings,
        )

    @model_validator(mode="after")
    def _prod_guardrails(self) -> Settings:
        """生产环境的安全兜底：不允许无鉴权对外监听、不允许关闭审计。"""
        if self.env != "prod":
            return self
        problems: list[str] = []
        if not self.audit.enabled:
            problems.append("prod 环境必须开启审计（ROBOT_AGENT_AUDIT__ENABLED=true）")
        if self.api.auth_token is None and self.api.host not in {"127.0.0.1", "localhost"}:
            problems.append(
                "prod 环境对外监听必须配置 ROBOT_AGENT_API__AUTH_TOKEN，"
                "或把 ROBOT_AGENT_API__HOST 改为 127.0.0.1"
            )
        if self.storage.url == "memory":
            problems.append("prod 环境必须使用持久化存储（sqlite:///...）")
        if problems:
            raise ValueError("; ".join(problems))
        return self

    def safe_dump(self) -> dict[str, Any]:
        """脱敏后的配置快照，用于启动日志与 /healthz。"""
        data = self.model_dump(mode="json")
        for path in (("llm", "api_key"), ("api", "auth_token")):
            section = data.get(path[0])
            if isinstance(section, dict) and section.get(path[1]):
                section[path[1]] = "***"
        return data


def _resolve_config_path() -> Path | None:
    """按 ROBOT_AGENT_CONFIG 环境变量定位 YAML；未指定则尝试 config/default.yaml。"""
    explicit = os.environ.get(f"{ENV_PREFIX}CONFIG")
    if explicit:
        return Path(explicit).expanduser()
    for candidate in (Path("config/default.yaml"), Path("config/config.yaml")):
        if candidate.exists():
            return candidate
    return None


def load_settings(**overrides: Any) -> Settings:
    """构造配置对象。异常统一转成 :class:`ConfigurationError`，便于启动阶段快速失败。"""
    try:
        return Settings(**overrides)
    except ConfigurationError:
        raise
    except Exception as exc:  # pydantic 的 ValidationError 信息较长，收敛成一句
        raise ConfigurationError(f"配置非法: {exc}", cause=exc) from exc
