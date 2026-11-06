#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Avatar Decision / Motion Runtime：把"对话语义 → 情绪 → 表情+动作"这层单独拎出来。

为什么要独立成模块：
    之前对话闭环里只做了一件事——`post("/set", {"emotion": emotion})`，而 `emotion`
    恒为 `"neutral"`（`DeepSeekBrain` 固定返回），`/motion` 从来没人调。
    于是"手动 /speak 时表情口型正常，一进对话就什么都不会动"。
    这层需要：情绪推断、动作选择、优先级、冲突、取消、急停、超时回默认。

五类动作（对应皮套 `avatar_profiles\\大肥鱼\\Animation\\` 里真实存在的 motion3 文件，
不是编出来的名字）：

| 语义类 | 情绪 | 表情 | 动作文件 | 说明 |
|---|---|---|---|---|
| 高兴 | happy | 脸红 | `竖手指切出来动画` | 竖手指/比手势 |
| 害羞 | shy | 脸红 | `狗尾草切换出来` | 尾巴动作，偏腼腆 |
| 思考 | thinking | 加载 | `举手切默认手` | 举手到默认手，像在比划/琢磨 |
| 认真交流 | serious | （清空） | `脱捕` | 手臂放下，稳定姿态 |
| 普通待机 | idle | （清空） | `""`（回默认） | 无动作，仅呼吸/眨眼 |

优先级：surprised > shy > thinking > happy > confused > sad > serious > idle
同一个动作有 TTL，到期自动回默认；新请求若优先级不低就抢占，否则被忽略并记日志。

用法：
    rt = AvatarRuntime()
    rt.request("happy", reason="reply")     # 立刻生效
    rt.tick()                               # 周期调用，处理 TTL 回落
    rt.emergency_stop()                     # 急停：清动作+清表情
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

R2_DIR = Path(__file__).resolve().parent
LOG_DIR = R2_DIR.parent / "logs"
CTL = "http://127.0.0.1:8770"

#: 情绪 → (表情, 动作文件, 展示时长秒, 优先级)
EMOTION_ACTIONS: dict[str, tuple[str, str, float, int]] = {
    "happy":     ("脸红",   "竖手指切出来动画", 4.5, 40),
    "shy":       ("脸红",   "狗尾草切换出来",   4.0, 70),
    "thinking":  ("加载",   "举手切默认手",     4.0, 60),
    "surprised": ("＞＜",   "举手切默认手",     2.5, 90),
    "confused":  ("流冷汗", "狗尾草切回默认手", 3.0, 30),
    "sad":       ("流泪",   "脱捕",             4.0, 20),
    "serious":   ("",       "脱捕",             6.0, 10),
    # R2.1 Listening Feedback：全部是轻量、低优先级的现有表情/动作复用。
    # 优先级低于 serious(10)，因此不会覆盖 P2 对话情绪；
    # 只在没有 TTS/对话动作时短暂出现，TTL 由 feedback 的 0.5–1.5s 覆盖。
    "listening":    ("",     "狗尾草切换出来",   1.2,  6),
    "acknowledging": ("",    "狗尾草切回默认手", 1.0,  7),
    "asr_thinking": ("加载", "举手切默认手",     1.2,  5),
    "asr_error":    ("流冷汗", "狗尾草切回默认手", 1.0, 4),
    "idle":      ("",       "",                 0.0,  0),
}

#: 情绪推断的关键词表（中文/英文混排，命中即加票）
_EMOTION_KEYWORDS: dict[str, tuple[str, ...]] = {
    "happy": ("哈哈", "开心", "高兴", "太好了", "好棒", "谢谢", "喜欢", "笑", "嘻嘻",
              "嘿嘿", "耶", "呀", "嘛", "💕", "🐟", "～", "!", "！"),
    "shy": ("害羞", "不好意思", "脸红", "腼腆", "羞", "比心", "亲亲", "抱抱"),
    "thinking": ("让我想想", "想想", "嗯…", "嗯...", "思考", "琢磨", "consider", "hmm",
                 "大概", "也许", "可能", "？", "?"),
    "surprised": ("啊？", "欸？", "真的吗", "居然", "竟然", "天啊", "哇", "惊讶", "！？"),
    "confused": ("不懂", "听不懂", "什么", "啥", "奇怪", "歪头", "困惑"),
    "sad": ("难过", "伤心", "哭", "呜呜", "可惜", "抱歉", "对不起", "唉"),
    "serious": ("认真", "严肃", "重要", "注意", "提醒", "建议", "分析", "结论"),
}


def infer_emotion(text: str) -> str:
    """从回复文本推断情绪。规则简单但可解释：命中关键词最多的一类胜出。

    注意：这是**启发式**。它比"永远 neutral"好，但仍不如模型自己给标签。
    若将来大脑能在回复里带 `[emotion=happy]` 这样的标注，优先用标注。
    """
    if not text:
        return "idle"
    t = text.lower()
    scores = {e: sum(1 for k in kws if k in t) for e, kws in _EMOTION_KEYWORDS.items()}
    best = max(scores, key=lambda e: scores[e])
    return best if scores[best] > 0 else "serious"


def parse_emotion_tag(text: str) -> tuple[str | None, str]:
    """若大脑回复里带 `[emotion=xxx]` 标注，取出并去掉标注。"""
    import re

    m = re.search(r"\[emotion\s*=\s*([a-zA-Z]+)\]", text or "")
    if not m:
        return None, text
    emo = m.group(1).lower()
    return (emo if emo in EMOTION_ACTIONS else None), (text[:m.start()] + text[m.end():]).strip()


class AvatarRuntime:
    """动作/表情的运行时：带优先级、TTL、取消与急停。"""

    def __init__(self, base: str = CTL, log_path: Path | None = None) -> None:
        self.base = base
        self.current: str = ""
        self.current_emo: str = ""
        self.priority = 0
        self.expires_at = 0.0
        self.started_at = 0.0
        self.history: list[dict] = []
        self.log_path = log_path or (LOG_DIR / "avatar_runtime.jsonl")
        self.failures = 0
        # 反馈线程与主对话线程会同时请求动作；RLock 保证“优先级检查 + HTTP”是原子的。
        self._lock = threading.RLock()

    # ------------------------------------------------------------ HTTP
    def _post(self, path: str, payload: dict, timeout: float = 8.0) -> dict | None:
        try:
            from mini_control import read_token
            req = urllib.request.Request(
                self.base + path, data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json",
                         "X-ADI-Control-Token": read_token() or ""})
            return json.loads(urllib.request.urlopen(req, timeout=timeout).read())
        except Exception as exc:  # noqa: BLE001
            self.failures += 1
            self._log({"event": "post_failed", "path": path, "error": str(exc)})
            return None

    def _log(self, entry: dict) -> None:
        entry = {"at": time.strftime("%H:%M:%S"), **entry}
        self.history.append(entry)
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------ 决策
    def decide(self, reply_text: str, tag_emotion: str | None = None) -> tuple[str, str]:
        """返回 (emotion, 清洗后的回复文本)。优先用大脑标注，否则按文本推断。"""
        tagged, clean = parse_emotion_tag(reply_text)
        emo = tag_emotion or tagged or infer_emotion(clean)
        if emo not in EMOTION_ACTIONS:
            emo = "serious"
        return emo, clean

    def request(self, emotion: str, reason: str = "reply", force: bool = False,
                duration: float | None = None, timeout: float = 8.0) -> dict:
        """请求一次动作/表情。返回执行结果（含是否被更高优先级挡住）。

        duration：仅 Listening Feedback 使用的短 TTL（0.5–1.5s）；None 用映射表默认值。
        timeout：反馈线程用较短超时，避免控制面异常长时间占住对话线程。
        """
        with self._lock:
            expr, motion, ttl, prio = EMOTION_ACTIONS.get(emotion, EMOTION_ACTIONS["serious"])
            if duration is not None:
                ttl = max(0.05, float(duration))
            now = time.time()
            if not force and prio < self.priority and now < self.expires_at:
                rec = {"event": "ignored", "emotion": emotion, "reason": reason,
                       "why": "lower_priority", "current": self.current_emo,
                       "motion": None, "expression": None}
                self._log(rec)
                return {"applied": False, **rec}

            # 先设表情，再切动作（顺序不影响最终态，但便于日志对齐）
            r1 = self._post("/set", {"emotion": emotion}, timeout=timeout)
            r2 = self._post("/motion", {"name": motion}, timeout=timeout) if motion else self._post("/motion", {"name": ""}, timeout=timeout)
            self.current = motion
            self.current_emo = emotion
            self.priority = prio
            self.started_at = now
            self.expires_at = now + ttl if ttl > 0 else now + 3600
            rec = {"event": "applied", "emotion": emotion, "expression": expr, "motion": motion,
                   "ttl": ttl, "priority": prio, "reason": reason,
                   "set_ok": bool(r1 and r1.get("ok")), "motion_ok": bool(r2 and r2.get("ok"))}
            self._log(rec)
            return {"applied": True, **rec}

    def tick(self) -> dict | None:
        """TTL 到期→回默认（idle）。返回回落事件（没有则 None）。"""
        with self._lock:
            if self.current_emo and self.current_emo != "idle" and time.time() >= self.expires_at:
                return self.request("idle", reason="ttl_expired", force=True)
            return None

    def cancel(self, reason: str = "cancel") -> dict:
        with self._lock:
            return self.request("idle", reason=reason, force=True)

    def emergency_stop(self, reason: str = "emergency") -> dict:
        """急停：立刻清动作、清表情、口型归零。异常/挂断时调用。"""
        with self._lock:
            self._log({"event": "emergency_stop", "reason": reason})
            r1 = self._post("/reset", {})
            if r1 is None:
                r1 = self._post("/set", {"emotion": "neutral", "mouth": 0.0})
            self.current = ""
            self.current_emo = ""
            self.priority = 0
            self.expires_at = 0.0
            return {"stopped": True, "reset_ok": bool(r1 and r1.get("ok"))}

    def speak_plan(self, reply_text: str, tag_emotion: str | None = None) -> dict:
        """一次完整的"决定 + 执行"：给对话闭环直接调用。"""
        emo, clean = self.decide(reply_text, tag_emotion)
        res = self.request(emo, reason="reply")
        return {"emotion": emo, "text": clean, "action": res}
