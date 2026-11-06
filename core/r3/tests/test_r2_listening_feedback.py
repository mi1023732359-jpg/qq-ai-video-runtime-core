#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""R2.1 Listening Feedback unit tests (no HTTP / no real QQ call)."""
from __future__ import annotations

import random
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "r2"))

from avatar_decision import EMOTION_ACTIONS, AvatarRuntime  # noqa: E402
from listening_feedback import ListeningFeedback  # noqa: E402


class Clock:
    def __init__(self, t: float = 1000.0):
        self.t = float(t)

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> float:
        self.t += float(seconds)
        return self.t


class FakeAvatar:
    def __init__(self):
        self.calls: list[dict] = []
        self.priority = 0
        self.expires = 0.0
        self.current = ""

    def request(self, emotion, reason="", force=False, duration=None, timeout=8.0):
        prio = EMOTION_ACTIONS[emotion][3]
        if not force and prio < self.priority and self.expires > 0:
            return {"applied": False, "why": "lower_priority", "emotion": emotion}
        self.calls.append({"emotion": emotion, "reason": reason, "duration": duration,
                           "timeout": timeout})
        self.current = emotion
        self.priority = prio
        self.expires = 1.0
        return {"applied": True, "emotion": emotion, "duration": duration}


class ListeningFeedbackTests(unittest.TestCase):
    def make(self, *, avatar=None, voice_enabled=False, voice_delay=8.0,
             start_worker=False, speaking=None, queue_max=32):
        self.clock = Clock()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        return ListeningFeedback(
            avatar=avatar if avatar is not None else FakeAvatar(),
            speaking=speaking if speaking is not None else {},
            enabled=True, voice_enabled=voice_enabled, voice_delay=voice_delay,
            rng=random.Random(7), clock=self.clock,
            log_path=Path(self.tmp.name) / "feedback.jsonl",
            session_id="test-session", start_worker=start_worker,
            queue_max=queue_max,
        )

    def test_event_state_mapping_and_actions(self):
        avatar = FakeAvatar()
        fb = self.make(avatar=avatar)
        t = self.clock.t

        fb.handle_event("user_speech_started", {"rms": 0.08}, now=t)
        self.assertEqual(fb.state, "listening")
        self.assertTrue(any(c["emotion"] == "listening" for c in avatar.calls))
        self.assertGreater(fb.next_feedback_at, t)

        fb.handle_event("user_speech_paused", {"rms": 0.001}, now=t + 0.5)
        self.assertEqual(fb.state, "thinking")

        fb.handle_event("asr_processing", {"audio_s": 2.0}, now=t + 0.7)
        self.assertEqual(fb.state, "thinking")
        # 若 listening 动作尚在 TTL 内，P4 的 asr_thinking 不应覆盖 P3；
        # 这里只验证状态推进，不强制要求动作被应用（真实 AvatarRuntime 同样会拒绝）。

        fb.handle_event("asr_completed", {"text": "你好", "asr_s": 1.0}, now=t + 1.0)
        self.assertEqual(fb.state, "acknowledging")
        self.assertTrue(any(c["emotion"] == "acknowledging" for c in avatar.calls))

        fb.handle_event("dialogue_processing", {"text": "你好"}, now=t + 1.1)
        self.assertTrue(fb.pending_thinking)
        fb.tick(t + 1.9)
        self.assertEqual(fb.state, "thinking")

        fb.handle_event("reply_started", {"seconds": 3.0, "kind": "reply"}, now=t + 2.0)
        self.assertEqual(fb.state, "speaking")
        before = len(avatar.calls)
        fb.tick(t + 3.0)
        self.assertEqual(len(avatar.calls), before)  # TTS 期间不插反馈
        fb.tick(t + 6.0)
        self.assertEqual(fb.state, "idle")

        fb.handle_event("call_ended", {"why": "test"}, now=t + 6.1)
        self.assertTrue(fb.closed)
        self.assertEqual(fb.state, "ended")
        fb.close("test")
        self.assertTrue(fb.snapshot()["closed"])

    def test_continuous_speech_does_not_reset_interval(self):
        fb = self.make()
        t = self.clock.t
        fb.handle_event("user_speech_started", {}, now=t)
        first_next = fb.next_feedback_at
        fb.handle_event("user_speech_started", {}, now=t + 1.0)
        self.assertEqual(fb.next_feedback_at, first_next)
        # 只在到达调度点时才动作
        calls_before = len(fb.avatar.calls)
        fb.tick(first_next + 0.05)
        self.assertGreater(len(fb.avatar.calls), calls_before)
        self.assertGreater(fb.next_feedback_at, first_next)

    def test_tts_suppresses_and_does_not_override(self):
        speaking = {"speaking_until": 1010.0}
        fb = self.make(speaking=speaking)
        fb.handle_event("user_speech_started", {}, now=1001.0)
        self.assertEqual(fb.actions_suppressed, 1)
        self.assertEqual(fb.avatar.calls, [])
        fb.handle_event("dialogue_processing", {}, now=1002.0)
        fb.tick(1003.0)
        self.assertEqual(fb.avatar.calls, [])

    def test_voice_once_after_delay_and_per_turn(self):
        voice = []
        fb = self.make(voice_enabled=True, voice_delay=8.0)
        fb.set_voice_cb(lambda text: voice.append(text))
        t = self.clock.t
        fb.handle_event("user_speech_started", {}, now=t)
        fb.handle_event("asr_processing", {"audio_s": 7.0}, now=t + 0.2)
        fb.tick(t + 4.0)
        self.assertEqual(voice, [])
        fb.tick(t + 8.3)
        self.assertEqual(len(voice), 1)
        fb.tick(t + 9.0)
        self.assertEqual(len(voice), 1)

    def test_asr_error_reasks_once_without_pretending(self):
        voice = []
        fb = self.make(voice_enabled=True)
        fb.set_voice_cb(lambda text: voice.append(text))
        t = self.clock.t
        fb.handle_event("user_speech_started", {}, now=t)
        fb.handle_event("asr_error", {"error": "TimeoutError: timed out"}, now=t + 1.0)
        self.assertEqual(fb.state, "acknowledging")
        self.assertEqual(voice, ["我没听清，能再说一遍吗？"])
        fb.handle_event("asr_error", {"error": "again"}, now=t + 2.0)
        self.assertEqual(len(voice), 1)

    def test_bounded_queue_drops_without_blocking(self):
        fb = self.make(queue_max=3)
        for i in range(10):
            fb.emit("user_speech_started", rms=i / 10.0)
        self.assertEqual(fb.dropped_events, 7)
        self.assertLessEqual(fb._queue.qsize(), 3)
        fb.drain()
        fb.close("test")

    def test_action_duration_in_range_and_repeat_suppressed(self):
        fb = self.make()
        t = self.clock.t
        fb.handle_event("user_speech_started", {}, now=t)
        for _ in range(5):
            fb.tick(t + 100)
            t += 100
        self.assertGreaterEqual(fb.actions_applied, 2)
        durations = [c["duration"] for c in fb.avatar.calls if c["duration"] is not None]
        self.assertTrue(durations)
        self.assertTrue(all(0.5 <= d <= 1.5 for d in durations))
        self.assertTrue(all(a != b for a, b in zip(list(fb.recent_actions), list(fb.recent_actions)[1:])))

    def test_rejected_transcript_does_not_acknowledge_understanding(self):
        avatar = FakeAvatar()
        fb = self.make(avatar=avatar)
        t = self.clock.t
        fb.handle_event("user_speech_started", {}, now=t)
        fb.handle_event("asr_processing", {}, now=t + 1.0)
        fb.handle_event("asr_completed", {"text": "噪声", "accepted": False,
                                          "reason": "chars=1"}, now=t + 1.2)
        self.assertNotEqual(fb.state, "acknowledging")
        self.assertFalse(any(c["emotion"] == "acknowledging" for c in avatar.calls))

    def test_silence_without_speech_stays_idle(self):
        fb = self.make()
        fb.handle_event("user_speech_paused", {"rms": 0.0}, now=self.clock.t)
        self.assertEqual(fb.state, "idle")
        fb.tick(self.clock.t + 100)
        self.assertEqual(fb.avatar.calls, [])

    def test_cooldown_blocks_immediate_second_action(self):
        fb = self.make()
        t = self.clock.t
        fb.handle_event("user_speech_started", {}, now=t)
        first_calls = len(fb.avatar.calls)
        # 调度动作必须遵守 cooldown；事件驱动的状态切换不走这条路径。
        fb._apply_action("listening", t + 0.2, reason="scheduled")
        self.assertEqual(len(fb.avatar.calls), first_calls)
        self.assertGreaterEqual(fb.actions_suppressed, 1)

    def test_close_clears_pending_and_worker_can_join(self):
        fb = self.make(start_worker=True)
        fb.emit("user_speech_started", rms=0.05)
        fb.close("test")
        self.assertTrue(fb.closed)
        self.assertEqual(fb.state, "ended")
        self.assertFalse(fb._queue.qsize())

    def test_three_turn_sequence_keeps_state_and_cleans_up(self):
        fb = self.make()
        t = self.clock.t
        for turn in range(3):
            fb.handle_event("user_speech_started", {}, now=t)
            fb.handle_event("user_speech_paused", {}, now=t + 0.8)
            fb.handle_event("asr_processing", {}, now=t + 1.0)
            fb.handle_event("asr_completed", {"text": f"第{turn}轮", "accepted": True},
                            now=t + 1.5)
            fb.handle_event("dialogue_processing", {}, now=t + 1.6)
            fb.handle_event("reply_started", {"seconds": 2.0}, now=t + 2.0)
            fb.tick(t + 4.2)
            t += 5.0
        self.assertGreaterEqual(fb.turn_id, 3)
        self.assertGreaterEqual(fb.actions_applied, 3)
        fb.handle_event("call_ended", {}, now=t)
        fb.close("test")
        self.assertTrue(fb.closed)
        self.assertFalse(fb._queue.qsize())

    def test_priority_constants(self):
        # P2 serious 必须高于 P3 listening，P3 高于 P4 thinking，P5=0
        self.assertLess(EMOTION_ACTIONS["listening"][3], EMOTION_ACTIONS["serious"][3])
        self.assertLess(EMOTION_ACTIONS["asr_thinking"][3], EMOTION_ACTIONS["listening"][3])
        self.assertEqual(EMOTION_ACTIONS["idle"][3], 0)


class AvatarPriorityTests(unittest.TestCase):
    def test_low_feedback_cannot_override_dialogue_emotion(self):
        with tempfile.TemporaryDirectory() as td:
            rt = AvatarRuntime(log_path=Path(td) / "avatar.jsonl")
            rt._post = lambda *a, **k: {"ok": True}
            self.assertTrue(rt.request("happy", reason="reply", force=True)["applied"])
            self.assertFalse(rt.request("listening", reason="feedback")["applied"])
            self.assertTrue(rt.request("idle", reason="reset", force=True)["applied"])
            self.assertTrue(rt.request("listening", reason="feedback", duration=1.0)["applied"])
            self.assertEqual(rt.priority, EMOTION_ACTIONS["listening"][3])
            # 高优先级仍可抢占低优先级反馈
            self.assertTrue(rt.request("serious", reason="reply")["applied"])


if __name__ == "__main__":
    unittest.main()
