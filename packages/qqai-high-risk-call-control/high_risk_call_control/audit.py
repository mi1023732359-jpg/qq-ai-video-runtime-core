# -*- coding: utf-8 -*-
"""审计日志：所有高风险动作留痕；写失败不阻断主流程，但会被计数。

约束：
- 只写白名单字段，凭据类键名（password/token/api_key/secret/cookie）一律脱敏。
- 每行一个 JSON 对象，追加写；单文件超过上限自动轮转（保留 N 个）。
- 不联网、不依赖第三方库。
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

SENSITIVE_HINTS = ("password", "passwd", "token", "api_key", "apikey", "secret",
                   "cookie", "credential", "authorization", "session_key", "sk-")
MAX_BYTES = 8 * 1024 * 1024
KEEP_FILES = 3


def _redact(value):
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if any(h in str(k).lower() for h in SENSITIVE_HINTS):
                out[k] = "[REDACTED]"
            else:
                out[k] = _redact(v)
        return out
    if isinstance(value, (list, tuple)):
        return [_redact(v) for v in value]
    if isinstance(value, str) and len(value) > 400:
        return value[:400] + "…"
    return value


class AuditLog:
    """追加式审计日志。线程安全；失败计数可在自检里读到。"""

    def __init__(self, path: str, max_bytes: int = MAX_BYTES, keep: int = KEEP_FILES,
                 clock=time.time):
        self.path = Path(path)
        self.max_bytes = max_bytes
        self.keep = keep
        self.clock = clock
        self._lock = threading.Lock()
        self.write_failures = 0
        self.records = 0

    def _rotate_if_needed(self) -> None:
        try:
            if self.path.exists() and self.path.stat().st_size >= self.max_bytes:
                for i in range(self.keep - 1, 0, -1):
                    src = self.path.with_suffix(self.path.suffix + f".{i}")
                    dst = self.path.with_suffix(self.path.suffix + f".{i + 1}")
                    if src.exists():
                        os.replace(src, dst)
                os.replace(self.path, self.path.with_suffix(self.path.suffix + ".1"))
        except Exception:
            # 轮转失败不影响写入（继续追加）
            pass

    def write(self, event: str, **detail) -> None:
        rec = {"ts": round(self.clock(), 3), "event": str(event), "detail": _redact(detail)}
        line = json.dumps(rec, ensure_ascii=False, sort_keys=True)
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self._rotate_if_needed()
                with open(self.path, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
                self.records += 1
            except Exception:
                self.write_failures += 1

    def tail(self, limit: int = 20) -> list[dict]:
        """读最近 N 条（用于自检 / 运维排障），不修改文件。"""
        out: list[dict] = []
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        out.append(json.loads(line))
                    except Exception:
                        continue
        except FileNotFoundError:
            return []
        except Exception:
            return out[-limit:]
        return out[-limit:]
