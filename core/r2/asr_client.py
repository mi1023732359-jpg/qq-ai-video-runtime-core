#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""ASR 服务客户端：发送 int16 PCM，拿回 text/quality。"""
from __future__ import annotations

import json
import socket
import struct
import time


class AsrServiceError(RuntimeError):
    pass


class AsrClient:
    name = "asr-service"

    def __init__(self, host: str = "127.0.0.1", port: int = 3010, timeout: float = 12.0):
        self.host = host
        self.port = int(port)
        self.timeout = float(timeout)
        self.calls = 0

    def available(self) -> bool:
        try:
            s = socket.create_connection((self.host, self.port), timeout=1.5)
            s.close()
            return True
        except OSError:
            return False

    def transcribe_detail(self, pcm, rate: int = 16000):
        import numpy as np
        a = np.asarray(pcm, dtype=np.int16).reshape(-1)
        body = a.tobytes()
        header = json.dumps({"rate": int(rate), "len": len(body),
                             "timeout": float(self.timeout)}).encode("utf-8")
        resp = {}
        for attempt in range(2):
            s = socket.create_connection((self.host, self.port), timeout=5)
            try:
                s.settimeout(self.timeout)
                s.sendall(struct.pack(">I", len(header)) + header + body)
                raw_len = self._recv(s, 4)
                n = struct.unpack(">I", raw_len)[0]
                resp = json.loads(self._recv(s, n).decode("utf-8"))
            finally:
                try:
                    s.close()
                except Exception:  # noqa: BLE001
                    pass
            if resp.get("error") == "busy" and attempt == 0:
                time.sleep(0.15)
                continue
            break
        self.calls += 1
        if not resp.get("ok"):
            raise AsrServiceError(resp.get("error") or "asr service failed")
        quality = dict(resp.get("quality") or {})
        for key in ("asr_s", "queue_s", "audio_s"):
            if resp.get(key) is not None:
                quality[key] = resp.get(key)
        return resp.get("text") or "", quality

    @staticmethod
    def _recv(sock, n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise AsrServiceError("asr service closed")
            buf.extend(chunk)
        return bytes(buf)

    def transcribe(self, pcm, rate: int = 16000) -> str:
        return self.transcribe_detail(pcm, rate)[0]
