# -*- coding: utf-8 -*-
"""测试替身：全部本地假对象，**不碰真实 QQ、不碰真实 Core 状态机文件**。"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from high_risk_call_control.call_detector import WindowInfo, Fingerprint
from high_risk_call_control.config import HighRiskConfig


@dataclass
class FakeClock:
    now: float = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> float:
        self.now += float(seconds)
        return self.now


class FakeProbe:
    """可控窗口列表；raise_on_list 用于模拟探测失败。"""

    def __init__(self, windows=None, error: Exception | None = None):
        self.windows = list(windows or [])
        self.error = error
        self.calls = 0

    def list_windows(self):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return list(self.windows)


class FakeActuator:
    """记录点击；可配置失败或"点完状态不变"。"""

    def __init__(self, fail_reason: str = ""):
        self.fail_reason = fail_reason
        self.clicks: list[tuple[int, tuple[int, int]]] = []
        self.on_click = None

    def click_point(self, window, offset):
        from high_risk_call_control.qq_ui_controller import ActionResult
        if self.fail_reason:
            return ActionResult(False, self.fail_reason)
        self.clicks.append((window.hwnd, tuple(offset)))
        if callable(self.on_click):
            self.on_click(window, offset)
        return ActionResult(True, "clicked", clicked=True, detail={"hwnd": window.hwnd})


def win(hwnd=1, title="通话中", cls="QQCallWnd", pid=4242, rect=(100, 100, 500, 400),
        process="QQ.exe", visible=True):
    l, t, r, b = rect
    return WindowInfo(hwnd=hwnd, title=title, class_name=cls, pid=pid,
                      left=l, top=t, right=r, bottom=b, visible=visible,
                      process_name=process)


def fp(call_cls=("QQCallWnd",), call_kw=(), active_cls=("QQCallWndActive",),
       active_kw=(), point=(20, 30), hangup=(10, 12), confirmed="director"):
    return Fingerprint(call_classes=call_cls, call_title_keywords=call_kw,
                       active_classes=active_cls, active_title_keywords=active_kw,
                       automation_id="ctrl-answer-001",
                       call_point=point, hangup_point=hangup, confirmed_by=confirmed)


def cfg(**kw) -> HighRiskConfig:
    base = dict(enabled=True, enable_auto_answer=True, enable_auto_hangup=True,
                admin_uins=("10001",), allow_uins=("10001", "10002"),
                deny_uins=("19999",), require_single_account=True,
                max_call_seconds=60, hangup_max_retries=1,
                hangup_retry_backoff_seconds=0.0, max_actions_per_minute=100,
                require_confirmation_file=True, require_env_click_flag=True,
                state_dir=".", audit_log_path="", stop_file="",
                answer_confirm_file="", window_fingerprint_file="")
    base.update(kw)
    return HighRiskConfig(**base)


class FakeCoordinator:
    """模拟 Core SessionCoordinator 的对外语义（不读取真实 store）。"""

    def __init__(self, topology="split_sessions", recover_state=None, hangup_raises=False, clock=None):
        self.state = "idle"
        self.call = None
        self.lease = None
        self.topology = topology
        self.calls: list[dict] = []
        self.heartbeats = 0
        self.hangup_reasons: list[str] = []
        self.auto_answer_flag = True          # 故意预置为 True，检验扩展是否显式关闭
        self._recover_state = recover_state or {"state": "idle", "recovered": True}
        self.messages: list[dict] = []
        self.hangup_raises = hangup_raises
        self.clock = clock

    # --- 只读 ---
    def snapshot(self):
        return {"state": self.state, "call": dict(self.call) if self.call else None,
                "lease": dict(self.lease) if self.lease else None,
                "session_topology": self.topology,
                "requires_account_handover": self.topology != "split_sessions",
                "send_allowed": self.lease is None}

    def send_allowed(self):
        return self.lease is None

    # --- 会话 ---
    def on_incoming_call(self, caller, call_id=None, authorized=None):
        call_id = call_id or "call-x"
        if any(c["call_id"] == call_id for c in self.calls):
            return {"accepted": False, "reason": "duplicate", "call_id": call_id,
                    "state": self.state, "duplicate": True}
        if self.state in ("ringing", "in_call"):
            rec = {"accepted": False, "reason": "busy", "call_id": call_id,
                   "state": self.state}
            self.calls.append({"call_id": call_id, **rec})
            return rec
        allow = True if authorized is None else bool(authorized)
        self.call = {"call_id": call_id, "caller": str(caller),
                     "session_id": "video-" + call_id, "ring_started": 0.0,
                     "answered_at": None, "turn": 0}
        self.state = "ringing" if allow else "rejected"
        self.calls.append({"call_id": call_id, "accepted": allow})
        return {"accepted": allow, "call_id": call_id, "caller": str(caller),
                "state": self.state, "session_id": "video-" + call_id,
                "reason": "authorized" if allow else "manual"}

    def answer(self, call_id=None):
        if self.state != "ringing":
            return {"answered": False, "reason": "not_ringing", "state": self.state}
        self.state = "in_call"
        self.call["answered_at"] = float(self.clock()) if self.clock else 0.0
        self.lease = {"owner": "video", "call_id": self.call["call_id"]}
        return {"answered": True, "state": self.state, "call_id": self.call["call_id"]}

    def heartbeat(self, call_id=None):
        self.heartbeats += 1
        return {"ok": self.state == "in_call"}

    def hangup(self, call_id=None, reason="normal", by="user"):
        self.hangup_reasons.append(reason)
        if self.hangup_raises:
            raise RuntimeError("simulated hangup failure")
        self.state = "idle"
        self.call = None
        self.lease = None
        return {"hung_up": True, "reason": reason, "by": by}

    def recover_from_crash(self):
        if self._recover_state.get("state") == "in_call" and not self._recover_state.get("recovered"):
            self.state = "in_call"
            self.call = {"call_id": "stale"}
        return dict(self._recover_state)

    def set_auto_answer(self, enabled, by="admin"):
        self.auto_answer_flag = bool(enabled)
        return {"ok": True, "enabled": self.auto_answer_flag, "by": by}

    def force_hangup(self, reason="forced"):
        self.state = "idle"
        self.call = None
        self.lease = None
        return {"ok": True, "reason": reason}

    # --- 普通消息 ---
    def record_incoming_message(self, chat_id, sender, text, scope="chat"):
        self.messages.append({"chat_id": chat_id, "sender": sender, "text": text})
        return {"recorded": True, "send_allowed": self.send_allowed()}
