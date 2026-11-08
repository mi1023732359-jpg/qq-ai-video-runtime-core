# -*- coding: utf-8 -*-
"""运行时编排：把检测、策略、接听、挂断串成一个可单步驱动的状态机。

设计要点：
- 默认关闭：cfg.enabled 为假时，构造后 status() 只报 disabled，step() 不动作。
- 单步驱动：step() 只推进一次，便于测试与"管理员随时叫停"；
  run() 有显式 max_ticks 上限，**没有无限循环**。
- 重启安全：启动时调用协调器 recover_from_crash()，并在自动接听未获授权时
  显式 set_auto_answer(False)，绝不"重启后自己恢复"。
- 不 import core、不碰 sys.path、不在 import 时做任何 IO。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from .audit import AuditLog
from .call_detector import CallDetector, CallState, Fingerprint, load_fingerprint
from .call_policy import CallPolicy
from .config import HighRiskConfig
from .core_bridge import BridgeError
from .safety_guard import SafetyGuard
from .answer_controller import AnswerController
from .hangup_controller import HangupController


@dataclass
class RuntimeStatus:
    enabled: bool
    auto_answer: bool
    auto_hangup: bool
    stopped: str
    abnormal: bool
    state: str
    call_id: str = ""
    last_event: str = ""
    detail: dict = field(default_factory=dict)


class CallRuntime:
    def __init__(self, cfg: HighRiskConfig, bridge, *, probe=None, actuator=None,
                 audit: AuditLog | None = None, fingerprint: Fingerprint | None = None,
                 clock=time.monotonic, wall=time.time, sleep=time.sleep,
                 notify_admin=None):
        self.cfg = cfg
        self.bridge = bridge
        self.clock = clock
        self.wall = wall
        self.sleep = sleep
        self.notify_admin = notify_admin or (lambda msg, detail=None: None)

        log_path = cfg.audit_log_path or "high_risk_call_control_audit.jsonl"
        self.audit = audit or AuditLog(log_path, clock=wall)
        self.guard = SafetyGuard(cfg, self.audit, clock=clock, wall=time.time)
        self.policy = CallPolicy(cfg, clock=wall)

        self.fingerprint = fingerprint or Fingerprint()
        self.probe = probe
        self.actuator = actuator
        self.detector = None
        self.answer_controller = None
        self.hangup_controller = None

        if cfg.enabled and probe is not None:
            self.detector = CallDetector(probe, self.fingerprint, cfg.qq_process_names)
        if self.detector is not None and bridge is not None and actuator is not None:
            self.answer_controller = AnswerController(
                cfg, self.guard, self.policy, self.detector, actuator, bridge,
                self.fingerprint, self.audit, clock=clock, wall=wall)
            self.hangup_controller = HangupController(
                cfg, self.guard, self.detector, actuator, bridge, self.fingerprint,
                self.audit, notify_admin=self.notify_admin, clock=clock, sleep=sleep)

        self._pending_incoming: list[dict] = []
        self.last_event = "constructed"
        self.last_state = "disabled" if not cfg.enabled else "idle"
        self._initialized = False

    # ---------- 生命周期 ----------
    def initialize(self) -> dict:
        """启动自检。未启用时直接返回 disabled，不碰协调器。"""
        if not self.cfg.enabled:
            self.audit.write("init_skipped_disabled")
            self.last_event = "init_skipped"
            return {"ok": False, "reason": "extension_disabled"}

        warnings = list(self.cfg.warnings)
        try:
            self.bridge.recover_from_crash()
        except BridgeError as exc:
            warnings.append(f"recover_from_crash 失败：{exc}")

        # 重启后由本扩展单向同步协调器状态：
        # - 未授权（含闸门缺失）→ 强制 False，绝不沿用 Core 里可能残留的旧值
        # - 已授权 → 显式置 True，避免"看起来开着、实际没开"的不一致
        try:
            self.bridge.set_auto_answer(bool(self.cfg.auto_answer_active), by="extension_init")
        except BridgeError:
            warnings.append("set_auto_answer 同步失败（协调器可能不支持），已按本扩展配置为准")

        # 异常状态持久化：崩溃/重启后保持 abnormal，直到管理员显式清除
        abnormal_file = self._abnormal_path()
        if abnormal_file is not None and abnormal_file.exists():
            self.guard.abnormal = True
            warnings.append("检测到历史异常状态文件，扩展保持 abnormal，等待管理员清除")

        if self.detector is None or self.answer_controller is None:
            warnings.append("检测器或执行器未注入，扩展将以只读模式运行")

        self._initialized = True
        self.audit.write("init", flags=self.cfg.to_public_dict(), warnings=warnings)
        self.last_event = "initialized"
        return {"ok": True, "warnings": warnings, "flags": self.cfg.to_public_dict()}

    def status(self) -> RuntimeStatus:
        snap = self.bridge.snapshot() if self.bridge is not None else {}
        return RuntimeStatus(
            enabled=self.cfg.enabled,
            auto_answer=self.cfg.auto_answer_active,
            auto_hangup=self.cfg.auto_hangup_active,
            stopped=self.guard.stopped_reason(),
            abnormal=self.guard.abnormal,
            state=str(snap.get("state") or self.last_state),
            call_id=str((snap.get("call") or {}).get("call_id") or ""),
            last_event=self.last_event,
            detail={"flags": self.cfg.to_public_dict()},
        )

    # ---------- 来电注入 ----------
    def notify_incoming(self, caller: str, call_id: str | None = None) -> dict:
        """由消息/事件层把"谁在打来"显式交给扩展；不猜窗口标题。"""
        rec = {"caller": str(caller), "call_id": call_id or f"call-{int(self.wall())}"}
        if len(self._pending_incoming) >= 8:
            self._pending_incoming.pop(0)
        self._pending_incoming.append(rec)
        self.audit.write("incoming_notified", caller=rec["caller"], call_id=rec["call_id"])
        return dict(rec)

    def _abnormal_path(self):
        state_dir = (self.cfg.state_dir or "").strip()
        if not state_dir:
            return None
        try:
            from pathlib import Path
            return Path(state_dir) / "abnormal.json"
        except Exception:
            return None

    def _persist_abnormal(self) -> None:
        if not self.guard.abnormal:
            return
        path = self._abnormal_path()
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('{"abnormal": true, "since": %d}' % int(self.wall()),
                            encoding="utf-8")
        except Exception:
            pass

    # ---------- 单步 ----------
    def step(self) -> dict:
        if not self.cfg.enabled:
            return {"action": "none", "reason": "extension_disabled"}
        stopped = self.guard.stopped_reason()
        if stopped:
            return {"action": "none", "reason": stopped}
        if self.detector is None or not self._initialized:
            return {"action": "none", "reason": "not_initialized"}

        det = self.detector.detect()
        snap = self.bridge.snapshot() if self.bridge is not None else {}
        state = str(snap.get("state") or "idle")

        if det.state is CallState.UNKNOWN:
            self.audit.write("detect_unknown", reason=det.reason,
                             candidates=[w.public() for w in det.candidates])
            return {"action": "none", "reason": "unknown", "detail": det.reason}

        # 来电
        if det.state is CallState.INCOMING and state != "in_call":
            queued = self._pending_incoming.pop(0) if self._pending_incoming else None
            caller = (queued or {}).get("caller") or self._guess_caller(det)
            call_id = (queued or {}).get("call_id") or (
                f"call-{det.window.hwnd}-{int(self.wall())}" if det.window else f"call-{int(self.wall())}")
            if self.answer_controller is None or not self.cfg.auto_answer_active:
                self.audit.write("incoming_not_answered", caller=caller,
                                 reason="auto_answer_off")
                return {"action": "observed", "reason": "auto_answer_off"}
            res = self.answer_controller.handle_incoming(caller, call_id)
            self.last_event = "answer:" + res.reason
            return {"action": "answer", "ok": res.answered, "reason": res.reason,
                    "call_id": res.call_id}

        # 通话中
        if state == "in_call":
            call = snap.get("call") or {}
            call_id = str(call.get("call_id") or "")
            try:
                self.bridge.heartbeat(call_id)
            except BridgeError:
                pass
            if self.hangup_controller is not None:
                due, reason = self.hangup_controller.should_hangup(
                    call_id, window_gone=(det.state is CallState.IDLE))
                if due and self.cfg.auto_hangup_active:
                    res = self.hangup_controller.hangup(call_id, reason=reason, by="extension")
                    self._persist_abnormal()
                    self.last_event = "hangup:" + res.reason
                    return {"action": "hangup", "ok": res.hung_up, "reason": res.reason,
                            "call_id": call_id}
            self.last_event = "heartbeat"
            return {"action": "heartbeat", "state": "in_call"}

        # 空闲：如果协调器还认为在通话里，但窗口已消失 → 收尾
        if state == "in_call" and det.state is CallState.IDLE:
            call_id = str((snap.get("call") or {}).get("call_id") or "")
            if self.hangup_controller is not None:
                res = self.hangup_controller.hangup(call_id, reason="window_gone", by="extension")
                self.last_event = "hangup:" + res.reason
                return {"action": "hangup", "ok": res.hung_up, "reason": "window_gone",
                        "call_id": call_id}
        self.last_event = "idle"
        return {"action": "none", "reason": "idle"}

    def run(self, max_ticks: int = 120, interval: float | None = None) -> list[dict]:
        """有界运行：最多 max_ticks 步；不存在无限循环。"""
        out = []
        gap = self.cfg.tick_interval_seconds if interval is None else float(interval)
        for _ in range(max(0, int(max_ticks))):
            if self.guard.stopped_reason():
                out.append({"action": "stopped", "reason": self.guard.stopped_reason()})
                break
            out.append(self.step())
            if gap > 0:
                self.sleep(gap)
        return out

    # ---------- 管理员控制 ----------
    def stop(self, by: str = "admin", reason: str = "manual_stop") -> dict:
        self.guard.admin_request_stop(by=by, reason=reason)
        self.last_event = "admin_stopped"
        return {"ok": True, "stopped": self.guard.stopped_reason()}

    def resume(self, by: str = "admin") -> dict:
        self.guard.admin_clear_stop(by=by)
        self.last_event = "admin_resumed"
        return {"ok": True, "stopped": self.guard.stopped_reason()}

    def force_hangup(self, by: str = "admin") -> dict:
        snap = self.bridge.snapshot() if self.bridge is not None else {}
        call_id = str((snap.get("call") or {}).get("call_id") or "")
        self.audit.write("admin_force_hangup", call_id=call_id, by=by)
        if self.hangup_controller is not None and call_id:
            res = self.hangup_controller.hangup(call_id, reason="admin_hangup", by=by)
            self.last_event = "admin_force_hangup:" + res.reason
            return {"ok": res.hung_up, "reason": res.reason}
        try:
            self.bridge.force_hangup("admin_hangup")
            return {"ok": True, "reason": "core_forced"}
        except BridgeError:
            return {"ok": False, "reason": "unsupported"}

    def deny_current(self, by: str = "admin") -> dict:
        snap = self.bridge.snapshot() if self.bridge is not None else {}
        call = snap.get("call") or {}
        if self.answer_controller is None or not call:
            return {"ok": False, "reason": "no_call"}
        res = self.answer_controller.deny(str(call.get("caller") or ""),
                                          str(call.get("call_id") or ""), by=by)
        return {"ok": True, "reason": res.reason}

    def clear_abnormal(self, by: str = "admin") -> dict:
        self.guard.clear_abnormal(by=by)
        path = self._abnormal_path()
        if path is not None:
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass
        self.last_event = "abnormal_cleared"
        return {"ok": True}

    # ---------- 辅助 ----------
    def _guess_caller(self, det) -> str:
        snap = self.bridge.snapshot() if self.bridge is not None else {}
        call = snap.get("call") or {}
        if call.get("caller"):
            return str(call["caller"])
        # 从窗口标题里提取纯数字（QQ 来电窗口通常显示昵称，不是号；拿不到就返回 unknown）
        title = (det.window.title if det.window else "") or ""
        digits = "".join(ch for ch in title if ch.isdigit())
        return digits if digits else "unknown"
