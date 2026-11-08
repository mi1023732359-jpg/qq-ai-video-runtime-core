# -*- coding: utf-8 -*-
"""接听 / 挂断控制器测试（全部使用 Fake，不触碰真实 QQ）。"""
import hashlib
import json
import os
import tempfile
import time
import unittest

from high_risk_call_control.tests import fakes


class AnswerHarness:
    """装配一个可用的 AnswerController，指纹/确认/环境变量三道闸门可分别破坏。"""

    def __init__(self, *, windows=None, fail_click="", verify_ok=True,
                 gates=True, caller="10002", cfg_over=None, topology="split_sessions"):
        self.tmp = tempfile.TemporaryDirectory()
        fp_path = os.path.join(self.tmp.name, "fingerprint.json")
        cf_path = os.path.join(self.tmp.name, "confirm.json")
        if gates:
            with open(fp_path, "w", encoding="utf-8") as fh:
                json.dump({"call_candidates": [{"hwnd": 1}],
                           "call_classes": ["QQCallWnd"],
                           "active_classes": ["QQCallWndActive"],
                           "call_point": [20, 30], "hangup_point": [10, 12]}, fh)
            self.write_confirm(cf_path, fp_path)
        over = dict(enable_auto_answer=True, enable_auto_hangup=True) if cfg_over is None else dict(cfg_over)
        base = dict(window_fingerprint_file=fp_path, answer_confirm_file=cf_path)
        base.update(over)
        self.cfg = fakes.cfg(**base)
        from high_risk_call_control.audit import AuditLog
        from high_risk_call_control.safety_guard import SafetyGuard
        from high_risk_call_control.call_policy import CallPolicy
        from high_risk_call_control.call_detector import CallDetector
        from high_risk_call_control.answer_controller import AnswerController
        from high_risk_call_control.core_bridge import CoreBridge
        self.clock = fakes.FakeClock()
        self.audit = AuditLog(os.path.join(self.tmp.name, "audit.jsonl"), clock=self.clock)
        env = {"VIDEO_AUTO_ANSWER_CLICK": "1"} if gates else {}
        self.guard = SafetyGuard(self.cfg, self.audit, clock=self.clock, env=env)
        self.bridge = CoreBridge(fakes.FakeCoordinator(topology=topology))
        self.fingerprint = fakes.fp()
        self.probe = fakes.FakeProbe(windows if windows is not None else [fakes.win()])
        self.detector = CallDetector(self.probe, self.fingerprint) if verify_ok else CallDetector(
            fakes.FakeProbe(windows or [fakes.win()]), self.fingerprint)
        self.actuator = fakes.FakeActuator(fail_click)
        self.ac = AnswerController(self.cfg, self.guard, CallPolicy(self.cfg),
                                   self.detector, self.actuator, self.bridge,
                                   self.fingerprint, self.audit,
                                   clock=self.clock, wall=self.clock)
        self.caller = caller

    @staticmethod
    def write_confirm(cf_path, fp_path, issued_at=None, sha=None, auto_id="ctrl-answer-001"):
        with open(fp_path, "rb") as fh:
            real_sha = hashlib.sha256(fh.read()).hexdigest()
        payload = {"automation_id": auto_id, "confirmed_by": "director",
                   "issued_at": int(time.time()) if issued_at is None else int(issued_at),
                   "fingerprint_sha256": real_sha if sha is None else sha}
        with open(cf_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)

    def close(self):
        self.tmp.cleanup()


class TestAnswerController(unittest.TestCase):
    def test_successful_answer_transitions_core(self):
        h = AnswerHarness()
        try:
            res = h.ac.handle_incoming(h.caller, "call-1")
            self.assertTrue(res.answered, res.reason)
            self.assertEqual(h.bridge.snapshot()["state"], "in_call")
            self.assertEqual(len(h.actuator.clicks), 1)
        finally:
            h.close()

    def test_unauthorized_caller_no_click(self):
        h = AnswerHarness(caller="30003")
        try:
            res = h.ac.handle_incoming(h.caller, "call-2")
            self.assertFalse(res.answered)
            self.assertEqual(res.reason, "caller_not_allowlisted")
            self.assertEqual(h.actuator.clicks, [])
        finally:
            h.close()

    def test_missing_fingerprint_blocks_click(self):
        h = AnswerHarness(gates=False)
        try:
            res = h.ac.handle_incoming(h.caller, "call-3")
            self.assertFalse(res.answered)
            self.assertIn(res.reason, ("fingerprint_missing", "answer_gates"))
            self.assertEqual(h.actuator.clicks, [])
        finally:
            h.close()

    def test_missing_env_flag_blocks_click(self):
        h = AnswerHarness()
        try:
            h.guard.env.pop("VIDEO_AUTO_ANSWER_CLICK", None)
            res = h.ac.handle_incoming(h.caller, "call-4")
            self.assertFalse(res.answered)
            self.assertEqual(res.reason, "env_flag_missing")
            self.assertEqual(h.actuator.clicks, [])
        finally:
            h.close()

    def test_no_incoming_window_blocks_click(self):
        h = AnswerHarness(windows=[])
        try:
            res = h.ac.handle_incoming(h.caller, "call-5")
            self.assertFalse(res.answered)
            self.assertIn("no_incoming_window", res.reason)
            self.assertEqual(h.actuator.clicks, [])
        finally:
            h.close()

    def test_click_failure_reported(self):
        h = AnswerHarness(fail_click="window_changed")
        try:
            res = h.ac.handle_incoming(h.caller, "call-6")
            self.assertFalse(res.answered)
            self.assertEqual(res.reason, "click_failed:window_changed")
        finally:
            h.close()

    def test_duplicate_answer_is_skipped(self):
        h = AnswerHarness()
        try:
            h.ac.handle_incoming(h.caller, "call-7")
            clicks = len(h.actuator.clicks)
            res = h.ac.handle_incoming(h.caller, "call-7")
            self.assertFalse(res.answered)
            self.assertEqual(len(h.actuator.clicks), clicks)
        finally:
            h.close()

    def test_admin_deny_before_any_click(self):
        h = AnswerHarness()
        try:
            res = h.ac.handle_incoming(h.caller, "call-8", admin_override=False)
            self.assertFalse(res.answered)
            self.assertEqual(res.reason, "admin_denied")
            self.assertEqual(h.actuator.clicks, [])
        finally:
            h.close()

    def test_admin_stop_blocks_answer(self):
        h = AnswerHarness()
        try:
            h.guard.admin_request_stop()
            res = h.ac.handle_incoming(h.caller, "call-9")
            self.assertFalse(res.answered)
            self.assertEqual(res.reason, "admin_stopped")
        finally:
            h.close()


class TestAnswerGates(unittest.TestCase):
    def test_gate_disabled_in_config_blocks_answer(self):
        h = AnswerHarness(cfg_over=dict(enable_auto_answer=True, require_env_click_flag=False))
        try:
            res = h.ac.handle_incoming(h.caller, "call-gate-1")
            self.assertFalse(res.answered)
            self.assertEqual(res.reason, "auto_answer_off")
            self.assertEqual(h.actuator.clicks, [])
        finally:
            h.close()

    def test_expired_confirm_blocks_click(self):
        h = AnswerHarness()
        try:
            h.write_confirm(h.cfg.answer_confirm_file, h.cfg.window_fingerprint_file,
                            issued_at=time.time() - 86400 * 3)
            res = h.ac.handle_incoming(h.caller, "call-gate-2")
            self.assertFalse(res.answered)
            self.assertEqual(res.reason, "confirm_expired")
            self.assertEqual(h.actuator.clicks, [])
        finally:
            h.close()

    def test_confirm_binding_mismatch_blocks_click(self):
        h = AnswerHarness()
        try:
            h.write_confirm(h.cfg.answer_confirm_file, h.cfg.window_fingerprint_file,
                            sha="0" * 64)
            res = h.ac.handle_incoming(h.caller, "call-gate-3")
            self.assertFalse(res.answered)
            self.assertEqual(res.reason, "confirm_binding_mismatch")
            self.assertEqual(h.actuator.clicks, [])
        finally:
            h.close()

    def test_missing_issued_at_blocks_click(self):
        h = AnswerHarness()
        try:
            with open(h.cfg.window_fingerprint_file, "rb") as fh:
                fp_sha = hashlib.sha256(fh.read()).hexdigest()
            with open(h.cfg.answer_confirm_file, "w", encoding="utf-8") as fh:
                json.dump({"automation_id": "ctrl-answer-001",
                           "confirmed_by": "director",
                           "fingerprint_sha256": fp_sha}, fh)
            res = h.ac.handle_incoming(h.caller, "call-gate-4")
            self.assertFalse(res.answered)
            self.assertEqual(res.reason, "confirm_missing_issued_at")
        finally:
            h.close()


class TestUiGuard(unittest.TestCase):
    def test_pid_change_blocks_click(self):
        from high_risk_call_control.qq_ui_controller import Win32ClickActuator
        target = fakes.win(hwnd=77, pid=1000)
        changed = fakes.win(hwnd=77, pid=2000)

        class Probe:
            def list_windows(self):
                return [changed]

        act = Win32ClickActuator(Probe())
        res = act.click_point(target, (10, 10))
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, "window_pid_changed")

    def test_point_outside_window_rejected(self):
        from high_risk_call_control.qq_ui_controller import Win32ClickActuator
        target = fakes.win(hwnd=78, rect=(0, 0, 100, 100))
        act = Win32ClickActuator(type("P", (), {"list_windows": lambda self: [target]})())
        res = act.click_point(target, (500, 500))
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, "point_outside_window")


class HangupHarness:
    def __init__(self, *, windows=None, fail_click="", call_active=True,
                 cfg_over=None, topology="split_sessions", hangup_raises=False):
        self.tmp = tempfile.TemporaryDirectory()
        over = dict(enable_auto_hangup=True) if cfg_over is None else dict(cfg_over)
        self.cfg = fakes.cfg(**over)
        from high_risk_call_control.audit import AuditLog
        from high_risk_call_control.safety_guard import SafetyGuard
        from high_risk_call_control.call_detector import CallDetector
        from high_risk_call_control.hangup_controller import HangupController
        from high_risk_call_control.core_bridge import CoreBridge
        self.clock = fakes.FakeClock()
        self.audit = AuditLog(os.path.join(self.tmp.name, "audit.jsonl"), clock=self.clock)
        self.guard = SafetyGuard(self.cfg, self.audit, clock=self.clock,
                                 env={"VIDEO_AUTO_ANSWER_CLICK": "1"})
        self.coord = fakes.FakeCoordinator(topology=topology, clock=self.clock,
                                           hangup_raises=hangup_raises)
        self.bridge = CoreBridge(self.coord)
        if call_active:
            self.coord.on_incoming_call("10002", "call-h")
            self.coord.answer("call-h")
        self.fingerprint = fakes.fp()
        default_windows = [fakes.win(cls="QQCallWndActive")] if call_active else []
        self.probe = fakes.FakeProbe(windows if windows is not None else default_windows)
        self.detector = CallDetector(self.probe, self.fingerprint)
        self.actuator = fakes.FakeActuator(fail_click)
        self.notified = []
        self.hc = HangupController(self.cfg, self.guard, self.detector, self.actuator,
                                   self.bridge, self.fingerprint, self.audit,
                                   notify_admin=lambda m, d=None: self.notified.append(m),
                                   clock=self.clock, sleep=lambda s: None)

    def close(self):
        self.tmp.cleanup()


class TestHangupController(unittest.TestCase):
    def test_max_duration_triggers_hangup_and_releases_lease(self):
        h = HangupHarness()
        try:
            self.assertFalse(h.coord.send_allowed())
            due, reason = h.hc.should_hangup("call-h")
            self.assertFalse(due)
            h.clock.advance(h.cfg.max_call_seconds + 1)
            due, reason = h.hc.should_hangup("call-h")
            self.assertTrue(due)
            self.assertEqual(reason, "max_duration")
            res = h.hc.hangup("call-h", reason=reason)
            self.assertTrue(res.hung_up, res.reason)
            self.assertIsNone(h.coord.lease)
            self.assertTrue(h.coord.send_allowed())
        finally:
            h.close()

    def test_admin_force_hangup(self):
        h = HangupHarness()
        try:
            res = h.hc.hangup("call-h", reason="admin_hangup", by="admin")
            self.assertTrue(res.hung_up)
            self.assertIn("admin_hangup", h.coord.hangup_reasons)
        finally:
            h.close()

    def test_duplicate_hangup_is_idempotent(self):
        h = HangupHarness()
        try:
            first = h.hc.hangup("call-h", reason="user_request")
            self.assertTrue(first.hung_up)
            clicks = len(h.actuator.clicks)
            second = h.hc.hangup("call-h", reason="user_request")
            self.assertFalse(second.hung_up)
            self.assertEqual(second.reason, "duplicate")
            self.assertEqual(len(h.actuator.clicks), clicks)
        finally:
            h.close()

    def test_hangup_failure_exhausts_retries_then_abnormal(self):
        # 窗口一直在、点击一直失败 → 有限重试后进入异常并通知管理员
        h = HangupHarness(fail_click="click_error:OSError", hangup_raises=True,
                          cfg_over=dict(enable_auto_hangup=True, hangup_max_retries=2))
        try:
            res = h.hc.hangup("call-h", reason="max_duration")
            self.assertFalse(res.hung_up)
            self.assertEqual(res.reason, "hangup_failed")
            self.assertLessEqual(res.attempts, h.cfg.hangup_max_retries + 1)
            self.assertTrue(h.guard.abnormal)
            self.assertTrue(h.notified)
            self.assertIsNotNone(h.coord.lease, "失败时不得伪造已释放租约")
        finally:
            h.close()

    def test_hangup_total_attempts_capped_at_five(self):
        h = HangupHarness(fail_click="click_error:OSError", hangup_raises=True,
                          cfg_over=dict(enable_auto_hangup=True, hangup_max_retries=5))
        try:
            res = h.hc.hangup("call-h", reason="max_duration")
            self.assertFalse(res.hung_up)
            self.assertLessEqual(res.attempts, 5)
            self.assertLessEqual(len(h.actuator.clicks), 5)
        finally:
            h.close()

    def test_window_gone_counts_as_success(self):
        h = HangupHarness(windows=[])
        try:
            res = h.hc.hangup("call-h", reason="window_gone")
            self.assertTrue(res.hung_up, res.reason)
            self.assertEqual(len(h.actuator.clicks), 0)
        finally:
            h.close()


if __name__ == "__main__":
    unittest.main()
