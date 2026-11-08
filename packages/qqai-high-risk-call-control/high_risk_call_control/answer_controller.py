# -*- coding: utf-8 -*-
"""自动接听控制器：策略 → 协调器 → 点击 → 真实状态复核。

每一步失败都返回结构化结果并留痕；**不伪造成功**。
同一通来电（call_id / automation_id）只允许一次点击（幂等）。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from .call_policy import CallPolicy, PolicyInput
from .call_detector import CallState, CallDetector, Fingerprint
from .core_bridge import BridgeError


@dataclass
class AnswerResult:
    answered: bool
    reason: str
    call_id: str = ""
    detail: dict = field(default_factory=dict)


class AnswerController:
    def __init__(self, cfg, guard, policy: CallPolicy, detector: CallDetector,
                 actuator, bridge, fingerprint: Fingerprint, audit,
                 clock=time.monotonic, wall=time.time):
        self.cfg = cfg
        self.guard = guard
        self.policy = policy
        self.detector = detector
        self.actuator = actuator
        self.bridge = bridge
        self.fingerprint = fingerprint
        self.audit = audit
        self.clock = clock
        self.wall = wall
        self._answered_calls: set[str] = set()
        self._last_clicked_call = ""
        self.answer_attempts = 0

    # ---------- 管理员 ----------
    def deny(self, caller: str, call_id: str, by: str = "admin") -> AnswerResult:
        self.guard.audit.write("admin_deny", caller=caller, call_id=call_id, by=by)
        if self.bridge is not None:
            try:
                self.bridge.on_incoming_call(caller, call_id, authorized=False)
            except BridgeError:
                pass
        return AnswerResult(False, "admin_denied", call_id)

    # ---------- 主流程 ----------
    def handle_incoming(self, caller: str, call_id: str, *, duplicate: bool = False,
                        admin_override: bool | None = None) -> AnswerResult:
        snap = self.bridge.snapshot() if self.bridge is not None else {}
        in_call = bool(snap.get("state") == "in_call")

        # 先看 UI：没有可接的来电就不要动策略、更不要点
        det_pre = self.detector.detect()
        if det_pre.state is not CallState.INCOMING or det_pre.window is None:
            self.audit.write("answer_no_window", call_id=call_id,
                             state=det_pre.state.value, reason=det_pre.reason)
            return AnswerResult(False, f"no_incoming_window:{det_pre.reason}", call_id)

        decision = self.policy.decide(PolicyInput(
            caller=caller,
            now_hhmm=self.policy._now_hhmm(),
            in_call=in_call,
            ringing=True,
            core_authorized=None,
            account_confirmed=self._account_confirmed(snap),
            single_account_ok=self._single_account_ok(snap),
            duplicate=duplicate,
            admin_override=admin_override,
        ))
        self.audit.write("answer_decision", caller=caller, call_id=call_id,
                         allow=decision.allow, reason=decision.reason)
        if not decision.allow:
            return AnswerResult(False, decision.reason, call_id, decision.detail)

        gate = self.guard.base_gate("answer")
        if not gate.allowed:
            return AnswerResult(False, gate.reason, call_id, gate.detail)

        # 协调器先裁决（一切以 Core 的状态机为准）
        try:
            res = self.bridge.on_incoming_call(caller, call_id)
        except BridgeError as exc:
            self.audit.write("answer_bridge_error", caller=caller, call_id=call_id, error=str(exc))
            return AnswerResult(False, "bridge_error", call_id)
        if not res.get("accepted"):
            self.audit.write("answer_core_rejected", caller=caller, call_id=call_id,
                             reason=res.get("reason"))
            return AnswerResult(False, "core_rejected", call_id, {"core": res.get("reason")})

        # 三道闸门（指纹 / 确认文件 / 环境变量）
        gates = self.guard.answer_gates()
        if not gates.allowed:
            self.audit.write("answer_gate_blocked", call_id=call_id, reason=gates.reason)
            return AnswerResult(False, gates.reason, call_id, gates.detail)

        # 幂等：同一通只点一次
        if call_id in self._answered_calls or call_id == self._last_clicked_call:
            self.audit.write("answer_duplicate_skip", call_id=call_id)
            return AnswerResult(False, "duplicate", call_id)

        det = det_pre

        point = self.fingerprint.call_point
        if point is None:
            self.audit.write("answer_no_click_point", call_id=call_id)
            return AnswerResult(False, "no_click_point", call_id)

        self.answer_attempts += 1
        action = self.actuator.click_point(det.window, point)
        self.audit.write("answer_click", call_id=call_id, ok=action.ok,
                         reason=action.reason, detail=action.detail or {})
        if not action.ok:
            return AnswerResult(False, f"click_failed:{action.reason}", call_id)

        # 点击成功后，让 Core 的协调器进入 in_call（唯一状态来源），再复核
        try:
            core_ans = self.bridge.answer(call_id)
        except BridgeError as exc:
            self.audit.write("answer_core_answer_error", call_id=call_id, error=str(exc))
            core_ans = {}
        if not core_ans.get("answered", True):
            self.audit.write("answer_core_refused_state", call_id=call_id,
                             reason=core_ans.get("reason"))
            return AnswerResult(False, "core_state_refused", call_id, {"core": core_ans.get("reason")})

        # 点击后复核真实状态
        verify = self._verify_in_call(call_id)
        if not verify:
            self.guard.mark_abnormal("answer_verify_failed")
            self.audit.write("answer_verify_failed", call_id=call_id)
            try:
                self.bridge.hangup(call_id, reason="answer_verify_failed", by="extension")
            except BridgeError:
                pass
            return AnswerResult(False, "verify_failed", call_id)

        self._answered_calls.add(call_id)
        self._last_clicked_call = call_id
        self.audit.write("answered", call_id=call_id, caller=caller)
        return AnswerResult(True, "answered", call_id)

    # ---------- 复核 ----------
    def _verify_in_call(self, call_id: str, attempts: int = 3, delay: float = 0.35) -> bool:
        for i in range(max(1, attempts)):
            snap = self.bridge.snapshot()
            if snap.get("state") == "in_call":
                return True
            state = self.detector.detect().state
            if state is CallState.ACTIVE:
                try:
                    self.bridge.answer(call_id)
                except BridgeError:
                    return False
                return self.bridge.snapshot().get("state") == "in_call"
            time.sleep(delay)
        return False

    # ---------- 账号条件 ----------
    @staticmethod
    def _account_confirmed(snap: dict) -> bool:
        # 协调器必须给出明确的拓扑信息，否则视为无法确认
        topo = snap.get("session_topology")
        return bool(topo) and topo != "unknown"

    @staticmethod
    def _single_account_ok(snap: dict) -> bool:
        # 需要账号交接的拓扑（独立客户端/远端）视为"互斥条件不满足"
        if snap.get("requires_account_handover") is True:
            return False
        return True
