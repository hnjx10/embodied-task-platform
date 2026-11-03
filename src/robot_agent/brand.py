"""产品标识的唯一事实源。

项目名、英文名、副标题集中在这里，README / CLI / HTTP 文档 / 报告落款都引用同一份，
避免改名时漏掉某一处（改名最容易出的事就是文档改了、`/healthz` 还吐旧名字）。

**包名与 CLI 名故意不跟着改**：``robot_agent`` 与 ``robot-agent`` 是部署契约——
systemd unit、Dockerfile 的 ENTRYPOINT、CI 脚本、k8s manifest 里都写着它们。
产品名是给人看的，导入名和可执行文件名是给机器看的，两者混为一谈会让一次改名
变成一次全量部署变更。真要改 CLI 名，应该单独做一次带兼容别名的迁移。
"""

from __future__ import annotations

PRODUCT_NAME_ZH = "具身任务规划与执行验证平台"
PRODUCT_NAME_EN = "Embodied Task Planning & Execution Verification Platform"
PRODUCT_SLUG = "embodied-task-platform"

PRODUCT_TAGLINE = "把自然语言指令编译成受约束的机器人技能序列，可审计、可接管、可回放"

#: CLI / 包 / 部署脚本使用的技术标识，不随产品名变化。
TECH_NAME = "robot-agent"
