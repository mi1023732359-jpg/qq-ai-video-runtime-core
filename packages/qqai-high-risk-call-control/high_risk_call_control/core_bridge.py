# -*- coding: utf-8 -*-
"""Core 适配层：只通过显式注入的协调器对象与 Core 交互。

本模块 **不 import core**、不改 sys.path、不做全局钩子。
用法：
    # 由调用方自己 import Core 的协调器（本模块不 import core）：
    #   from core.video_runtime.session_coordinator import SessionCoordinator
    coord = SessionCoordinator(...)              # 由调用方创建（Core 自己的配置）
    bridge = CoreBridge(coord)                   # 只接受鸭子类型
    runtime = CallRuntime(cfg, bridge, ...)

若扩展未启用，调用方根本不需要创建 bridge。
"""
from __future__ import annotations

REQUIRED_ATTRS = (
    "snapshot",
    "on_incoming_call",
    "answer",
    "heartbeat",
    "hangup",
    "recover_from_crash",
)
OPTIONAL_ATTRS = (
    "set_auto_answer",
    "force_hangup",
    "acquire_send_lease",
    "release_send_lease",
    "video_turn",
)


class BridgeError(RuntimeError):
    pass


class CoreBridge:
    """窄接口：只暴露扩展需要的那几个方法。"""

    def __init__(self, coordinator):
        missing = [a for a in REQUIRED_ATTRS if not callable(getattr(coordinator, a, None))]
        if missing:
            raise BridgeError("协调器缺少必要方法：" + ",".join(missing))
        self._c = coordinator

    # ---------- 只读 ----------
    def snapshot(self) -> dict:
        try:
            data = self._c.snapshot()
            return dict(data) if isinstance(data, dict) else {}
        except Exception:
            return {}

    def in_call(self) -> bool:
        return self.snapshot().get("state") == "in_call"

    def send_allowed(self) -> bool:
        try:
            return bool(self._c.send_allowed())
        except Exception:
            return False

    # ---------- 会话 ----------
    def on_incoming_call(self, caller: str, call_id: str, authorized: bool | None = None) -> dict:
        try:
            res = self._c.on_incoming_call(str(caller), call_id=call_id, authorized=authorized)
        except TypeError:  # 老版本签名
            res = self._c.on_incoming_call(str(caller), call_id=call_id)
        except Exception as exc:
            raise BridgeError(f"on_incoming_call 失败：{type(exc).__name__}") from exc
        return dict(res or {})

    def answer(self, call_id: str) -> dict:
        try:
            return dict(self._c.answer(call_id) or {})
        except Exception as exc:
            raise BridgeError(f"answer 失败：{type(exc).__name__}") from exc

    def heartbeat(self, call_id: str) -> dict:
        try:
            return dict(self._c.heartbeat(call_id) or {})
        except Exception as exc:
            raise BridgeError(f"heartbeat 失败：{type(exc).__name__}") from exc

    def hangup(self, call_id: str, reason: str = "normal", by: str = "user") -> dict:
        try:
            return dict(self._c.hangup(call_id, reason=reason, by=by) or {})
        except TypeError:
            return dict(self._c.hangup(call_id) or {})
        except Exception as exc:
            raise BridgeError(f"hangup 失败：{type(exc).__name__}") from exc

    def recover_from_crash(self) -> dict:
        try:
            return dict(self._c.recover_from_crash() or {})
        except Exception as exc:
            raise BridgeError(f"recover_from_crash 失败：{type(exc).__name__}") from exc

    # ---------- 管理员 ----------
    def set_auto_answer(self, enabled: bool, by: str = "admin") -> dict:
        fn = getattr(self._c, "set_auto_answer", None)
        if not callable(fn):
            return {"ok": False, "reason": "unsupported"}
        try:
            return dict(fn(bool(enabled), by=by) or {})
        except Exception as exc:
            raise BridgeError(f"set_auto_answer 失败：{type(exc).__name__}") from exc

    def force_hangup(self, reason: str = "forced") -> dict:
        fn = getattr(self._c, "force_hangup", None)
        if not callable(fn):
            return {"ok": False, "reason": "unsupported"}
        try:
            return dict(fn(reason) or {})
        except Exception as exc:
            raise BridgeError(f"force_hangup 失败：{type(exc).__name__}") from exc
