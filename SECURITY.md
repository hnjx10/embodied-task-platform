# 安全说明

## 报告漏洞

请**不要**开公开 issue。发邮件到仓库 owner 的 GitHub 邮箱，或使用 GitHub 的
[Private vulnerability reporting](https://github.com/hnjx10/gazebo-langgraph-demo/security/advisories/new)。

请包含：受影响版本、复现步骤、影响评估。48 小时内回复，修复后会在 CHANGELOG 致谢。

## 信任边界

理解这个系统的安全模型，先要理解它「不信任什么」：

| 组件 | 可信度 | 依据 |
|---|---|---|
| **LLM 输出** | **不可信** | 会被注入、会幻觉。技能白名单 + 参数 pydantic 校验 + 安全闸门三道拦截 |
| **用户指令** | **不可信** | 长度限制、红线词、坐标与速度的硬边界 |
| **执行器返回** | **半可信** | 不信它自述的"成功"，`verify` 阶段用 `get_pose` 实测位姿 |
| 配置文件 | 可信（但 prod 有启动护栏） | 关审计/无鉴权对外监听/内存存储都会被拒绝启动 |
| 审计日志 | 可信（哈希链可检测篡改） | 需外部锚定才能防"整链重写"，见下方限制 |

## 已实现的防护

- 安全策略 **fail-closed**：策略自身异常按拒绝处理（[ADR 0001](docs/adr/0001-safety-fail-closed.md)）
- 安全拒绝 **不可重试**：防止用重试试探策略边界
- 审批后 **重新校验**：人工修改同样受策略约束
- 技能参数 `extra="forbid"`：拒绝 LLM 幻觉出的多余参数
- Bearer Token 用 `secrets.compare_digest` 比较：防时序侧信道
- 500 错误**不回显内部信息**，只返回 trace_id
- 容器**非 root** + `readOnlyRootFilesystem` + `drop: ALL` capabilities
- 依赖**不信任**：LLM 调用设硬超时、重试上限、熔断

## 已知的边界（诚实说明）

1. **审计链不是密码学不可篡改**：能检测中间条目被改，但如果攻击者拿到文件写权限并
   **重算整条链**，本地校验无法发现。需要外部锚定（定期把链头哈希写到独立系统/时间戳服务）。
2. **`auth_token` 是共享密钥**：没有 per-user 身份与权限分级。多用户场景需要接 OIDC。
3. **没有速率限制**：内部网络假设下未实现。对公网暴露前必须在网关加限流。
4. **LLM 提示注入**：指令被拼进 prompt，恶意构造可能让模型输出危险计划——
   但危险计划最终仍要过安全闸门，这是纵深防御的意义。
5. **技能超时无法强杀线程**：只能放弃等待 + 急停，线程可能继续持有资源。

## 部署加固清单

- [ ] 设置 `ROBOT_AGENT_ENV=prod`（启用启动护栏）
- [ ] 设置 `ROBOT_AGENT_API__AUTH_TOKEN`（≥32 字节随机）
- [ ] 开启 `ROBOT_AGENT_AUDIT__FSYNC=true`
- [ ] 收紧 `safety.max_linear_speed` / `max_steps` / `geofences` 到现场实际值
- [ ] 对 `push_cube` 这类物理接触动作启用 `require_approval_for`
- [ ] 审计日志目录权限 `750`，属主为服务账号
- [ ] 前面挂 TLS 终止代理（服务本身只跑 HTTP）
- [ ] 网关层加限流
