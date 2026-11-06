# -*- coding: utf-8 -*-
"""远端语音采集（QQ 通话里对方说的话）。只读，不碰 QQ 进程。

后端选择（按优先级自动探测，`check_backend()` 如实报告实际走哪条）：

  1. **ProcTap**（进程级 WASAPI Process Loopback，最干净）
     ✗ 本机不可用：PyPI 无包、GitHub 版需 `/std:c++20` + MSVC 编译，而本机
       没有 git、也没有 Visual Studio。**不可静默假装可用**。
  2. **PyAudioWPatch**（设备级 WASAPI loopback）→ 本机实际使用这条。

采集端点怎么选（重要）：
  QQ 通话音量从"QQ 的扬声器"出来，所以采集哪一路取决于 QQ 扬声器设到哪：
    - QQ 扬声器 = 真实扬声器 (Realtek) → 采 `扬声器 (Realtek(R) Audio) [Loopback]`
    - QQ 扬声器 = VB-Audio Point     → 采 `CABLE Output (VB-Audio Point)`
  绝不要去采 `CABLE Input [Loopback]`：那是我们自己灌 TTS 的那条线，采它等于自问自答。

格式归一：设备给 48kHz 立体声 → 统一 **16kHz 单声道 int16**（faster-whisper 要的）。

自测：python video_runtime\\test_audio_capture.py
"""
import threading
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
TARGET_RATE = 16000
CHUNK = 1024

BACKEND_PROCTAP = "proctap"
BACKEND_PYAUDIOWPATCH = "pyaudiowpatch"
BACKEND_UNAVAILABLE = "unavailable"


def _module_ok(name):
    import importlib.util as util
    try:
        return bool(util.find_spec(name))
    except Exception:
        return False


def check_backend():
    """报告每条后端是否可用、实际会用哪条。不猜、不静默降级。"""
    proctap = _module_ok("proctap")
    pyaudiowpatch = _module_ok("pyaudiowpatch")
    active = BACKEND_PROCTAP if proctap else (
        BACKEND_PYAUDIOWPATCH if pyaudiowpatch else BACKEND_UNAVAILABLE)
    notes = []
    if not proctap:
        notes.append("ProcTap 未安装：PyPI 无包，GitHub 版需 MSVC(/std:c++20) 编译；"
                     "本机无 git 且无 Visual Studio → 进程级采集暂不可用")
    if pyaudiowpatch:
        notes.append("PyAudioWPatch 可用：设备级 WASAPI loopback（会采到该输出设备的全部声音）")
    else:
        notes.append("PyAudioWPatch 未安装：python -m pip install PyAudioWPatch")
    return {"proctap": proctap, "pyaudiowpatch": pyaudiowpatch,
            "active": active, "target_rate": TARGET_RATE, "notes": notes,
            "process_isolation": proctap}


def to_16k_mono(samples, src_rate):
    """统一成 16kHz 单声道 int16。

    samples: numpy float32，形状 (frames,) 或 (frames, channels)
    """
    data = np.asarray(samples, dtype=np.float32)
    if data.ndim == 2:
        data = data.mean(axis=1)
    if data.size == 0:
        return np.zeros(0, dtype=np.int16)
    src_rate = int(src_rate)
    if src_rate != TARGET_RATE:
        ratio = src_rate / float(TARGET_RATE)
        if abs(ratio - round(ratio)) < 1e-9 and round(ratio) >= 2:
            step = int(round(ratio))
            usable = (data.size // step) * step
            # 盒式均值 = 廉价抗混叠，比直接抽点干净得多
            data = data[:usable].reshape(-1, step).mean(axis=1)
        else:
            n_out = max(1, int(round(data.size / ratio)))
            idx = np.linspace(0, data.size - 1, n_out)
            data = np.interp(idx, np.arange(data.size), data).astype(np.float32)
    clipped = np.clip(data, -1.0, 1.0)
    return (clipped * 32767.0).astype(np.int16)


class FakeSource:
    """离线测试用：按块吐出给定的 float32 波形。"""

    def __init__(self, chunks, rate=48000, channels=2, fail_after=None):
        self._chunks = list(chunks)
        self.rate = rate
        self.channels = channels
        self.fail_after = fail_after
        self.started = False
        self.closed = False
        self.reads = 0

    def start(self):
        self.started = True

    def read(self):
        if self.fail_after is not None and self.reads >= self.fail_after:
            raise RuntimeError("source lost")
        if not self._chunks:
            return None
        self.reads += 1
        return self._chunks.pop(0)

    def stop(self):
        self.closed = True


class LoopbackSource:
    """PyAudioWPatch 设备级 loopback。读默认输出（或指定设备）的混音。"""

    def __init__(self, device_index=None, match_name=None):
        self.device_index = device_index
        self.match_name = match_name
        self.rate = 48000
        self.channels = 2
        self._pa = None
        self._stream = None
        self.device_name = None
        self._lock = threading.Lock()

    def _pick_device(self):
        import pyaudiowpatch as pyaudio
        pa = pyaudio.PyAudio()
        chosen = None
        if self.device_index is not None:
            info = pa.get_device_info_by_index(self.device_index)
            chosen = info
        else:
            try:
                default_out = pa.get_default_wasapi_loopback()
            except Exception:
                default_out = None
            if self.match_name:
                lowered = self.match_name.casefold()
                for info in pa.get_loopback_device_info_generator():
                    if lowered in info["name"].casefold():
                        chosen = info
                        break
            chosen = chosen or default_out
        if chosen is None:
            pa.terminate()
            raise RuntimeError("找不到可用的 WASAPI loopback 设备")
        return pa, chosen

    def start(self):
        import pyaudiowpatch as pyaudio
        self._pa, info = self._pick_device()
        self.rate = int(info["defaultSampleRate"])
        self.channels = int(info["maxInputChannels"]) or 2
        self.device_name = info["name"]
        self._stream = self._pa.open(
            format=pyaudio.paFloat32, channels=self.channels, rate=self.rate,
            frames_per_buffer=CHUNK, input=True, input_device_index=info["index"])
        return self

    def read(self):
        if self._stream is None:
            return None
        raw = self._stream.read(CHUNK, exception_on_overflow=False)
        return np.frombuffer(raw, dtype=np.float32).reshape(-1, self.channels)

    def stop(self):
        try:
            if self._stream is not None:
                self._stream.stop_stream()
                self._stream.close()
        finally:
            self._stream = None
            if self._pa is not None:
                self._pa.terminate()
                self._pa = None


class Capture:
    """把任意 source 的 PCM 归一成 16k 单声道，并统计丢帧/断线。"""

    def __init__(self, source, max_chunks=None, on_frame=None):
        self.source = source
        self.max_chunks = max_chunks
        self.on_frame = on_frame
        self.dropped_frames = 0
        self.frames_read = 0
        self.source_lost = False
        self.error = None
        self.pcm = np.zeros(0, dtype=np.int16)

    def pump(self):
        """拉完 source 的全部数据（同步、可测）；返回 int16 单声道数组。"""
        self.source.start()
        pieces = []
        try:
            while self.max_chunks is None or self.frames_read < self.max_chunks:
                try:
                    chunk = self.source.read()
                except Exception as exc:
                    self.source_lost = True
                    self.error = str(exc)
                    break
                if chunk is None:
                    break
                self.frames_read += 1
                mono = to_16k_mono(chunk, self.source.rate)
                pieces.append(mono)
                if self.on_frame is not None:
                    self.on_frame(mono)
        finally:
            self.source.stop()
        self.pcm = np.concatenate(pieces) if pieces else np.zeros(0, dtype=np.int16)
        self.dropped_frames = 0 if not self.source_lost else 1
        return self.pcm

    def stats(self):
        if self.pcm.size == 0:
            return {"frames": 0, "seconds": 0.0, "peak": 0.0, "rms": 0.0}
        data = self.pcm.astype(np.float32) / 32768.0
        return {"frames": int(self.pcm.size),
                "seconds": round(self.pcm.size / float(TARGET_RATE), 3),
                "peak": round(float(np.max(np.abs(data))), 5),
                "rms": round(float(np.sqrt(np.mean(data ** 2))), 6)}


def capture_seconds(seconds, device_index=None, match_name=None):
    """真机：采 N 秒并返回 (pcm, stats)。给 probe_qq_audio.py 用。"""
    backend = check_backend()
    if backend["active"] == BACKEND_UNAVAILABLE:
        raise RuntimeError("; ".join(backend["notes"]))
    source = LoopbackSource(device_index=device_index, match_name=match_name)
    source.start()
    rate = source.rate
    want = max(1, int(rate * seconds / CHUNK))
    pieces = []
    try:
        for _ in range(want):
            chunk = source.read()
            if chunk is None:
                break
            pieces.append(to_16k_mono(chunk, rate))
    finally:
        source.stop()
    pcm = np.concatenate(pieces) if pieces else np.zeros(0, dtype=np.int16)
    cap = Capture(FakeSource([]))
    cap.pcm = pcm
    stats = cap.stats()
    stats["device"] = source.device_name
    stats["source_rate"] = rate
    return pcm, stats


if __name__ == "__main__":
    import json
    print(json.dumps(check_backend(), ensure_ascii=False, indent=2))
