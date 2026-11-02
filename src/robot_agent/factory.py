"""对象图装配。

CLI、HTTP 服务、测试三处都用 :func:`build_assembly` 装配，保证「跑起来的东西」
和「测的东西」是同一套。依赖注入是显式的：每层只接收自己需要的对象，
替换执行器 / 规划器 / 存储不需要改任何业务代码。
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from robot_agent.audit import AuditLog, NullAuditLog
from robot_agent.config import Settings
from robot_agent.errors import ExecutorUnavailableError
from robot_agent.logging import get_logger
from robot_agent.orchestration.events import EventBus
from robot_agent.orchestration.executor import StepExecutor
from robot_agent.orchestration.graph import AgentComponents, build_graph, build_resume_graph
from robot_agent.orchestration.runner import TaskRunner
from robot_agent.planner.pipeline import Planner
from robot_agent.safety.gate import SafetyGate
from robot_agent.skills.base import SkillRegistry
from robot_agent.storage.base import TaskRepository
from robot_agent.storage.sqlite import SqliteTaskRepository, build_repository

logger = get_logger(__name__)


@dataclass
class Assembly:
    """一次装配产出的全部对象。"""

    settings: Settings
    registry: SkillRegistry
    audit: AuditLog
    events: EventBus
    repository: TaskRepository
    components: AgentComponents
    runner: TaskRunner
    backend_handle: Any = None
    _closers: list[Callable[[], Any]] = field(default_factory=list, repr=False)

    async def aclose(self) -> None:
        """按逆序释放资源；单个释放失败不影响其它资源。"""
        for closer in reversed(self._closers):
            try:
                result = closer()
                if hasattr(result, "__await__"):
                    await result
            except Exception:
                logger.warning("assembly_close_failed", closer=getattr(closer, "__name__", "?"))
        logger.info("assembly_closed")


def build_skill_backend(
    settings: Settings,
) -> tuple[SkillRegistry, Any, Callable[[], Any]]:
    """按配置装配执行器，返回 (注册表, 执行器句柄, 关闭函数)。"""
    if settings.executor.backend == "ros2":
        if settings.executor.ros_domain_id is not None:
            os.environ["ROS_DOMAIN_ID"] = str(settings.executor.ros_domain_id)
        try:
            from robot_agent.skills.ros2 import build_ros_registry
        except ImportError as exc:  # rclpy 缺失
            raise ExecutorUnavailableError(
                "ROS2 执行器不可用：未检测到 rclpy。请在 ROS2 环境运行，"
                "或改用 ROBOT_AGENT_EXECUTOR__BACKEND=mock",
                cause=exc,
            ) from exc
        registry, bridge = build_ros_registry(settings)
        logger.info("backend_ready", backend="ros2", skills=registry.names())
        return registry, bridge, bridge.shutdown

    from robot_agent.skills.mock import build_mock_registry

    registry, world = build_mock_registry(settings.arena)
    logger.info("backend_ready", backend="mock", skills=registry.names())
    return registry, world, lambda: None


async def build_assembly(settings: Settings, *, force_rules: bool = False) -> Assembly:
    """装配完整运行时。调用方负责在结束时 ``await assembly.aclose()``。"""
    registry, backend_handle, backend_close = build_skill_backend(settings)

    # 白名单在这里生效一次，后续所有层看到的是同一份受限注册表
    registry = registry.filter(settings.safety.allowed_skills)

    audit: AuditLog = (
        AuditLog(settings.audit, service=settings.service_name)
        if settings.audit.enabled
        else NullAuditLog()  # type: ignore[assignment]
    )

    planner = Planner(settings, registry, force_rules=force_rules)
    gate = SafetyGate(settings.safety, settings.arena, registry)
    executor = StepExecutor(registry, settings.executor)

    components = AgentComponents(
        settings=settings,
        registry=registry,
        gate=gate,
        planner=planner,
        executor=executor,
        audit=audit,
    )

    repository = build_repository(settings.storage.url)
    if isinstance(repository, SqliteTaskRepository):
        await repository.init()
        logger.info("storage_ready", url=settings.storage.url)
    elif settings.env != "test":
        logger.warning("storage_ephemeral", url=settings.storage.url)

    events = EventBus(max_subscribers_per_task=settings.api.max_sse_subscribers)
    runner = TaskRunner(
        settings=settings,
        repository=repository,  # type: ignore[arg-type]
        events=events,
        audit=audit,
        graph=build_graph(components),
        resume_graph=build_resume_graph(components),
        executor=executor,
    )

    closers: list[Callable[[], Any]] = [executor.close, repository.close, backend_close]

    return Assembly(
        settings=settings,
        registry=registry,
        audit=audit,
        events=events,
        repository=repository,  # type: ignore[arg-type]
        components=components,
        runner=runner,
        backend_handle=backend_handle,
        _closers=closers,
    )
