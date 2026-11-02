# Changelog

格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循语义化版本。

## [1.0.0] - 2026-11-02

从演示脚本重构为可交付服务。**这是一次不兼容重构**：模块路径由 `src/*.py` 平铺改为
`robot_agent.*` 包，调用方需按新接口迁移。

### Added

- **分层包结构**：`planner` / `safety` / `skills` / `orchestration` / `storage` / `api`，
  依赖方向单向，可 `pip install -e .` 安装并暴露 `robot-agent` 命令。
- **配置系统**：pydantic-settings 分层配置（env > .env > YAML > 默认值），
  密钥统一 `SecretStr`，`prod` 环境带启动护栏（禁无鉴权对外监听/禁关审计/禁内存存储）。
- **异常体系**：统一 `code` / `http_status` / `retryable`，安全拒绝显式不可重试。
- **安全策略化**：6 条独立策略（白名单/红线词/边界/禁区/限速/审批），支持注入自定义策略；
  策略自身异常按拒绝处理（fail-closed）。
- **技能参数模型**：用 pydantic 替代字符串字典 schema，`extra="forbid"` 拒绝 LLM 幻觉参数。
- **LLM 规划器健壮性**：硬超时、指数退避重试、熔断器（closed/open/half_open）、
  输出逐层校验、token 与耗时指标；失败可配置降级到规则规划。
- **执行器可靠性**：单步超时、瞬时故障重试、超时后自动急停、按 `task_id` 隔离的事件回调。
- **任务生命周期**：幂等键、CAS 状态迁移、人工审批/驳回/取消、SQLite 持久化、过期清理。
- **事件总线**：线程安全投递（`call_soon_threadsafe`）、队列背压丢旧保新、终态哨兵。
- **审计日志 v2**：哈希链防篡改、大小轮转、崩溃后自动续链、`audit verify` 校验命令。
- **可观测性**：structlog JSON 日志（trace_id 上下文）、Prometheus 指标（未装则 no-op）。
- **CLI**：`run` / `serve` / `tasks list|show` / `audit verify|tail` / `doctor` / `config` / `version`。
- **测试**：116 个用例，覆盖策略边界、fail-closed、CAS 并发、哈希链篡改、CLI 退出码。
- **质量门禁**：ruff（含 bandit/async/bugbear 规则）+ ruff format + mypy strict-ish。

### Changed

- 规则规划器改为**括号感知分句**，修复坐标 `(1.0, 0.5)` 被逗号切断的问题。
- 方向目标点收缩 20% 边距，避免目标贴住物理围挡。
- 日志输出改到 **stderr**，stdout 留给结构化输出（`--json` 可被 `jq` 直接消费）。

### Fixed

- `datetime.UTC` 仅 3.11+ 可用，改用 `timezone.utc` 以支持 3.10。
- structlog 保留字段 `event` 作为 kwargs 导致 `TypeError`。
- CLI `standalone_mode=False` 时退出码被丢弃，导致失败任务返回 0。
- `.gitignore` 误排除 `docs/*.gif`，演示素材无法入库。

### Removed

- `src/audit.py` / `src/graph.py` / `src/planner.py` / `src/safety.py` / `src/skills/*`
  的平铺模块（迁移到 `src/robot_agent/` 对应子包）。
