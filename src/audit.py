"""全链路审计：每一步规划/拦截/执行结果追加写入 JSONL，可回放定位。"""
import json
import os
import time
from typing import Any


class AuditLog:
    def __init__(self, path: str = "logs/audit.jsonl") -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.path = path

    def record(self, event: str, payload: Any) -> None:
        entry = {"ts": round(time.time(), 3), "event": event, "data": payload}
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
