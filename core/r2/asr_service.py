#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""常驻 ASR 服务：Runtime 启动时预热模型，通话来临时 already ready。

为什么需要（2026-11-01 真实通话）：
    conversation_loop 每通电话新起进程再加载 faster-whisper，实测 13–18s；
    对方在这段时间说的话会被延迟/丢弃。把模型放到常驻进程，状态写入
    `logs/asr_service.json`（initializing/ready/failed），conversation_loop 只发 PCM。

协议（127.0.0.1:3010）：
    客户端 -> 4B big-endian JSON 长度 + JSON {"rate":16000,"len":N} + N 字节 int16 LE
    服务端 -> 4B big-endian JSON 长度 + JSON {"ok":true,"text":...,"quality":{...}}

用法：
    python r2\\asr_service.py --port 3010
    python r2\\asr_service.py --status
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import socketserver
import struct
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

R2_DIR = Path(__file__).resolve().parent
ROOT = R2_DIR.parent
LOGS = ROOT / "logs"
STATE = LOGS / "asr_service.json"

_TR = None
_LOCK = threading.Lock()
_STATE_EXTRA: dict = {}
#: 有界请求队列：handler 只入队并等待，不再各自抢模型锁。
_REQUEST_Q: queue.Queue = queue.Queue(maxsize=8)
_INFLIGHT_LOCK = threading.Lock()
_INFLIGHT: dict = {}
_STUCK_S = 30.0
QUEUE_STATS = LOGS / "asr_queue_stats.json"


def _queue_snapshot() -> dict:
    with _INFLIGHT_LOCK:
        inflight = dict(_INFLIGHT)
    return {"queue_depth": _REQUEST_Q.qsize(),
            "inflight": inflight or None,
            "maxsize": getattr(_REQUEST_Q, "maxsize", None),
            "at": time.time()}


def _write_queue_stats() -> None:
    try:
        QUEUE_STATS.parent.mkdir(parents=True, exist_ok=True)
        QUEUE_STATS.write_text(json.dumps(_queue_snapshot(), ensure_ascii=False),
                               encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def write_state(state: str, **detail) -> None:
    LOGS.mkdir(parents=True, exist_ok=True)
    payload = {"state": state, "pid": os.getpid(),
               "at": datetime.now().isoformat(timespec="seconds"), **detail}
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE)


def read_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _recv_exact(sock, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("client closed")
        buf.extend(chunk)
    return bytes(buf)


def _worker_loop() -> None:
    """单模型 worker：串行消费有界队列，但不再让 handler 线程堆积。"""
    global _TR
    while True:
        item = _REQUEST_Q.get()
        if item is None:
            return
        pcm, rate, slot = item
        t0 = time.time()
        if slot.get("abandoned") or (slot.get("deadline") and time.time() > float(slot["deadline"])):
            # The client already timed out and closed its socket.  Do not burn
            # the single worker on stale speech; mark it and move on.
            slot["error"] = "expired"
            slot["asr_s"] = 0.0
            slot["done"] = True
            slot["event"].set()
            print("[ASR] expired before worker; skipped", flush=True)
            continue
        with _INFLIGHT_LOCK:
            _INFLIGHT.clear()
            _INFLIGHT.update({"started": round(t0, 3), "audio_s": round(len(pcm) / 2 / rate, 2)})
        _write_queue_stats()
        try:
            with _LOCK:
                text, quality = _TR.transcribe_detail(pcm, rate)
            slot["text"], slot["quality"] = text, quality
        except Exception as exc:  # noqa: BLE001
            slot["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            slot["asr_s"] = round(time.time() - t0, 2)
            slot["done"] = True
            with _INFLIGHT_LOCK:
                _INFLIGHT.clear()
            slot["event"].set()
            _write_queue_stats()
            print(f"[ASR] worker audio={slot.get('audio_s', 0):.1f}s "
                  f"asr_s={slot['asr_s']:.2f} ok={slot.get('error') is None}", flush=True)


def _stuck_monitor(stuck_s: float) -> None:
    """worker 卡死时让进程自杀，由 watchdog 重新拉起，避免永久堵队列。"""
    while True:
        time.sleep(2.0)
        with _INFLIGHT_LOCK:
            inflight = dict(_INFLIGHT)
        started = float(inflight.get("started") or 0.0)
        if started and time.time() - started > stuck_s:
            msg = f"ASR worker stuck >{stuck_s:.0f}s; exiting for watchdog restart"
            print(f"[ASR] {msg}", flush=True)
            write_state("failed", error=msg, queue_depth=_REQUEST_Q.qsize())
            os._exit(42)


def start_worker(maxsize: int = 8, stuck_s: float = 30.0) -> None:
    global _REQUEST_Q, _STUCK_S
    _STUCK_S = float(stuck_s)
    _REQUEST_Q = queue.Queue(maxsize=max(1, int(maxsize)))
    threading.Thread(target=_worker_loop, name="asr-worker", daemon=True).start()
    threading.Thread(target=_stuck_monitor, args=(_STUCK_S,), name="asr-stuck-monitor",
                     daemon=True).start()


class Handler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        sock = self.request
        sock.settimeout(60)
        try:
            hlen = struct.unpack(">I", _recv_exact(sock, 4))[0]
            if hlen <= 0 or hlen > 65536:
                return
            req = json.loads(_recv_exact(sock, hlen).decode("utf-8"))
            n = int(req.get("len") or 0)
            rate = int(req.get("rate") or 16000)
            if n <= 0 or n > rate * 2 * 60:
                resp = {"ok": False, "error": "bad_len"}
            else:
                pcm = _recv_exact(sock, n)
                import numpy as np
                a = np.frombuffer(pcm, dtype=np.int16)
                slot = {"event": threading.Event(), "done": False,
                        "text": "", "quality": {}, "error": None, "asr_s": None,
                        "abandoned": False,
                        "deadline": time.time() + max(1.0, min(25.0, float(req.get("timeout") or 12.0))),
                        "audio_s": round(n / 2 / rate, 2)}
                enq = time.time()
                try:
                    _REQUEST_Q.put_nowait((a, rate, slot))
                except queue.Full:
                    resp = {"ok": False, "error": "busy",
                            "queue_depth": _REQUEST_Q.qsize(),
                            "maxsize": getattr(_REQUEST_Q, "maxsize", None)}
                else:
                    wait_s = max(0.5, min(25.0, float(slot["deadline"]) - time.time() + 0.5))
                    slot["event"].wait(timeout=wait_s)
                    queue_s = round(time.time() - enq - float(slot.get("asr_s") or 0), 2)
                    if not slot["done"]:
                        # Tell the worker this slot is abandoned so it can skip
                        # it instead of transcribing stale audio after the
                        # client has already given up and moved on.
                        slot["abandoned"] = True
                        resp = {"ok": False, "error": "timeout",
                                "queue_s": queue_s, "queue_depth": _REQUEST_Q.qsize()}
                    elif slot["error"]:
                        resp = {"ok": False, "error": slot["error"],
                                "queue_s": queue_s}
                    else:
                        resp = {"ok": True, "text": slot["text"],
                                "quality": slot["quality"], "asr_s": slot["asr_s"],
                                "queue_s": max(0.0, queue_s), "audio_s": slot["audio_s"],
                                "queue_depth": _REQUEST_Q.qsize()}
                _write_queue_stats()
                print(f"[ASR] req audio={slot.get('audio_s', 0):.1f}s "
                      f"queue={resp.get('queue_s', 0)}s asr={resp.get('asr_s')} "
                      f"ok={resp.get('ok')}", flush=True)
            raw = json.dumps(resp, ensure_ascii=False).encode("utf-8")
            sock.sendall(struct.pack(">I", len(raw)) + raw)
        except Exception as exc:  # noqa: BLE001
            try:
                raw = json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                                 ensure_ascii=False).encode("utf-8")
                sock.sendall(struct.pack(">I", len(raw)) + raw)
            except Exception:  # noqa: BLE001
                pass


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def warmup(model: str, device: str, compute_type: str, prompt: str | None,
           cpu_threads: int = 8) -> None:
    global _TR
    t0 = time.time()
    write_state("initializing", model=model, device=device, compute_type=compute_type,
                cpu_threads=cpu_threads)
    try:
        from video_runtime.asr_in import WhisperTranscriber
        from conversation_loop import VadTranscriber
        _TR = VadTranscriber(WhisperTranscriber(model_size=model, device=device,
                                                compute_type=compute_type,
                                                cpu_threads=cpu_threads), prompt=prompt)
        import numpy as np
        t1 = time.time()
        text, quality = _TR.transcribe_detail(np.zeros(16000, dtype=np.int16), 16000)
        write_state("ready", model=model, device=device, compute_type=compute_type,
                    cpu_threads=cpu_threads,
                    load_s=round(t1 - t0, 2), warm_s=round(time.time() - t1, 2),
                    total_s=round(time.time() - t0, 2))
        print(f"[ASR] ready model={model} threads={cpu_threads} "
              f"load={t1-t0:.1f}s warm={time.time()-t1:.1f}s", flush=True)
    except Exception as exc:  # noqa: BLE001
        write_state("failed", error=f"{type(exc).__name__}: {exc}")
        print(f"[ASR] failed: {type(exc).__name__}: {exc}", flush=True)
        raise


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=3010)
    ap.add_argument("--model", default="base",
                    help="base 是延迟/质量的折中；small 更准但 CPU 上明显更慢")
    ap.add_argument("--device", default="cpu")
    ap.add_argument("--compute-type", default="int8")
    ap.add_argument("--cpu-threads", type=int, default=8,
                    help="CTranslate2 CPU 线程数；16 核机器上 8 通常比默认 4 快约 1.5x")
    ap.add_argument("--queue-max", type=int, default=8,
                    help="有界请求队列；满时返回 busy，由调用方背压/重试")
    ap.add_argument("--stuck-timeout", type=float, default=30.0,
                    help="worker 超过这么久没有完成就退出，让 watchdog 重启")
    ap.add_argument("--prompt", default="大肥鱼,直播间,你好呀")
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args(argv)
    if args.status:
        state = read_state()
        port = int(state.get("port") or args.port)
        live = False
        try:
            import socket
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                live = True
        except OSError:
            live = False
        state["live"] = live
        state["status_source"] = ("persisted+loopback_probe" if live
                                  else "persisted_state_only_NOT_LIVE")
        print(json.dumps(state, ensure_ascii=False, indent=2))
        return 0 if live else 2
    warmup(args.model, args.device, args.compute_type, args.prompt, args.cpu_threads)
    start_worker(args.queue_max, args.stuck_timeout)
    with Server(("127.0.0.1", args.port), Handler) as srv:
        prev = read_state()
        extra = {k: v for k, v in prev.items() if k not in ("state", "pid", "at")}
        write_state("ready", port=args.port, queue_max=args.queue_max, **extra)
        print(f"[ASR] listening 127.0.0.1:{args.port}", flush=True)
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    # Script mode only (FIX-SYSPATH-1): importing this module must not mutate
    # sys.path.  Direct `python core/r2/asr_service.py` keeps working.
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(R2_DIR))
    sys.exit(main())
