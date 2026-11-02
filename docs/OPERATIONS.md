# 运维手册（Runbook）

面向值班运维。目标：**出问题时能在 5 分钟内定位到是「模型/安全/执行器/存储」哪一层**。

---

## 1. 部署

### 单机（systemd）

```bash
sudo useradd -r -s /usr/sbin/nologin robot-agent
sudo mkdir -p /opt/robot-agent /var/lib/robot-agent/audit
sudo chown -R robot-agent:robot-agent /var/lib/robot-agent

python3.12 -m venv /opt/robot-agent/venv
/opt/robot-agent/venv/bin/pip install "robot-agent[api]"

sudo cp deploy/systemd/robot-agent.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now robot-agent
sudo systemctl status robot-agent
```

### 容器

```bash
docker build -t robot-agent:1.0.0 .
docker compose up -d
docker compose logs -f --tail=100
```

### ROS2 场景

执行器需要与 Gazebo 同域通信。容器化时用 `network_mode: host` 并挂载 ROS 环境；
或直接在宿主机以 systemd 运行（推荐，避免 DDS 跨网络发现的问题）。

```ini
# 关键环境变量
Environment=ROBOT_AGENT_EXECUTOR__BACKEND=ros2
Environment=ROBOT_AGENT_EXECUTOR__ROS_DOMAIN_ID=42
Environment=TURTLEBOT3_MODEL=waffle
```

---

## 2. 启动前自检

```bash
robot-agent doctor
```

输出示例：

```
✓ 配置加载成功
  env=prod backend=ros2 storage=sqlite:////var/lib/robot-agent/tasks.db
✓ 执行器就绪，技能: get_pose, goto_point, push_cube, stop
✓ LLM 已配置：qwen-plus
✓ 审计目录可写: /var/lib/robot-agent/audit
```

任一项 ✗ 就不要启动。生产环境的配置错误应该在启动时暴露，而不是在事故后。

---

## 3. 关键指标与告警阈值

| 指标 | 含义 | 建议告警 |
|---|---|---|
| `robot_agent_safety_blocked_total{policy}` | 被拦截的步数 | 5 分钟内某策略突增 → 可能有人试探边界，或模型在幻觉 |
| `robot_agent_tasks_total{status="failed"}` | 失败任务 | 5 分钟内 > 3 次 |
| `robot_agent_steps_total{outcome="rejected"}` | 技能返回失败 | 突增说明场景/硬件异常 |
| `robot_agent_task_duration_seconds_p99` | 任务耗时 | > 120s 说明导航卡住 |
| `robot_agent_llm_requests_total{outcome!="ok"}` | LLM 异常 | 连续失败 → 熔断打开，已自动降级到规则 |
| `robot_agent_audit_write_errors_total` | 审计写入失败 | **> 0 立即告警**（合规风险） |
| `robot_agent_tasks_in_flight` | 在飞任务 | 持续 > 0 但无新完成 → 卡死 |
| `robot_agent_event_subscriber_rejected_total` | SSE 订阅被拒次数 | 突增 → 客户端在狂开连接，按 `max_sse_subscribers` 治理 |

Prometheus 抓取配置：

```yaml
scrape_configs:
  - job_name: robot-agent
    static_configs:
      - targets: ["robot-agent:8080"]
    metrics_path: /metrics
```

---

## 4. 故障排查决策树

### 任务 `blocked`

不是故障，是**安全层按设计工作**。

```bash
robot-agent tasks show <task_id> | jq '.report, .corrections'
robot-agent audit tail -n 20 | grep blocked
```

- `policy=arena_bounds` → 坐标越界，检查指令或场地配置 `arena.half_size`
- `policy=geofence` → 目标落在禁区，确认 `safety.geofences` 是否符合现场
- `policy=skill_allowlist` → LLM 幻觉出未注册技能，看 `plan.warnings`
- `policy=blocked_keyword` → 指令含红线词

**不要为了「让任务跑通」而放宽策略**。先确认是配置错了还是指令错了。

### 任务 `failed` 且 `error.code=planning_error`

规则解析器覆盖不了这句话，且 LLM 未配置或失败。

```bash
robot-agent config --json | jq '.llm'
robot-agent audit tail | grep planning_fallback_to_rules
```

### 任务卡在 `running`

```bash
robot-agent tasks show <id> | jq '.started_at, .updated_at'
```

- 检查 `robot_agent_steps_total` 是否还在增长；
- 检查 Gazebo 是否还活着：`ros2 topic hz /odom`；
- 若确认卡死：**先急停**，再重启服务。

```bash
robot-agent estop --reason "任务卡死，现场确认"        # 或 POST /v1/robot/stop
```

急停不绑任务、不排队，直接下发零速并写一条 `emergency_stop` 审计（含原因）。
它只消除动势、不改任务状态：卡住的任务仍然停在 `running`，需要人工改状态或等过期清理——
**不要**为了让面板好看而把它改成 `cancelled`，那会污染事后复盘。

### 机器人不停 / 急停无效

按顺序排查，每一步都要确认「指令真的到了硬件」，而不是「接口返回了 200」：

1. `robot-agent estop --reason "..."` 的返回码：非零说明 **没有** `stop` 技能被注册（HTTP 侧为 503），
   此时问题在装配而不是网络；
2. `ros2 topic echo /cmd_vel --once` 看是否收到 `linear.x == 0`；没有的话查 DDS 域/网段；
3. 若节点仍在发布旧速度，说明有第二个发布者：`ros2 node list`、`ros2 topic info /cmd_vel -v`；
4. 以上都正常仍不停车 → 现场物理急停按钮，服务端问题之后再查。

`robot_agent_event_subscriber_rejected_total` 持续增长说明有客户端在狂开 SSE 连接，
它们不会影响停车，但会掩盖真实告警——按 `max_sse_subscribers` 调参或查客户端。

### `readyz` 返回 503

```bash
curl -s localhost:8080/readyz | jq '.checks'
```

| 失败项 | 原因 | 处理 |
|---|---|---|
| `storage.ok=false` | SQLite 打不开/磁盘满 | `df -h`，检查文件权限 |
| `executor.skills=[]` | 白名单配置错误或注册失败 | 看启动日志 `backend_ready` |
| `audit.ok=false` | 审计目录不存在 | `mkdir -p` 并检查属主 |

### LLM 熔断

日志出现 `llm_circuit_opened` 后，规划自动降级到规则解析（服务不中断）。

```bash
robot-agent audit tail | grep -c planning_fallback  # 降级次数
```

冷却 60s 后自动半开试探。若持续失败，检查网关配额与 `base_url`。

---

## 5. 审计合规

**为什么审计是硬要求**：机器人执行的是物理动作，事后追责必须有不可否认的记录。

```bash
# 校验完整性（篡改会定位到具体行）
robot-agent audit verify

# 导出某任务的全部事件
robot-agent audit tail -n 1000 | jq 'select(.task_id=="task_xxx")'
```

每条记录形如：

```json
{"v":"1","seq":42,"ts":"2026-11-02T05:50:43.427Z","event":"executed",
 "task_id":"task_122b...","trace_id":"43edb387...","data":{"skill":"push_cube"},
 "prev_hash":"a1b2…","hash":"9f8e…"}
```

**单写者约束**：哈希链要求串行写入。多实例部署时必须每实例独立文件：

```bash
ROBOT_AGENT_AUDIT__PATH=/var/lib/robot-agent/audit/$(hostname).jsonl
```

需要集中式审计时，用 Filebeat/Promtail 采集 JSONL 到 Loki/ES —— 采集是只读的，
不影响链完整性；校验在原始文件上做。

**保留期**：生产配置为 720 小时（30 天）。轮转保留 5 份，单文件上限 32 MiB。
更长的保留期应在采集端（Loki/ES）做，而不是留在一台机器上。

---

## 6. 备份与恢复

| 数据 | 位置 | 频率 | 方式 |
|---|---|---|---|
| 任务库 | `/var/lib/robot-agent/tasks.db` | 每日 | `sqlite3 tasks.db ".backup /backup/tasks-$(date +%F).db"` |
| 审计日志 | `/var/lib/robot-agent/audit/*.jsonl` | 每日 | 冷拷贝（写完后 `rotate` 再拷，避免半条记录） |

恢复：停服务 → 覆盖文件 → 启服务 → `robot-agent audit verify`。

> 不要用 `cp` 拷正在写的 SQLite 文件，WAL 模式下会得到不一致快照。

---

## 7. 升级与回滚

```bash
# 升级
systemctl stop robot-agent
/opt/robot-agent/venv/bin/pip install --upgrade "robot-agent[api]==1.1.0"
systemctl start robot-agent
robot-agent doctor && curl -fsS localhost:8080/readyz | jq .status

# 回滚
systemctl stop robot-agent
/opt/robot-agent/venv/bin/pip install --force-reinstall "robot-agent[api]==1.0.0"
systemctl start robot-agent
```

数据库 schema 目前只有 `tasks` 单表且 payload 为 JSON，向后兼容；
破坏性变更会通过 `STATE_SCHEMA_VERSION` 标记，并在 CHANGELOG 的 **Changed** 段说明迁移步骤。

---

## 8. 值班检查清单

**每日**

- [ ] `robot-agent audit verify` 通过
- [ ] `robot_agent_audit_write_errors_total` 为 0
- [ ] 失败任务数与昨日对比无异常
- [ ] 磁盘剩余 > 20%

**每周**

- [ ] 抽查 3 个 `blocked` 任务，确认拦截合理
- [ ] 检查 `planning_fallback` 比例（LLM 健康度）
- [ ] 清理过期任务（服务自动按 `retention_hours` 清理，确认生效）

**变更前后**

- [ ] 变更前导出审计校验基线
- [ ] 变更后跑一遍 `robot-agent run "把红色方块推到左边"` 冒烟
