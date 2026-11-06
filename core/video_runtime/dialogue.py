# -*- coding: utf-8 -*-
"""视频会话的 DialogueService（AI 侧记忆与轮次），与生产 QQAI 完全隔离。

用户硬性要求落点：
  · 视频会话独立 `history / summary / facts / archive / world`
  · **视频链路不得触发原 sender**（空间发布/点赞/自动回复一律不许走）→ 本模块**从不调用** sender，
    `sender` 只作为"隔离证明"被持有，测试断言其调用数为 0
  · 视频内容不得污染普通聊天记忆 → 只写 `video` 命名空间（T2 的 Store 物理分目录）

设计边界：
  · `brain` 可注入；默认 `PlaceholderBrain` **不是 AI**，只是带情绪的固定回声，
    明确标注 `is_real_ai = False`，避免把占位实现当成对话能力交付。
  · 真正接 QQAI/ADI 由 T8 的交接接口完成（只读引用，不改生产目录）。
  · 播报通过注入的 `speak` 回调触发（默认不播），避免本模块直接耦合 HTTP/音频。

自测：python video_runtime\\test_dialogue.py
"""
import json
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 六种基础情绪（T9 的表情映射只认这几个）
EMOTIONS = ("neutral", "happy", "shy", "confused", "thinking", "surprised")
SCOPE = "video"


class PlaceholderBrain:
    """占位大脑：**不是 AI**。只做回声 + 关键词情绪，用于打通链路与测试。"""

    name = "placeholder-echo"
    is_real_ai = False

    KEYWORDS = (("开心", "happy"), ("笑", "happy"), ("可爱", "shy"), ("害羞", "shy"),
                ("为什么", "confused"), ("不懂", "confused"), ("想", "thinking"),
                ("哇", "surprised"), ("真的", "surprised"))

    def respond(self, text, ctx=None):
        emotion = "neutral"
        for word, emotion_name in self.KEYWORDS:
            if word in (text or ""):
                emotion = emotion_name
                break
        reply = "（占位回声，不是 AI）我听到你说：%s" % (text or "")
        return {"reply": reply, "emotion": emotion, "brain": self.name,
                "is_real_ai": False}


class DialogueService:
    """视频会话的对话服务：轮次 → 记忆写入 → 播报回调。"""

    def __init__(self, store, session_id=None, brain=None, sender=None, speak=None,
                 max_turns=8, log_path=None, clock=time.monotonic):
        self.store = store
        self.session_id = session_id
        self.brain = brain or PlaceholderBrain()
        self.sender = sender            # 只持有，绝不调用（隔离证明）
        self.speak = speak              # 可注入的播报回调（默认不播）
        self.max_turns = int(max_turns)
        self.log_path = Path(log_path) if log_path else (ROOT / "logs" / "dialogue.jsonl")
        self.clock = clock
        self._started = self.clock()
        self._turns = []                # [{"turn": n, "user":..., "ai":..., "emotion":...}]
        self._seen_turn_ids = set()
        self._facts = {"turns": 0, "emotions": {}, "first_seen": round(time.time(), 3),
                       "session_id": None}
        self.summary_written = False

    # ---------- 日志 ----------
    def _log(self, event, **detail):
        rec = {"ts": round(time.time(), 3), "mono": round(self.clock() - self._started, 6),
               "event": event, "session_id": self.session_id,
               "turns": len(self._turns), "detail": detail}
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.log_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:
            pass
        return rec

    # ---------- 会话 ----------
    def begin(self, session_id):
        """开始一段视频会话；已有会话则拒绝（防止两条流写进同一 session）。"""
        if self.session_id and self.session_id != session_id:
            return {"ok": False, "reason": "session_already_active",
                    "session_id": self.session_id}
        self.session_id = session_id
        self._started = self.clock()
        self._facts.update({"session_id": session_id, "turns": 0,
                            "first_seen": round(time.time(), 3)})
        self.store.put(SCOPE, session_id, "world",
                       {"session_id": session_id, "opened_at": round(time.time(), 3),
                        "brain": getattr(self.brain, "name", "?"),
                        "is_real_ai": bool(getattr(self.brain, "is_real_ai", False))})
        self._log("session_begin")
        return {"ok": True, "session_id": session_id}

    def hear(self, text, turn_id=None, speak=True):
        """一轮：用户说话 → 大脑 → 写记忆 → （可选）播报。"""
        if not self.session_id:
            return {"ok": False, "reason": "no_session"}
        if turn_id is not None:
            if turn_id in self._seen_turn_ids:
                self._log("turn_duplicate", turn_id=turn_id)
                return {"ok": False, "reason": "duplicate", "turn_id": turn_id,
                        "turns": len(self._turns)}
            self._seen_turn_ids.add(turn_id)
        turn_no = len(self._turns) + 1
        started = self.clock()
        try:
            result = self.brain.respond(text, {"session_id": self.session_id,
                                               "turn": turn_no,
                                               "history": list(self._turns)})
        except Exception as exc:
            self._log("brain_error", error=str(exc), turn=turn_no)
            return {"ok": False, "reason": "brain_error", "error": str(exc)}
        reply = str((result or {}).get("reply") or "")
        emotion = str((result or {}).get("emotion") or "neutral")
        if emotion not in EMOTIONS:
            emotion = "neutral"
        record = {"turn": turn_no, "user": str(text), "ai": reply,
                  "emotion": emotion, "brain": (result or {}).get("brain"),
                  "is_real_ai": (result or {}).get("is_real_ai"),
                  "at": round(time.time(), 3)}
        # 只写 video 命名空间
        self.store.append(SCOPE, self.session_id, "history", record)
        self._turns.append(record)
        self._update_facts(emotion)
        self.store.append(SCOPE, self.session_id, "archive",
                          {"event": "turn", "turn": turn_no, "emotion": emotion,
                           "at": record["at"]})
        spoken = None
        if speak and self.speak is not None:
            try:
                spoken = self.speak(reply, emotion)
            except Exception as exc:
                spoken = {"error": str(exc)}
        if len(self._turns) >= self.max_turns:
            self.write_summary(reason="max_turns")
        self._log("turn", turn=turn_no, emotion=emotion,
                  elapsed=round(self.clock() - started, 4), spoken=bool(spoken))
        return {"ok": True, "turn": turn_no, "reply": reply, "emotion": emotion,
                "session_id": self.session_id, "spoken": spoken,
                "is_real_ai": record["is_real_ai"]}

    def _update_facts(self, emotion):
        self._facts["turns"] = len(self._turns)
        self._facts["emotions"][emotion] = self._facts["emotions"].get(emotion, 0) + 1
        self._facts["last_updated"] = round(time.time(), 3)
        facts = dict(self._facts)
        facts["recent"] = [{"turn": t["turn"], "user": t["user"][:60],
                            "emotion": t["emotion"]} for t in self._turns[-5:]]
        self.store.put(SCOPE, self.session_id, "facts", facts)

    def write_summary(self, reason="manual"):
        """写摘要（滚动：最近 N 轮）。幂等：同一轮数重复调用不重复写。"""
        if not self.session_id or not self._turns:
            return {"written": False, "reason": "no_turns"}
        key = (len(self._turns), reason)
        if getattr(self, "_summary_key", None) == key:
            return {"written": False, "reason": "duplicate", "summary_key": str(key)}
        emotions = {}
        for t in self._turns:
            emotions[t["emotion"]] = emotions.get(t["emotion"], 0) + 1
        summary = {"session_id": self.session_id, "turns": len(self._turns),
                   "emotion_counts": emotions,
                   "topics": [t["user"][:40] for t in self._turns[-6:]],
                   "duration": round(self.clock() - self._started, 3),
                   "reason": reason,
                   "is_real_ai": bool(getattr(self.brain, "is_real_ai", False)),
                   "at": round(time.time(), 3)}
        self.store.put(SCOPE, self.session_id, "summary", summary)
        self._summary_key = key
        self.summary_written = True
        self._log("summary_written", reason=reason, turns=len(self._turns))
        return {"written": True, "summary": summary}

    def end(self, reason="hangup"):
        """结束会话：写摘要 + 归档，返回快照。"""
        if not self.session_id:
            return {"ok": False, "reason": "no_session"}
        if self._turns:
            self.write_summary(reason=reason)
        self.store.append(SCOPE, self.session_id, "archive",
                          {"event": "session_end", "reason": reason,
                           "turns": len(self._turns), "at": round(time.time(), 3)})
        snap = self.snapshot()
        self._log("session_end", reason=reason)
        self.session_id = None
        self._turns = []
        self._seen_turn_ids = set()
        return {"ok": True, "snapshot": snap}

    def snapshot(self):
        return {"session_id": self.session_id, "turns": len(self._turns),
                "emotions": dict(self._facts.get("emotions", {})),
                "sender_calls": self.sender_calls(),
                "brain": getattr(self.brain, "name", "?"),
                "is_real_ai": bool(getattr(self.brain, "is_real_ai", False))}

    # ---------- 隔离断言 ----------
    def sender_calls(self):
        """sender 替身的调用次数（视频链路必须恒为 0）。"""
        if self.sender is None:
            return 0
        for attr in ("calls", "call_count", "n"):
            if hasattr(self.sender, attr):
                value = getattr(self.sender, attr)
                return value() if callable(value) else int(value)
        return getattr(self.sender, "__dummy__", 0)

    def isolation_report(self, chat_key):
        """双向检查：视频有自己的记录，普通聊天里没有任何视频条目。"""
        video_history = self.store.load(SCOPE, self.session_id or "", "history") or []
        chat_history = self.store.load("chat", chat_key, "history") or []
        chat_keys = set()
        for rec in chat_history:
            if isinstance(rec, dict):
                chat_keys.update(rec.keys())
        return {"video_turns": len(video_history),
                "chat_records": len(chat_history),
                "chat_has_video_fields": bool({"user", "ai", "emotion"} & chat_keys),
                "video_has_chat_fields": any(isinstance(r, dict) and ("text" in r or "sender" in r)
                                             for r in video_history),
                "sender_calls": self.sender_calls(),
                "isolated": (not ({"user", "ai", "emotion"} & chat_keys)) and
                            (self.sender_calls() == 0)}


class RecordingSender:
    """测试/证据用：记录被调用的次数。DialogueService 永远不会调用它。"""

    def __init__(self):
        self.calls = 0

    def send(self, *args, **kwargs):      # pragma: no cover - 正常路径不该被调到
        self.calls += 1
        return {"sent": True}
