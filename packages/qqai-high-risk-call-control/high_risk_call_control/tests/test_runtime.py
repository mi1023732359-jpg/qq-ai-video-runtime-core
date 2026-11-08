# -*- coding: utf-8 -*-
"""运行时编排测试：默认关闭、重启安全、互斥、崩溃恢复、无失控循环。"""
import hashlib
import json
import os
import tempfile
import time
import unittest

from high_risk_call_control.tests import fakes


def build_runtime(*, enabled=False, auto_answer=True, auto_hangup=True,
                  windows=None, fail_click="", topology="split_sessions",
                  recover_state=None, env_click=True):
    from high_risk_call_control.audit import AuditLog
    from high_risk_call_control.core_bridge import CoreBridge
    from high_risk_call_control.runtime import CallRuntime
    tmp = tempfile.TemporaryDirectory()
    fp_path = os.path.join(tmp.name, "fp.json")
    cf_path = os.path.join(tmp.name, "cf.json")
    with open(fp_path, "w", encoding="utf-8") as fh:
        json.dump({"call_candidates": [{"hwnd": 1}], "call_classes": ["QQCallWnd"],
                   "active_classes": ["QQCallWndActive"],
                   "call_point": [20, 30], "hangup_point": [10, 12]}, fh)
    with open(fp_path, "rb") as fh:
        fp_sha = hashlib.sha256(fh.read()).hexdigest()
    with open(cf_path, "w", encoding="utf-8") as fh:
        json.dump({"automation_id": "ctrl-answer-001", "confirmed_by": "director",
                   "issued_at": int(time.time()), "fingerprint_sha256": fp_sha}, fh)
    cfg = fakes.cfg(enabled=enabled, enable_auto_answer=auto_answer,
                    enable_auto_hangup=auto_hangup,
                    window_fingerprint_file=fp_path, answer_confirm_file=cf_path,
                    audit_log_path=os.path.join(tmp.name, "audit.jsonl"),
                    state_dir=os.path.join(tmp.name, "state"))
    clock = fakes.FakeClock()
    audit = AuditLog(cfg.audit_log_path, clock=clock)
    coord = fakes.FakeCoordinator(topology=topology, recover_state=recover_state, clock=clock)
    bridge = CoreBridge(coord)
    probe = fakes.FakeProbe(windows if windows is not None else [])
    actuator = fakes.FakeActuator(fail_click)
    fingerprint = fakes.fp()
    rt = CallRuntime(cfg, bridge, probe=probe, actuator=actuator, audit=audit,
                     fingerprint=fingerprint, clock=clock, wall=clock,
                     sleep=lambda s: None,
                     notify_admin=lambda m, d=None: None)
    if env_click:
        rt.guard.env["VIDEO_AUTO_ANSWER_CLICK"] = "1"
    return tmp, rt, coord, probe, actuator


def CallRuntimeLike(prev):
    """用同一份配置/依赖造一个新实例（模拟进程重启）。"""
    from high_risk_call_control.runtime import CallRuntime
    return CallRuntime(prev.cfg, prev.bridge, probe=prev.probe, actuator=prev.actuator,
                       audit=prev.audit, fingerprint=prev.fingerprint,
                       clock=prev.clock, wall=prev.wall, sleep=lambda s: None,
                       notify_admin=lambda m, d=None: None)


class TestRuntime(unittest.TestCase):
    def test_disabled_runtime_touches_nothing(self):
        tmp, rt, coord, probe, actuator = build_runtime(enabled=False, windows=[fakes.win()])
        try:
            init = rt.initialize()
            self.assertFalse(init["ok"])
            out = rt.step()
            self.assertEqual(out["action"], "none")
            self.assertEqual(probe.calls, 0, "关闭时不得探测窗口")
            self.assertEqual(actuator.clicks, [])
            self.assertEqual(coord.calls, [], "关闭时不得调用协调器")
            self.assertFalse(rt.status().enabled)
        finally:
            tmp.cleanup()

    def test_initialize_disables_stale_auto_answer(self):
        tmp, rt, coord, probe, actuator = build_runtime(enabled=True, auto_answer=False,
                                                        windows=[])
        try:
            self.assertTrue(coord.auto_answer_flag, "前置条件：协调器里有意残留 True")
            rt.initialize()
            self.assertFalse(coord.auto_answer_flag, "重启后必须显式关闭未授权的自动接听")
        finally:
            tmp.cleanup()

    def test_restart_syncs_auto_answer_true_when_authorized(self):
        tmp, rt, coord, probe, actuator = build_runtime(enabled=True, auto_answer=True, windows=[])
        try:
            coord.auto_answer_flag = False      # 模拟 Core 侧残留为 False
            rt.initialize()
            self.assertTrue(coord.auto_answer_flag, "已授权时必须显式同步为 True")
        finally:
            tmp.cleanup()

    def test_abnormal_state_persists_across_restart(self):
        tmp, rt, coord, probe, actuator = build_runtime(enabled=True, windows=[])
        try:
            path = rt._abnormal_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('{"abnormal": true}', encoding="utf-8")
            rt2 = CallRuntimeLike(rt)
            rt2.initialize()
            self.assertTrue(rt2.guard.abnormal, "历史异常必须保持，直到管理员清除")
        finally:
            tmp.cleanup()

    def test_recover_from_crash_is_called(self):
        tmp, rt, coord, probe, actuator = build_runtime(
            enabled=True, windows=[], recover_state={"state": "idle", "recovered": True})
        try:
            rt.initialize()
            self.assertEqual(coord.state, "idle")
        finally:
            tmp.cleanup()

    def test_incoming_detected_but_auto_answer_off(self):
        tmp, rt, coord, probe, actuator = build_runtime(enabled=True, auto_answer=False,
                                                        windows=[fakes.win()])
        try:
            rt.initialize()
            out = rt.step()
            self.assertEqual(out["action"], "observed")
            self.assertEqual(out["reason"], "auto_answer_off")
            self.assertEqual(actuator.clicks, [])
        finally:
            tmp.cleanup()

    def test_incoming_answered_and_lease_held(self):
        tmp, rt, coord, probe, actuator = build_runtime(enabled=True, windows=[fakes.win()])
        try:
            rt.initialize()
            rt.notify_incoming("10002", "call-answer-1")
            out = rt.step()
            self.assertEqual(out["action"], "answer")
            self.assertTrue(out["ok"], out.get("reason"))
            self.assertEqual(coord.state, "in_call")
            self.assertFalse(coord.send_allowed(), "通话中必须持有发送租约")
            # 通话期间普通消息仍可记录，但不允许发送
            rec = coord.record_incoming_message("chat-1", "10002", "你好")
            self.assertTrue(rec["recorded"])
            self.assertFalse(rec["send_allowed"])
        finally:
            tmp.cleanup()

    def test_max_duration_hangup_releases_lease(self):
        tmp, rt, coord, probe, actuator = build_runtime(
            enabled=True, windows=[fakes.win(cls="QQCallWndActive")])
        try:
            rt.initialize()
            coord.on_incoming_call("10002", "call-r")
            coord.answer("call-r")
            rt.clock.advance(rt.cfg.max_call_seconds + 1)
            out = rt.step()
            self.assertEqual(out["action"], "hangup")
            self.assertTrue(out["ok"], out.get("reason"))
            self.assertTrue(coord.send_allowed(), "挂断后必须释放租约")
        finally:
            tmp.cleanup()

    def test_window_gone_triggers_cleanup(self):
        tmp, rt, coord, probe, actuator = build_runtime(enabled=True, windows=[])
        try:
            rt.initialize()
            coord.on_incoming_call("10002", "call-g")
            coord.answer("call-g")
            probe.windows = []
            out = rt.step()
            self.assertEqual(out["action"], "hangup")
            self.assertEqual(out["reason"], "window_gone")
            self.assertTrue(coord.send_allowed())
        finally:
            tmp.cleanup()

    def test_admin_stop_prevents_actions(self):
        tmp, rt, coord, probe, actuator = build_runtime(enabled=True, windows=[fakes.win()])
        try:
            rt.initialize()
            rt.stop()
            out = rt.step()
            self.assertEqual(out["action"], "none")
            self.assertEqual(out["reason"], "admin_stopped")
            self.assertEqual(actuator.clicks, [])
        finally:
            tmp.cleanup()

    def test_run_loop_is_bounded(self):
        tmp, rt, coord, probe, actuator = build_runtime(enabled=True, windows=[])
        try:
            rt.initialize()
            steps = rt.run(max_ticks=5, interval=0)
            self.assertLessEqual(len(steps), 5)
        finally:
            tmp.cleanup()

    def test_unknown_detection_no_action(self):
        tmp, rt, coord, probe, actuator = build_runtime(enabled=True, windows=[fakes.win()])
        try:
            rt.initialize()
            probe.windows = [fakes.win(hwnd=1), fakes.win(hwnd=2)]
            out = rt.step()
            self.assertEqual(out["action"], "none")
            self.assertEqual(out["reason"], "unknown")
            self.assertEqual(actuator.clicks, [])
        finally:
            tmp.cleanup()

    def test_rate_limit_blocks_actions(self):
        tmp, rt, coord, probe, actuator = build_runtime(enabled=True, windows=[fakes.win()])
        try:
            rt.initialize()
            rt.guard.rate.limit = 1
            rt.guard.rate._events = [rt.clock()]      # 预算已用尽
            rt.notify_incoming("10002", "call-rl")
            out = rt.step()
            self.assertEqual(out["action"], "answer")
            self.assertFalse(out["ok"])
            self.assertEqual(out["reason"], "rate_limited")
            self.assertEqual(actuator.clicks, [])
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
