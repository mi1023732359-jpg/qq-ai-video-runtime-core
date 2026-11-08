# -*- coding: utf-8 -*-
"""检测器与策略测试。"""
import unittest

from high_risk_call_control.tests import fakes


class TestDetector(unittest.TestCase):
    def test_idle_when_no_windows(self):
        from high_risk_call_control.call_detector import CallDetector, CallState
        det = CallDetector(fakes.FakeProbe([]), fakes.fp())
        self.assertIs(det.detect().state, CallState.IDLE)

    def test_incoming_window(self):
        from high_risk_call_control.call_detector import CallDetector, CallState
        det = CallDetector(fakes.FakeProbe([fakes.win()]), fakes.fp())
        self.assertIs(det.detect().state, CallState.INCOMING)

    def test_active_window(self):
        from high_risk_call_control.call_detector import CallDetector, CallState
        det = CallDetector(fakes.FakeProbe([fakes.win(cls="QQCallWndActive")]), fakes.fp())
        self.assertIs(det.detect().state, CallState.ACTIVE)

    def test_window_not_found_is_idle_not_actionable(self):
        from high_risk_call_control.call_detector import CallDetector
        det = CallDetector(fakes.FakeProbe([fakes.win(cls="OtherWnd", title="记事本")]), fakes.fp())
        res = det.detect()
        self.assertFalse(res.actionable)
        self.assertIsNone(res.window)

    def test_ambiguous_windows_is_unknown(self):
        from high_risk_call_control.call_detector import CallDetector, CallState
        det = CallDetector(fakes.FakeProbe([fakes.win(hwnd=1), fakes.win(hwnd=2)]), fakes.fp())
        self.assertIs(det.detect().state, CallState.UNKNOWN)

    def test_probe_error_is_unknown(self):
        from high_risk_call_control.call_detector import CallDetector, CallState
        det = CallDetector(fakes.FakeProbe(error=RuntimeError("boom")), fakes.fp())
        self.assertIs(det.detect().state, CallState.UNKNOWN)

    def test_foreign_process_filtered(self):
        from high_risk_call_control.call_detector import CallDetector, CallState
        det = CallDetector(fakes.FakeProbe([fakes.win(process="notepad.exe")]), fakes.fp())
        self.assertIs(det.detect().state, CallState.IDLE)

    def test_unusable_fingerprint_is_unknown(self):
        from high_risk_call_control.call_detector import CallDetector, CallState, Fingerprint
        det = CallDetector(fakes.FakeProbe([fakes.win()]), Fingerprint())
        self.assertIs(det.detect().state, CallState.UNKNOWN)


class TestPolicy(unittest.TestCase):
    def _decide(self, **over):
        from high_risk_call_control.call_policy import CallPolicy, PolicyInput
        cfg = over.pop("cfg", fakes.cfg())
        pol = CallPolicy(cfg)
        base = dict(caller="10002", now_hhmm=(12, 0), in_call=False, ringing=True,
                    core_authorized=None, account_confirmed=True, single_account_ok=True)
        base.update(over)
        return pol.decide(PolicyInput(**base))

    def test_unauthorized_caller_rejected(self):
        res = self._decide(caller="30003")
        self.assertFalse(res.allow)
        self.assertEqual(res.reason, "caller_not_allowlisted")

    def test_denylisted_caller_rejected(self):
        res = self._decide(caller="19999")
        self.assertFalse(res.allow)
        self.assertEqual(res.reason, "caller_in_denylist")

    def test_dnd_rejected(self):
        cfg = fakes.cfg(dnd_windows=(((22, 0), (8, 0)),))
        res = self._decide(cfg=cfg, now_hhmm=(23, 30))
        self.assertFalse(res.allow)
        self.assertEqual(res.reason, "dnd_window")

    def test_admin_bypasses_dnd(self):
        cfg = fakes.cfg(dnd_windows=(((22, 0), (8, 0)),))
        res = self._decide(cfg=cfg, caller="10001", now_hhmm=(23, 30))
        self.assertTrue(res.allow)

    def test_admin_can_disable_auto_answer(self):
        cfg = fakes.cfg(enable_auto_answer=False)
        res = self._decide(cfg=cfg)
        self.assertFalse(res.allow)
        self.assertEqual(res.reason, "auto_answer_off")

    def test_duplicate_event_rejected(self):
        res = self._decide(duplicate=True)
        self.assertFalse(res.allow)
        self.assertEqual(res.reason, "duplicate_event")

    def test_second_call_while_in_call_rejected(self):
        res = self._decide(in_call=True)
        self.assertFalse(res.allow)
        self.assertEqual(res.reason, "already_in_call")

    def test_admin_deny_has_priority(self):
        res = self._decide(admin_override=False, caller="10002")
        self.assertFalse(res.allow)
        self.assertEqual(res.reason, "admin_denied")

    def test_account_unconfirmed_rejected(self):
        res = self._decide(account_confirmed=False)
        self.assertFalse(res.allow)
        self.assertEqual(res.reason, "account_unconfirmed")

    def test_single_account_unsatisfied_rejected(self):
        res = self._decide(single_account_ok=False)
        self.assertFalse(res.allow)
        self.assertEqual(res.reason, "single_account_unsatisfied")


if __name__ == "__main__":
    unittest.main()
