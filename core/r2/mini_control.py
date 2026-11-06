#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""裁剪版控制面：只负责驱动 Live2D 的表情/口型（**不含**旧接听链、不点 QQ）。

为什么要单独写一个：
    旧 `video_runtime/control_server.py` 在 import 期就会拉入旧接听链
    （answer_watcher / call_answer），带点击 QQ 的能力。R2 只需要"驱动渲染"这一层，
    所以这里重写一个最小实现，契约与旧页面完全兼容：

        GET  /cmd?since=N   -> {"version": int, "mouth": 0..1, "expression": "名字"}
        POST /set           -> {"expression"|"emotion"|"mouth"|"hold"}
        POST /speak         -> {"text": "...", "seconds": n}   # 无 TTS 时按时长动嘴
        POST /speak/stop
        GET  /health  /state
        POST /page/report   -> 页面自报（只记录，不做判断）

用法（在 QQAI_video_runtime 根目录）：
    python r2\\mini_control.py --port 8770
    python r2\\mini_control.py --selftest      # 离线自测：起服务→打接口→校验
"""
from __future__ import annotations

import argparse
import hmac
import json
import math
import os
import secrets
import shutil
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

R2_ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = R2_ROOT / "logs"

# Cross-origin policy (2026-11-06): Core serves no web UI, so **no** web origin
# is trusted.  Requests without an Origin header (local CLI / health probes)
# remain allowed; every request that carries an Origin is denied unless the
# origin is explicitly listed here.  Never add a wildcard or a port owned by
# another application.
ALLOWED_ORIGINS: set[str] = set()
CONTROL_TOKEN = ""
MAX_BODY = 4096


def token_path() -> Path:
    return LOG_DIR / "control_token"


def read_token() -> str | None:
    """Read the per-run control token; never create it from a client."""
    if CONTROL_TOKEN:
        return CONTROL_TOKEN
    env = (os.environ.get("QQAI_CONTROL_TOKEN") or "").strip()
    if env:
        return env
    try:
        value = token_path().read_text(encoding="utf-8").strip()
        return value or None
    except OSError:
        return None


def rotate_token() -> str:
    """Create a fresh token for this owned server instance."""
    global CONTROL_TOKEN
    CONTROL_TOKEN = secrets.token_urlsafe(32)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    token_path().write_text(CONTROL_TOKEN, encoding="utf-8")
    try:
        os.chmod(token_path(), 0o600)
    except OSError:
        pass
    return CONTROL_TOKEN

# 页面使用的表情目录（web/Exp3 由旧静态服务提供；名字要与 .exp3.json 对应）
DEFAULT_EXPRESSION = ""

# 情绪 → 表情名（沿用旧项目已验证的映射；旧名可在 web/Exp3 下找到）
# 2026-10-31 补 serious / idle：AvatarRuntime 会用它们表示"认真交流"与"回默认"。
# 缺这两个键时 `.get(emotion, emotion)` 会把情绪名本身当成表情名，
# 页面拿到不存在的表情名就什么都不变（实测 expression 变成了字面量 "serious"/"idle"）。
EMOTION_TO_EXPRESSION = {
    "happy": "脸红",
    "shy": "脸红",
    "confused": "流冷汗",
    "thinking": "加载",
    "surprised": "＞＜",
    "sad": "流泪",
    "serious": "",
    "idle": "",
    "neutral": "",
    # R2.1 Listening Feedback：复用现有表情，不新增页面素材。
    "listening": "",
    "acknowledging": "",
    "asr_thinking": "加载",
    "asr_error": "流冷汗",
}


class ControlState:
    """全部状态集中在这里，加锁；version 变化页面才会应用。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.version = 0
        self.mouth = 0.0
        self.expression = DEFAULT_EXPRESSION
        self.motion = ""
        self.pose = {"x": 0.0, "y": 0.0, "z": 0.0}
        self.page_report = {}
        self.updated = time.time()
        self._speak_thread: threading.Thread | None = None
        self._speak_stop = threading.Event()
        self.log: list[dict] = []

    def bump(self) -> None:
        self.version += 1
        self.updated = time.time()

    def snapshot(self) -> dict:
        with self._lock:
            return {"version": self.version, "mouth": round(self.mouth, 4),
                    "expression": self.expression, "motion": self.motion, "pose": self.pose}

    def set(self, *, expression: str | None = None, emotion: str | None = None,
            mouth: float | None = None, motion: str | None = None,
            pose: dict | None = None) -> dict:
        with self._lock:
            if motion is not None:
                self.motion = motion
            if emotion is not None:
                self.expression = EMOTION_TO_EXPRESSION.get(emotion.lower(), emotion)
            if expression is not None:
                self.expression = expression
            if mouth is not None:
                try:
                    value = float(mouth)
                    if math.isfinite(value):
                        self.mouth = max(0.0, min(1.0, value))
                except (TypeError, ValueError, OverflowError):
                    pass
            if isinstance(pose, dict):
                for k in ("x", "y", "z"):
                    if k in pose:
                        try:
                            value = float(pose[k])
                            if math.isfinite(value):
                                self.pose[k] = value
                        except (TypeError, ValueError, OverflowError):
                            pass
            self.version += 1
            self.updated = time.time()
            snap = {"version": self.version, "mouth": round(self.mouth, 4),
                    "expression": self.expression, "motion": self.motion,
                    "pose": dict(self.pose)}
        self.log.append({"at": time.time(), "kind": "set", **snap})
        return snap

    def speak(self, seconds: float) -> None:
        """没有接 TTS 时，用包络按时长驱动嘴部；有 TTS 后把这里换成真实包络。"""
        self._speak_stop.set()
        if self._speak_thread and self._speak_thread.is_alive():
            self._speak_thread.join(timeout=1.0)
        self._speak_stop = threading.Event()
        stop = self._speak_stop

        def run() -> None:
            import math
            t0 = time.time()
            while not stop.is_set():
                t = time.time() - t0
                if t >= seconds:
                    break
                # 5Hz 的包络，模拟音节起伏
                v = 0.55 + 0.35 * math.sin(2 * math.pi * 5.0 * t)
                self.set(mouth=max(0.0, min(1.0, v)))
                time.sleep(0.04)
            self.set(mouth=0.0)
            self.log.append({"at": time.time(), "kind": "speak_end"})

        self._speak_thread = threading.Thread(target=run, daemon=True)
        self._speak_thread.start()

    def stop_speak(self) -> None:
        self._speak_stop.set()
        self.set(mouth=0.0)


STATE = ControlState()

# TTS 相关配置（main 里按参数设置）
TTS_ENABLED = True
TTS_DEVICE = "CABLE Input"     # 播到 VB-Cable 播放侧 → QQ 麦克风（CABLE Output）能"听到"
_TTS_LOCK = threading.RLock()
_TTS_PLAYBACK = None
_TTS_SESSION = 0
_TTS_STATE = "idle"
_TTS_ERROR: str | None = None


def qqai_tts_path(text: str) -> str | None:
    """用 **QQAI 已配置的语音接口**（doubao seed-tts-2.0）合成，返回 WAV 路径。

    只读导入生产模块，不修改 QQAI；失败返回 None，由调用方回退本地 SAPI。
    """
    try:
        import os
        import sys
        from pathlib import Path as _P

        from runtime_config import resolve_bot_config
        bot_dir = resolve_bot_config()["bot_dir"]
        if not bot_dir.is_dir():
            return None
        if str(bot_dir) not in sys.path:
            sys.path.insert(0, str(bot_dir))
        cwd = os.getcwd()
        try:
            os.chdir(str(bot_dir))          # voice_out 用相对路径找配置/缓存
            import voice_out                # noqa: PLC0415
            path = voice_out.synthesize(text)
        finally:
            os.chdir(cwd)
        if path and os.path.exists(path):
            return str(path)
        return None
    except Exception as exc:  # noqa: BLE001
        STATE.log.append({"at": time.time(), "kind": "qqai_tts_error", "error": str(exc)})
        return None


def tts_status() -> dict:
    with _TTS_LOCK:
        handle = _TTS_PLAYBACK
        return {"state": _TTS_STATE, "session": _TTS_SESSION,
                "error": _TTS_ERROR,
                "shutdown_ok": bool(handle is not None
                                    and getattr(handle, "shutdown_ok", False))}


def stop_tts_playback(timeout: float = 2.0) -> dict:
    """Stop the actual playback; the controller lock is never held across wait."""
    global _TTS_PLAYBACK, _TTS_SESSION, _TTS_STATE, _TTS_ERROR
    with _TTS_LOCK:
        handle = _TTS_PLAYBACK
        session = _TTS_SESSION
        if handle is None:
            if _TTS_STATE == "shutdown_unknown":
                return {"ok": False, "stopped": False, "session": session,
                        "state": _TTS_STATE, "error": _TTS_ERROR or "shutdown_unknown"}
            # A request can still be between "synthesizing" and publishing its
            # handle.  Bump the session so it stops its handle instead of
            # starting playback after this stop returned.
            if _TTS_STATE == "synthesizing":
                _TTS_SESSION += 1
                _TTS_STATE = "stopped"
                return {"ok": True, "stopped": True, "session": session,
                        "superseded": True}
            _TTS_STATE = "stopped"
            return {"ok": True, "stopped": True, "session": session}
        _TTS_STATE = "stopping"
    err = None
    try:
        handle.stop()
    except Exception as exc:  # noqa: BLE001
        err = f"stop:{type(exc).__name__}:{exc}"
    done = False
    try:
        done = bool(handle.wait(timeout))
    except Exception as exc:  # noqa: BLE001
        err = (err + "; " if err else "") + f"wait:{type(exc).__name__}:{exc}"
    with _TTS_LOCK:
        shutdown_ok = bool(getattr(handle, "shutdown_ok", False))
        actual = str(getattr(handle, "state", "shutdown_unknown"))
        if _TTS_PLAYBACK is handle:
            if done and shutdown_ok:
                _TTS_PLAYBACK = None
                _TTS_STATE = "stopped" if actual in ("cancelled", "completed") else actual
                _TTS_ERROR = getattr(handle, "error", None) or err
            else:
                # Ownership remains busy until shutdown is confirmed.
                _TTS_STATE = "stopping" if not done else "shutdown_unknown"
                _TTS_ERROR = err or getattr(handle, "error", None) or "shutdown_unknown"
        return {"ok": bool(done and shutdown_ok), "stopped": bool(done and shutdown_ok),
                "session": session, "state": _TTS_STATE, "error": _TTS_ERROR}


def _monitor_tts(handle, session: int) -> None:
    global _TTS_PLAYBACK, _TTS_STATE, _TTS_ERROR
    try:
        handle.wait()
    finally:
        with _TTS_LOCK:
            if _TTS_SESSION != session or _TTS_PLAYBACK is not handle:
                return
            _TTS_STATE = str(getattr(handle, "state", "shutdown_unknown"))
            _TTS_ERROR = getattr(handle, "error", None)
            if getattr(handle, "shutdown_ok", False):
                _TTS_PLAYBACK = None


def speak_with_tts(text: str) -> dict:
    """Start one request-owned playback; no global synthesizer mutation/restore."""
    global _TTS_PLAYBACK, _TTS_SESSION, _TTS_STATE, _TTS_ERROR
    if not TTS_ENABLED:
        return {"ok": False, "error": "TTS disabled by server policy"}
    with _TTS_LOCK:
        # A retained handle means an earlier shutdown is still unconfirmed;
        # no new playback may start until it is resolved.
        if _TTS_PLAYBACK is not None or _TTS_STATE in (
                "synthesizing", "playing", "stopping", "shutdown_unknown"):
            return {"ok": False, "busy": True, "error": "playback busy"}
        _TTS_SESSION += 1
        session = _TTS_SESSION
        _TTS_STATE = "synthesizing"
        _TTS_ERROR = None
    temp_path = None
    handle = None
    try:
        from video_runtime import audio_out as ao

        wav = qqai_tts_path(text)
        synth_fn = None
        if wav:
            temp_dir = LOG_DIR / "tts_tmp"
            temp_dir.mkdir(parents=True, exist_ok=True)
            fd, temp_name = tempfile.mkstemp(prefix="tts_", suffix=".wav", dir=temp_dir)
            os.close(fd)
            temp_path = _P(temp_name)
            shutil.copyfile(wav, temp_path)
            synth_fn = lambda _text, _canceled: str(temp_path)
        handle = ao.play(text, device=TTS_DEVICE,
                         on_mouth=lambda v: STATE.set(mouth=float(v)),
                         audible=True, synthesize_fn=synth_fn,
                         autostart=False,
                         cleanup_path=str(temp_path) if temp_path is not None else None)
        with _TTS_LOCK:
            superseded = _TTS_SESSION != session
            if not superseded:
                # Publish ownership before any thread/synthesis/stream exists;
                # a concurrent stop can now cancel this exact request.
                _TTS_PLAYBACK = handle
                _TTS_STATE = "starting"
        if superseded:
            handle.stop()  # pre-start cancellation; bounded and no stream
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass
            return {"ok": False, "busy": True, "error": "superseded"}
        started = handle.start()
        if not started:
            # stop() won before start(); no stream was opened.
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass
        else:
            with _TTS_LOCK:
                if (_TTS_PLAYBACK is handle and _TTS_SESSION == session
                        and _TTS_STATE == "starting"):
                    _TTS_STATE = "playing"
        threading.Thread(target=_monitor_tts, args=(handle, session),
                         daemon=True).start()
        return {"ok": True, "mode": "tts", "device": TTS_DEVICE,
                "session": session, "engine": "qqai" if wav else "local_sapi"}
    except Exception as exc:  # noqa: BLE001
        if handle is not None:
            try:
                handle.stop()
                handle.wait(1.0)
            except Exception:  # noqa: BLE001
                pass
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
        with _TTS_LOCK:
            shutdown_ok = bool(handle is not None
                               and getattr(handle, "shutdown_ok", False))
            if _TTS_SESSION == session:
                if handle is not None and not shutdown_ok:
                    _TTS_STATE = "shutdown_unknown"
                else:
                    _TTS_STATE = "failed"
                _TTS_ERROR = f"{type(exc).__name__}: {exc}"
            if _TTS_PLAYBACK is handle and (handle is None or shutdown_ok):
                _TTS_PLAYBACK = None
        return {"ok": False, "error": _TTS_ERROR, "session": session}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # 静音默认访问日志
        pass

    def _origin_allowed(self) -> bool:
        origin = self.headers.get("Origin")
        return not origin or origin in ALLOWED_ORIGINS

    def _host_allowed(self) -> bool:
        host = (self.headers.get("Host") or "").strip().lower()
        port = int(self.server.server_address[1])
        return host in {f"127.0.0.1:{port}", f"localhost:{port}"}

    def _token_allowed(self) -> bool:
        token = read_token()
        supplied = self.headers.get("X-ADI-Control-Token") or ""
        if not token or not supplied:
            return False
        return hmac.compare_digest(str(token), str(supplied))

    def _path(self) -> str:
        return urllib.parse.urlsplit(self.path).path

    def _cors(self) -> None:
        origin = self.headers.get("Origin")
        if origin in ALLOWED_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Headers",
                             "Content-Type, X-ADI-Control-Token")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def _json(self, obj, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):  # noqa: N802
        if not self._host_allowed() or not self._origin_allowed():
            return self._json({"error": "host/origin not allowed"}, 403)
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):  # noqa: N802
        if not self._host_allowed() or not self._origin_allowed():
            return self._json({"error": "host/origin not allowed"}, 403)
        path = self._path()
        if path == "/cmd":
            return self._json(STATE.snapshot())
        if path == "/health":
            return self._json({"ok": True, "port": self.server.server_address[1],
                               "version": STATE.version})
        if path == "/page/report":
            return self._json({"ok": True, "report": STATE.page_report})
        if path == "/state":
            return self._json({**STATE.snapshot(), "page_report": STATE.page_report,
                               "tts": tts_status()})
        return self._json({"error": "not found"}, 404)

    def do_POST(self):  # noqa: N802
        if not self._host_allowed() or not self._origin_allowed():
            return self._json({"error": "host/origin not allowed"}, 403)
        if not self._token_allowed():
            return self._json({"error": "unauthorized"}, 401)
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            return self._json({"error": "bad content-length"}, 400)
        if length < 0 or length > MAX_BODY:
            return self._json({"error": "body too large"}, 413)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            return self._json({"error": "bad json"}, 400)
        if not isinstance(data, dict):
            return self._json({"error": "json object required"}, 400)

        path = self._path()
        if path == "/motion":
            name = data.get("name")
            if name is not None and not isinstance(name, str):
                return self._json({"error": "bad motion"}, 400)
            snap = STATE.set(motion=(name or "")[:80])
            return self._json({"ok": True, **snap})
        if path == "/set":
            expression = data.get("expression")
            emotion = data.get("emotion")
            motion = data.get("motion")
            for value in (expression, emotion, motion):
                if value is not None and not isinstance(value, str):
                    return self._json({"error": "bad string field"}, 400)
            mouth = data.get("mouth")
            if mouth is not None:
                if isinstance(mouth, bool) or not isinstance(mouth, (int, float)):
                    return self._json({"error": "bad mouth"}, 400)
                try:
                    if not math.isfinite(float(mouth)):
                        return self._json({"error": "bad mouth"}, 400)
                except (ValueError, OverflowError):
                    return self._json({"error": "bad mouth"}, 400)
            pose = data.get("pose")
            if pose is not None and not isinstance(pose, dict):
                return self._json({"error": "bad pose"}, 400)
            if isinstance(pose, dict):
                for key in ("x", "y", "z"):
                    if key not in pose:
                        continue
                    value = pose[key]
                    if isinstance(value, bool) or not isinstance(value, (int, float)):
                        return self._json({"error": "bad pose"}, 400)
                    try:
                        if not math.isfinite(float(value)):
                            return self._json({"error": "bad pose"}, 400)
                    except (ValueError, OverflowError):
                        return self._json({"error": "bad pose"}, 400)
            snap = STATE.set(expression=expression, pose=pose, emotion=emotion,
                             mouth=None if mouth is None else float(mouth), motion=motion)
            return self._json({"ok": True, **snap})
        if path == "/speak/stop":
            STATE.stop_speak()
            result = stop_tts_playback(2.0)
            return self._json(result, 200 if result.get("ok") else 409)
        if path == "/speak":
            text = data.get("text", "")
            if not isinstance(text, str):
                return self._json({"error": "text must be a string"}, 400)
            text = text[:2000]
            # --no-tts is a server policy, not a client preference.
            requested_tts = data.get("tts", True)
            if not isinstance(requested_tts, bool):
                return self._json({"error": "bad tts flag"}, 400)
            use_tts = bool(TTS_ENABLED) and requested_tts and bool(text.strip())
            if use_tts:
                res = speak_with_tts(text)
                return self._json({**res, "text": text[:80]},
                                  200 if not res.get("busy") else 409)
            raw_seconds = data.get("seconds")
            if raw_seconds is None:
                seconds = max(0.8, min(8.0, len(text) * 0.12))
            else:
                if isinstance(raw_seconds, bool) or not isinstance(raw_seconds, (int, float)):
                    return self._json({"error": "bad seconds"}, 400)
                try:
                    seconds = float(raw_seconds)
                except (ValueError, OverflowError):
                    return self._json({"error": "bad seconds"}, 400)
                if not math.isfinite(seconds) or seconds <= 0 or seconds > 8.0:
                    return self._json({"error": "bad seconds"}, 400)
            STATE.speak(seconds)
            return self._json({"ok": True, "mode": "envelope",
                               "seconds": seconds, "tts": False})
        if path == "/reset":
            STATE.stop_speak()
            stop_tts_playback(2.0)
            # 2026-10-31 20:5x 修：原来只清 expression 与 mouth，**没清 motion**，
            # 导致"普通待机/急停"回不到默认姿态（实测 /reset 后 state.motion 仍是上个动作）。
            return self._json({"ok": True, "tts": tts_status(),
                               **STATE.set(expression="", mouth=0.0, motion="")})
        if path == "/page/report":
            STATE.page_report = data
            return self._json({"ok": True})
        return self._json({"error": "not found"}, 404)


def serve(port: int) -> ThreadingHTTPServer:
    # Bind first: a failed duplicate start must not rotate the token used by
    # the already-owned server.
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    rotate_token()
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def selftest(port: int) -> int:
    checks: list[dict] = []

    def check(name: str, ok: bool, detail: dict) -> None:
        checks.append({"name": name, "pass": bool(ok), **detail})

    srv = serve(port)
    base = f"http://127.0.0.1:{port}"
    try:
        time.sleep(0.6)

        def get(path: str):
            with urllib.request.urlopen(base + path, timeout=3) as r:
                return json.loads(r.read().decode())

        def post(path: str, obj: dict):
            req = urllib.request.Request(
                base + path, data=json.dumps(obj).encode(),
                headers={"Content-Type": "application/json",
                         "X-ADI-Control-Token": CONTROL_TOKEN})
            with urllib.request.urlopen(req, timeout=3) as r:
                return json.loads(r.read().decode())

        st0 = get("/cmd?since=0")
        check("初始版本=0", st0["version"] == 0, st0)

        st1 = post("/set", {"emotion": "happy"})
        check("情绪映射到表情", st1["expression"] == "脸红" and st1["version"] == 1, st1)

        st2 = post("/set", {"mouth": 0.8})
        check("嘴部设置被夹紧", abs(st2["mouth"] - 0.8) < 1e-6, st2)

        st3 = post("/set", {"mouth": 5})
        check("超出范围被夹到 1.0", abs(st3["mouth"] - 1.0) < 1e-6, st3)

        post("/speak", {"seconds": 0.4})
        time.sleep(0.15)
        mid = get("/cmd?since=0")
        check("说话中嘴在动", mid["mouth"] > 0.05, mid)
        time.sleep(0.6)
        end = get("/cmd?since=0")
        check("说完归零", end["mouth"] == 0.0, end)

        h = get("/health")
        check("健康检查", h["ok"] is True, h)
    finally:
        srv.shutdown()

    passed = sum(1 for c in checks if c["pass"])
    payload = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "checks": checks,
               "summary": {"passed": passed, "total": len(checks)},
               "verdict": "PASS" if passed == len(checks) else "FAIL"}
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    out = LOG_DIR / "mini_control_selftest.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[mini_control 自测] {passed}/{len(checks)} {payload['verdict']}")
    for c in checks:
        print(f"  [{'OK ' if c['pass'] else 'FAIL'}] {c['name']}")
    print(f"证据：{out}")
    return 0 if passed == len(checks) else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="裁剪版 Live2D 控制面（表情/口型）")
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--tts-device", default="CABLE Input", help="TTS 播放到哪个输出设备")
    ap.add_argument("--no-tts", action="store_true", help="关闭真实 TTS，只用合成包络动嘴")
    args = ap.parse_args(argv)

    global TTS_ENABLED, TTS_DEVICE
    TTS_ENABLED = not args.no_tts
    TTS_DEVICE = args.tts_device
    # 用户 2026-11-01 硬约束：禁止扬声器链路，本机不得发出任何声音。
    # TTS 只能播到虚拟线（CABLE）；否则直接关闭 TTS，绝不回退到默认扬声器。
    if TTS_ENABLED and "cable" not in TTS_DEVICE.lower():
        print(f"[拒绝] TTS 设备 {TTS_DEVICE!r} 不是虚拟线（CABLE）；"
              f"按“禁止扬声器链路”要求已关闭 TTS")
        TTS_ENABLED = False

    if args.selftest:
        return selftest(8799)

    srv = serve(args.port)
    print(f"[mini_control] 已启动 http://127.0.0.1:{args.port}  "
          f"（GET /cmd /health /state；POST 需 X-ADI-Control-Token）")
    print(f"[mini_control] control token file: {token_path()}")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        srv.shutdown()
        print("\n[mini_control] 已停止")
    return 0


if __name__ == "__main__":
    # Script mode only (FIX-SYSPATH-1): importing this module must not mutate
    # sys.path.  Direct `python core/r2/mini_control.py` keeps working.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    raise SystemExit(main())


