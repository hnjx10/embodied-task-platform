# robot-agent

面向 ROS2 / Gazebo 机器人的 **LLM 任务编排服务**：把自然语言指令编译成受约束的技能序列，经安全闸门校验后驱动执行器完成任务，全过程可审计、可观测、可人工接管。

不是「跑一次 demo 的脚本」，而是按生产标准组织的服务：分层依赖注入、策略化安全模型、哈希链审计、结构化日志、Prometheus 指标、可插拔执行器，以及 160 个测试与 CI 门禁。

```
自然语言 ──► 规划 ──► 安全闸门 ──► 执行 ──► 核验 ──► 报告
             │         │           │        │
          LLM/规则   策略链      ROS2/mock  实测位姿
             │         │           │        │
             └─────────┴── 审计（哈希链）───┴── 指标
```

---

## 为什么这样设计

一个「能用自然语言指挥机器人」的系统，99% 的风险不在模型能不能理解指令，而在**理解错了之后会发生什么**。所以整个架构围绕一条原则展开：

> **宁可不动，不可误动。**

具体落到五个设计决策：

| 决策 | 原因 | 代码位置 |
|---|---|---|
| **安全策略与业务逻辑分离**，每条约束是独立策略 | 新增约束不改主流程；每条策略可单独测试；策略链顺序可审计 | `safety/policies.py` |
| **策略自身崩溃按「拒绝」处理**（fail-closed） | 安全组件故障时放行，等于把安全组件变成攻击面 | `safety/gate.py` |
| **部分解析的指令整条拒绝** | 只执行「半条指令」往往比不执行更危险 | `planner/rules.py` |
| **执行失败立即停止后续步骤** | 前一步没到位时，后续动作可能撞到未预期物体 | `orchestration/executor.py` |
| **安全拒绝永不可重试** | 否则重试就变成了试探策略边界的后门 | `errors.py`（`retryable=False`） |

---

## 快速开始

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,api]"

robot-agent doctor                                  # 自检：配置/执行器/审计目录
robot-agent run "把红色方块推到左边"                  # mock 执行器，无需 ROS
```

```
任务: task_122baa8a7cbd4366b7c4
指令: 把红色方块推到左边
规划来源: rules｜步数: 1
  [PASS] 0.push_cube (1ms, 1 次): red 方块已推至 (-1.20, 0.00)
  [PASS] 核验 red: red 距目标 0.00m（容差 0.25m）
结论: 任务成功
```

试试会被拦下的指令：

```bash
robot-agent run "把绿色方块推到 (9, 9)"      # 已拦截 — 超出场地边界 ±1.5m
robot-agent run "把蓝色方块推到 (1.3, 1.3)"  # 已拦截 — 位于禁区 'charging_dock' 内
robot-agent run "全速开到 (1.0, 1.0)"        # 自动降速至 0.22 m/s 后执行，修正入审计
```

### HTTP 控制面

```bash
robot-agent serve                                  # 默认 127.0.0.1:8080
open http://127.0.0.1:8080/docs                    # OpenAPI 交互文档

curl -s -X POST localhost:8080/v1/tasks \
  -H 'Content-Type: application/json' \
  -d '{"instruction":"把红色方块推到左边","wait":true}' | jq '.status, .outcomes'
```

实时进度用 SSE：`GET /v1/tasks/{id}/events`　｜　完整接口见 [docs/API.md](docs/API.md)

---

## 架构

```
                    ┌────────────────────────────────┐
   CLI (typer) ────►│  TaskRunner                    │  任务生命周期
   HTTP (fastapi)──►│  ├── asyncio 并发控制           │  · 幂等键
                    │  ├── 物理动作串行锁             │  · CAS 状态迁移
                    │  └── 事件总线（SSE/WS 就绪）    │  · 人工审批
                    └───────────────┬────────────────┘
                                    ▼
                    ┌────────────────────────────────┐
                    │  LangGraph 状态机               │
                    │  plan → screen → execute        │
                    │       → verify → report         │
                    └───────────────┬────────────────┘
              ┌─────────────────────┼─────────────────────┐
              ▼                     ▼                     ▼
      ┌──────────────┐      ┌──────────────┐      ┌──────────────┐
      │  Planner     │      │  SafetyGate  │      │  Executor    │
      │  LLM + 熔断   │      │  策略链       │      │  超时/重试/急停│
      │  规则兜底     │      │  fail-closed │      │  事件回调     │
      └──────────────┘      └──────────────┘      └───────┬──────┘
                                                           ▼
                                                 ┌──────────────────┐
                                                 │ ROS2 执行器/mock  │
                                                 │ 同一接口，可互换   │
                                                 └──────────────────┘
```

基础设施层：`config`（分层配置）· `logging`（structlog JSON）· `metrics`（Prometheus）· `audit`（哈希链）· `errors`（异常层级）· `storage`（SQLite / 内存）

### 目录结构

```
src/robot_agent/
├── cli.py                 # 命令行：run / serve / tasks / audit / doctor
├── factory.py             # 对象图装配（CLI、API、测试共用同一套）
├── config.py              # pydantic-settings 分层配置 + 生产护栏
├── errors.py              # 异常层级：code / http_status / retryable
├── logging.py             # structlog JSON + trace_id 上下文
├── metrics.py             # Prometheus 指标（未装则降级为 no-op）
├── audit.py               # 哈希链审计，轮转 + 篡改检测
├── planner/               # 规划层
│   ├── base.py            #   PlanStep / Plan（层间唯一契约）
│   ├── llm.py             #   LLM 规划：超时 + 重试 + 熔断 + 输出校验
│   ├── rules.py           #   规则兜底：括号感知分句、坐标解析
│   └── pipeline.py        #   降级链（LLM → 规则）
├── safety/                # 安全层
│   ├── policies.py        #   6 条独立策略
│   └── gate.py            #   策略链执行器
├── skills/                # 能力层
│   ├── base.py            #   Skill ABC + 注册表（pydantic 参数模型）
│   ├── mock.py            #   mock 执行器（确定性世界模型）
│   └── ros2.py            #   ROS2 执行器（cmd_vel + odom 闭环）
├── orchestration/         # 编排层
│   ├── graph.py           #   LangGraph 五阶段状态机
│   ├── executor.py        #   步骤执行：超时、重试、急停
│   ├── runner.py          #   任务生命周期与状态机
│   ├── events.py          #   事件总线（线程安全 + 背压）
│   └── state.py           #   TaskRecord 状态机
├── storage/               # 持久化：SQLite（CAS）/ 内存
└── api/                   # HTTP 控制面（FastAPI）

ros2_ws/src/robot_agent_ros2/     # ament_python 包：世界启动 + 方块生成
├── robot_agent_ros2/spawn_cubes.py
└── launch/demo.launch.py
```

---

## 配置

优先级：**构造参数 > 环境变量 > `.env` > YAML > 代码默认值**

```bash
export ROBOT_AGENT_SAFETY__MAX_LINEAR_SPEED=0.18   # 嵌套字段用双下划线
export ROBOT_AGENT_EXECUTOR__BACKEND=ros2
export ROBOT_AGENT_LLM__API_KEY=sk-xxx

# 也可以用 YAML（环境变量仍会覆盖它）
ROBOT_AGENT_CONFIG=config/production.yaml robot-agent run "把红色方块推到左边"
```

**生产护栏**：`ROBOT_AGENT_ENV=prod` 时，以下任一情况**直接拒绝启动**——审计被关闭、对外监听却没配 `auth_token`、使用内存存储。配置错误应该在启动时炸，而不是在事故后。

查看生效配置（密钥自动脱敏）：`robot-agent config --json`

---

## 安全模型

每次任务执行前，所有步骤必须通过策略链。任何一步被拒 → **整条计划不执行**。

| 策略 | 拦截内容 | 默认行为 |
|---|---|---|
| `skill_allowlist` | 未注册技能、白名单外技能（防 LLM 幻觉） | 拒绝 |
| `blocked_keyword` | 指令级红线词（"冲向"、"撞"…） | 拒绝 |
| `arena_bounds` | 目标点超出场地边界 | 拒绝 |
| `geofence` | 目标点落入圆形禁区（充电桩、人员通道） | 拒绝 |
| `speed_limit` | 线速度超硬件上限 | **自动降速**并记录修正 |
| `approval` | 高风险技能（配置或 `risk="high"`） | 挂起等待人工确认 |

三个容易被忽略的细节：

1. **降速 ≠ 拦截**：风险可控时保留任务可用性，但修正会写进审计与报告，不是悄悄改。
2. **审批后重新校验**：批准的计划在续跑时会**再跑一次策略链**——人工修改同样受约束，否则审批就成了绕过安全层的通道。
3. **超时先急停**：技能超时后立即发零速指令，再谈重试。Python 线程无法强杀，所以超时处理是「放弃等待 + 急停」，不是假设线程已死。

---

## 可观测性

**结构化日志**（JSON，含 `trace_id` / `task_id`，可直接进 Loki / ELK；日志走 stderr，stdout 留给结构化输出）：

```json
{"timestamp":"2026-11-02T05:50:43Z","level":"info","event":"step_start",
 "task_id":"task_122b...","trace_id":"43edb387...","index":0,
 "step":"push_cube(color=red, x=-1.2, y=0.0)","service":"robot-agent"}
```

**审计日志**（哈希链，可检测篡改）：

```bash
robot-agent audit verify    # ✓ 审计链完整（37 条记录）
robot-agent audit tail -n 5
```

每条记录带 `prev_hash`，任何中间条目被删改，校验立刻定位到断点。

**指标**（Prometheus）：

`robot_agent_tasks_total{status}` · `robot_agent_task_duration_seconds` · `robot_agent_steps_total{skill,outcome}` · `robot_agent_safety_blocked_total{policy}` · `robot_agent_llm_tokens_total{model,direction}` · `robot_agent_tasks_in_flight`

---

## ROS2 / Gazebo 接入

```bash
# Ubuntu 22.04 + ROS2 Humble
sudo apt install ros-humble-turtlebot3-gazebo ros-humble-gazebo-ros-pkgs
export TURTLEBOT3_MODEL=waffle

# 1) 编译仿真辅助包（世界启动 + 方块生成）
cd ros2_ws && colcon build --symlink-install && source install/setup.bash && cd ..

# 2) 启动世界与方块（launch 内置 5s 延时，等 Gazebo 就绪再 spawn，避免服务未起的竞态）
ros2 launch robot_agent_ros2 demo.launch.py

# 3) 切到 ROS2 执行器
export ROBOT_AGENT_EXECUTOR__BACKEND=ros2
robot-agent run "把红色方块推到左边"
```

方块坐标必须是**唯一事实源**：`spawn_cubes.py` 里的 `CUBES` 与 `config/default.yaml` 的 `arena.cube_layout` 必须一致，否则规划器会对着空气推箱子。详见 [ros2_ws/README.md](ros2_ws/README.md)。

`skills/ros2.py` 与 `skills/mock.py` 实现**同一组技能接口**，编排层只依赖 `SkillRegistry`（依赖倒置）。CI 无仿真环境也能跑完整链路，切到真机只需改一个环境变量。

---

## 测试与质量门禁

```bash
pytest                                              # 160 个用例
pytest --cov=robot_agent --cov-report=term-missing  # 覆盖率约 79%
ruff check src tests && ruff format --check src tests
mypy src
```

测试用**真实组件**装配（真 SQLite、真审计文件、真图），只把物理世界换成 mock——mock 掉自己写的业务代码，测试就退化成自我确认。

覆盖重点：策略链边界、fail-closed、CAS 并发审批、哈希链篡改检测、CLI 退出码契约、幂等、SSE 事件流、LLM 重试与熔断。

CI 四个 job：静态检查（ruff + mypy）→ 测试矩阵（3.10 / 3.11 / 3.12）→ ROS2 导入检查（无 rclpy 环境下必须可导入）→ 镜像构建。

---

## 部署

三种形态，按现场条件选（详见 [deploy/README.md](deploy/README.md)）：

```bash
# 容器
docker compose up -d

# systemd（ROS2 场景推荐：避免 DDS 跨网络问题）
sudo cp deploy/systemd/robot-agent.service /etc/systemd/system/ && sudo systemctl enable --now robot-agent

# Kubernetes
kubectl apply -f deploy/k8s/
```

生产启动护栏会在以下情况**直接拒绝启动**：关闭审计、对外监听却无 token、使用内存存储。

---

## 文档

| 文档 | 内容 |
|---|---|
| [docs/API.md](docs/API.md) | HTTP 接口、SSE 事件、错误码、客户端示例 |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | 分层与依赖方向、并发模型、扩展点、失败隔离 |
| [docs/OPERATIONS.md](docs/OPERATIONS.md) | 部署、告警阈值、故障决策树、审计合规、备份恢复 |
| [docs/adr/](docs/adr/) | 架构决策记录：为什么 fail-closed、为什么用 CAS |
| [ros2_ws/README.md](ros2_ws/README.md) | 仿真环境搭建、方块坐标一致性约定 |
| [SECURITY.md](SECURITY.md) | 信任边界、已知限制、加固清单 |
| [CONTRIBUTING.md](CONTRIBUTING.md) | 开发流程、如何新增技能/策略 |

---

## 已知限制

诚实记录，而不是假装没有：

- **审计链要求单写者**：多实例部署时每实例独立文件，或改造为集中式日志（见 `docs/OPERATIONS.md`）。
- **审批事件在内存总线**：跨进程审批需接 Redis Pub/Sub；任务本身已持久化在 SQLite，重启后仍可查询与续跑。
- **技能超时无法强杀线程**：只能放弃等待 + 急停。彻底解决需要把技能执行放到独立进程。
- **单机器人假设**：物理动作通过信号量串行化，多机器人需按 `robot_id` 分区调度。
- **规则规划器只覆盖常见句式**：复杂表达依赖 LLM，解析不了就明确失败，不做猜测性执行。

---

## Roadmap

- [x] HTTP 控制面（任务 CRUD、SSE 实时进度、OpenAPI 文档、审批流）
- [x] ROS2 ament 包（一键拉起世界与方块）
- [ ] MoveIt2 机械臂抓放（替换推箱）
- [ ] Isaac Sim 执行器
- [ ] OpenTelemetry 链路追踪
- [ ] 多机器人调度

## License

MIT
