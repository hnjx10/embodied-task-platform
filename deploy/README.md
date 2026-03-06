# 部署

三种形态，按现场条件选：

| 形态 | 适用 | 目录 |
|---|---|---|
| systemd | 宿主机直跑，**ROS2 场景推荐**（避免 DDS 跨网络问题） | `systemd/` |
| Docker Compose | 单机容器化，执行器用 mock 或远程 ROS | 仓库根 `docker-compose.yml` |
| Kubernetes | 有集群、执行器为 mock/HTTP 代理 | `k8s/` |

## 顺序

```bash
# systemd
sudo cp systemd/robot-agent.service /etc/systemd/system/
sudo install -d -o robot-agent -g robot-agent /var/lib/robot-agent/audit
sudo systemctl daemon-reload && sudo systemctl enable --now robot-agent

# k8s
kubectl create secret generic robot-agent-secrets \
  --from-literal=api-auth-token="$(openssl rand -hex 24)" \
  --from-literal=llm-api-key="sk-xxx"
kubectl apply -f k8s/
kubectl rollout status deploy/robot-agent
```

## 两个必须知道的约束

1. **`replicas` 只能是 1**：SQLite 单写者 + 单机器人物理动作串行。要扩副本得先换
   PostgreSQL 存储并把 `robot_id` 引入调度。
2. **liveness 与 readiness 分工不同**：`/healthz` 不查外部依赖，`/readyz` 才查。
   把 liveness 配成查数据库会导致数据库抖动时 Pod 被反复重启——故障放大而不是恢复。
