"""单点版本号，供包元数据、CLI、/healthz 与审计头共用。"""

from __future__ import annotations

__version__ = "1.4.1"

# 审计条目与规划结果里写入的协议版本；破坏性变更时递增，便于回放旧日志。
STATE_SCHEMA_VERSION = "1"
