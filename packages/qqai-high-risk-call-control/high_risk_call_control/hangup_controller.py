# -*- coding: utf-8 -*-
"""自动挂断控制器：最大时长 / 管理员 / 用户要求 / 心跳丢失 / 窗口消失。

规则：
- 幂等：同一通电话只真正挂断一次；重复调用返回 duplicate，不增加点击。
- 有限重试：最多 cfg.hangup_max_retries 次，指数退避；**没有无限点击**。
- 失败不伪造：重试耗尽 → abnormal 状态 + 通知管理员，返回值失败。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from .call_detector import CallState
from .core_bridge import BridgeError


@dataclass
class HangupResult:
    hung_up: bool
    reason: str
    call_id: str = ""
    attempts: int = 0
    detail: dict = field(default_factory=dict)


class HangupController:
    def __init__(self, cfg, guard, detector, actuator, bridge, fingerprint, audit,
                 notify_admin=None, clock=time.monotonic, sleep=time.sleep):
        self.cfg = cfg
        self.guard = guard
        self.detector = detector
        self.actuator = actuator
        self.bridge = bridge
        self.fingerprint = fingerprint
        self.audit = audit
        self.notify_admin = notify_admin or (lambda msg, detail=None: None)
        self.clock = clock
        self.sleep = sleep
        self._done: dict[str, HangupResult] = {}
        self._in_progress: set[str] = set()

    # ---------- 判定 ----------
    def elapsed(self, call_id: str) -> float:
        snap = self.bridge.snapshot()
        call = snap.get("call") or {}
        if call.get("call_id") != call_id:
            return 0.0
        answered = call.get("answered_at")
        started = answered if answered is not None else call.get("ring_started")
        if started is None:
            return 0.0
        try:
            return max(0.0, float(self.clock()) - float(started))
        except (TypeError, ValueError):
            return 0.0

    def should_hangup(self, call_id: str, *, admin: bool = False,
                      user_request: bool = False, heartbeat_lost: bool = False,
                      window_gone: bool = False) -> tuple[bool, str]:
        if admin:
            return True, "admin_hangup"
        if user_request:
            return True, "user_request"
        if heartbeat_lost:
            return True, "heartbeat_timeout"
        if window_gone:
            return True, "window_gone"
        if self.elapsed(call_id) >= self.cfg.max_call_seconds:
            return True, "max_duration"
        return False, "not_yet"

    def _core_idle(self) -> bool:
        try:
            return self.bridge.snapshot().get("state") != "in_call"
        except Exception:
            return False

    # ---------- 执行 ----------
    def hangup(self, call_id: str, reason: str = "user_request", by: str = "user") -> HangupResult:
        if self._done.get(call_id, HangupResult(False, "")).hung_up:
            self.audit.write("hangup_duplicate", call_id=call_id, reason=reason)
            return HangupResult(False, "duplicate", call_id, attempts=0)
        if call_id in self._in_progress:
            return HangupResult(False, "in_progress", call_id)

        gate = self.guard.base_gate("hangup")
        if not gate.allowed:
            self.audit.write("hangup_blocked", call_id=call_id, reason=gate.reason)
            return HangupResult(False, gate.reason, call_id)

        self._in_progress.add(call_id)
        attempts = 0
        try:
            # 先让协调器进入收尾（释放租约/结束会话）
            try:
                self.bridge.hangup(call_id, reason=reason, by=by)
            except BridgeError as exc:
                self.audit.write("hangup_bridge_error", call_id=call_id, error=str(exc))
                # 协调器失败仍然尝试 UI 挂断，但不宣告成功

            # 硬上限：总点击尝试 ≤ 5 次（含首次），配置再大也不会超过
            max_retries = min(int(self.cfg.hangup_max_retries), 4)
            max_attempts = max(1, min(5, max_retries + 1))
            backoff = self.cfg.hangup_retry_backoff_seconds
            while attempts < max_attempts:
                attempts += 1
                det = self.detector.detect()
                if det.state is CallState.IDLE or self._core_idle():
                    self._done[call_id] = HangupResult(True, reason, call_id, attempts)
                    self.audit.write("hangup_done", call_id=call_id, reason=reason,
                                     attempts=attempts, via="window_gone")
                    return self._done[call_id]

                point = self.fingerprint.hangup_point
                if point is None or det.window is None:
                    break
                action = self.actuator.click_point(det.window, point)
                self.audit.write("hangup_click", call_id=call_id, ok=action.ok,
                                 reason=action.reason, attempt=attempts)
                if action.ok:
                    self.sleep(0.25)
                if self.detector.detect().state is CallState.IDLE or self._core_idle():
                    self._done[call_id] = HangupResult(True, reason, call_id, attempts)
                    self.audit.write("hangup_done", call_id=call_id, reason=reason,
                                     attempts=attempts, via="ui")
                    return self._done[call_id]
                if attempts < max_attempts and backoff > 0:
                    self.sleep(backoff * attempts)

            # 重试耗尽：进入异常，不伪造成功
            self.guard.mark_abnormal(f"hangup_failed:{reason}")
            self._done[call_id] = HangupResult(False, "hangup_failed", call_id, attempts)
            self.notify_admin("自动挂断失败，扩展已进入异常状态，请人工处理",
                              {"call_id": call_id, "reason": reason, "attempts": attempts})
            self.audit.write("hangup_failed", call_id=call_id, reason=reason, attempts=attempts)
            return self._done[call_id]
        finally:
            self._in_progress.discard(call_id)
