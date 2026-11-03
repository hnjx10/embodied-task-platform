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

from robot_agent import metrics
from robot_agent.audit import AuditLog, NullAuditLog
from robot_agent.config import Settings
from robot_agent.domain.plan import PlanStep
from robot_agent.errors import ExecutorUnavailableError
from robot_agent.logging import get_logger
from robot_agent.orchestration.events import EventBus
from robot_agent.orchestration.execution_lock import build_lock
from robot_agent.orchestration.executor import PreDispatchCheck, StepExecutor
from robot_agent.orchestration.graph import AgentComponents, build_graph, build_resume_graph
from robot_agent.orchestration.maintenance import PurgeScheduler
from robot_agent.orchestration.runner import TaskRunner
from robot_agent.planner.pipeline import Planner
from robot_agent.safety.gate import SafetyGate
from robot_agent.skills.base import SkillRegistry
from robot_agent.storage.base import TaskRepository
from robot_agent.storage.sqlite import SqliteTaskRepository, build_repository

logger = get_logger(__name__)


#: 需要实时位姿才能安全下发的技能。急停 ``stop`` **刻意不在其中**：
#: 位姿过期时最该能执行的恰恰是停下来，把它也拦掉等于用安全规则制造危险。
MOTION_SKILLS = frozenset(
    {
        "goto_point",
        "move_relative",
        "rotate",
        "look_at",
        "patrol",
        "push_cube",
        "pick_cube",
        "place_cube",
        "dock",
        "undock",
    }
)


def _pre_dispatch(
    gate: SafetyGate,
    *,
    pose_age: Callable[[], float | None] | None = None,
    stale_s: float = 2.0,
) -> PreDispatchCheck:
    """把闸门的单步校验包成执行器要的形状。

    计划级校验发生在「计划生成之后、执行之前」，中间还夹着一次人工审批。
    审批人有权改步骤参数，所以真正下发硬件前的这一刻必须再过一遍策略链——
    否则「人工批准」就成了一条绕开安全层的通道。

    在这之上还多查一件事：**位姿是不是还新鲜**。ROS2 侧的 ``/odom`` 回调如果
    断了（节点挂了、DDS 丢包、话题名改了），``self._odom`` 会安静地停在最后一次
    收到的值上——后续的「再往前 3 米」就会拿一条几分钟前的位姿去算目标点。
    这种故障不报错、不告警，只能靠时间戳发现。
    """

    def check(step: PlanStep) -> tuple[PlanStep, str | None]:
        if pose_age is not None and step.skill in MOTION_SKILLS:
            age = pose_age()
            if age is None:
                metrics.SAFETY_BLOCKED_TOTAL.labels(policy="stale_pose").inc()
                return step, "尚未收到任何位姿数据（/odom 未上报），拒绝下发移动动作"
            if age > stale_s:
                metrics.SAFETY_BLOCKED_TOTAL.labels(policy="stale_pose").inc()
                return step, (
                    f"位姿已过期 {age:.1f}s（阈值 {stale_s:.1f}s），"
                    "拒绝下发移动动作；请检查 /odom 话题与机器人驱动"
                )
        corrected, summary = gate.screen_step(step)
        return corrected, (None if summary.allowed else summary.reason)

    return check


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
    purger: PurgeScheduler | None = None
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

    registry, world = build_mock_registry(settings.arena, image_dir=settings.executor.image_dir)
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
    # 后端句柄同时管着「怎么动」和「现在在哪」：ROS2 下是 RosBridge（订阅 /odom），
    # mock 下是 MockWorld。把「位姿新鲜度」从后端抽出来交给安全层，而不是让安全层
    # 直接依赖 rclpy——安全规则不该为了跑单元测试而引入一个中间件。
    executor = StepExecutor(
        registry,
        settings.executor,
        pre_dispatch=_pre_dispatch(
            gate,
            pose_age=getattr(backend_handle, "pose_age_s", None),
            stale_s=settings.safety.pose_stale_s,
        ),
    )

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
        await repository.open()
        logger.info("storage_ready", url=settings.storage.url)
    elif settings.env != "test":
        logger.warning("storage_ephemeral", url=settings.storage.url)

    events = EventBus(max_subscribers_per_task=settings.api.max_sse_subscribers)
    device_lock = build_lock(settings.lock)
    if settings.lock.backend != "none":
        logger.info(
            "device_lock_ready",
            backend=settings.lock.backend,
            robot_id=settings.robot_id,
            ttl_s=settings.lock.ttl_s,
        )
    runner = TaskRunner(
        settings=settings,
        repository=repository,  # type: ignore[arg-type]
        events=events,
        audit=audit,
        graph=build_graph(components, approval_mode=settings.approval_mode),
        resume_graph=build_resume_graph(components, approval_mode=settings.approval_mode),
        executor=executor,
        device_lock=device_lock,
    )
    purger = PurgeScheduler(runner, settings.storage.purge_interval_s)

    # 逆序释放：清理协程必须先停，否则它会去用一个已经关掉的数据库
    closers: list[Callable[[], Any]] = [
        executor.close,
        repository.close,
        backend_close,
        purger.stop,
        device_lock.aclose,
    ]

    return Assembly(
        settings=settings,
        registry=registry,
        audit=audit,
        events=events,
        repository=repository,  # type: ignore[arg-type]
        components=components,
        runner=runner,
        purger=purger,
        backend_handle=backend_handle,
        _closers=closers,
    )
