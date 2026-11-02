# Changelog

格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循语义化版本。

## [Unreleased]

### Changed

- **项目更名为 `robot-agent`**（原 `gazebo-langgraph-demo`）。名称里不再带 `demo`：
  交付形态是服务而非示例脚本，`pyproject` 名称、CLI、镜像名、systemd 单元本就叫
  `robot-agent`，这次把仓库名与文档链接统一过来，`gazebo` 只作为可选执行器后端出现在
  `ros2_ws/` 里。旧仓库地址由 GitHub 自动重定向。
- **凭据不再硬编码、不落仓库**：发布工具改为按「环境变量 `GH_TOKEN` → 权限 600 的凭据文件」
  顺序取 token，任何输出前统一脱敏；同步前先扫描工作区，命中 token 字面量直接拒绝上传。
- **抽取纯领域层 `domain/`**（`plan.py` + `state.py`）：`PlanStep`/`Plan` 原在 `planner/base.py`、
  `TaskRecord` 原在 `orchestration/state.py`，导致 `safety → planner`、`storage → orchestration`
  两条反向依赖。契约对象下沉后，四个使用方（规划/安全/编排/存储）只依赖模型本身。
  同时删除死代码 `runner.run_once`（唯一一处把组合根反向 import 进编排层的地方）。
  `planner` 与 `orchestration` 的公开导出保持不变，调用方无需改动。

### Changed（交付层单一职责）

- **CLI 拆出渲染层** `console.py`：表格、JSON、错误行、审计列表等纯字符串函数从命令函数里移出，
  命令只表达「做什么」。顺手修掉中英混排时**表格错位**——原实现用 `len()` 对齐，
  中文占两列却按一列算；现在按终端显示宽度（East Asian Width）补空格与截断。
- **仓储打开语义收进存储层**：`TaskRepository` 协议新增 `open()`（建立连接/建表），
  `sqlite` 的 `init()` 更名对齐；新增 `storage.session.open_repository()` 上下文管理器。
  CLI 三个子命令原本各自抄一遍「build → isinstance 判断 → init → try/finally close」，
  还把 `SqliteTaskRepository` 的实例判断泄漏到交付层，现在只写 `async with open_repository(url)`。

### Added

- **分层约束门禁**（`tests/unit/test_layering.py`）：AST 静态分析全部源码，校验「包必须登记层号 /
  依赖只能向下 / 领域层零外部依赖 / 独立纵切不互依赖 / 只有交付层可碰组合根 / 包图无环」六条规则。
  违规报错精确到 `robot_agent.skills (L2) 反向依赖 robot_agent.orchestration (L4)`。
- **仓库级密钥扫描**（`tests/unit/test_no_secrets.py`）：对全部文本文件匹配 GitHub PAT /
  云厂商长期密钥 / 模型服务密钥 / 私钥块，随 `pytest` 在 CI 每次提交上执行。
  凭据一旦进入 git 历史就只能改写历史 + 吊销重发，这道门禁比 code review 更可靠。

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
- **HTTP 控制面**（FastAPI）：任务提交/查询/列表、审批/驳回/取消、SSE 实时事件、
  `/healthz` `/readyz` `/metrics`、Bearer 鉴权、X-Trace-Id 透传、OpenAPI 文档。
  纯 ASGI 中间件避免 SSE 被缓冲；异常统一映射为稳定错误码。
- **优雅关闭**：先等在飞任务收尾（默认 10s）再释放资源，避免任务状态卡在 running。
- **请求体上限**：纯 ASGI 中间件按字节数拦截（默认 64 KiB，超限 413 `payload_too_large`），
  同时校验声明长度与实际读取量，chunked 传输与伪造 `Content-Length` 都挡得住。
- **急停通道**：`POST /v1/robot/stop` 与 `robot-agent estop`——不绑任务、不排物理动作信号量，
  直接下发零速并写审计；执行器缺 `stop` 技能时返回 503 而不是假报成功。
  mock 后端补齐 `stop` 技能（速度归零 + 命令历史留痕），使急停可在无 ROS 环境下测试。
- **事件总线资源边界**：单任务 SSE 订阅数上限（默认 32，超限 429 `too_many_subscribers`）；
  「已结束任务」集合改为定长环形缓冲（原先只写不读，跑久了就是一张永不释放的表）；
  任务结束后才订阅的客户端立刻收到结束哨兵，不再白等。
- **过期清理真正被执行**：`PurgeScheduler` 随应用 lifespan 起停，按
  `storage.purge_interval_s`（默认 30 分钟）调用 `retention_hours` 清理。
  此前该配置只写在文档里没人调用，数据库实际会无限增长；单轮失败只记日志，不中断后续轮次。
- **容器化**：多阶段 Dockerfile（非 root、只读根文件系统、HEALTHCHECK）、docker-compose。
- **部署清单**：systemd unit（加固项齐全）、K8s Deployment/Service/PVC/ServiceMonitor。
- **文档**：API 参考、架构说明（含失败隔离矩阵）、运维 Runbook（故障决策树 + 告警阈值）、
  两份 ADR（fail-closed、状态 CAS）、SECURITY 信任边界、CONTRIBUTING 扩展指南。
- **测试**：244 个用例，覆盖策略边界、fail-closed、CAS 并发、哈希链篡改、CLI 退出码、
  LLM 重试/熔断/降级、HTTP 接口与 SSE、优雅关闭。
- **质量门禁**：ruff（含 bandit/async/bugbear 规则）+ ruff format + mypy + GitHub Actions
  （3.10/3.11/3.12 矩阵、ROS 降级导入检查、镜像构建与容器冒烟）。
- **CI 新增两道防线**：「最小依赖可导入」job 只装核心依赖验证 CLI 与指标 no-op 降级；
  「ROS2 包结构与语法」校验 `package.xml` / `resource` 标记 / launch 入口。
- **ROS2 包**：`ros2_ws/src/robot_agent_ros2` 采用标准 `ament_python` 结构，
  `ros2 launch robot_agent_ros2 demo.launch.py` 一条命令拉起世界与三个方块；
  `spawn_cubes` 由 `TimerAction` 延时 5s，规避 `/spawn_entity` 服务未就绪的竞态。
- `tests/` 分三层：`unit`（策略/规则/技能/审计/存储/LLM）、`integration`（图/API）、`e2e`（CLI）。
- `Makefile` 提供 install/test/check/run/doctor 等常用命令。
- `config/default.yaml` 与 `config/production.yaml` 双环境示例；`.env.example` 说明全部变量。
- `pre-commit` 钩子：ruff、mypy、单元测试（pre-push）。

### Changed

- 规则规划器改为**括号感知分句**，修复坐标 `(1.0, 0.5)` 被逗号切断的问题。
- 方向目标点收缩 20% 边距，避免目标贴住物理围挡。
- 日志输出改到 **stderr**，stdout 留给结构化输出（`--json` 可被 `jq` 直接消费）。
- 仿真辅助脚本迁入 `ros2_ws`，README 的接入步骤改为 `colcon build` + `ros2 launch`。

### Fixed

- **Python 3.10 下 `asyncio.wait_for` 超时捕获不到**：3.10 的 `asyncio.TimeoutError` 不是内置 `TimeoutError`，`except TimeoutError` 会漏接——清理协程会被一次正常超时打死，同步等待也会变成 500。统一改为 `except asyncio.TimeoutError`（3.11+ 两者为同一别名）。
- **幂等键复用会静默返回别人的任务**：同键不同指令现在返回 `409 task_conflict`，`details` 带上原指令与原任务 ID；比较忽略空白差异。
- **过期清理会误删未完成任务**：`purge_expired` 原来只按 `created_at` 判断，超过保留期的 `awaiting_approval` / `running` 任务会连同待办一起消失。现在只清终态。
- **`retention_hours` 从未被真正执行**：新增 `PurgeScheduler` 与 `robot-agent tasks purge`。
- **CI 三处失败**（44cefd7）：`typer` 未声明为依赖导致测试环境 collection 失败；
  `tests/unit/test_storage.py` 仍用 3.11+ 的 `datetime.UTC`；无 `prometheus-client` 时
  no-op 桩不吞构造参数，`Counter(...)` 直接 `TypeError` 把 mock 模式拖死。
- **依赖分层纠正**：`aiosqlite` / `pyyaml` 移入核心依赖（CLI 默认就要用），
  `fastapi` / `uvicorn` / `prometheus-client` 留在 `api` extra。
- **配置文件读不到不再静默忽略**：显式指定或自动发现的 YAML 缺失、非映射、缺 PyYAML
  时抛 `ConfigurationError`——否则限速与禁区策略没加载，人还以为生效了。
- `datetime.UTC` 仅 3.11+ 可用，改用 `timezone.utc` 以支持 3.10。
- structlog 保留字段 `event` 作为 kwargs 导致 `TypeError`。
- CLI `standalone_mode=False` 时退出码被丢弃，导致失败任务返回 0。
- `.gitignore` 误排除 `docs/*.gif`，演示素材无法入库。

### Removed

- `src/audit.py` / `src/graph.py` / `src/planner.py` / `src/safety.py` / `src/skills/*`
  的平铺模块（迁移到 `src/robot_agent/` 对应子包）。
- 根目录 `launch/demo.launch.py` 与 `scripts/spawn_cubes.py`（迁入 `ros2_ws` 的 ament 包）。
- 根目录 `scripts/demo.py`（已被 `robot-agent run` 取代，且引用了已删除的模块）。
