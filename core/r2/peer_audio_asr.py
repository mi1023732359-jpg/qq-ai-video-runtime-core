#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Stage D：把 **QQ 进程的音频**接进本地 ASR（进程级隔离，不混入其他软件的声音）。

链路：
    QQ.exe（通话对端声音）→ ActivateAudioInterfaceAsync + PROCESS_LOOPBACK（仅目标进程树）
    → PCM 16k 单声道 → faster-whisper 转写 → 文本

为什么用进程级而不是系统 loopback：
    系统 loopback 会同时采到音乐/游戏/通知等一切声音，会把别人的声音当成"对方在说话"。
    进程级只采目标进程树，才谈得上"对方在说什么"。

安全护栏（任务书要求）：
    - 采集失败必须报错，**不允许**静默回退系统级采集；
    - 启动时打印实际目标 PID 与进程名，便于核对"采的确实是它"。

用法：
    python r2\\peer_audio_asr.py --pids <pid,pid> --seconds 20
    python r2\\peer_audio_asr.py --qq                 # 自动找 QQ 根进程及其子进程
    python r2\\peer_audio_asr.py --pid <pid> --wav out.wav   # 只录音不转写（便于核对）
退出码：0 正常；1 采集失败（带原因）。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np

R2_DIR = Path(__file__).resolve().parent
R2_ROOT = R2_DIR.parent

LOG_DIR = R2_ROOT / "logs"
TARGET_RATE = 16000


def _process_table() -> list[tuple[int, int, str]]:
    """Read-only Toolhelp snapshot: (pid, ppid, exe name).

    Pure ``ctypes``; no subprocess, no PowerShell, no command string.  Returns an
    empty list when the snapshot cannot be created.
    """
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    TH32CS_SNAPPROCESS = 0x00000002
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    class PE32(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]

    # 64-bit safe prototypes (FIX-HANDLE-1).  HANDLE is pointer-sized: without an
    # explicit ``restype`` ctypes assumes C ``int`` and truncates the snapshot
    # handle to 32 bits on 64-bit Windows, and without ``argtypes`` it would pass
    # the handle to ``CloseHandle`` as a 32-bit value.  Every function that takes
    # or returns a HANDLE is typed here.
    k32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k32.Process32FirstW.restype = wintypes.BOOL
    k32.Process32FirstW.argtypes = [ctypes.c_void_p, ctypes.POINTER(PE32)]
    k32.Process32NextW.restype = wintypes.BOOL
    k32.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(PE32)]
    k32.CloseHandle.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = [ctypes.c_void_p]

    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == INVALID_HANDLE_VALUE:
        return []
    found: list[tuple[int, int, str]] = []
    try:
        e = PE32()
        e.dwSize = ctypes.sizeof(PE32)
        ok = k32.Process32FirstW(snap, ctypes.byref(e))
        while ok:
            found.append((int(e.th32ProcessID), int(e.th32ParentProcessID),
                          str(e.szExeFile or "")))
            ok = k32.Process32NextW(snap, ctypes.byref(e))
    finally:
        k32.CloseHandle(snap)
    return found


def qq_pids() -> list[int]:
    """取 QQ.exe 的全部 pid（根 + 子进程）。只读 Toolhelp 快照。"""
    found = _process_table()
    qq = {pid for pid, _pp, name in found if name.lower() == "qq.exe"}
    return sorted(qq)


def _coerce_pids(pids) -> list[int]:
    """Validate PID input strictly: positive ints only (bool is rejected).

    Raises ``TypeError``/``ValueError`` so callers cannot smuggle strings into a
    command or a system call; ``process_names()`` converts that into ``[]``.
    """
    if not isinstance(pids, (list, tuple, set)):
        raise TypeError("pids must be a sequence of positive ints")
    out: list[int] = []
    for pid in pids:
        if isinstance(pid, bool) or not isinstance(pid, int):
            raise TypeError("pid must be an int")
        if pid <= 0:
            raise ValueError("pid must be positive")
        out.append(pid)
    return out


def process_names(pids: list[int]) -> list[str]:
    """Names for the given PIDs, from a read-only Toolhelp snapshot.

    There is **no** subprocess / PowerShell / string interpolation here: the
    earlier implementation built a ``-Command`` string from caller input
    (command injection).  Invalid input returns ``[]`` and never executes
    anything.
    """
    try:
        wanted = set(_coerce_pids(pids))
    except (TypeError, ValueError):
        return []
    if not wanted:
        return []
    return [f"{pid}:{name}" for pid, _pp, name in _process_table() if pid in wanted]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="QQ 进程音频 → ASR")
    ap.add_argument("--qq", action="store_true", help="自动选择全部 QQ.exe 进程")
    ap.add_argument("--pid", type=int, action="append", default=None, help="指定 pid（可多次）")
    ap.add_argument("--pids", default=None, help="逗号分隔的 pid 列表")
    ap.add_argument("--seconds", type=float, default=20.0)
    ap.add_argument("--chunk-seconds", type=float, default=3.0, help="每段送 ASR 的时长")
    ap.add_argument("--wav", default=None, help="只录音存成 wav（不转写）")
    ap.add_argument("--min-rms", type=float, default=0.004, help="低于此 RMS 视为静音，不送 ASR")
    args = ap.parse_args(argv)

    pids: list[int] = []
    if args.qq:
        pids = qq_pids()
    elif args.pids:
        pids = [int(x) for x in args.pids.split(",") if x.strip()]
    elif args.pid:
        pids = list(args.pid)
    if not pids:
        print("[FAIL] 没有目标 pid（用 --qq 或 --pid）")
        return 1

    print(f"[目标] {len(pids)} 个进程: {process_names(pids)}")

    from video_runtime.process_loopback_stream import ProcessLoopbackSource

    src = ProcessLoopbackSource(pids=pids, rate=48000)  # 原生率：实测内置重采样会破坏音频
    try:
        src.start()
    except Exception as exc:  # noqa: BLE001
        # 明确报错，绝不回退系统级采集
        print(f"[FAIL] 进程级采集启动失败：{type(exc).__name__}: {exc}")
        print("        按项目约定：不允许回退到系统级 loopback。")
        return 2
    print(f"[采集] 已开启：native {src.native_rate}Hz/{src.native_channels}ch → 自做 3:1 抗混叠降采样到 {TARGET_RATE}Hz 单声道")

    buffered: list[np.ndarray] = []
    samples = 0
    seg_samples = int(args.chunk_seconds * TARGET_RATE)
    texts: list[dict] = []
    t0 = time.time()
    transcriber = None
    if not args.wav:
        from video_runtime.asr_in import WhisperTranscriber
        transcriber = WhisperTranscriber(model_size="small", language="zh")
        print("[ASR] faster-whisper small 已就绪（首次加载可能几秒）")

    try:
        while time.time() - t0 < args.seconds:
            chunk = src.read()
            if chunk is None:
                time.sleep(0.02)
                continue
            chunk = np.asarray(chunk, dtype=np.float32).reshape(-1)
            # 进程采集返回的是"int16 数值但用 float32 装"（未归一化到 -1..1）。
            # 不先归一化会导致 PCM 溢出成方波 → ASR 只会输出"啊啊啊"。
            if chunk.size:
                peak = float(np.max(np.abs(chunk)))
                if peak > 2.0:
                    chunk = chunk / 32768.0
                # 48k -> 16k：3 点盒式平均（先低通再抽取，避免混叠）
                n = (chunk.size // 3) * 3
                if n:
                    chunk = chunk[:n].reshape(-1, 3).mean(axis=1)
            buffered.append(chunk)
            samples += len(chunk)
            if samples < seg_samples:
                continue
            audio = np.concatenate(buffered)
            buffered, samples = [], 0
            rms = float(np.sqrt(np.mean(audio ** 2))) if audio.size else 0.0
            if rms < args.min_rms:
                print(f"[跳过] 静音段 rms={rms:.4f}")
                continue
            pcm16 = np.clip(audio * 32767.0, -32768, 32767).astype(np.int16)
            if args.wav:
                continue
            t1 = time.time()
            text = transcriber.transcribe(pcm16, TARGET_RATE)
            dt = time.time() - t1
            print(f"[ASR] rms={rms:.4f} 用时{dt:.1f}s → {text!r}")
            texts.append({"at": time.strftime("%H:%M:%S"), "rms": round(rms, 4),
                          "seconds": round(len(pcm16) / TARGET_RATE, 2), "text": text})
    finally:
        src.stop()
        if args.wav and buffered:
            import wave
            pcm16 = np.clip(np.concatenate(buffered) * 32767.0, -32768, 32767).astype(np.int16)
            with wave.open(args.wav, "wb") as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(TARGET_RATE)
                w.writeframes(pcm16.tobytes())
            print(f"[录音] 已保存 {args.wav} ({len(pcm16)/TARGET_RATE:.1f}s)")

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    out = LOG_DIR / "peer_asr.json"
    out.write_text(json.dumps({"at": time.strftime("%Y-%m-%d %H:%M:%S"),
                               "pids": pids, "texts": texts}, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print(f"[结束] 转写 {len(texts)} 段；证据 {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

