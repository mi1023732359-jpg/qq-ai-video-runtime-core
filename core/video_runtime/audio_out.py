# -*- coding: utf-8 -*-
# 由 Codex 实现/审查：严格解析输出设备，仅显式授权时回退。
# 由 Codex 实现/审查。自测：python -m video_runtime.audio_out --devices；运行 test_audio_runtime.py。
"""Local WAV synthesis/playback. Mouth position follows the PortAudio DAC clock.

No production imports or writes. device is an output-name substring (or index),
defaulting to VIDEO_AUDIO_DEVICE / CABLE Input. play=False keeps the audio clock
running with silent samples, for existing /speak preview clients.
"""
import collections
import math
import os
from pathlib import Path
import struct
import subprocess
import threading
import uuid
import warnings
import wave

ROOT = Path(__file__).resolve().parents[1]

# 默认输出设备名。T1 定稿：/speak 未显式传 device 时也解析到这里（control_server
# 的 DEFAULT_SPEAK_DEVICE 与本常量同值，避免两处漂移）。
DEFAULT_OUTPUT_DEVICE = "CABLE Input"
# VB-Cable 的完整驱动名；Windows 上同一物理端点会被注册成多条，靠它区分真身。
VB_CABLE_NAME = "VB-Audio Virtual Cable"


def list_output_devices():
    """所有可播放设备，含解析规则需要的声道数/host API/采样率。"""
    import sounddevice as sd
    hostapis = sd.query_hostapis()
    return [{"index": i, "name": d["name"], "channels": d["max_output_channels"],
             "hostapi": hostapis[d["hostapi"]]["name"],
             "samplerate": float(d["default_samplerate"])}
            for i, d in enumerate(sd.query_devices()) if d["max_output_channels"] > 0]


def _matches(devices, wanted):
    """精确名优先，其次包含匹配；int 直接按 index 命中。"""
    if isinstance(wanted, bool):
        raise TypeError("device must be an index (int) or a name (str), not bool")
    if isinstance(wanted, int):
        return [d for d in devices if d["index"] == wanted]
    needle = str(wanted).strip().casefold()
    if not needle:
        return []
    hits = [d for d in devices if needle in d["name"].casefold()]
    exact = [d for d in hits if d["name"].strip().casefold() == needle]
    return exact or hits


def _rank(candidate):
    """T1 解析顺序：2 声道优先 -> 名含 VB-Audio Virtual Cable -> index 最小。

    Windows 上 `CABLE Input` 会同时出现 MME(8ch)/DirectSound(8ch)/WASAPI(2ch)
    三条，MME 那条名字还被截断成 `CABLE Input (VB-Audio Virtual C`。规则顺序
    固定后结果唯一且可复现，不再靠 coincidental 的枚举顺序。
    """
    return (0 if candidate["channels"] == 2 else 1,
            0 if VB_CABLE_NAME.casefold() in candidate["name"].casefold() else 1,
            candidate["index"])


def resolve_device_info(device=None, *, allow_fallback=False):
    """解析结果的完整记录：请求名 / 候选 / 命中 / 理由 / 是否回退。

    调用方（/health、preflight）直接展示这份记录，无需重新扫设备。
    """
    import sounddevice as sd
    wanted = device if device is not None else (
        os.getenv("VIDEO_AUDIO_DEVICE") or DEFAULT_OUTPUT_DEVICE)
    devices = list_output_devices()
    matches = sorted(_matches(devices, wanted), key=_rank)
    info = {"requested": wanted, "candidates": matches, "resolved": None,
            "fallback": False, "reason": None}
    if matches:
        info["resolved"] = matches[0]
        info["reason"] = ("exact unique match" if len(matches) == 1 else
                          "ranked %d candidates (2ch > VB-Audio full name > lowest index)"
                          % len(matches))
        return info
    info["reason"] = "Output device %r not found; available: %s" % (
        wanted, [(d["index"], d["name"]) for d in devices])
    if not allow_fallback:
        return info
    default = sd.default.device[1]
    if not any(d["index"] == default for d in devices):
        info["reason"] += "; no default audio output device is available"
        return info
    actual = [d for d in devices if d["index"] == default][0]
    info.update(resolved=actual, fallback=True, reason=info["reason"] + "; fell back to default")
    warnings.warn(
        "%s; falling back to default output: requested=%r; actual=%r (index=%s); "
        "host api=%r; sample_rate=%s; channels=%s (device defaults/capacity)" % (
            info["reason"], wanted, actual["name"], default, actual["hostapi"],
            actual["samplerate"], actual["channels"]),
        RuntimeWarning, stacklevel=2)
    return info


class ResolvedDevice(int):
    """解析结果：本身就是设备 index（可直接喂给 sounddevice），同时带可读信息。

    T1 验收要求 `print(audio_out.resolve_device('CABLE Input'))` 能看到
    index/name/channels，所以用 int 子类而不用裸 int：既保持既有调用方式
    （`device=resolve_device(...)`）不变，又让日志和 /health 一眼看清命中谁。
    """

    def __new__(cls, record):
        obj = super().__new__(cls, record["index"])
        obj.record = dict(record)
        return obj

    @property
    def index(self):
        return int(self)

    @property
    def name(self):
        return self.record["name"]

    @property
    def channels(self):
        return self.record["channels"]

    @property
    def hostapi(self):
        return self.record["hostapi"]

    @property
    def samplerate(self):
        return self.record["samplerate"]

    def __str__(self):
        return "%d (%s | %sch | %s | %dHz)" % (
            int(self), self.name, self.channels, self.hostapi, round(self.samplerate))

    __repr__ = __str__


def resolve_device(device=None, *, allow_fallback=False):
    """解析出唯一输出设备；找不到或回退未授权一律失败，绝不静默走扬声器。"""
    info = resolve_device_info(device, allow_fallback=allow_fallback)
    if info["resolved"] is None:
        raise RuntimeError(info["reason"])
    return ResolvedDevice(info["resolved"])


def synthesize(text, canceled):
    """Same text -> WAV-path interface as voice_out, with all files local to runtime."""
    folder = ROOT / "logs" / "audio"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (uuid.uuid4().hex + ".wav")
    # Pass text via stdin; never interpolate user input into PowerShell source.
    script = r"""$ErrorActionPreference='Stop'
[Console]::InputEncoding = New-Object System.Text.UTF8Encoding
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding
Add-Type -AssemblyName System.Speech
$s=New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
  $s.SetOutputToWaveFile($args[0])
  $s.Speak([Console]::In.ReadToEnd())
} finally { $s.Dispose() }
"""
    script_path = folder / (path.stem + ".ps1")
    script_path.write_text(script, encoding="utf-8-sig")
    process = None
    try:
        process = subprocess.Popen(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", str(script_path), str(path)], stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        process.stdin.write(str(text).encode("utf-8"))
        process.stdin.close()
        # Bound synthesis and allow stop during generation, before any stream opens.
        import time
        deadline = time.monotonic() + 60
        while process.poll() is None:
            if canceled.wait(0.01) or time.monotonic() > deadline:
                process.kill()
                process.wait()
                raise RuntimeError("Speech synthesis canceled or timed out")
        error = process.stderr.read().decode("utf-8", errors="replace")
        if process.returncode or not path.exists():
            raise RuntimeError("Windows SAPI synthesis failed: " + error)
        if canceled.is_set():
            raise RuntimeError("Speech synthesis canceled")
        return path
    except BaseException:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait()
        path.unlink(missing_ok=True)
        raise
    finally:
        if process is not None:
            if process.stderr:
                process.stderr.close()
        script_path.unlink(missing_ok=True)


def _read_wav(path):
    """SAPI 出来的 WAV 通常是 22050 单声道，与播放设备原生格式无关。"""
    with wave.open(str(path), "rb") as wav:
        channels, width, rate = wav.getnchannels(), wav.getsampwidth(), wav.getframerate()
        raw = wav.readframes(wav.getnframes())
    if width not in (1, 2, 3, 4):
        raise ValueError("Only 8/16/24/32-bit PCM WAV is supported")
    samples = []
    for i in range(0, len(raw), width):
        if width == 1:
            value = (raw[i] - 128) << 8
        else:
            value = int.from_bytes(raw[i:i+width], "little", signed=True) >> (8 * (width - 2))
        samples.append(value)
    return samples, channels, rate


def _resample(samples, channels, src_rate, dst_rate):
    """按帧线性插值到设备原生采样率。

    T1 根因：WASAPI 共享模式只接受设备混音格式的采样率，22050 的 WAV 直接
    `Invalid sample rate (-9997)`，于是"解析成功但一点声音都没有"。这里改由
    播放侧适配设备，而不是把设备降级成 90ms 延迟的 MME。
    """
    if src_rate == dst_rate:
        return samples
    src_frames = len(samples) // channels
    if src_frames == 0:
        return samples
    out_frames = max(1, int(round(src_frames * dst_rate / float(src_rate))))
    step = src_rate / float(dst_rate)
    out = []
    for frame in range(out_frames):
        pos = frame * step
        i0 = int(pos)
        i1 = min(i0 + 1, src_frames - 1)
        frac = pos - i0
        for ch in range(channels):
            s0 = samples[i0 * channels + ch]
            s1 = samples[i1 * channels + ch]
            out.append(int(s0 + (s1 - s0) * frac))
    return out


def _encode(samples, channels, rate):
    """打包成 16bit PCM，并算出 10ms 一步的口型包络。"""
    step = max(1, rate // 100)  # 10 ms envelope; keep the final partial window.
    rms = []
    for i in range(0, len(samples), step * channels):
        chunk = samples[i:i + step * channels]
        rms.append(math.sqrt(sum(v * v for v in chunk) / len(chunk)))
    peak = max(rms, default=0)
    env = [((v / peak) ** 0.65 if peak and v / peak > 0.04 else 0.0) for v in rms]
    return struct.pack("<%dh" % len(samples), *samples), step, env


def device_format(index):
    """设备原生格式（采样率/声道数）——播放前必须对齐，否则 WASAPI 打不开。"""
    import sounddevice as sd
    d = sd.query_devices(index)
    return int(round(d["default_samplerate"])), int(d["max_output_channels"])


_OLE32 = None


def _com_begin():
    """播放线程必须先初始化 COM，否则 WASAPI 流 start() 直接失败。

    实测（logs/t1_cable_route.json）：工作线程不调 CoInitializeEx 时，WASAPI 设备
    `start()` 报 "WdmSyncIoctl: DeviceIoControl GLE = 0x00000492"，全程静音
    （peak 0.0000）；同一线程加上 CoInitializeEx(MTA) 后 peak 0.4996。
    MME/DirectSound 不需要 COM，所以旧路径"能响"掩盖了这个坑。
    返回 True 表示本线程负责配套 CoUninitialize。
    """
    if os.name != "nt":
        return False
    global _OLE32
    try:
        import ctypes
        if _OLE32 is None:
            _OLE32 = ctypes.windll.ole32
        hr = _OLE32.CoInitializeEx(None, 0)  # COINIT_MULTITHREADED
    except Exception:
        return False
    return hr in (0, 1)  # S_OK / S_FALSE


def _com_end():
    try:
        if _OLE32 is not None:
            _OLE32.CoUninitialize()
    except Exception:
        pass


class Playback:
    def __init__(self, text, device, on_mouth, audible, synthesize_fn=None,
                 autostart=True, cleanup_path=None):
        self._cancel = threading.Event()
        self.done = threading.Event()
        self._lock = threading.RLock()
        self._stream = None
        self._cleanup_path = cleanup_path
        self._on_mouth = on_mouth or (lambda value: None)
        # Capture the callable before the thread starts; never mutate a global
        # provider from another thread/controller.
        self._synthesize = synthesize_fn or synthesize
        self.mouth = 0.0
        self.played_frames = 0
        self.sample_rate = 0
        self.error = None
        self.abort_error = None
        self.close_error = None
        self.cleanup_error = None
        self.shutdown_ok = False
        self.state = "starting"
        self.status = []
        self._started = False
        self._start_attempted = threading.Event()
        self._abort_thread = None
        self._abort_done = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(text, device, audible), daemon=True)
        if autostart:
            self.start()

    def start(self):
        """Start exactly once; a stop before start wins and never opens a stream."""
        with self._lock:
            if self._started:
                return False
            self._started = True
            if self._cancel.is_set() or self.done.is_set():
                self.state = "cancelled"
                self.shutdown_ok = True
                self.done.set()
                return False
            self.state = "playing"
            thread = self._thread
        thread.start()
        return True

    def _publish(self, value):
        with self._lock:
            if self._cancel.is_set() and value:
                return
            self.mouth = value
            self._on_mouth(value)

    def _abort_stream(self, stream):
        try:
            # Cancelled before the native start was attempted: nothing to abort.
            if not self._start_attempted.wait(1.0):
                return
            stream.abort()  # discard queued samples, do not drain them
        except Exception as exc:  # noqa: BLE001
            self.abort_error = f"{type(exc).__name__}: {exc}"
        finally:
            self._abort_done.set()

    def _close_stream_bounded(self, stream, timeout: float = 2.0):
        """Close natively without letting the caller block indefinitely."""
        errors: list[str] = []

        def run():
            try:
                stream.close()
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{type(exc).__name__}: {exc}")

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        worker.join(timeout)
        if worker.is_alive():
            self.close_error = "close_timeout"
        elif errors:
            self.close_error = errors[0]

    def stop(self):
        """Request cancellation; never block on a native start/abort."""
        if self.done.is_set():
            return
        self._cancel.set()
        with self._lock:
            self._publish(0.0)
            stream = self._stream
            self.state = "stopping"
            if not self._started and stream is None:
                # Published but not started yet: no thread/stream exists.
                self._started = True
                self.state = "cancelled"
                self.shutdown_ok = True
                self.done.set()
                return
            if stream is None or self._abort_thread is not None:
                return
            self._abort_thread = threading.Thread(
                target=self._abort_stream, args=(stream,), daemon=True)
            abort_thread = self._abort_thread
        abort_thread.start()

    def wait(self, timeout=None):
        return self.done.wait(timeout)

    def _run(self, text, device, audible):
        path = None
        owned_com = _com_begin()  # WASAPI 需要本线程 COM 初始化，否则静音失败
        try:
            import sounddevice as sd
            index = resolve_device(device)  # 先解析：设备缺失要立刻失败，不白等合成
            dev_rate, dev_channels = device_format(index)
            path = self._synthesize(text, self._cancel)
            if self._cancel.is_set():
                return
            samples, channels, rate = _read_wav(path)
            if rate != dev_rate:
                samples = _resample(samples, channels, rate, dev_rate)
                rate = dev_rate
            channels = max(1, min(channels, dev_channels))
            pcm, step, env = _encode(samples, channels, rate)
            self.sample_rate = rate
            total = len(pcm) // (channels * 2)
            submitted = 0
            segments = collections.deque()
            finished = threading.Event()

            def callback(outdata, frames, timing, status):
                nonlocal submitted
                outdata[:] = bytes(len(outdata))
                if self._cancel.is_set():
                    raise sd.CallbackAbort
                if status:
                    self.status.append(str(status))
                count = min(frames, total - submitted)
                if audible:
                    outdata[:count * channels * 2] = pcm[submitted * channels * 2:(submitted + count) * channels * 2]
                # outputBufferDacTime includes the device buffer latency. Never
                # count submitted-but-not-yet-audible frames as played frames.
                segments.append((timing.outputBufferDacTime, submitted, count))
                submitted += count
                if submitted >= total:
                    raise sd.CallbackStop

            stream = sd.RawOutputStream(samplerate=rate, channels=channels, dtype="int16",
                                        device=index,
                                        blocksize=max(1, rate // 100),
                                        latency="low", callback=callback, finished_callback=finished.set)
            with self._lock:
                self._stream = stream
                cancelled_before_start = self._cancel.is_set()
            if cancelled_before_start:
                return
            # A native start can block; never hold the controller lock across it
            # or stop() could not reach its bounded wait.
            self._start_attempted.set()
            stream.start()
            while not self._cancel.is_set():
                now = stream.time  # PortAudio stream clock, not time.time/sleep increments
                while len(segments) > 1 and segments[1][0] <= now:
                    segments.popleft()
                if segments:
                    dac, start, count = segments[0]
                    offset = (now - dac) * rate
                    played = start + max(0, min(count, int(offset)))
                    self.played_frames = max(self.played_frames, played)
                    self._publish(round(env[min(self.played_frames // step, len(env) - 1)], 3)
                                  if 0 <= offset < count and self.played_frames < total and env else 0.0)
                if finished.is_set():
                    self.played_frames = total
                    break
                self._cancel.wait(0.005)  # polling frequency only; never advances progress
        except Exception as exc:
            if not self._cancel.is_set():
                self.error = str(exc)
                warnings.warn("Audio playback failed: " + self.error, RuntimeWarning)
        finally:
            try:
                try:
                    with self._lock:
                        stream = self._stream
                        self._stream = None
                        abort_thread = self._abort_thread
                        self._publish(0.0)
                    if stream is not None:
                        # Never close the same stream while an abort worker is
                        # still inside the native call; a timeout is UNKNOWN.
                        if abort_thread is not None and not self._abort_done.wait(2.0):
                            self.close_error = "abort_pending_timeout"
                        else:
                            self._close_stream_bounded(stream)
                except Exception as exc:  # noqa: BLE001
                    self.close_error = f"{type(exc).__name__}: {exc}"
                # Independent cleanup: close failure must not skip WAV removal.
                for owned in (path, self._cleanup_path):
                    if owned is None:
                        continue
                    try:
                        Path(owned).unlink(missing_ok=True)
                    except Exception as exc:  # noqa: BLE001
                        self.cleanup_error = f"{type(exc).__name__}: {exc}"
                self.shutdown_ok = (not self.abort_error and not self.close_error
                                    and not self.cleanup_error)
                if self.shutdown_ok:
                    if self.error:
                        self.state = "failed"
                    else:
                        self.state = "cancelled" if self._cancel.is_set() else "completed"
                else:
                    self.state = "shutdown_unknown"
            finally:
                if owned_com:
                    _com_end()
                self.done.set()


def play(text, device=None, *, on_mouth=None, audible=True, synthesize_fn=None,
         autostart=True, cleanup_path=None):
    """Return a playback handle; optional synth is captured before thread start."""
    return Playback(text, device, on_mouth, audible, synthesize_fn=synthesize_fn,
                    autostart=autostart, cleanup_path=cleanup_path)


if __name__ == "__main__":
    import json
    print(json.dumps(list_output_devices(), ensure_ascii=False, indent=2))
