"""防篡改审计日志。

每条记录都是 JSONL 一行，字段::

    {"v":1, "seq":12, "ts":"2026-11-02T05:40:12.123Z", "event":"executed",
     "task_id":"...", "trace_id":"...", "data":{...},
     "prev_hash":"a1b2...", "hash":"9f8e..."}

``hash = sha256(prev_hash + canonical_json(本条不含 hash 的字段))``，形成哈希链：
任何中间条目被删改，后续条目的 ``prev_hash`` 就对不上，:meth:`AuditLog.verify_chain`
可以定位到第一处断点。这不是密码学意义上的不可篡改（需要外部锚定），
但足以满足「内部操作可追溯、可发现篡改」的常规合规要求。

**写者约定**：哈希链要求单写者。同一文件只允许一个进程写；多实例部署时
每个实例写到独立文件（见 docs/OPERATIONS.md）。
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from robot_agent import metrics
from robot_agent.config import AuditSettings
from robot_agent.logging import current_trace_id, get_logger
from robot_agent.version import STATE_SCHEMA_VERSION

logger = get_logger(__name__)

_GENESIS = "0" * 64


def _canonical(payload: Mapping[str, Any]) -> str:
    """稳定序列化：键排序、无多余空格，保证跨进程重算哈希一致。"""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass
class ChainVerification:
    """链校验结果。"""

    ok: bool
    entries: int
    broken_at: int | None = None
    reason: str = ""
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "entries": self.entries,
            "broken_at": self.broken_at,
            "reason": self.reason,
            "errors": self.errors[:20],
        }


class AuditLog:
    """线程安全的追加式审计日志。"""

    def __init__(
        self,
        settings: AuditSettings | None = None,
        *,
        service: str = "robot-agent",
        fail_closed: bool = False,
    ) -> None:
        self.settings = settings or AuditSettings()
        self.service = service
        self.fail_closed = fail_closed
        self.path = Path(self.settings.path)
        self._lock = threading.Lock()
        self._seq, self._last_hash = self._recover_tail()
        if self.settings.enabled:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ 写入

    def record(
        self,
        event: str,
        payload: Mapping[str, Any] | None = None,
        *,
        task_id: str | None = None,
        trace_id: str | None = None,
        **extra: Any,
    ) -> dict[str, Any] | None:
        """追加一条审计记录，返回写入的条目（关闭审计时返回 None）。

        审计失败默认不阻塞主流程（``fail_closed=False``），但会打 error 日志并累加
        ``audit_write_errors_total``——审计静默失效比丢一条记录更危险。
        """
        if not self.settings.enabled:
            return None

        try:
            with self._lock:
                self._seq += 1
                entry: dict[str, Any] = {
                    "v": STATE_SCHEMA_VERSION,
                    "seq": self._seq,
                    "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                    "service": self.service,
                    "event": event,
                    "task_id": task_id,
                    "trace_id": trace_id or current_trace_id(),
                    "data": dict(payload or {}),
                }
                if extra:
                    entry["extra"] = extra
                entry["prev_hash"] = self._last_hash
                entry["hash"] = self._digest(entry)
                self._append(entry)
                self._last_hash = entry["hash"]
            metrics.AUDIT_ENTRIES_TOTAL.labels(event=event).inc()
            return entry
        except OSError as exc:
            metrics.AUDIT_WRITE_ERRORS_TOTAL.inc()
            logger.error(
                "audit_write_failed",
                audit_event=event,
                path=str(self.path),
                error=str(exc),
                exc_info=True,
            )
            if self.fail_closed:
                from robot_agent.errors import StorageError

                raise StorageError(f"审计写入失败: {exc}", cause=exc) from exc
            return None

    def _digest(self, entry: Mapping[str, Any]) -> str:
        body = {k: v for k, v in entry.items() if k != "hash"}
        return hashlib.sha256((entry["prev_hash"] + _canonical(body)).encode("utf-8")).hexdigest()

    def _append(self, entry: Mapping[str, Any]) -> None:
        self._rotate_if_needed()
        line = json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(line)
            if self.settings.fsync:
                fh.flush()
                os.fsync(fh.fileno())

    def _rotate_if_needed(self) -> None:
        """按大小轮转：audit.jsonl → audit.jsonl.1 → .2 …（最多保留 5 份）。"""
        if not self.path.exists():
            return
        try:
            if self.path.stat().st_size < self.settings.rotate_bytes:
                return
        except OSError:  # pragma: no cover - 文件被外部删除
            return
        for idx in range(5, 1, -1):
            src = self.path.with_suffix(self.path.suffix + f".{idx - 1}")
            dst = self.path.with_suffix(self.path.suffix + f".{idx}")
            if src.exists():
                src.replace(dst)
        self.path.replace(self.path.with_suffix(self.path.suffix + ".1"))
        logger.info("audit_rotated", path=str(self.path))

    # ------------------------------------------------------------------ 恢复

    def _recover_tail(self) -> tuple[int, str]:
        """从文件尾部恢复 ``seq`` 与 ``hash``，保证重启后链不断。

        只读取最后 64 KiB：足以覆盖最后若干条记录，且不必扫全文件。
        """
        if not self.settings.enabled or not self.path.exists():
            return 0, _GENESIS
        try:
            with self.path.open("rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                fh.seek(max(0, size - 64 * 1024))
                tail = fh.read().decode("utf-8", errors="ignore")
            for line in reversed(tail.splitlines()):
                line = line.strip()
                if not line:
                    continue
                try:
                    last = json.loads(line)
                except json.JSONDecodeError:
                    continue
                return int(last.get("seq", 0)), str(last.get("hash", _GENESIS))
        except OSError:  # pragma: no cover
            logger.warning("audit_tail_recover_failed", path=str(self.path))
        return 0, _GENESIS

    # ------------------------------------------------------------------ 校验

    def verify_chain(self) -> ChainVerification:
        """从头重算哈希链，返回首个断点。大于 100 万条时建议离线跑。"""
        if not self.path.exists():
            return ChainVerification(ok=True, entries=0, reason="日志为空")

        prev = _GENESIS
        count = 0
        with self.path.open("r", encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, start=1):
                line = line.strip()
                if not line:
                    continue
                count += 1
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError as exc:
                    return ChainVerification(
                        False, count, lineno, f"第 {lineno} 行不是合法 JSON: {exc}"
                    )
                if entry.get("prev_hash") != prev:
                    return ChainVerification(
                        False, count, lineno, f"第 {lineno} 行 prev_hash 与前一条不连续"
                    )
                expected = self._digest(entry)
                if entry.get("hash") != expected:
                    return ChainVerification(
                        False, count, lineno, f"第 {lineno} 行内容被修改（哈希不匹配）"
                    )
                prev = entry["hash"]
        return ChainVerification(True, count, reason="链完整")


class NullAuditLog(AuditLog):
    """关闭审计时的占位实现，避免调用方到处判空。"""

    def __init__(self) -> None:
        super().__init__(AuditSettings(enabled=False))

    def record(self, event: str, payload: Mapping[str, Any] | None = None, **_: Any) -> None:
        return None


def tail_entries(path: Path, limit: int = 50) -> list[dict[str, Any]]:
    """读取最后 N 条审计记录，供 CLI ``audit tail`` 与排障使用。"""
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as fh:
        lines = fh.readlines()[-limit:]
    out: list[dict[str, Any]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _now_ms() -> int:  # pragma: no cover - 保留给未来的耗时统计
    return int(time.time() * 1000)
