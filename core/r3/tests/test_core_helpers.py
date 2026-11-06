#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Core capture helpers (peer_audio_asr / proc_capture_fix) — boundary tests.

These two modules were added to Core on 2026-11-06 (decision A).  They provide
**read-only** QQ process enumeration and process-loopback audio capture.  The
tests below pin:

* the modules import and expose the symbols conversation_loop needs;
* process enumeration parses CIM output and degrades to an empty list;
* ``conversation_loop`` degrades cleanly without ``--pids`` and reaches the
  capture factory with ``--pids`` (factory stubbed: no capture is started);
* the modules contain no physical-input / process-termination / generic command
  runner primitive.
"""
from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "r2"))
sys.path.insert(0, str(ROOT))

import conversation_loop  # noqa: E402
import peer_audio_asr  # noqa: E402
import proc_capture_fix  # noqa: E402


def _completed(stdout: str):
    return types.SimpleNamespace(stdout=stdout, returncode=0, stderr="")


class HelperImportTests(unittest.TestCase):
    def test_both_helpers_import_and_expose_symbols(self):
        self.assertTrue(callable(proc_capture_fix.qq_processes))
        self.assertTrue(callable(proc_capture_fix.qq_root_pids))
        self.assertTrue(callable(proc_capture_fix.qq_all_pids))
        self.assertTrue(callable(proc_capture_fix.stats))
        self.assertTrue(hasattr(proc_capture_fix, "FixedProcessLoopbackSource"))
        self.assertTrue(callable(peer_audio_asr.qq_pids))
        self.assertTrue(callable(peer_audio_asr.process_names))

    def test_conversation_loop_imports_resolve(self):
        # The two function-level imports that used to fail are now satisfiable.
        self.assertTrue(hasattr(conversation_loop, "qq_pids"))
        self.assertTrue(hasattr(conversation_loop, "main"))


class ProcessEnumerationTests(unittest.TestCase):
    def test_qq_processes_parses_list(self):
        payload = '[{"ProcessId":1,"ParentProcessId":0,"CommandLine":"qq.exe"},' \
                  '{"ProcessId":2,"ParentProcessId":1,"CommandLine":"qq.exe"}]'
        with mock.patch.object(proc_capture_fix.subprocess, "run",
                               return_value=_completed(payload)):
            rows = proc_capture_fix.qq_processes()
        self.assertEqual([r["ProcessId"] for r in rows], [1, 2])

    def test_qq_processes_wraps_single_object(self):
        payload = '{"ProcessId":7,"ParentProcessId":3}'
        with mock.patch.object(proc_capture_fix.subprocess, "run",
                               return_value=_completed(payload)):
            rows = proc_capture_fix.qq_processes()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ProcessId"], 7)

    def test_qq_processes_degrades_on_garbage(self):
        with mock.patch.object(proc_capture_fix.subprocess, "run",
                               return_value=_completed("not json")):
            self.assertEqual(proc_capture_fix.qq_processes(), [])

    def test_qq_processes_degrades_on_failure(self):
        with mock.patch.object(proc_capture_fix.subprocess, "run",
                               side_effect=OSError("no powershell")):
            self.assertEqual(proc_capture_fix.qq_processes(), [])

    def test_qq_root_pids_selects_tree_roots(self):
        rows = [{"ProcessId": 10, "ParentProcessId": 1},
                {"ProcessId": 11, "ParentProcessId": 10},
                {"ProcessId": 20, "ParentProcessId": 2}]
        with mock.patch.object(proc_capture_fix, "qq_processes", return_value=rows):
            self.assertEqual(proc_capture_fix.qq_root_pids(), [10, 20])

    def test_qq_all_pids(self):
        rows = [{"ProcessId": 10, "ParentProcessId": 1},
                {"ProcessId": 11, "ParentProcessId": 10}]
        with mock.patch.object(proc_capture_fix, "qq_processes", return_value=rows):
            self.assertEqual(proc_capture_fix.qq_all_pids(), [10, 11])

    def test_peer_process_names_uses_toolhelp_snapshot(self):
        # FIX-INJECT-1: process_names() is now pure Toolhelp (no PowerShell).
        rows = [(10, 0, "QQ.exe"), (11, 10, "QQ.exe"), (12, 0, "notepad.exe")]
        with mock.patch.object(peer_audio_asr, "_process_table", return_value=rows):
            self.assertEqual(peer_audio_asr.process_names([10, 11]),
                             ["10:QQ.exe", "11:QQ.exe"])
            self.assertEqual(peer_audio_asr.process_names([12]), ["12:notepad.exe"])

    def test_peer_qq_pids_is_read_only_list(self):
        pids = peer_audio_asr.qq_pids()
        self.assertIsInstance(pids, list)
        self.assertTrue(all(isinstance(p, int) for p in pids))


class ConversationLoopDegradationTests(unittest.TestCase):
    def test_without_pids_exits_cleanly(self):
        self.assertEqual(conversation_loop.main([]), 1)

    def test_pids_mode_reaches_capture_without_starting_it(self):
        class Factory:
            def __init__(self, *args, **kwargs):
                self.kwargs = kwargs

            def start(self):
                raise RuntimeError("stub: capture must not start in tests")

        with mock.patch.object(proc_capture_fix, "FixedProcessLoopbackSource", Factory):
            code = conversation_loop.main(["--pids", "424242"])
        self.assertEqual(code, 2)   # main's documented start-failure path


class NoDangerousPrimitiveTests(unittest.TestCase):
    FORBIDDEN = ("TerminateProcess", "taskkill", "os.kill", "SendInput",
                 "mouse_event", "SetCursorPos", "shell=True", "__import__(")

    def _assert_clean(self, path: Path):
        text = path.read_text(encoding="utf-8")
        for token in self.FORBIDDEN:
            self.assertNotIn(token, text, f"{path.name} contains {token}")

    def test_helpers_have_no_dangerous_primitives(self):
        self._assert_clean(ROOT / "r2" / "peer_audio_asr.py")
        self._assert_clean(ROOT / "r2" / "proc_capture_fix.py")

    def test_helpers_have_no_generic_command_runner(self):
        text = (ROOT / "r2" / "proc_capture_fix.py").read_text(encoding="utf-8")
        # The removed `_ps(cmd)` helper must not come back; only the fixed
        # module-level query constant is allowed to reach PowerShell.
        self.assertNotIn("def _ps(", text)
        self.assertIn("QQ_PROCESS_QUERY", text)

    def test_conversation_loop_imports_only_core_helpers(self):
        text = (ROOT / "r2" / "conversation_loop.py").read_text(encoding="utf-8")
        self.assertNotIn("auto_answer", text)
        self.assertNotIn("watchdog", text)
        self.assertNotIn("camera_lifecycle", text)


if __name__ == "__main__":
    unittest.main()
