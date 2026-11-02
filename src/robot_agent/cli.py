"""命令行入口。

CLI 的定位是「运维与人机共驾的接口」，不只是跑一次 demo：

* ``run``   提交一次任务，默认交互式审批高风险动作
* ``serve`` 启动 HTTP 控制面
* ``tasks`` 查看历史任务（读同一份存储）
* ``audit`` 校验哈希链 / 查看最近事件——出事后第一时间要能自证清白
* ``doctor`` 启动前自检：配置是否合法、执行器是否可用、审计目录是否可写
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import timedelta
from pathlib import Path
from typing import Annotated, Any

import typer

from robot_agent import console, metrics
from robot_agent.audit import tail_entries
from robot_agent.config import Settings, load_settings
from robot_agent.domain.state import utcnow
from robot_agent.errors import ExecutorUnavailableError, RobotAgentError
from robot_agent.logging import configure_logging, request_context
from robot_agent.storage.session import open_repository
from robot_agent.version import __version__

app = typer.Typer(
    name="robot-agent",
    help="LLM 驱动的机器人任务编排服务：规划 → 安全校验 → 执行 → 核验 → 报告",
    no_args_is_help=True,
    add_completion=False,
)
tasks_app = typer.Typer(help="任务查询", no_args_is_help=True)
audit_app = typer.Typer(help="审计日志", no_args_is_help=True)
app.add_typer(tasks_app, name="tasks")
app.add_typer(audit_app, name="audit")


# --------------------------------------------------------------------- 公共


def _settings(
    mock: bool = False,
    ros: bool = False,
    log_level: str | None = None,
    json_logs: bool | None = None,
    env: str | None = None,
) -> Settings:
    overrides: dict[str, Any] = {}
    if mock:
        overrides["executor"] = {"backend": "mock"}
    if ros:
        overrides["executor"] = {"backend": "ros2"}
    if env:
        overrides["env"] = env
    try:
        settings = load_settings(**overrides)
    except RobotAgentError as exc:
        typer.secho(f"配置错误: {exc.message}", fg=typer.colors.RED, err=True)
        raise typer.Exit(2) from exc
    configure_logging(
        log_level or settings.log_level,
        json_output=settings.log_json if json_logs is None else json_logs,
        service=settings.service_name,
        env=settings.env,
    )
    return settings


def _fail(exc: Exception, as_json: bool = False) -> None:
    """统一的失败出口：机读走 stdout 的 JSON，人读走 stderr 的彩色行。"""
    if isinstance(exc, RobotAgentError):
        if as_json:
            typer.echo(console.error_json(exc))
        else:
            typer.secho(console.error_line(exc), fg=typer.colors.RED, err=True)
    else:
        typer.secho(f"未预期错误: {exc}", fg=typer.colors.RED, err=True)
        if as_json:
            typer.echo(console.unknown_error_json(exc))
    raise typer.Exit(1) from exc


# --------------------------------------------------------------------- run


@app.command()
def run(
    instruction: Annotated[str, typer.Argument(help="自然语言指令")],
    mock: Annotated[bool, typer.Option("--mock", help="使用 mock 执行器（无需 ROS）")] = True,
    ros: Annotated[bool, typer.Option("--ros", help="使用 ROS2 执行器")] = False,
    approve: Annotated[
        bool, typer.Option("--approve/--no-approve", help="高风险动作是否交互式确认")
    ] = True,
    force_rules: Annotated[bool, typer.Option("--rules", help="强制使用规则规划器")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="以 JSON 输出结果")] = False,
    log_level: Annotated[str | None, typer.Option("--log-level")] = None,
) -> None:
    """规划并执行一条指令。"""
    settings = _settings(mock=mock, ros=ros, log_level=log_level, json_logs=False)
    try:
        code = asyncio.run(_run_async(settings, instruction, approve, force_rules, as_json))
    except RobotAgentError as exc:
        _fail(exc, as_json)
        return
    raise typer.Exit(code)


async def _run_async(
    settings: Settings, instruction: str, approve: bool, force_rules: bool, as_json: bool
) -> int:
    from robot_agent.factory import build_assembly

    assembly = await build_assembly(settings, force_rules=force_rules)
    try:
        with request_context() as trace_id:
            task = await assembly.runner.submit(instruction, trace_id=trace_id)
            record = await assembly.runner.execute(task.id)

            if record.status == "awaiting_approval" and approve:
                if not as_json:
                    typer.secho("\n" + console.plan_preview(record), fg=typer.colors.YELLOW)
                    confirmed = typer.confirm("是否批准执行？", default=False)
                else:
                    confirmed = False
                if confirmed:
                    record = await assembly.runner.approve(task.id, approved_by="cli-operator")
                else:
                    record = await assembly.runner.reject(task.id, reason="操作员拒绝")

            if as_json:
                typer.echo(console.task_json(record))
            else:
                typer.echo("\n" + console.task_report(record))
            if record.error:
                typer.secho(f"错误: {record.error}", fg=typer.colors.RED, err=True)
            return 0 if record.status == "succeeded" else 1
    finally:
        await assembly.aclose()


# --------------------------------------------------------------------- eval


@app.command("eval")
def evaluate(
    cases: Annotated[
        Path | None, typer.Option("--cases", "-c", help="评测集路径，默认用包内数据集")
    ] = None,
    as_json: Annotated[bool, typer.Option("--json", help="输出机读 JSON")] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="打印失败用例的输入与原因")] = (
        False
    ),
    fail_under: Annotated[
        float, typer.Option("--fail-under", help="通过率低于该阈值时返回非 0（用于 CI）")
    ] = 0.0,
    log_level: Annotated[
        str | None, typer.Option("--log-level", help="默认 error，避免拦截日志刷屏")
    ] = None,
) -> None:
    """跑安全评测集：拦截召回率、误拦率、分类明细。

    只做「规划 + 安全判定」，不执行任何技能——评测的是该不该动，不是动得对不对。
    因此不需要 ROS、不需要 Gazebo，毫秒级跑完，可以放进每次提交的 CI。
    """
    from robot_agent.evaluation import load_suite, run_suite
    from robot_agent.factory import build_skill_backend
    from robot_agent.planner.pipeline import Planner
    from robot_agent.safety.gate import SafetyGate

    settings = _settings(log_level=log_level or "error", json_logs=False)
    try:
        suite = load_suite(cases)
    except RobotAgentError as exc:
        _fail(exc, as_json)
        return

    registry, _handle, closer = build_skill_backend(settings)
    try:
        planner = Planner(settings, registry, force_rules=True)
        gate = SafetyGate(settings.safety, settings.arena, registry)
        report = run_suite(suite, planner, gate)
    finally:
        closer()

    typer.echo(
        console.eval_json(report) if as_json else console.eval_report(report, verbose=verbose)
    )
    passed = report.ok and report.pass_rate >= fail_under
    raise typer.Exit(0 if passed else 1)


# --------------------------------------------------------------------- serve


@app.command()
def serve(
    host: Annotated[str | None, typer.Option("--host")] = None,
    port: Annotated[int | None, typer.Option("--port")] = None,
    reload: Annotated[bool, typer.Option("--reload", help="开发模式热重载")] = False,
) -> None:
    """启动 HTTP 控制面（需要安装 api 依赖）。"""
    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover
        typer.secho("未安装 api 依赖，请执行 pip install 'robot-agent[api]'", fg=typer.colors.RED)
        raise typer.Exit(2) from exc

    settings = _settings(json_logs=True)
    from robot_agent.api.app import create_app

    uvicorn.run(
        create_app(settings),
        host=host or settings.api.host,
        port=port or settings.api.port,
        reload=reload,
        log_config=None,  # 复用 structlog 配置，避免 uvicorn 覆盖成纯文本
        access_log=False,
    )


# --------------------------------------------------------------------- tasks


@tasks_app.command("list")
def tasks_list(
    status: Annotated[str | None, typer.Option("--status", help="按状态过滤")] = None,
    limit: Annotated[int, typer.Option("--limit")] = 20,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """列出历史任务。"""
    settings = _settings(json_logs=False)
    records = asyncio.run(_list_tasks(settings, status, limit))
    typer.echo(console.task_list_json(records) if as_json else console.task_table(records))


@tasks_app.command("purge")
def tasks_purge(
    dry_run: Annotated[bool, typer.Option("--dry-run", help="只统计不删除")] = False,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """按保留期清理过期任务（只清终态）。服务运行时的周期清理不受此命令影响。"""
    settings = _settings(json_logs=False)
    retention = settings.storage.retention_hours

    async def _purge() -> int:
        async with open_repository(settings.storage.url) as repo:
            if dry_run:
                cutoff = utcnow() - timedelta(hours=retention)
                expired = await repo.list(status=None, limit=1000)
                return sum(1 for r in expired if r.is_terminal and r.created_at < cutoff)
            return await repo.purge_expired(retention)

    count = asyncio.run(_purge())
    if as_json:
        payload = {"retention_hours": retention, "dry_run": dry_run, "count": count}
        typer.echo(json.dumps(payload, ensure_ascii=False))
    else:
        typer.echo(console.purge_message(count, retention_hours=retention, dry_run=dry_run))


async def _list_tasks(settings: Settings, status: str | None, limit: int) -> list[Any]:
    async with open_repository(settings.storage.url) as repo:
        return await repo.list(status=status, limit=limit)


@tasks_app.command("show")
def tasks_show(task_id: str) -> None:
    """查看单个任务详情。"""
    settings = _settings(json_logs=False)

    async def _get() -> Any:
        async with open_repository(settings.storage.url) as repo:
            return await repo.get(task_id)

    record = asyncio.run(_get())
    if record is None:
        typer.secho(f"任务不存在: {task_id}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    typer.echo(console.task_json(record))


# --------------------------------------------------------------------- audit


@audit_app.command("verify")
def audit_verify(
    path: Annotated[Path | None, typer.Option("--path", help="审计文件路径")] = None,
) -> None:
    """校验审计日志哈希链完整性。"""
    settings = _settings(json_logs=False)
    from robot_agent.audit import AuditLog
    from robot_agent.config import AuditSettings

    target = path or settings.audit.path
    log = AuditLog(AuditSettings(path=target, enabled=True))
    result = log.verify_chain()
    if result.ok:
        typer.secho(f"✓ 审计链完整（{result.entries} 条记录）", fg=typer.colors.GREEN)
        return
    typer.secho(
        f"✗ 审计链在 {result.broken_at} 处断裂: {result.reason}", fg=typer.colors.RED, err=True
    )
    raise typer.Exit(1)


@audit_app.command("tail")
def audit_tail(limit: Annotated[int, typer.Option("--limit")] = 20) -> None:
    """查看最近的审计事件。"""
    settings = _settings(json_logs=False)
    entries = tail_entries(settings.audit.path, limit)
    typer.echo(console.audit_table(entries, path=str(settings.audit.path)))


# --------------------------------------------------------------------- 其它


@app.command()
def estop(
    reason: Annotated[str, typer.Option("--reason", "-r", help="急停原因（会写入审计）")] = (
        "cli emergency stop"
    ),
    ros: Annotated[bool, typer.Option("--ros", help="显式使用 ROS2 执行器")] = False,
    mock: Annotated[bool, typer.Option("--mock", help="显式使用 mock 执行器")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="以 JSON 输出结果")] = False,
) -> None:
    """立即急停（尽力而为）。

    刻意做成不需要 task_id、也不排队的独立命令：等拿到任务 ID 或排队进物理动作
    信号量，事故已经发生了。它绕过了编排层，因此这里必须自己写审计。
    """
    settings = _settings(mock=mock or not ros, ros=ros, json_logs=False)
    try:
        code = asyncio.run(_estop_async(settings, reason, as_json))
    except RobotAgentError as exc:
        _fail(exc, as_json)
        return
    raise typer.Exit(code)


async def _estop_async(settings: Settings, reason: str, as_json: bool) -> int:
    from robot_agent.factory import build_assembly

    assembly = await build_assembly(settings)
    try:
        if not assembly.registry.has("stop"):
            raise ExecutorUnavailableError(
                "当前执行器没有 stop 技能，急停指令未下发",
                details={"backend": settings.executor.backend},
            )
        assembly.components.executor.emergency_stop(reason=reason)
        assembly.audit.record("emergency_stop", {"reason": reason, "via": "cli"})
        if as_json:
            payload = {"ok": True, "stopped": True, "reason": reason}
            typer.echo(json.dumps(payload, ensure_ascii=False))
        else:
            typer.secho(f"已急停（{reason}）", fg=typer.colors.YELLOW)
        return 0
    finally:
        await assembly.aclose()


@app.command()
def doctor() -> None:
    """启动前自检：配置、执行器、存储、审计目录。"""
    ok = True
    try:
        settings = load_settings()
        configure_logging(settings.log_level, json_output=False, service=settings.service_name)
        typer.secho("✓ 配置加载成功", fg=typer.colors.GREEN)
    except RobotAgentError as exc:
        typer.secho(f"✗ 配置非法: {exc.message}", fg=typer.colors.RED)
        raise typer.Exit(2) from exc

    typer.echo(
        f"  env={settings.env} backend={settings.executor.backend} storage={settings.storage.url}"
    )

    try:
        from robot_agent.factory import build_skill_backend

        registry, _handle, closer = build_skill_backend(settings)
        typer.secho(f"✓ 执行器就绪，技能: {', '.join(registry.names())}", fg=typer.colors.GREEN)
        closer()
    except RobotAgentError as exc:
        ok = False
        typer.secho(f"✗ 执行器不可用: {exc.message}", fg=typer.colors.RED)

    if settings.llm.configured:
        typer.secho(f"✓ LLM 已配置：{settings.llm.model}", fg=typer.colors.GREEN)
    else:
        typer.secho("· LLM 未配置，将使用规则规划（不影响基础功能）", fg=typer.colors.YELLOW)

    audit_dir = settings.audit.path.parent
    try:
        audit_dir.mkdir(parents=True, exist_ok=True)
        probe = audit_dir / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        typer.secho(f"✓ 审计目录可写: {audit_dir}", fg=typer.colors.GREEN)
    except OSError as exc:
        ok = False
        typer.secho(f"✗ 审计目录不可写: {exc}", fg=typer.colors.RED)

    if not metrics.PROMETHEUS_AVAILABLE:
        typer.secho("· prometheus-client 未安装，/metrics 将为空", fg=typer.colors.YELLOW)

    raise typer.Exit(0 if ok else 1)


@app.command("config")
def show_config(as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """打印生效配置（密钥脱敏）。"""
    settings = load_settings()
    typer.echo(json.dumps(settings.safe_dump(), ensure_ascii=False, indent=2))


@app.command()
def version() -> None:
    """打印版本号。"""
    typer.echo(__version__)


@app.command()
def metrics_text() -> None:
    """打印当前进程的 Prometheus 指标（排障用）。"""
    payload, _ = metrics.render_metrics()
    typer.echo(payload.decode("utf-8"))


def main(argv: list[str] | None = None) -> int:
    """setuptools 入口。

    ``standalone_mode=False`` 时 click 会把 ``Exit`` 的退出码**作为返回值**给出，
    所以必须接住 ``result``——否则 `robot-agent run` 永远返回 0，CI 里
    「任务失败」会被当成成功，这是最容易被忽略也最危险的一类 bug。
    """
    try:
        result = app(args=argv, standalone_mode=False)
    except typer.Exit as exc:
        return int(exc.exit_code)
    except SystemExit as exc:  # pragma: no cover
        return int(exc.code or 0)
    return int(result) if isinstance(result, int) else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
