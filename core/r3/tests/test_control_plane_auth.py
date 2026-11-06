#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Control-plane authentication/bounds (loopback fixture, no real device)."""
from __future__ import annotations

import http.client
import json
import sys
import tempfile
import threading
import time
import types
import unittest
import wave
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "r2"))
sys.path.insert(0, str(ROOT))

import mini_control  # noqa: E402
from video_runtime import audio_out  # noqa: E402


class ControlPlaneAuthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._old_log = mini_control.LOG_DIR
        cls._td = tempfile.TemporaryDirectory()
        mini_control.LOG_DIR = Path(cls._td.name) / "logs"
        cls.srv = mini_control.serve(0)
        cls.port = int(cls.srv.server_address[1])
        cls.token = mini_control.CONTROL_TOKEN

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        mini_control.LOG_DIR = cls._old_log
        cls._td.cleanup()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        h = {"Content-Type": "application/json"}
        if headers:
            h.update(headers)
        raw = json.dumps(body).encode() if body is not None else None
        conn.request(method, path, body=raw, headers=h)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp.status, data

    def test_get_is_read_only_and_query_is_accepted(self):
        code, data = self.request("GET", "/cmd?since=0")
        self.assertEqual(code, 200)
        self.assertIn("version", json.loads(data))

    def test_mutation_without_token_is_denied(self):
        code, _ = self.request("POST", "/set", {"mouth": 0.3})
        self.assertEqual(code, 401)

    def test_mutation_with_token_succeeds(self):
        code, data = self.request("POST", "/set", {"mouth": 0.3},
                                  {"X-ADI-Control-Token": self.token})
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(data)["mouth"], 0.3)

    def test_foreign_host_is_denied(self):
        code, _ = self.request("GET", "/health", headers={"Host": "evil.test"})
        self.assertEqual(code, 403)
        code, _ = self.request("POST", "/set", {"mouth": 0.1},
                               {"Host": "evil.test", "X-ADI-Control-Token": self.token})
        self.assertEqual(code, 403)

    def test_foreign_origin_is_denied_even_with_token(self):
        code, _ = self.request("POST", "/set", {"mouth": 0.1},
                               {"Origin": "http://evil.test",
                                "X-ADI-Control-Token": self.token})
        self.assertEqual(code, 403)

    def test_unrelated_8765_origin_is_denied_even_with_token(self):
        # 8765 belongs to another local application; Core must not trust it.
        for origin in ("http://127.0.0.1:8765", "http://localhost:8765"):
            code, _ = self.request("POST", "/set", {"mouth": 0.1},
                                   {"Origin": origin,
                                    "X-ADI-Control-Token": self.token})
            self.assertEqual(code, 403, origin)

    def test_no_web_origin_is_trusted_by_default(self):
        self.assertEqual(mini_control.ALLOWED_ORIGINS, set())

    def test_request_without_origin_is_still_allowed(self):
        # Local CLI / health probes send no Origin header; that path stays usable.
        code, _ = self.request("POST", "/set", {"mouth": 0.2},
                               {"X-ADI-Control-Token": self.token})
        self.assertEqual(code, 200)

    def test_prefix_route_is_not_accepted(self):
        code, _ = self.request("POST", "/setevil", {"mouth": 0.1},
                               {"X-ADI-Control-Token": self.token})
        self.assertEqual(code, 404)

    def test_oversized_body_is_rejected_before_parse(self):
        code, _ = self.request("POST", "/set", {"x": "a" * 5000},
                               {"X-ADI-Control-Token": self.token})
        self.assertEqual(code, 413)

    def test_server_policy_disables_tts_override(self):
        old_enabled = mini_control.TTS_ENABLED
        old_speak = mini_control.speak_with_tts
        calls = []
        mini_control.TTS_ENABLED = False
        mini_control.speak_with_tts = lambda text: calls.append(text) or {"ok": True}
        try:
            code, data = self.request("POST", "/speak",
                                      {"text": "hello", "tts": True},
                                      {"X-ADI-Control-Token": self.token})
        finally:
            mini_control.TTS_ENABLED = old_enabled
            mini_control.speak_with_tts = old_speak
        self.assertEqual(code, 200)
        self.assertEqual(calls, [])
        self.assertFalse(json.loads(data)["tts"])

    def test_token_rotation_invalidates_old_token(self):
        old = self.token
        new = mini_control.rotate_token()
        self.assertNotEqual(old, new)
        code, _ = self.request("POST", "/set", {"mouth": 0.2},
                               {"X-ADI-Control-Token": old})
        self.assertEqual(code, 401)
        code, _ = self.request("POST", "/set", {"mouth": 0.2},
                               {"X-ADI-Control-Token": new})
        self.assertEqual(code, 200)

    def test_busy_playback_returns_409(self):
        old = (mini_control._TTS_PLAYBACK, mini_control._TTS_STATE)
        mini_control._TTS_PLAYBACK = object()
        mini_control._TTS_STATE = "playing"
        try:
            code, data = self.request("POST", "/speak", {"text": "hello"},
                                      {"X-ADI-Control-Token": self.token})
        finally:
            mini_control._TTS_PLAYBACK, mini_control._TTS_STATE = old
        self.assertEqual(code, 409)
        self.assertTrue(json.loads(data).get("busy"))


class _CallbackAbort(Exception):
    pass


class _CallbackStop(Exception):
    pass


class FakeStream:
    instances: list["FakeStream"] = []
    complete = True
    abort_error = None
    close_error = None
    abort_gate = None

    def __init__(self, **kwargs):
        self.callback = kwargs["callback"]
        self.finished_callback = kwargs["finished_callback"]
        self.started = threading.Event()
        self.closed = False
        self.aborted = False
        self.time = 0.0
        FakeStream.instances.append(self)

    def start(self):
        self.started.set()
        out = bytearray(2048)
        try:
            for _ in range(5):
                self.callback(out, 512,
                              types.SimpleNamespace(outputBufferDacTime=self.time + 0.01), "")
                self.time += 512 / 22050.0
        except (_CallbackStop, _CallbackAbort):
            pass
        if FakeStream.complete:
            self.finished_callback()

    def abort(self):
        self.aborted = True
        if FakeStream.abort_gate is not None:
            FakeStream.abort_gate.wait(10)
        if FakeStream.abort_error:
            raise FakeStream.abort_error

    def close(self):
        self.closed = True
        if FakeStream.close_error:
            raise FakeStream.close_error


class PlaybackTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.dir = Path(self._td.name)
        self.saved = {}

    def tearDown(self):
        for name, value in self.saved.items():
            setattr(audio_out, name, value)
        sys.modules.pop("sounddevice", None)
        self._td.cleanup()

    def _wav(self) -> Path:
        path = self.dir / "speech.wav"
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(22050)
            w.writeframes(b"\x00\x00" * 2205)
        return path

    def _install(self, stream_cls=FakeStream):
        sd = types.ModuleType("sounddevice")
        sd.CallbackAbort = _CallbackAbort
        sd.CallbackStop = _CallbackStop
        sd.RawOutputStream = stream_cls
        sys.modules["sounddevice"] = sd
        for name, value in (("resolve_device", lambda device: 0),
                            ("device_format", lambda index: (22050, 1)),
                            ("_com_begin", lambda: False),
                            ("_com_end", lambda: None)):
            self.saved[name] = getattr(audio_out, name)
            setattr(audio_out, name, value)
        stream_cls.instances = []
        stream_cls.complete = True
        stream_cls.abort_error = None
        stream_cls.close_error = None
        stream_cls.abort_gate = None

    def test_completed_playback_closes_and_removes_wav(self):
        self._install()
        wav = self._wav()
        p = audio_out.play("hi", device=0, synthesize_fn=lambda t, c: str(wav))
        self.assertTrue(p.wait(10))
        self.assertEqual(p.state, "completed")
        self.assertTrue(p.shutdown_ok)
        self.assertIsNone(p.error)
        self.assertFalse(wav.exists())
        self.assertTrue(FakeStream.instances[0].closed)
        self.assertEqual(p.mouth, 0.0)

    def test_stop_during_synthesis_never_opens_stream(self):
        self._install()
        entered = threading.Event()

        def synth(text, canceled):
            entered.set()
            self.assertTrue(canceled.wait(5))
            raise RuntimeError("canceled")

        p = audio_out.play("hi", device=0, synthesize_fn=synth)
        self.assertTrue(entered.wait(5))
        p.stop()
        self.assertTrue(p.wait(10))
        self.assertEqual(p.state, "cancelled")
        self.assertTrue(p.shutdown_ok)
        self.assertIsNone(p.error)
        self.assertEqual(FakeStream.instances, [])

    def test_abort_failure_is_shutdown_unknown_and_keeps_cleanup(self):
        self._install()
        FakeStream.complete = False
        FakeStream.abort_error = OSError("abort failed")
        wav = self._wav()
        p = audio_out.play("hi", device=0, synthesize_fn=lambda t, c: str(wav))
        deadline = time.time() + 5
        while not FakeStream.instances and time.time() < deadline:
            time.sleep(0.01)
        self.assertTrue(FakeStream.instances)
        self.assertTrue(FakeStream.instances[0].started.wait(5))
        p.stop()
        self.assertTrue(p.wait(10))
        self.assertEqual(p.state, "shutdown_unknown")
        self.assertFalse(p.shutdown_ok)
        self.assertIn("abort failed", p.abort_error)
        self.assertFalse(wav.exists())
        self.assertTrue(FakeStream.instances[0].closed)

    def test_close_failure_does_not_skip_wav_cleanup(self):
        self._install()
        FakeStream.close_error = OSError("close failed")
        wav = self._wav()
        p = audio_out.play("hi", device=0, synthesize_fn=lambda t, c: str(wav))
        self.assertTrue(p.wait(10))
        self.assertEqual(p.state, "shutdown_unknown")
        self.assertFalse(p.shutdown_ok)
        self.assertIn("close failed", p.close_error)
        self.assertFalse(wav.exists())

    def test_stop_after_completion_keeps_terminal_state(self):
        self._install()
        wav = self._wav()
        p = audio_out.play("hi", device=0, synthesize_fn=lambda t, c: str(wav))
        self.assertTrue(p.wait(10))
        p.stop()
        self.assertEqual(p.state, "completed")
        self.assertTrue(p.shutdown_ok)

    def test_device_resolution_failure_cleans_copied_wav(self):
        self._install()
        audio_out.resolve_device = lambda device: (_ for _ in ()).throw(
            RuntimeError("device missing"))
        wav = self._wav()
        p = audio_out.play("hi", device=0, synthesize_fn=None,
                           cleanup_path=str(wav))
        self.assertTrue(p.wait(10))
        self.assertEqual(p.state, "failed")
        self.assertFalse(wav.exists())

    def test_stop_before_start_never_opens_stream(self):
        self._install()
        wav = self._wav()
        p = audio_out.play("hi", device=0, synthesize_fn=lambda t, c: str(wav),
                           autostart=False)
        self.assertEqual(FakeStream.instances, [])
        p.stop()
        self.assertTrue(p.wait(2))
        self.assertEqual(p.state, "cancelled")
        self.assertTrue(p.shutdown_ok)
        self.assertTrue(p.done.is_set())
        self.assertFalse(p.start())
        self.assertEqual(FakeStream.instances, [])
        p2 = audio_out.play("hi", device=0, synthesize_fn=lambda t, c: str(wav),
                            autostart=False)
        p2.stop()
        p2.start()
        self.assertTrue(p2.wait(2))
        self.assertEqual(FakeStream.instances, [])

    def test_stop_returns_before_blocking_abort(self):
        self._install()
        FakeStream.complete = False
        FakeStream.abort_gate = threading.Event()
        wav = self._wav()
        p = audio_out.play("hi", device=0, synthesize_fn=lambda t, c: str(wav))
        deadline = time.time() + 5
        while not FakeStream.instances and time.time() < deadline:
            time.sleep(0.01)
        self.assertTrue(FakeStream.instances)
        self.assertTrue(FakeStream.instances[0].started.wait(5))
        started = time.time()
        p.stop()
        self.assertLess(time.time() - started, 1.0)
        # Cleanup must not publish success while abort is still blocked.
        self.assertFalse(p.wait(0.3))
        FakeStream.abort_gate.set()
        self.assertTrue(p.wait(10))
        self.assertEqual(p.state, "cancelled")
        self.assertTrue(p.shutdown_ok)
        self.assertTrue(FakeStream.instances[0].closed)


class TtsControllerTests(unittest.TestCase):
    def setUp(self):
        self._saved = (mini_control._TTS_PLAYBACK, mini_control._TTS_SESSION,
                       mini_control._TTS_STATE, mini_control._TTS_ERROR,
                       mini_control.TTS_ENABLED)
        mini_control._TTS_PLAYBACK = None
        mini_control._TTS_SESSION = 0
        mini_control._TTS_STATE = "idle"
        mini_control._TTS_ERROR = None
        mini_control.TTS_ENABLED = True

    def tearDown(self):
        (mini_control._TTS_PLAYBACK, mini_control._TTS_SESSION,
         mini_control._TTS_STATE, mini_control._TTS_ERROR,
         mini_control.TTS_ENABLED) = self._saved

    def test_retained_unknown_shutdown_blocks_new_playback(self):
        mini_control._TTS_STATE = "shutdown_unknown"
        mini_control._TTS_PLAYBACK = object()
        called = []
        old = mini_control.qqai_tts_path
        mini_control.qqai_tts_path = lambda text: called.append(text) or None
        try:
            res = mini_control.speak_with_tts("hello")
        finally:
            mini_control.qqai_tts_path = old
        self.assertTrue(res["busy"])
        self.assertEqual(called, [])

    def test_stop_cannot_clear_retained_unknown_state(self):
        mini_control._TTS_STATE = "shutdown_unknown"
        mini_control._TTS_PLAYBACK = None
        out = mini_control.stop_tts_playback(0.1)
        self.assertFalse(out["ok"])
        self.assertEqual(mini_control._TTS_STATE, "shutdown_unknown")

    def test_stop_during_synthesis_supersedes_without_starting_playback(self):
        stopped = []

        class Handle:
            shutdown_ok = True
            state = "cancelled"
            error = None

            def stop(self):
                stopped.append(True)

            def wait(self, timeout=None):
                return True

        old_path, old_play = mini_control.qqai_tts_path, None
        mini_control.qqai_tts_path = lambda text: None
        import video_runtime.audio_out as ao
        old_play = ao.play

        def fake_play(*args, **kwargs):
            mini_control.stop_tts_playback(0.1)
            return Handle()

        ao.play = fake_play
        try:
            res = mini_control.speak_with_tts("hello")
        finally:
            mini_control.qqai_tts_path = old_path
            ao.play = old_play
        self.assertFalse(res["ok"])
        self.assertTrue(res.get("busy"))
        self.assertEqual(stopped, [True])
        self.assertIsNone(mini_control._TTS_PLAYBACK)

    def test_monitor_retains_handle_when_shutdown_unconfirmed(self):
        handle = types.SimpleNamespace(shutdown_ok=False, state="shutdown_unknown",
                                       error=None, wait=lambda: True)
        mini_control._TTS_SESSION = 3
        mini_control._TTS_PLAYBACK = handle
        mini_control._monitor_tts(handle, 3)
        self.assertIs(mini_control._TTS_PLAYBACK, handle)
        self.assertEqual(mini_control._TTS_STATE, "shutdown_unknown")

    def test_controller_publishes_before_start_and_stop_reaches_handle(self):
        class Handle:
            shutdown_ok = True
            state = "cancelled"
            error = None

            def __init__(self):
                self.started = False
                self.stopped = False
                self.done = threading.Event()

            def start(self):
                self.published = mini_control._TTS_PLAYBACK is self
                self.started = True
                return True

            def stop(self):
                self.stopped = True
                self.done.set()

            def wait(self, timeout=None):
                return self.done.wait(timeout)

        handle = Handle()
        seen = {}
        old_path = mini_control.qqai_tts_path
        import video_runtime.audio_out as ao
        old_play = ao.play

        def fake_play(*args, **kwargs):
            seen.update(kwargs)
            return handle

        mini_control.qqai_tts_path = lambda text: None
        ao.play = fake_play
        try:
            res = mini_control.speak_with_tts("hello")
        finally:
            mini_control.qqai_tts_path = old_path
            ao.play = old_play
        self.assertTrue(res["ok"])
        self.assertTrue(handle.published)
        self.assertFalse(seen["autostart"])
        self.assertTrue(handle.started)
        out = mini_control.stop_tts_playback(0.5)
        self.assertTrue(out["ok"])
        self.assertTrue(handle.stopped)


if __name__ == "__main__":
    unittest.main()
