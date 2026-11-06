#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""R2.1 Listening Feedback：把已有对话事件映射成轻量 Live2D 聆听/思考反馈。

设计边界（重要）：
    · 不改 ASR、DialogueService、AvatarRuntime 的业务逻辑；
    · 只在**独立线程**里消费有界事件队列，主采集/ASR/对话循环只做非阻塞 emit；
    · 动作仍走现有 `AvatarRuntime` + `mini_control`；优先级由 AvatarRuntime 的
      priority/TTL 决定，本模块只发低优先级状态，绝不覆盖 TTS/对话情绪；
    · 普通等待语音默认关闭；开启后也必须走现有 speak_cb（TTS/半双工/急停）。

事件（全部来自 conversation_loop，不新建 ASR 状态机）：
    user_speech_started / user_speech_paused
    asr_processing / asr_completed / asr_error
    dialogue_processing / reply_started / reply_finished / call_ended

状态优先级（展示层）：
    P0 急停/ended > P1 speaking(TTS) > P2 对话情绪(AvatarRuntime 高优先级)
    > P3 listening > P4 asr_thinking > P5 idle
"""
from __future__ import annotations

import json
import queue
import random
import threading
import time
from collections import deque
from pathlib import Path

R2_DIR = Path(__file__).resolve().parent
LOG_DIR = R2_DIR.parent / "logs"
DEFAULT_LOG = LOG_DIR / "listening_feedback.jsonl"

#: 可复用的现有轻量动作（不新增 Live2D 素材）
FEEDBACK_ACTIONS = {
    "listening": ("", "狗尾草切换出来"),
    "acknowledging": ("", "狗尾草切回默认手"),
    "asr_thinking": ("加载", "举手切默认手"),
    "asr_error": ("流冷汗", "狗尾草切回默认手"),
}

#: 每个展示状态可用的轻量动作：交替使用，避免连续重复
ACTION_CHOICES = {
    "listening": ("listening", "acknowledging"),
    "thinking": ("asr_thinking", "acknowledging"),
}

#: R2.1 对外状态名（内部保留短名，避免改变现有动作/测试语义）
STATE_ALIASES = {
    "idle": "IDLE",
    "listening": "LISTENING",
    "thinking": "PROCESSING_AUDIO",
    "asr_delayed": "ASR_DELAYED",
    "acknowledging": "UNDERSTOOD",
    "asr_error": "ASR_FAILED",
    "speaking": "SPEAKING",
    "ended": "ENDED",
}


class ListeningFeedback:
    """有界队列 + 独立 worker 的展示层反馈调度器。"""

    def __init__(self, avatar=None, speaking: dict | None = None,
                 voice_cb=None, enabled: bool = True, voice_enabled: bool = False,
                 voice_delay: float = 8.0, interval=(6.0, 14.0),
                 action_range=(0.5, 1.5), cooldown: float = 2.0,
                 queue_max: int = 32, rng: random.Random | None = None,
                 clock=None, log_path: Path | None = None,
                 session_id: str | None = None, start_worker: bool = True):
        self.avatar = avatar
        self.speaking = speaking if speaking is not None else {}
        self.voice_cb = voice_cb
        self.enabled = bool(enabled)
        self.voice_enabled = bool(voice_enabled)
        self.voice_delay = max(0.0, float(voice_delay))
        self.interval = (max(0.1, float(interval[0])), max(float(interval[0]), float(interval[1])))
        self.action_range = (max(0.05, float(action_range[0])),
                             max(float(action_range[0]), float(action_range[1])))
        self.cooldown = max(0.0, float(cooldown))
        self.queue_max = max(1, int(queue_max))
        self.rng = rng or random.Random()
        self.clock = clock or time.time
        self.log_path = Path(log_path) if log_path else DEFAULT_LOG
        self.session_id = session_id

        self.state = "idle"
        self.state_since = 0.0
        self.last_event = ""
        self.last_user_activity = 0.0
        self.user_speaking = False
        self.turn_id = 0
        self.next_feedback_at = 0.0
        self.reply_until = 0.0
        self.ack_until = 0.0
        self.pending_thinking = False
        self.voice_due_at = 0.0
        self.voice_used = False
        self.last_action = ""
        self.last_action_at = 0.0
        self.recent_actions: deque[str] = deque(maxlen=4)
        self.actions_applied = 0
        self.actions_suppressed = 0
        self.actions_ignored = 0
        self.dropped_events = 0
        self.events_received = 0
        self.closed = False
        self._ending = False
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._queue: queue.Queue = queue.Queue(maxsize=self.queue_max)
        self._worker: threading.Thread | None = None
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        if start_worker and self.enabled:
            self._worker = threading.Thread(target=self._run, name="listening-feedback",
                                            daemon=True)
            self._worker.start()

    # ------------------------------------------------------------ 基础
    def _now(self) -> float:
        return float(self.clock())

    def _log(self, event: str, **detail) -> None:
        rec = {"at": time.strftime("%H:%M:%S"), "t": round(self._now(), 3),
               "session": self.session_id, "event": event,
               "state": self.state, "state_r21": STATE_ALIASES.get(self.state, self.state),
               "turn_id": self.turn_id, **detail}
        try:
            with self.log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:  # noqa: BLE001
            pass

    def _speaking_active(self, now: float) -> bool:
        if self.state == "speaking":
            return True
        return now < self._media_until()

    def _media_until(self) -> float:
        try:
            return max(float(self.speaking.get("speaking_until") or 0.0), self.reply_until)
        except (TypeError, ValueError):
            return self.reply_until

    def _schedule_next(self, now: float) -> None:
        lo, hi = self.interval
        delay = max(self.cooldown, self.rng.uniform(lo, hi))
        self.next_feedback_at = now + delay

    def _cancel_pending(self) -> None:
        self.next_feedback_at = 0.0
        self.voice_due_at = 0.0
        self.ack_until = 0.0
        self.pending_thinking = False

    def _pick_action(self, state: str) -> str:
        choices = ACTION_CHOICES.get(state, ("listening",))
        if len(choices) > 1 and self.last_action in choices:
            other = [c for c in choices if c != self.last_action]
            return self.rng.choice(other)
        return self.rng.choice(choices)

    def _end_low_action(self, reason: str) -> None:
        """用户停顿/ASR 开始时，结束仍在 TTL 内的聆听动作，给 P4 thinking 让位。

        只清 listening/acknowledging/asr_thinking 这类低优先级动作；如果当前是
        TTS/对话情绪（priority 更高），绝不 cancel，避免破坏 P1/P2。
        """
        if self.avatar is None:
            return
        current = str(getattr(self.avatar, "current_emo", "") or "")
        if current not in ("listening", "acknowledging", "asr_thinking"):
            return
        try:
            self.avatar.request("idle", reason=f"end_low:{reason}", force=True, timeout=1.5)
            self._log("action_cleared", action=current, reason=reason)
        except Exception as exc:  # noqa: BLE001
            self._log("action_clear_error", action=current, reason=reason,
                      error=f"{type(exc).__name__}: {exc}")

    def _apply_action(self, action: str, now: float, reason: str) -> dict:
        if self.closed:
            return {"applied": False, "why": "closed"}
        if not self.enabled:
            return {"applied": False, "why": "disabled"}
        if self._speaking_active(now):
            self.actions_suppressed += 1
            self._log("action_suppressed", action=action, reason=reason,
                      why="tts_or_reply_active")
            return {"applied": False, "why": "tts_active"}
        if reason == "scheduled" and self.last_action_at and now - self.last_action_at < self.cooldown:
            self.actions_suppressed += 1
            self._log("action_suppressed", action=action, reason=reason,
                      why="cooldown")
            return {"applied": False, "why": "cooldown"}
        duration = self.rng.uniform(*self.action_range)
        try:
            if self.avatar is not None:
                res = self.avatar.request(action, reason=f"listening:{reason}",
                                          duration=duration, timeout=1.5)
            else:
                res = {"applied": True, "emotion": action, "duration": duration}
        except Exception as exc:  # noqa: BLE001
            self.actions_ignored += 1
            self._log("action_error", action=action, reason=reason,
                      error=f"{type(exc).__name__}: {exc}")
            return {"applied": False, "error": str(exc)}
        applied = bool(res.get("applied"))
        if applied:
            self.actions_applied += 1
            self.last_action = action
            self.last_action_at = now
            self.recent_actions.append(action)
        else:
            self.actions_ignored += 1
        self._log("action" if applied else "action_ignored", action=action,
                  reason=reason, duration=round(duration, 3),
                  applied=applied, avatar=res)
        return res

    def _maybe_voice(self, now: float, force_text: str | None = None) -> bool:
        if not self.enabled or not self.voice_enabled or self.voice_used:
            return False
        if force_text is None and (not self.voice_due_at or now < self.voice_due_at):
            return False
        if self._speaking_active(now) or self.state not in ("listening", "thinking", "acknowledging"):
            return False
        text = force_text or "我在听，稍等我说。"
        self.voice_used = True
        self.voice_due_at = 0.0
        self._log("feedback_voice", text=text, delivery="existing_speak_cb")
        if self.voice_cb is not None:
            try:
                self.voice_cb(text)
            except Exception as exc:  # noqa: BLE001
                self._log("feedback_voice_error", error=f"{type(exc).__name__}: {exc}")
                return False
        return True

    # ------------------------------------------------------------ 事件
    def emit(self, event: str, **data) -> bool:
        """非阻塞入口：主循环只调用它；队列满时丢弃并计数，绝不阻塞。"""
        if not self.enabled or self.closed:
            return False
        item = (self._now(), event, data)
        try:
            self._queue.put_nowait(item)
            return True
        except queue.Full:
            self.dropped_events += 1
            self._log("event_dropped", feedback_event=event, dropped=self.dropped_events)
            return False

    def set_voice_cb(self, voice_cb) -> None:
        self.voice_cb = voice_cb

    def handle_event(self, event: str, data: dict | None = None,
                     now: float | None = None) -> None:
        """同步处理一个事件；worker 和单测共用。"""
        data = data or {}
        now = self._now() if now is None else float(now)
        with self._lock:
            if self.closed:
                return
            self.events_received += 1
            self.last_event = event
            self.state_since = now
            if event == "user_speech_started":
                first = self.state != "listening"
                self.user_speaking = True
                self.last_user_activity = now
                if first:
                    self.turn_id += 1
                    self.voice_used = False
                    self.state = "listening"
                    self._cancel_pending()
                    self._apply_action("listening", now, reason="speech_started")
                    self._schedule_next(now)
                self._log("state", reason="user_speech_started", first=first,
                          rms=data.get("rms"))
            elif event == "user_speech_paused":
                was_speaking = self.user_speaking
                self.user_speaking = False
                if was_speaking and self.state != "speaking":
                    self.state = "thinking"
                    if not self._speaking_active(now):
                        self._end_low_action("speech_paused")
                self._log("state", reason="user_speech_paused", rms=data.get("rms"),
                          was_speaking=was_speaking)
            elif event == "asr_processing":
                self.user_speaking = False
                if self.state != "speaking":
                    self.state = "thinking"
                    if not self._speaking_active(now):
                        self._end_low_action("asr_processing")
                self._apply_action("asr_thinking", now, reason="asr_processing")
                if self.voice_enabled and not self.voice_used:
                    self.voice_due_at = now + self.voice_delay
                self._schedule_next(now)
                self._log("state", reason="asr_processing", audio_s=data.get("audio_s"))
            elif event == "asr_completed":
                self.user_speaking = False
                accepted = bool(data.get("accepted", True))
                self.voice_due_at = 0.0
                if not accepted:
                    # 识别结果被质量门禁/回声门禁拒绝：不显示“我理解了”的 acknowledging。
                    if self.state != "speaking":
                        self.state = "thinking"
                    self._log("state", reason="asr_completed_rejected",
                              asr_s=data.get("asr_s"), why=data.get("reason"),
                              text=str(data.get("text") or "")[:80])
                else:
                    if self.state != "speaking":
                        self.state = "acknowledging"
                        self.ack_until = now + 0.8
                        self.pending_thinking = False
                        self._apply_action("acknowledging", now, reason="asr_completed")
                    self._log("state", reason="asr_completed", asr_s=data.get("asr_s"),
                              text=str(data.get("text") or "")[:80])
            elif event == "dialogue_processing":
                if self.state == "acknowledging" and now < self.ack_until:
                    self.pending_thinking = True
                else:
                    self.state = "thinking"
                    self._apply_action("asr_thinking", now, reason="dialogue_processing")
                    self._schedule_next(now)
                self.voice_due_at = 0.0
                self._log("state", reason="dialogue_processing")
            elif event == "reply_started":
                self.user_speaking = False
                self.state = "speaking"
                self._cancel_pending()
                try:
                    self.reply_until = now + max(0.5, float(data.get("seconds") or 1.0))
                except (TypeError, ValueError):
                    self.reply_until = now + 1.0
                self._log("state", reason="reply_started",
                          seconds=data.get("seconds"), kind=data.get("kind", "reply"))
            elif event == "reply_finished":
                self.state = "idle"
                self.reply_until = 0.0
                self._cancel_pending()
                self._log("state", reason="reply_finished")
            elif event == "asr_error":
                if self.state != "speaking":
                    self.state = "acknowledging"
                    self.ack_until = now + 1.2
                    if not self._speaking_active(now):
                        self._end_low_action("asr_error")
                    self._apply_action("asr_error", now, reason="asr_error")
                self.voice_due_at = 0.0
                self._log("state", reason="asr_error", error=str(data.get("error") or "")[:160])
                if self.voice_enabled and not self.voice_used:
                    self._maybe_voice(now, force_text="我没听清，能再说一遍吗？")
            elif event == "call_ended":
                self.state = "ended"
                self.user_speaking = False
                self._ending = True
                self.closed = True
                self._cancel_pending()
                self._log("state", reason="call_ended", why=data.get("why"))
                self._stop.set()
            else:
                self._log("event_unknown", feedback_event=event, data=data)

    def tick(self, now: float | None = None) -> dict | None:
        """推进 TTL/随机反馈；worker 与单测共用。返回本次 action 结果。"""
        now = self._now() if now is None else float(now)
        result = None
        with self._lock:
            if self.closed:
                return None
            if self.state == "acknowledging" and self.ack_until and now >= self.ack_until:
                if self.pending_thinking:
                    self.pending_thinking = False
                    self.state = "thinking"
                    self._apply_action("asr_thinking", now, reason="ack_done")
                    self._schedule_next(now)
                else:
                    self.state = "idle"
                self.ack_until = 0.0
                self._log("state", reason="ack_done")
            if self.state == "speaking" and now >= self._media_until():
                self.state = "idle"
                self.reply_until = 0.0
                self._log("reply_finished", reason="reply_ttl")
            if self.state in ("listening", "thinking") and self.next_feedback_at and now >= self.next_feedback_at:
                if self._speaking_active(now):
                    self.actions_suppressed += 1
                    self._schedule_next(now)
                else:
                    action = self._pick_action(self.state)
                    result = self._apply_action(action, now, reason="scheduled")
                    self._schedule_next(now)
            if self.voice_due_at and not self.closed:
                self._maybe_voice(now)
        return result

    # ------------------------------------------------------------ worker
    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                item = self._queue.get(timeout=0.1)
            except queue.Empty:
                item = None
            if item is not None:
                _, event, data = item
                try:
                    self.handle_event(event, data)
                except Exception as exc:  # noqa: BLE001
                    self._log("event_error", feedback_event=event,
                              error=f"{type(exc).__name__}: {exc}")
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001
                self._log("tick_error", error=f"{type(exc).__name__}: {exc}")
            if self.closed:
                break

    def drain(self) -> None:
        """测试/关闭时处理队列中的事件，不启动线程。"""
        while True:
            try:
                _, event, data = self._queue.get_nowait()
            except queue.Empty:
                break
            self.handle_event(event, data)

    def close(self, reason: str = "close") -> None:
        with self._lock:
            if self.closed and self.state == "ended":
                pass
            else:
                self.state = "ended"
                self.closed = True
                self._cancel_pending()
                self._log("closed", reason=reason)
            self._stop.set()
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break
        worker = self._worker
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=1.0)

    def snapshot(self) -> dict:
        with self._lock:
            return {"state": self.state, "state_r21": STATE_ALIASES.get(self.state, self.state),
                    "turn_id": self.turn_id,
                    "last_event": self.last_event, "last_action": self.last_action,
                    "next_feedback_at": round(self.next_feedback_at, 3),
                    "actions_applied": self.actions_applied,
                    "actions_suppressed": self.actions_suppressed,
                    "actions_ignored": self.actions_ignored,
                    "dropped_events": self.dropped_events,
                    "closed": self.closed, "voice_used": self.voice_used}
