#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""进程级回环采集的**修正版**（不改旧 `video_runtime`，用子类覆盖）。

修的是什么（2026-10-31 20:10 逐项定位）：

1) **采错了进程**（真正的主因）
   旧 `start()` 对一串 pid 逐个试激活，**谁先成功就用谁**。实测它选中了
   `pid=2356`（QQ 的 utility 子进程），而 QQ 的声音实际由**根进程树**里的进程渲染。
   → 修法：以 **QQ 根进程**为目标（`qq_root_pids()`），配
     `PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE`，一次性覆盖整棵树。

2) **未归一化的 PCM**
   旧 `read()` 返回的是 int16 量级的 float（±32767）。下游按 "max>2 才除以 32768"
   判断，遇到**接近静音**（max=±1）就跳过归一化，于是 ±1 被当成 ±1.0 的音量 →
   实测把一段**纯静音**算成了 RMS 0.34（我因此一度误判"进程隔离失效"）。
   → 修法：`read()` 直接返回 **[-1,1] 的 float32**，把归一化收进采集层，别让下游猜。

3) **SILENT 标志未处理**
   `AUDCLNT_BUFFERFLAGS_SILENT(0x2)` 置位时缓冲区内容**未定义**，旧代码照样读。
   → 修法：置位时按帧数补真正的静音。

用法：
    from proc_capture_fix import FixedProcessLoopbackSource, qq_root_pids
    src = FixedProcessLoopbackSource(pids=qq_root_pids(), rate=48000).start()
    pcm = src.read()          # float32, 范围 [-1,1]
"""
from __future__ import annotations

import ctypes
import json
import subprocess
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent

from video_runtime.process_loopback_stream import (  # noqa: E402
    ProcessLoopbackSource, _hr, _vcall,
)

#: IAudioCaptureClient::GetBuffer 的 flags 位。置位时缓冲区内容未定义，必须当静音处理。
AUDCLNT_BUFFERFLAGS_SILENT = 0x00000002


#: Read-only CIM query.  Kept as a module constant so this module contains no
#: general-purpose command runner: the only PowerShell invocation is this fixed
#: string (the earlier `_ps(cmd)` helper was removed for that reason).
QQ_PROCESS_QUERY = (
    "Get-CimInstance Win32_Process -Filter \"Name='QQ.exe'\" | "
    "Select-Object ProcessId,ParentProcessId,CommandLine | ConvertTo-Json -Compress"
)


def qq_processes() -> list[dict]:
    """列出 QQ.exe 的 pid / ppid / 命令行（只读，固定 CIM 查询）。"""
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command", QQ_PROCESS_QUERY],
            capture_output=True, text=True, timeout=25.0,
            encoding="utf-8", errors="replace")
        out = (r.stdout or "").strip()
    except Exception:  # noqa: BLE001
        return []
    if not out:
        return []
    try:
        data = json.loads(out)
    except Exception:  # noqa: BLE001
        return []
    if isinstance(data, dict):
        data = [data]
    return data


def qq_root_pids() -> list[int]:
    """QQ 里"父进程不是 QQ"的那些 pid —— 以它们为采集目标即可覆盖整棵树。"""
    procs = qq_processes()
    all_pids = {p.get("ProcessId") for p in procs}
    roots = [p.get("ProcessId") for p in procs if p.get("ParentProcessId") not in all_pids]
    return [int(r) for r in roots if r]


def qq_all_pids() -> list[int]:
    return [int(p["ProcessId"]) for p in qq_processes() if p.get("ProcessId")]


class FixedProcessLoopbackSource(ProcessLoopbackSource):
    """修好三处问题的进程回环采集。

    与旧类的接口兼容（`start()/read()/stop()`），但：
      · `read()` 返回 **[-1,1] 归一化 float32**；
      · 默认目标改为 **QQ 根进程**（覆盖整棵进程树）；
      · SILENT 缓冲区按静音处理。
    """

    silent_buffers = 0
    real_buffers = 0
    normalize = True

    #: Bounded audio buffer (2026-11-06): at 48 kHz stereo int16, 4 MiB is about
    #: 10.9 s of audio.  When the consumer falls behind we drop the OLDEST
    #: buffers (live ASR cares about the newest audio), count every drop, and
    #: never block or grow without bound.
    MAX_BUFFERED_BYTES = 4 * 1024 * 1024
    MAX_SINGLE_BUFFER_BYTES = 1 * 1024 * 1024

    def __init__(self, pids=None, rate=48000, chunk=1024, parent_pids=None,
                 prefer_root: bool = True):
        super().__init__(pids=pids, rate=rate, chunk=chunk, parent_pids=parent_pids)
        self.prefer_root = prefer_root
        self.candidates: list[int] = []
        self._buffered_bytes = 0
        self.dropped_buffers = 0
        self.dropped_bytes = 0
        self.last_drop_reason: str | None = None

    # ------------------------------------------------------------ 有界缓冲
    def _drop(self, size: int, reason: str) -> None:
        self.dropped_buffers += 1
        self.dropped_bytes += int(size)
        self.last_drop_reason = reason

    def _enqueue(self, buf: bytes, frames: int) -> None:
        """Append one capture buffer; drop the oldest when the cap is reached.

        Called from the capture thread.  The lock is only held for bookkeeping,
        so a slow consumer can never block capture: excess data is dropped and
        counted instead.
        """
        size = len(buf)
        if size <= 0:
            return
        if size > self.MAX_SINGLE_BUFFER_BYTES:
            self._drop(size, "oversized_buffer")
            return
        with self._lock:
            while self._frames and self._buffered_bytes + size > self.MAX_BUFFERED_BYTES:
                old = self._frames.pop(0)          # oldest first: keep newest audio
                self._buffered_bytes -= len(old)
                self.dropped_buffers += 1
                self.dropped_bytes += len(old)
                self.last_drop_reason = "buffer_full"
            self._frames.append(bytes(buf))
            self._buffered_bytes += size
            self.samples_read += int(frames)

    # ------------------------------------------------------------ 目标选择
    def _candidate_pids(self) -> list[int]:
        if self.parent_pids:
            return list(self.parent_pids)
        roots = qq_root_pids()
        if self.prefer_root and roots:
            # 根进程能覆盖整棵树，优先它；其余作为兜底
            return roots + [p for p in qq_all_pids() if p not in roots]
        return qq_all_pids()

    def start(self):
        """以根进程为目标开流；根进程失败再退回逐个尝试。

        注意：**必须**先做 COM(MTA) 初始化与支持性检查（父类 start() 里做过，
        这里覆盖时漏过一次，表现为 `ActivateAudioInterfaceAsync hr=0x800401F0`
        = CO_E_NOTINITIALIZED）。
        """
        from video_runtime import process_loopback as pl

        support = pl.check_support()
        if not support.get("supported"):
            raise RuntimeError("; ".join(support.get("reasons") or []) or "系统不支持进程 loopback")
        if not getattr(self, "_com_ready", False):
            hr_com = ctypes.windll.ole32.CoInitializeEx(None, 0x0)   # COINIT_MULTITHREADED
            self._com_ready = _hr(hr_com) in (0, 1)
            self._com_hr = _hr(hr_com)

        cands = self._candidate_pids()
        self.candidates = list(cands)
        if not cands:
            raise RuntimeError("没有找到 QQ 进程（先启动 QQ 客户端）")
        errors = []
        for pid in cands:
            try:
                unk = self._activate(pid)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"pid={pid}: {exc}")
                continue
            self._client = unk
            self.open_pid = pid
            break
        if not self._client:
            raise RuntimeError("所有候选进程都开流失败：" + " | ".join(errors[:3]))
        self._init_client()
        return self

    # ------------------------------------------------------------ 采集线程
    def _loop(self):
        cap = self._capture
        while self._run.is_set():
            try:
                n = ctypes.c_uint32(0)
                if _hr(_vcall(cap, 5, ctypes.byref(n))) != 0:      # GetNextPacketSize
                    time.sleep(0.02)
                    continue
                if n.value == 0:
                    time.sleep(0.02)
                    continue
                data = ctypes.c_void_p()
                frames = ctypes.c_uint32(0)
                flags = ctypes.c_uint32(0)
                if _hr(_vcall(cap, 3, ctypes.byref(data), ctypes.byref(frames),
                              ctypes.byref(flags), None, None)) != 0:   # GetBuffer
                    continue
                if frames.value:
                    if flags.value & AUDCLNT_BUFFERFLAGS_SILENT:
                        buf = b"\x00" * (frames.value * 4)          # 内容未定义，补真静音
                        self.silent_buffers += 1
                    else:
                        buf = ctypes.string_at(data, frames.value * 4)
                        self.real_buffers += 1
                    self._enqueue(bytes(buf), int(frames.value))
                _vcall(cap, 4, ctypes.c_uint32(frames.value))       # ReleaseBuffer
            except Exception as exc:  # noqa: BLE001
                self.last_error = str(exc)
                time.sleep(0.05)

    # ------------------------------------------------------------ 读取
    def read(self):
        """返回 [-1,1] 归一化 float32；没有数据返回 None。"""
        with self._lock:
            if not self._frames:
                return None
            raw = self._frames.pop(0)
            self._buffered_bytes = max(0, self._buffered_bytes - len(raw))
        pcm = np.frombuffer(raw, dtype=np.int16)
        if self.native_channels and self.native_channels > 1:
            pcm = pcm.reshape(-1, self.native_channels).mean(axis=1)
        out = pcm.astype(np.float32)
        if self.normalize:
            out = out / 32768.0
        return out


def stats(src) -> dict:
    return {
        "open_pid": getattr(src, "open_pid", None),
        "candidates": getattr(src, "candidates", []),
        "native_rate": getattr(src, "native_rate", None),
        "native_channels": getattr(src, "native_channels", None),
        "samples_read": getattr(src, "samples_read", 0),
        "real_buffers": getattr(src, "real_buffers", 0),
        "silent_buffers": getattr(src, "silent_buffers", 0),
        "last_error": getattr(src, "last_error", None),
        # Bounded-buffer accounting (FIX-QUEUE-1)
        "buffered_buffers": len(getattr(src, "_frames", []) or []),
        "buffered_bytes": getattr(src, "_buffered_bytes", 0),
        "max_buffered_bytes": getattr(src, "MAX_BUFFERED_BYTES", None),
        "dropped_buffers": getattr(src, "dropped_buffers", 0),
        "dropped_bytes": getattr(src, "dropped_bytes", 0),
        "last_drop_reason": getattr(src, "last_drop_reason", None),
    }
