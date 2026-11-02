# HTTP API

基地址：`http://<host>:8080`　｜　交互文档：`/docs`　｜　OpenAPI：`/openapi.json`

## 鉴权

配置了 `ROBOT_AGENT_API__AUTH_TOKEN` 时，所有 `/v1/*` 需要：

```
Authorization: Bearer <token>
```

未配置时仅允许本机访问（`prod` 环境启动校验会强制要求 token，见 `config.py`）。
token 比较使用 `secrets.compare_digest`，避免时序侧信道。

## 追踪

每个响应都带 `X-Trace-Id`；请求可自带同名头以串联上下游链路。日志里同名字段可用于检索。

---

## 提交任务

`POST /v1/tasks`

```json
{
  "instruction": "把红色方块推到左边",
  "wait": false,
  "idempotency_key": "order-20261102-001"
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `instruction` | string | 必填，1–500 字符 |
| `wait` | bool | `true` 同步等到终态（受 `api.request_timeout_s` 限制）；默认 `false` 立即返回 202 |
| `idempotency_key` | string? | 幂等键，相同键不会重复执行 |

幂等键也可放在请求头：`Idempotency-Key: order-...`

**响应 202**

```json
{
  "id": "task_122baa8a7cbd4366b7c4",
  "instruction": "把红色方块推到左边",
  "status": "succeeded",
  "plan": [{"skill": "push_cube", "args": {"color": "red", "x": -1.2, "y": 0.0}}],
  "plan_source": "rules",
  "outcomes": [{"index": 0, "skill": "push_cube", "outcome": "ok", "latency_ms": 1.2,
                "attempts": 1, "detail": "red 方块已推至 (-1.20, 0.00)"}],
  "verifications": [{"target": "red", "ok": true, "distance_m": 0.0, "tolerance_m": 0.25}],
  "corrections": [{"index": 0, "policy": "speed_limit", "clamped_from": 0.5, "clamped_to": 0.22}],
  "report": "…",
  "error": null
}
```

`status` 取值：`queued` / `running` / `awaiting_approval` / `succeeded` / `failed` / `blocked` / `cancelled`

> `blocked` 与 `failed` 的区别：`blocked` 是**安全闸门拒绝**（没动），`failed` 是**执行或规划失败**（可能动了一半）。告警策略应当区别对待。

## 查询任务

```
GET /v1/tasks/{task_id}
GET /v1/tasks?status=blocked&limit=20&offset=0
```

## 订阅事件（SSE）

```
GET /v1/tasks/{task_id}/events
Accept: text/event-stream
```

```
event: status
data: {"status":"running","instruction":"…"}

event: step
data: {"index":0,"skill":"push_cube","outcome":"ok","latency_ms":1.2}

event: status
data: {"status":"succeeded","report":"…"}

event: end
data: {"status":"closed"}
```

- 订阅时先补一条当前状态快照（`snapshot: true`），避免事件已发生导致客户端空等；
- 每 15s 发一次 `: keep-alive` 注释行，防止中间代理断流；
- 任务到终态后发送 `event: end` 并关闭连接。

nginx 需要 `X-Accel-Buffering: no`（本服务已设置）或 `proxy_buffering off;`。

## 审批

```
POST /v1/tasks/{id}/approve   {"approved_by": "zhang", "note": "现场已清空"}
POST /v1/tasks/{id}/reject    {"reason": "现场有人"}
POST /v1/tasks/{id}/cancel    {"reason": "计划变更"}
```

审批是 CAS 的：并发批准只有一个成功，另一个得到 `409 task_conflict`。

批准后**会重新跑一次策略链**（人工修改同样受约束）；驳回直接把任务置为 `blocked`。

> 执行中的任务不能被 `cancel` 静默打断——那是急停的职责，不是取消。此时返回 409。

## 运维端点

| 端点 | 用途 | 说明 |
|---|---|---|
| `GET /healthz` | 存活探针 | 只反映进程状态，**不查外部依赖** |
| `GET /readyz` | 就绪探针 | 检查存储 / 执行器 / 审计目录，失败返回 503 |
| `GET /version` | 版本 | |
| `GET /metrics` | Prometheus | 未装 `prometheus-client` 时返回空 |

```bash
curl -s localhost:8080/readyz | jq
```

## 错误格式

```json
{
  "error": {"code": "safety_violation", "message": "目标点 (9.00, 9.00) 超出场地边界 ±1.5m",
            "details": {"policy": "arena_bounds", "step": "push_cube(color=green, x=9.0, y=9.0)"}},
  "trace_id": "99f2d398b8a543b9"
}
```

| HTTP | code | 场景 |
|---|---|---|
| 401 | `unauthorized` | 缺 token / token 无效 |
| 404 | `task_not_found` | 任务不存在 |
| 409 | `task_conflict` | 状态已变更（重复审批、取消执行中任务） |
| 422 | `validation_error` | 请求体校验失败 |
| 422 | `planning_error` | 指令无法解析 |
| 422 | `safety_violation` | 安全闸门拒绝 |
| 500 | `internal_error` | 未预期错误（只回 trace_id，不回内部细节） |
| 502 | `llm_error` | LLM 上游异常 |
| 503 | `executor_unavailable` | 执行器不可用 |

## 客户端示例

```bash
# 提交并同步等结果
curl -s -X POST localhost:8080/v1/tasks \
  -H 'Content-Type: application/json' \
  -H 'X-Trace-Id: my-trace-001' \
  -d '{"instruction":"把红色方块推到左边","wait":true}' | jq '.status'

# 异步 + 订阅
TASK=$(curl -s -X POST localhost:8080/v1/tasks \
  -H 'Content-Type: application/json' \
  -d '{"instruction":"把红色方块推到左边"}' | jq -r .id)
curl -N localhost:8080/v1/tasks/$TASK/events
```

```python
import httpx

with httpx.Client(base_url="http://localhost:8080", timeout=60) as client:
    task = client.post("/v1/tasks", json={"instruction": "把红色方块推到左边"}).json()
    for _ in range(100):
        detail = client.get(f"/v1/tasks/{task['id']}").json()
        if detail["status"] in {"succeeded", "failed", "blocked"}:
            break
    assert detail["status"] == "succeeded"
```
