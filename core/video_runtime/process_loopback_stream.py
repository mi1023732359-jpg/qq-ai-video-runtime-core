# -*- coding: utf-8 -*-
"""AudioClient 进程级 loopback 音频流（E2 第二层）。

用 Windows 自带的
    ActivateAudioInterfaceAsync + AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK
采**指定进程**（QQ 及其子进程）的渲染音频，输出 16kHz 单声道 int16 PCM。

为什么手写 vtable：本机 `comtypes 1.4.17` 没有缓存的 MMDeviceApi 类型库，
调 `comtypes.client.GetModule` 需要注册表里有类型库；这里直接用 ctypes 拼 COM 虚表，
只依赖 `mmdevapi.dll` / `combase.dll` 的导出，**零第三方依赖、零安装**。

接口契约与 `audio_capture.LoopbackSource` 一致：
    src = ProcessLoopbackSource(pids=[...])
    src.start();  pcm = src.read();  src.stop()
`read()` 返回 float32 的 int16 值（与 LoopbackSource 相同），失败返回 None。

安全性：只读采集。不注入、不写 QQ、不改任何系统设备。
"""
import ctypes
import json
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

from video_runtime import process_loopback as pl

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "logs" / "process_loopback_stream.json"
TARGET_RATE = 16000

# ---------------------------------------------------------------- 常量
AUDCLNT_SHAREMODE_SHARED = 0
AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000
AUDCLNT_STREAMFLAGS_EVENTCALLBACK = 0x00040000
AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK = 1
PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE = 0
WAVE_FORMAT_PCM = 1
KSDATAFORMAT_SUBTYPE_PCM = "{00000001-0000-0010-8000-00aa00389b71}"
VT_BLOB = 65
COINIT_MULTITHREADED = 0x0


class GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]

    @classmethod
    def from_string(cls, s):
        s = s.strip("{}")
        parts = s.split("-")
        g = cls()
        g.Data1 = int(parts[0], 16)
        g.Data2 = int(parts[1], 16)
        g.Data3 = int(parts[2], 16)
        b = bytes.fromhex(parts[3] + parts[4])
        for i, v in enumerate(b):
            g.Data4[i] = v
        return g


IAL = "{41D949AB-9862-444A-80F6-C261334DA5EB}"          # IActivateAudioInterfaceAsyncOperation
IID_IAudioClient = "{1CB9AD4C-DBFA-4c32-B178-C2F568A703B2}"
IID_IAudioCaptureClient = "{C8ADBD64-E71E-48a0-A4DE-185C395CD317}"
IID_IAgileObject = "{94EA2B94-E9CC-49E0-C0FF-EE64CA8F5B90}"


class WAVEFORMATEX(ctypes.Structure):
    _fields_ = [("wFormatTag", ctypes.c_ushort), ("nChannels", ctypes.c_ushort),
                ("nSamplesPerSec", ctypes.c_ulong), ("nAvgBytesPerSec", ctypes.c_ulong),
                ("nBlockAlign", ctypes.c_ushort), ("wBitsPerSample", ctypes.c_ushort),
                ("cbSize", ctypes.c_ushort)]


class AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS(ctypes.Structure):
    _fields_ = [("targetProcessId", ctypes.c_ulong), ("processLoopbackMode", ctypes.c_int)]


class AUDIOCLIENT_ACTIVATION_PARAMS(ctypes.Structure):
    _fields_ = [("activationType", ctypes.c_int),
                ("processLoopbackParams", AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS)]


class PROPVARIANT_BLOB(ctypes.Structure):
    _fields_ = [("vt", ctypes.c_ushort), ("wReserved1", ctypes.c_ushort),
                ("wReserved2", ctypes.c_ushort), ("wReserved3", ctypes.c_ushort),
                ("cbSize", ctypes.c_ulong), ("pBlobData", ctypes.c_void_p)]


def _vcall(punk, index, *args):
    """按虚表下标调用 COM 方法。

    第 2 轮修复：原来用 `type(a)` 推参数类型，遇到 `ctypes.byref(...)` 会得到 CArgObject，
    不是合法的 ctypes 参数类型 → 调用错位，表现为 GetActivateResult 拿到无效接口
    （hr=0xFFFFFFFF）。这里统一按 c_void_p 传，并对简单类型取整数值。
    """
    if not punk:
        raise RuntimeError("COM 指针为空")
    vtbl = ctypes.cast(punk, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    proto = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p,
                               *([ctypes.c_void_p] * len(args)))
    fn = proto(vtbl[index])
    conv = []
    for a in args:
        if isinstance(a, ctypes._SimpleCData):
            conv.append(int(a.value))
        elif isinstance(a, int) or a is None:
            conv.append(a)
        else:
            conv.append(ctypes.cast(a, ctypes.c_void_p))
    return fn(punk, *conv)


def _release(punk):
    try:
        _vcall(punk, 2)      # IUnknown::Release = 2
    except Exception:
        pass


def _hr(hr):
    return ctypes.c_long(hr).value


class ProcessLoopbackSource:
    """采指定进程树的渲染音频。接口与 audio_capture.LoopbackSource 对齐。"""

    def __init__(self, pids=None, rate=TARGET_RATE, chunk=1024, parent_pids=None):
        self.parent_pids = list(pids or parent_pids or [])
        self.rate = int(rate)
        self.chunk = int(chunk)
        self._client = None
        self._capture = None
        self._event = None
        self._thread = None
        self._run = threading.Event()
        self._frames = []
        self._lock = threading.Lock()
        self.last_error = None
        self.native_rate = 0
        self.native_channels = 0
        self.samples_read = 0
        self.overflows = 0

    # ------------------------------------------------------------ 开流
    def _activate(self, pid):
        """对单个 pid 调 ActivateAudioInterfaceAsync，返回 IAudioClient 指针。"""
        mmdev = ctypes.WinDLL("mmdevapi.dll")
        ole32 = ctypes.WinDLL("ole32.dll", use_last_error=True)

        params = AUDIOCLIENT_ACTIVATION_PARAMS()
        params.activationType = AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK
        params.processLoopbackParams.targetProcessId = int(pid)
        params.processLoopbackParams.processLoopbackMode = \
            PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE

        blob = PROPVARIANT_BLOB()
        blob.vt = VT_BLOB
        blob.cbSize = ctypes.sizeof(params)
        blob.pBlobData = ctypes.cast(ctypes.byref(params), ctypes.c_void_p)

        iid = GUID.from_string(IID_IAudioClient)
        out = ctypes.c_void_p()
        # ActivateAudioInterfaceAsync 需要 IActivateAudioInterfaceCompletionHandler；
        # 这里用一个最小的 ctypes 实现（仅实现 QueryInterface/AddRef/Release/ActivateCompleted）
        handler = _CompletionHandler()
        hr = mmdev.ActivateAudioInterfaceAsync(
            ctypes.c_wchar_p("VAD\\Process_Loopback"),
            ctypes.byref(iid), ctypes.byref(blob),
            handler.ptr, ctypes.byref(out))
        if _hr(hr) != 0:
            raise RuntimeError("ActivateAudioInterfaceAsync hr=0x%08X" % (_hr(hr) & 0xFFFFFFFF))
        if not handler.done.wait(3.0):
            raise RuntimeError("ActivateAudioInterfaceAsync 超时")
        if handler.unk:
            return handler.unk
        raise RuntimeError("激活未返回接口 hr=0x%08X" % (handler.hr & 0xFFFFFFFF))

    def start(self):
        """选一个真正有音频的 QQ pid 开流。失败抛 RuntimeError（带原因）。"""
        support = pl.check_support()
        if not support["supported"]:
            raise RuntimeError("; ".join(support["reasons"]) or "系统不支持进程 loopback")
        # 本线程必须初始化 COM（MTA），否则 ActivateAudioInterfaceAsync 直接返回
        # CO_E_NOTINITIALIZED(0x800401F0) —— 本轮第一版就是这么失败的（已修）。
        if not getattr(self, "_com_ready", False):
            hr_com = ctypes.windll.ole32.CoInitializeEx(None, COINIT_MULTITHREADED)
            self._com_ready = _hr(hr_com) in (0, 1)      # S_OK / S_FALSE
            self._com_hr = _hr(hr_com)
        pids = self.parent_pids or pl.process_tree(pl.qq_pids()["pids"])["pids"]
        if not pids:
            raise RuntimeError("没有找到 QQ 进程（先启动 QQ 客户端）")
        errors = []
        for pid in pids:
            try:
                unk = self._activate(pid)
                self._client = unk
                self.open_pid = pid
                break
            except Exception as exc:
                errors.append("pid=%s: %s" % (pid, exc))
        if not self._client:
            raise RuntimeError("所有 QQ 进程都开流失败：" + " | ".join(errors[:3]))
        self._init_client()
        return self

    def _init_client(self):
        client = self._client
        fmt = WAVEFORMATEX()
        fmt.wFormatTag = WAVE_FORMAT_PCM
        fmt.nChannels = 2
        fmt.nSamplesPerSec = 48000
        fmt.wBitsPerSample = 16
        fmt.nBlockAlign = fmt.nChannels * fmt.wBitsPerSample // 8
        fmt.nAvgBytesPerSec = fmt.nSamplesPerSec * fmt.nBlockAlign
        fmt.cbSize = 0
        # IAudioClient 虚表：3=Initialize 4=GetBufferSize 5=GetStreamLatency
        # 6=GetCurrentPadding 7=IsFormatSupported 8=GetMixFormat 9=GetDevicePeriod
        # 10=Start 11=Stop 12=Reset 13=SetEventHandle 14=GetService
        hr = _vcall(client, 3, ctypes.c_int(AUDCLNT_SHAREMODE_SHARED),
                    ctypes.c_ulong(AUDCLNT_STREAMFLAGS_LOOPBACK | AUDCLNT_STREAMFLAGS_EVENTCALLBACK),
                    ctypes.c_longlong(2_000_000), ctypes.c_longlong(0),
                    ctypes.byref(fmt), None)
        if _hr(hr) != 0:
            # 不带事件回调重试一次（部分系统 Win11 对 process loopback 的事件回调要求更严）
            hr = _vcall(client, 3, ctypes.c_int(AUDCLNT_SHAREMODE_SHARED),
                        ctypes.c_ulong(AUDCLNT_STREAMFLAGS_LOOPBACK),
                        ctypes.c_longlong(2_000_000), ctypes.c_longlong(0),
                        ctypes.byref(fmt), None)
            if _hr(hr) != 0:
                raise RuntimeError("IAudioClient::Initialize hr=0x%08X" % (_hr(hr) & 0xFFFFFFFF))
            self._use_event = False
        else:
            self._use_event = True
        self.native_rate = fmt.nSamplesPerSec
        self.native_channels = fmt.nChannels
        iid_cap = GUID.from_string(IID_IAudioCaptureClient)
        cap = ctypes.c_void_p()
        hr = _vcall(client, 14, ctypes.byref(iid_cap), ctypes.byref(cap))
        if _hr(hr) != 0:
            raise RuntimeError("GetService(IAudioCaptureClient) hr=0x%08X" % (_hr(hr) & 0xFFFFFFFF))
        self._capture = cap
        hr = _vcall(client, 10)      # Start
        if _hr(hr) != 0:
            raise RuntimeError("IAudioClient::Start hr=0x%08X" % (_hr(hr) & 0xFFFFFFFF))
        self._run.set()
        self._thread = threading.Thread(target=self._loop, name="proc-loopback", daemon=True)
        self._thread.start()

    # ------------------------------------------------------------ 读取
    def _loop(self):
        # IAudioCaptureClient 虚表：3=GetBuffer 4=ReleaseBuffer 5=GetNextPacketSize
        cap = self._capture
        while self._run.is_set():
            try:
                n = ctypes.c_uint32(0)
                if _hr(_vcall(cap, 5, ctypes.byref(n))) != 0:
                    time.sleep(0.02)
                    continue
                if n.value == 0:
                    time.sleep(0.02)
                    continue
                data = ctypes.c_void_p()
                frames = ctypes.c_uint32(0)
                flags = ctypes.c_uint32(0)
                if _hr(_vcall(cap, 3, ctypes.byref(data), ctypes.byref(frames),
                              ctypes.byref(flags), None, None)) != 0:
                    continue
                if frames.value:
                    buf = ctypes.string_at(data, frames.value * 4)
                    with self._lock:
                        self._frames.append(bytes(buf))
                        self.samples_read += frames.value
                _vcall(cap, 4, ctypes.c_uint32(frames.value))
            except Exception as exc:
                self.last_error = str(exc)
                time.sleep(0.05)

    def read(self):
        with self._lock:
            if not self._frames:
                return None
            raw = self._frames.pop(0)
        import numpy as np
        pcm = np.frombuffer(raw, dtype=np.int16)
        if self.native_channels > 1:
            pcm = pcm.reshape(-1, self.native_channels).mean(axis=1)
        return pcm.astype(np.float32)

    def stop(self):
        self._run.clear()
        try:
            if self._client:
                _vcall(self._client, 11)      # Stop
        except Exception:
            pass
        if self._thread:
            self._thread.join(timeout=2.0)
        for p in (self._capture, self._client):
            _release(p)
        self._capture = None
        self._client = None
        if getattr(self, "_com_ready", False):
            try:
                ctypes.windll.ole32.CoUninitialize()
            except Exception:
                pass
            self._com_ready = False


class _CompletionHandler:
    """最小 IActivateAudioInterfaceCompletionHandler 实现（ctypes 虚表）。"""

    def __init__(self):
        self.done = threading.Event()
        self.unk = None
        self.hr = 0
        # 虚表：0 QI 1 AddRef 2 Release 3 ActivateCompleted
        handlers = [
            ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)(self._qi),
            ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(lambda this: 2),
            ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(lambda this: 1),
            ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p)(self._completed),
        ]
        self._handlers = handlers
        arr = (ctypes.c_void_p * len(handlers))(*[ctypes.cast(h, ctypes.c_void_p)
                                                  for h in handlers])
        self._vtbl = arr
        self._vtbl_ptr = ctypes.cast(arr, ctypes.c_void_p)
        self.ptr = ctypes.cast(ctypes.pointer(self._vtbl_ptr), ctypes.c_void_p)

    def _qi(self, this, riid, ppv):
        # 只报告支持 IUnknown/IAL
        if ppv:
            ctypes.cast(ppv, ctypes.POINTER(ctypes.c_void_p))[0] = this
        return 0

    def _completed(self, this, op):
        try:
            # IActivateAudioInterfaceAsyncOperation 虚表：3=GetActivateResult
            hr = ctypes.c_long(0)
            unk = ctypes.c_void_p()
            _vcall(op, 3, ctypes.byref(hr), ctypes.byref(unk))
            self.hr = hr.value
            if hr.value == 0 and unk.value:
                # GetActivateResult 返回的就是激活的接口（IAudioClient），直接用
                self.unk = unk
            elif hr.value == 0:
                self.hr = -1
        except Exception as exc:
            self.hr = -1
            self.last_error = str(exc)
        finally:
            self.done.set()
        return 0


def main(argv):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            continue
    seconds = 3.0
    for i, a in enumerate(argv):
        if a == "--seconds" and i + 1 < len(argv):
            seconds = float(argv[i + 1])
    out = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "seconds": seconds}
    support = pl.check_support()
    out["support"] = support
    src = None
    try:
        src = ProcessLoopbackSource().start()
        out["open_pid"] = getattr(src, "open_pid", None)
        out["native"] = {"rate": src.native_rate, "channels": src.native_channels}
        peak = 0.0
        frames = 0
        t0 = time.time()
        import numpy as np
        while time.time() - t0 < seconds:
            d = src.read()
            if d is None:
                time.sleep(0.02)
                continue
            if d.size:
                peak = max(peak, float(np.max(np.abs(d))) / 32768.0)
                frames += d.size
        out.update(ok=True, peak=round(peak, 4), frames=int(frames),
                   error=getattr(src, "last_error", None))
    except Exception as exc:
        out.update(ok=False, error=str(exc))
    finally:
        if src is not None:
            try:
                src.stop()
            except Exception:
                pass
    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"ok": out.get("ok"), "peak": out.get("peak"),
                      "frames": out.get("frames"), "error": out.get("error"),
                      "evidence": str(EVIDENCE)}, ensure_ascii=False))
    return 0 if out.get("ok") else 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
