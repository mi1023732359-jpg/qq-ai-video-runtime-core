#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""唯一 PCM Capture Owner 的共享环形缓冲区。

设计目标（2026-11-01 真实通话后）：
    conversation_loop 是 QQ 进程音频的**唯一采集者**；诊断/探针/ASR 都读同一份 PCM，
    禁止再各自打开 ActivateAudioInterfaceAsync（双采集会互相抢流，trace 与探针曾出现
    proc=0.193 vs proc=1e-05 的矛盾）。

布局（文件 `logs/pcm_ring.bin`）：
    header 128 bytes：
      magic(8) version(u32) rate(u32) channels(u32) frame_bytes(u32)
      capacity_frames(u64) write_seq(u64) write_pos(u64) total_frames(u64)
      dropped_frames(u64) owner_pid(u32) reserved(u32) first_ts(f64) last_ts(f64)
    随后 capacity_frames * frame_bytes 的 int16 PCM。

写入者：`PcmRingWriter.write(float32 mono)`，覆盖最旧数据，更新统计。
读取者：`snapshot()` / `read_stats()`，不会打开音频设备。

`dropped_frames` 的准确语义是“环形窗口写满后被覆盖的帧数”，不是 ASR 丢帧：
ASR 消费的是 conversation_loop 的实时缓冲，不依赖这个环形窗口；探针只读最新
N 秒。超过窗口的音频会被覆盖，因此用 `overwritten_frames` 同时暴露该值，避免
把容量轮转误读成“采集丢了近一半”。
"""
from __future__ import annotations

import json
import mmap
import os
import struct
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PATH = ROOT / "logs" / "pcm_ring.bin"
DEFAULT_STATS = ROOT / "logs" / "pcm_ring_stats.json"

MAGIC = b"PCMRING1"
VERSION = 1
HEADER = struct.Struct("<8sIIIIQQQQQII dd")
assert HEADER.size <= 128


class PcmRingWriter:
    def __init__(self, path: Path | str = DEFAULT_PATH, rate: int = 48000,
                 channels: int = 1, seconds: float = 10.0,
                 stats_path: Path | str = DEFAULT_STATS, owner_pid: int | None = None):
        self.path = Path(path)
        self.rate = int(rate)
        self.channels = int(channels)
        self.frame_bytes = self.channels * 2
        self.capacity = max(1, int(self.rate * float(seconds)))
        self.stats_path = Path(stats_path)
        self.owner_pid = int(owner_pid or os.getpid())
        self.path.parent.mkdir(parents=True, exist_ok=True)
        size = 128 + self.capacity * self.frame_bytes
        with self.path.open("wb") as fh:
            fh.truncate(size)
        self._fh = self.path.open("r+b")
        self._mm = mmap.mmap(self._fh.fileno(), 0)
        self.write_seq = 0
        self.write_pos = 0
        self.total_frames = 0
        self.dropped = 0
        self.first_ts = 0.0
        self.last_ts = 0.0
        self._last_stats = 0.0
        self._write_header()

    def _write_header(self, seq: int | None = None) -> None:
        HEADER.pack_into(self._mm, 0, MAGIC, VERSION, self.rate, self.channels,
                         self.frame_bytes, self.capacity,
                         self.write_seq if seq is None else seq,
                         self.write_pos, self.total_frames, self.dropped,
                         self.owner_pid, 0, self.first_ts, self.last_ts)

    def write(self, pcm) -> int:
        """Append mono float32 [-1,1]; returns frames written."""
        a = np.asarray(pcm, dtype=np.float32).reshape(-1)
        if a.size == 0:
            return 0
        int16 = np.clip(a, -1.0, 1.0)
        int16 = (int16 * 32767.0).astype(np.int16)
        if self.channels > 1:
            int16 = np.repeat(int16, self.channels)
        raw = int16.tobytes()
        frames = a.size
        before = self.total_frames
        pos = self.write_pos
        cap = self.capacity
        # 两段写入，允许环绕
        first = min(frames, cap - pos)
        self._mm[128 + pos * self.frame_bytes:
                 128 + (pos + first) * self.frame_bytes] = raw[:first * self.frame_bytes]
        if first < frames:
            rest = frames - first
            self._mm[128:128 + rest * self.frame_bytes] = raw[first * self.frame_bytes:]
        self.write_pos = (pos + frames) % cap
        self.write_seq += 1
        self.total_frames += frames
        self.dropped += frames if before >= cap else max(0, before + frames - cap)
        now = time.time()
        if not self.first_ts:
            self.first_ts = now
        self.last_ts = now
        self._write_header()
        if now - self._last_stats >= 0.5:
            self._last_stats = now
            self._stats(now)
        return frames

    def _stats(self, now: float) -> None:
        try:
            self.stats_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                       "owner_pid": self.owner_pid, "rate": self.rate,
                       "channels": self.channels, "capacity_frames": self.capacity,
                       "total_frames": self.total_frames, "dropped_frames": self.dropped,
                       "overwritten_frames": self.dropped,
                       "retained_seconds": round(self.capacity / max(1, self.rate), 2),
                       "dropped_note": "ring overwrite after capacity; not ASR loss",
                       "write_seq": self.write_seq, "first_ts": self.first_ts,
                       "last_ts": self.last_ts, "age_s": round(max(0.0, now - self.last_ts), 3)}
            self.stats_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass

    def close(self) -> None:
        try:
            self._stats(time.time())
        except Exception:  # noqa: BLE001
            pass
        try:
            self._mm.flush()
        except Exception:  # noqa: BLE001
            pass
        try:
            self._mm.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            self._fh.close()
        except Exception:  # noqa: BLE001
            pass


def read_header(path: Path | str = DEFAULT_PATH) -> dict | None:
    p = Path(path)
    try:
        with p.open("rb") as fh:
            raw = fh.read(128)
    except OSError:
        return None
    if len(raw) < HEADER.size:
        return None
    vals = HEADER.unpack_from(raw, 0)
    magic, version, rate, channels, frame_bytes, capacity, seq, pos, total, dropped, \
        owner, _res, first_ts, last_ts = vals
    if magic != MAGIC or version != VERSION:
        return None
    return {"rate": rate, "channels": channels, "frame_bytes": frame_bytes,
            "capacity": capacity, "write_seq": seq, "write_pos": pos,
            "total_frames": total, "dropped": dropped, "owner_pid": owner,
            "first_ts": first_ts, "last_ts": last_ts}


def snapshot(seconds: float = 2.0, path: Path | str = DEFAULT_PATH) -> dict:
    """Read the most recent N seconds; never opens an audio device."""
    p = Path(path)
    h = read_header(p)
    if not h:
        return {"ok": False, "reason": "no_ring", "rms": 0.0, "peak": 0.0}
    n = max(1, min(h["total_frames"], int(h["rate"] * float(seconds))))
    if h["total_frames"] <= 0:
        return {"ok": True, "rms": 0.0, "peak": 0.0, "frames": 0, **h}
    try:
        with p.open("rb") as fh:
            fh.seek(128)
            data = fh.read(h["capacity"] * h["frame_bytes"])
    except OSError:
        return {"ok": False, "reason": "read_failed", "rms": 0.0, "peak": 0.0}
    arr = np.frombuffer(data, dtype=np.int16)
    if arr.size == 0:
        return {"ok": True, "rms": 0.0, "peak": 0.0, "frames": 0, **h}
    frames = arr.size // max(1, h["channels"])
    if frames == 0:
        return {"ok": True, "rms": 0.0, "peak": 0.0, "frames": 0, **h}
    arr = arr[:frames * h["channels"]].reshape(frames, h["channels"]).mean(axis=1)
    idx = (h["write_pos"] - n + np.arange(n)) % h["capacity"]
    tail = arr[idx]
    x = tail.astype(np.float32) / 32768.0
    return {"ok": True, "rms": round(float(np.sqrt(np.mean(x ** 2))), 6),
            "peak": round(float(np.max(np.abs(x))), 5), "frames": int(tail.size),
            **h}


def read_stats(path: Path | str = DEFAULT_STATS) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
