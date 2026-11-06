#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Core v0.1 hardening regressions: FIX-INJECT-1, FIX-QUEUE-1, FIX-SYSPATH-1.

FIX-INJECT-1  ``process_names()`` is pure Toolhelp and rejects non-int input;
              it must never spawn a shell.
FIX-QUEUE-1   the capture buffer is bounded, drops the oldest data, counts the
              drops and never blocks the producer.
FIX-SYSPATH-1 importing any Core module must not mutate ``sys.path`` (the
              bootstrap now lives behind ``if __name__ == "__main__"``).
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "r2"))
sys.path.insert(0, str(ROOT))

import peer_audio_asr  # noqa: E402
import proc_capture_fix  # noqa: E402


class InjectionTests(unittest.TestCase):
    """FIX-INJECT-1: no shell, no interpolation, strict input validation."""

    ROWS = [(10, 0, "QQ.exe"), (11, 10, "QQ.exe"), (12, 0, "notepad.exe")]
    INJECTION = "1; Write-Host INJECTED"
    INJECTION_2 = "1) -or (Get-Process"

    def _no_shell(self):
        def boom(*args, **kwargs):
            raise AssertionError("process_names() must not spawn a process")
        return mock.patch.object(subprocess, "run", side_effect=boom)

    def test_accepts_positive_ints(self):
        with mock.patch.object(peer_audio_asr, "_process_table", return_value=self.ROWS):
            self.assertEqual(peer_audio_asr.process_names([10]), ["10:QQ.exe"])

    def test_accepts_multiple_pids(self):
        with mock.patch.object(peer_audio_asr, "_process_table", return_value=self.ROWS):
            got = peer_audio_asr.process_names([10, 11, 12])
        self.assertEqual(got, ["10:QQ.exe", "11:QQ.exe", "12:notepad.exe"])

    def test_empty_list(self):
        self.assertEqual(peer_audio_asr.process_names([]), [])

    def test_rejects_string(self):
        with self._no_shell():
            self.assertEqual(peer_audio_asr.process_names(["123"]), [])

    def test_rejects_injection_payload(self):
        with self._no_shell():
            self.assertEqual(peer_audio_asr.process_names([self.INJECTION]), [])
            self.assertEqual(peer_audio_asr.process_names([self.INJECTION_2]), [])

    def test_rejects_bool_and_negative_and_float(self):
        with self._no_shell():
            self.assertEqual(peer_audio_asr.process_names([True]), [])
            self.assertEqual(peer_audio_asr.process_names([False]), [])
            self.assertEqual(peer_audio_asr.process_names([-1]), [])
            self.assertEqual(peer_audio_asr.process_names([0]), [])
            self.assertEqual(peer_audio_asr.process_names([1.5]), [])

    def test_rejects_mixed_valid_and_invalid(self):
        with self._no_shell():
            self.assertEqual(peer_audio_asr.process_names([10, "11"]), [])

    def test_rejects_non_sequence(self):
        with self._no_shell():
            for value in (None, 123, "10", {"10": 1}):
                self.assertEqual(peer_audio_asr.process_names(value), [], value)

    def test_module_has_no_shell_out_path(self):
        """FIX-INJECT-1: no shell-out primitive reaches the module at all."""
        tree = ast.parse((ROOT / "r2" / "peer_audio_asr.py").read_text(encoding="utf-8"))
        banned = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                banned += [a.name for a in node.names if a.name in ("subprocess", "pty")]
            elif isinstance(node, ast.ImportFrom) and node.module in ("subprocess", "pty"):
                banned.append(node.module)
            elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                if node.value.id == "subprocess":
                    banned.append(f"subprocess.{node.attr}")
                elif node.value.id == "os" and node.attr in ("system", "popen", "execv", "spawnv"):
                    banned.append(f"os.{node.attr}")
        self.assertEqual(banned, [])

    def test_toolhelp_handles_have_explicit_prototypes(self):
        """FIX-HANDLE-1: HANDLE-returning kernel32 calls must be typed explicitly.

        Without ``restype``/``argtypes`` ctypes defaults to C ``int`` and truncates
        the pointer-sized snapshot handle on 64-bit Windows.
        """
        tree = ast.parse((ROOT / "r2" / "peer_audio_asr.py").read_text(encoding="utf-8"))
        typed = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            for tgt in node.targets:
                if (isinstance(tgt, ast.Attribute)
                        and tgt.attr in ("restype", "argtypes")
                        and isinstance(tgt.value, ast.Attribute)
                        and isinstance(tgt.value.value, ast.Name)):
                    typed.add((tgt.value.attr, tgt.attr))
        for fn in ("CreateToolhelp32Snapshot", "Process32FirstW", "Process32NextW", "CloseHandle"):
            with self.subTest(function=fn):
                self.assertIn((fn, "restype"), typed)
                self.assertIn((fn, "argtypes"), typed)

    def test_process_table_reads_real_snapshot(self):
        """The typed call works against the live process table (read-only)."""
        rows = peer_audio_asr._process_table()
        self.assertIsInstance(rows, list)
        self.assertTrue(rows, "Toolhelp snapshot returned nothing")
        for pid, ppid, name in rows:
            self.assertIsInstance(pid, int)
            self.assertIsInstance(ppid, int)
            self.assertIsInstance(name, str)
        self.assertIn(os.getpid(), [p for p, _pp, _n in rows])


class BoundedQueueTests(unittest.TestCase):
    """FIX-QUEUE-1: the capture buffer is bounded and accounted for."""

    def _src(self):
        # Construction only: start() is what touches COM/audio.
        return proc_capture_fix.FixedProcessLoopbackSource(pids=[1], rate=48000)

    def test_normal_produce_and_consume(self):
        src = self._src()
        src._enqueue(b"\x00\x00" * 8, 8)
        self.assertEqual(src._buffered_bytes, 16)
        out = src.read()
        self.assertIsNotNone(out)
        self.assertEqual(src._buffered_bytes, 0)
        self.assertIsNone(src.read())

    def test_cap_is_enforced(self):
        src = self._src()
        chunk = 64 * 1024
        for _ in range(200):
            src._enqueue(b"\x00" * chunk, chunk // 4)
        self.assertLessEqual(src._buffered_bytes, src.MAX_BUFFERED_BYTES)
        self.assertGreater(src.dropped_buffers, 0)
        self.assertEqual(src.last_drop_reason, "buffer_full")

    def test_drop_counters_are_exact(self):
        src = self._src()
        chunk = 64 * 1024
        for _ in range(100):
            src._enqueue(b"\x00" * chunk, chunk // 4)
        kept = sum(len(b) for b in src._frames)
        total = 100 * chunk
        self.assertEqual(src._buffered_bytes, kept)
        self.assertEqual(src.dropped_bytes, total - kept)
        self.assertEqual(kept + src.dropped_bytes, total)

    def test_oversized_single_buffer_is_rejected(self):
        src = self._src()
        src._enqueue(b"\x00" * (src.MAX_SINGLE_BUFFER_BYTES + 1), 1)
        self.assertEqual(src._frames, [])
        self.assertEqual(src.dropped_buffers, 1)
        self.assertEqual(src.last_drop_reason, "oversized_buffer")

    def test_producer_never_blocks_when_consumer_stops(self):
        src = self._src()
        chunk = 32 * 1024
        began = time.time()
        for _ in range(400):
            src._enqueue(b"\x00" * chunk, chunk // 4)
        elapsed = time.time() - began
        self.assertLess(elapsed, 2.0)
        self.assertLessEqual(src._buffered_bytes, src.MAX_BUFFERED_BYTES)
        self.assertGreater(src.dropped_buffers, 0)

    def test_stats_reports_buffer_and_drops(self):
        src = self._src()
        src._enqueue(b"\x00" * 64, 16)
        st = proc_capture_fix.stats(src)
        self.assertEqual(st["buffered_bytes"], 64)
        self.assertEqual(st["buffered_buffers"], 1)
        self.assertEqual(st["max_buffered_bytes"], src.MAX_BUFFERED_BYTES)
        self.assertEqual(st["dropped_buffers"], 0)
        self.assertIn("last_drop_reason", st)

    def test_capture_loop_records_error_and_exits(self):
        """An exception in the capture loop is recorded and the loop stops."""
        src = self._src()
        src._capture = object()
        src._run.set()
        worker = threading.Thread(target=src._loop, daemon=True)
        worker.start()
        time.sleep(0.2)
        src._run.clear()
        worker.join(2.0)
        self.assertFalse(worker.is_alive(), "capture loop did not exit")
        self.assertIsNotNone(src.last_error)


def _is_main_guard(node):
    """True for ``if __name__ == "__main__":`` (either operand order)."""
    if not isinstance(node, ast.If):
        return False
    test = node.test
    if not isinstance(test, ast.Compare) or len(test.ops) != 1:
        return False
    if not isinstance(test.ops[0], ast.Eq):
        return False
    left, right = test.left, test.comparators[0]

    def dunder(n):
        return isinstance(n, ast.Name) and n.id == "__name__"

    def main(n):
        return isinstance(n, ast.Constant) and n.value == "__main__"

    return (dunder(left) and main(right)) or (main(left) and dunder(right))


def _sys_path_inserts(tree):
    """Return ``[(lineno, guarded_by_main)]`` for every ``sys.path.insert`` call."""
    found = []
    stack = [(tree, False)]
    while stack:
        node, guarded = stack.pop()
        func = getattr(node, "func", None)
        if (isinstance(node, ast.Call) and isinstance(func, ast.Attribute)
                and func.attr == "insert"):
            owner = func.value
            if (isinstance(owner, ast.Attribute) and owner.attr == "path"
                    and isinstance(owner.value, ast.Name) and owner.value.id == "sys"):
                found.append((node.lineno, guarded))
        child_guarded = guarded or _is_main_guard(node)
        for child in ast.iter_child_nodes(node):
            stack.append((child, child_guarded))
    return sorted(found)


class SysPathCleanTests(unittest.TestCase):
    """FIX-SYSPATH-1: importing a Core module must not rewrite sys.path."""

    MODULES = ["peer_audio_asr", "proc_capture_fix", "asr_service",
               "conversation_loop", "doctor", "mini_control"]
    PACKAGE_MODULES = ["video_runtime.process_loopback_stream"]

    #: Inserts that run at call time for a user-configured directory
    #: (CORE-DYN-1).  Documented, not module-level.
    DOCUMENTED_DYNAMIC = {"qqai_memory.py", "mini_control.py"}

    def _import_path_delta(self, module):
        code = ("import sys; before=list(sys.path); import " + module +
                "; after=list(sys.path); print(len(after)-len(before))")
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join([str(ROOT / "r2"), str(ROOT)])
        out = subprocess.run([sys.executable, "-B", "-c", code],
                             capture_output=True, text=True, timeout=60,
                             cwd=str(ROOT), env=env)
        self.assertEqual(out.returncode, 0, out.stderr)
        return int(out.stdout.strip() or "0")

    def test_imports_do_not_mutate_sys_path(self):
        for module in self.MODULES + self.PACKAGE_MODULES:
            with self.subTest(module=module):
                self.assertEqual(self._import_path_delta(module), 0, module)

    def test_no_module_level_sys_path_insert_left(self):
        """Only ``__main__`` guards and documented runtime inserts may remain."""
        offenders = []
        sources = sorted((ROOT / "r2").glob("*.py")) + sorted((ROOT / "video_runtime").glob("*.py"))
        for path in sources:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for lineno, guarded in _sys_path_inserts(tree):
                if guarded or path.name in self.DOCUMENTED_DYNAMIC:
                    continue
                offenders.append(f"{path.name}:{lineno}")
        self.assertEqual(offenders, [])

    def test_script_entrypoints_guard_their_bootstrap(self):
        """Script mode keeps working, but only behind ``__main__``."""
        for name in ("asr_service.py", "conversation_loop.py", "doctor.py", "mini_control.py"):
            with self.subTest(module=name):
                tree = ast.parse((ROOT / "r2" / name).read_text(encoding="utf-8"))
                inserts = _sys_path_inserts(tree)
                self.assertTrue(any(g for _, g in inserts),
                                f"{name} lost its script-mode bootstrap")
                if name not in self.DOCUMENTED_DYNAMIC:
                    self.assertTrue(all(g for _, g in inserts),
                                    f"{name} mutates sys.path at import time")


if __name__ == "__main__":
    unittest.main()
