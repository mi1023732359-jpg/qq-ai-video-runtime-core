# -*- coding: utf-8 -*-
"""ASR 输入 + 半双工收听闸门。

为什么不能直接复用 QQAI 的 `transcribe_file()`（只读核查后的结论）：
  QQAI `bot/voice.py::_to_wav()` 会把中间 wav 写进 **QQAI 自己的 VOICE_DIR**，
  而 `_get_whisper()` 又把模型尺寸/设备/缓存路径写死（`WhisperModel("small", ...)`，
  无 `download_root`）。直接调用会污染生产目录，因此按 T5「替代 2」自带最小封装：
  **只依赖 faster-whisper 公开 API**，模型目录/临时目录/缓存目录全部可注入。

半双工（本模块的核心）：
    IDLE --接听--> LISTENING --开始播放--> PLAYING --播放结束--> LISTENING
                      ^                        |
                      +---- 播放期间音频丢弃 ---+
  播放期间**绝不提交转写**，否则会把自己的 TTS/回声识别成"用户在说话" → 自问自答。

强制断言（Codex 指定）：
  1. 路由断言：闸门关闭时 submit() 返回 submitted=False 且转写器调用数**不增加**
  2. 状态断言：只有"本轮播放完成"才恢复收听；超时不得自行恢复
  3. 日志：session_id / turn_id / speak_id + 单调时钟 + 音频时长 + 丢帧数

自测：python video_runtime\\test_asr_in.py
"""
import json
import time
import uuid
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TMP = ROOT / "logs" / "asr_tmp"
DEFAULT_LOG = ROOT / "logs" / "asr_in.jsonl"
TARGET_RATE = 16000

IDLE = "IDLE"
LISTENING = "LISTENING"
PLAYING = "PLAYING"
HUNG_UP = "HUNG_UP"

# 低于这个 RMS 不送 ASR：whisper 在静音上容易产生幻觉文本
DEFAULT_SILENCE_RMS = 0.006
DEFAULT_MIN_SECONDS = 0.4


def check_asr():
    """如实报告 ASR 能力：缺什么、能不能用。不静默降级。"""
    import importlib.util as util
    has_fw = bool(util.find_spec("faster_whisper"))
    has_np = bool(util.find_spec("numpy"))
    notes = []
    if not has_fw:
        notes.append("faster-whisper 未安装：python -m pip install faster-whisper "
                     "（闸门逻辑与 --dry-run 不需要它）")
    if not has_np:
        notes.append("numpy 缺失：无法做静音门限")
    return {"faster_whisper": has_fw, "numpy": has_np,
            "available": has_fw and has_np,
            "default_tmp": str(DEFAULT_TMP), "target_rate": TARGET_RATE,
            "reuses_qqai_transcribe": False,
            "notes": notes}


class FakeTranscriber:
    """离线测试用：记录调用次数与收到的字节，不做真转写。"""

    name = "fake"

    def __init__(self, text="测试语音", fail=False):
        self.calls = 0
        self.received = []
        self.text = text
        self.fail = fail

    def transcribe(self, pcm, rate=TARGET_RATE):
        self.calls += 1
        self.received.append((int(len(pcm)), int(rate)))
        if self.fail:
            raise RuntimeError("transcriber boom")
        return self.text


class WhisperTranscriber:
    """最小 faster-whisper 封装：路径全部可注入，不写 QQAI 目录。"""

    name = "faster-whisper"

    def __init__(self, model_size="small", device="cpu", compute_type="int8",
                 download_root=None, tmp_dir=None, language="zh",
                 cpu_threads: int = 0):
        self.model_size = model_size
        self.device = device
        self.compute_type = compute_type
        self.download_root = str(download_root) if download_root else None
        self.tmp_dir = Path(tmp_dir) if tmp_dir else DEFAULT_TMP
        self.language = language
        self.cpu_threads = int(cpu_threads or 0)
        self._model = None
        # `AsrGate.submit()` 的**每个**返回体都要带 transcriber_calls（闸门拒收时也带）。
        # 真模型这条以前没有这个计数器 → 播放期 submit 会 AttributeError（T29 实测发现）。
        self.calls = 0

    def _load(self):
        if self._model is None:
            from faster_whisper import WhisperModel
            kwargs = {"device": self.device, "compute_type": self.compute_type}
            if self.download_root:
                kwargs["download_root"] = self.download_root
            if self.cpu_threads > 0:
                kwargs["cpu_threads"] = self.cpu_threads
            self._model = WhisperModel(self.model_size, **kwargs)
        return self._model

    def transcribe(self, pcm, rate=TARGET_RATE):
        self.calls += 1
        model = self._load()
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        path = self.tmp_dir / (uuid.uuid4().hex + ".wav")
        try:
            with wave.open(str(path), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(rate)
                wav.writeframes(np.asarray(pcm, dtype=np.int16).tobytes())
            segments, _info = model.transcribe(str(path), language=self.language, beam_size=1)
            return "".join(s.text for s in segments).strip()
        finally:
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass


class AsrGate:
    """半双工收听闸门 + 提交判定。"""

    def __init__(self, transcriber=None, log_path=None, clock=time.monotonic,
                 silence_rms=DEFAULT_SILENCE_RMS, min_seconds=DEFAULT_MIN_SECONDS,
                 session_id=None):
        self.transcriber = transcriber or FakeTranscriber()
        self.log_path = Path(log_path) if log_path else DEFAULT_LOG
        self.clock = clock
        self.silence_rms = silence_rms
        self.min_seconds = min_seconds
        self.session_id = session_id
        self.speak_id = None
        self.turn_id = 0
        self.discarded_frames = 0
        self.submitted = 0
        self._state = IDLE
        self._state_since = clock()

    # ---------- 状态 ----------
    @property
    def state(self):
        return self._state

    @property
    def listening(self):
        return self._state == LISTENING

    def _set(self, state, **detail):
        old, self._state = self._state, state
        self._state_since = self.clock()
        self._log("gate", old=old, new=state, **detail)
        return state

    def _log(self, event, **detail):
        rec = {"ts": round(time.time(), 3), "mono": round(self.clock(), 6),
               "event": event, "state": self._state,
               "session_id": self.session_id, "turn_id": self.turn_id,
               "speak_id": self.speak_id, "detail": detail}
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.log_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:
            pass
        return rec

    def on_call_answered(self, session_id=None):
        if session_id:
            self.session_id = session_id
        self._state = IDLE            # 先复位，避免从 PLAYING 直接跳进 LISTENING
        return self._set(LISTENING, reason="call_answered")

    def on_speak_start(self, speak_id=None):
        """开始播放 TTS：立刻关收听，并清空未提交音频。"""
        if self._state != LISTENING:
            return {"state": self._state, "changed": False}
        self.speak_id = speak_id
        return {"state": self._set(PLAYING, reason="speak_start", speak_id=speak_id),
                "changed": True}

    def on_speak_end(self, speak_id=None):
        """只有"本轮播放完成"才恢复收听；speak_id 不匹配不恢复（防旧线程乱回写）。"""
        if self._state != PLAYING:
            return {"state": self._state, "changed": False}
        if speak_id is not None and self.speak_id is not None and speak_id != self.speak_id:
            return {"state": self._state, "changed": False, "reason": "speak_id_mismatch"}
        self.speak_id = None
        return {"state": self._set(LISTENING, reason="speak_end"), "changed": True}

    def on_hangup(self):
        self.speak_id = None
        self._state = HUNG_UP
        return self._set(IDLE, reason="hangup")

    def tick(self, now=None):
        """超时看门狗：**不得**把 PLAYING 自行改回 LISTENING。"""
        now = self.clock() if now is None else now
        return {"state": self._state, "changed": False,
                "state_age": round(now - self._state_since, 3)}

    # ---------- 提交 ----------
    def submit(self, pcm, rate=TARGET_RATE):
        """把一段 PCM 交给 ASR。闸门关着就**不转写**、只计数丢弃。"""
        pcm = np.asarray(pcm)
        frames = int(pcm.size)
        if self._state != LISTENING:
            self.discarded_frames += frames
            self._log("submit_rejected", reason=self._state.lower(), frames=frames)
            return {"submitted": False, "reason": self._state.lower(),
                    "frames": frames, "transcriber_calls": self.transcriber.calls}
        seconds = frames / float(rate or TARGET_RATE)
        if seconds < self.min_seconds:
            self._log("submit_rejected", reason="too_short", seconds=round(seconds, 3))
            return {"submitted": False, "reason": "too_short",
                    "seconds": round(seconds, 3),
                    "transcriber_calls": self.transcriber.calls}
        data = np.asarray(pcm).astype(np.float32)
        scale = 32768.0 if data.size and float(np.max(np.abs(data))) > 1.5 else 1.0
        rms = float(np.sqrt(np.mean((data / scale) ** 2))) if data.size else 0.0
        if rms < self.silence_rms:
            self._log("submit_rejected", reason="silence", rms=round(rms, 5))
            return {"submitted": False, "reason": "silence", "rms": round(rms, 5),
                    "transcriber_calls": self.transcriber.calls}
        self.turn_id += 1
        started = self.clock()
        try:
            text = self.transcriber.transcribe(pcm, rate or TARGET_RATE)
        except Exception as exc:
            self._log("transcribe_error", error=str(exc), turn_id=self.turn_id)
            return {"submitted": True, "ok": False, "error": str(exc),
                    "turn_id": self.turn_id, "transcriber_calls": self.transcriber.calls}
        self.submitted += 1
        elapsed = round(self.clock() - started, 3)
        self._log("transcribed", turn_id=self.turn_id, seconds=round(seconds, 3),
                  chars=len(text or ""), audio_ms=round(seconds * 1000, 1),
                  discarded_frames=self.discarded_frames, elapsed=elapsed)
        return {"submitted": True, "ok": True, "text": text, "turn_id": self.turn_id,
                "seconds": round(seconds, 3), "rms": round(rms, 5),
                "transcriber_calls": self.transcriber.calls, "elapsed": elapsed}

    def snapshot(self):
        return {"state": self._state, "session_id": self.session_id,
                "turn_id": self.turn_id, "speak_id": self.speak_id,
                "submitted": self.submitted, "discarded_frames": self.discarded_frames,
                "transcriber": getattr(self.transcriber, "name", "?"),
                "transcriber_calls": self.transcriber.calls}


if __name__ == "__main__":
    print(json.dumps(check_asr(), ensure_ascii=False, indent=2))
