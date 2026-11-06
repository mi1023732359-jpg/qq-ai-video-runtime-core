# Test Report

QQ AI Video Runtime Core — Experimental Source Preview — v0.1

## Result

```text
Ran 90 tests in 4.450s

OK
```

- Interpreter: CPython **3.12.0** (`-B -X utf8`)
- Platform: Windows 11 Home China, build 26200
- Command: `python -B -X utf8 -m unittest discover -s r3/tests -p "test_*.py"` from `core/`
- 90 test methods → 90 executed cases. No skips, no `xfail`, no `expectedFailure`.

Cold import check (each module in its own interpreter, `core/` and `core/r2/` on
`PYTHONPATH`): **20 / 20** modules imported successfully.

---

## Where the tests live

| File | Cases | Subject |
|---|---:|---|
| `test_control_plane_auth.py` | 26 | Control-plane auth, origin policy, request bounds |
| `test_core_hardening.py` | 21 | The four security fixes (injection, queue, sys.path, handle prototypes) |
| `test_core_helpers.py` | 15 | Capture helper symbols, CIM parsing, PID selection, degradation |
| `test_r2_listening_feedback.py` | 14 | Listening-feedback state machine |
| `test_runtime_config.py` | 10 | Config precedence, paths, camera selection |
| `test_asr_chunking.py` | 4 | PCM chunking and resampling math |
| **Total** | **90** | |

---

## What the suite actually proves

### Security regressions (the important part)

| Property | How it is tested |
|---|---|
| `process_names()` cannot spawn a process | Injected inputs are run under a patch that makes any `subprocess.run` raise; the call must return `[]` |
| PID validation is strict | `"123"`, `True`, `False`, `-1`, `0`, `1.5`, mixed lists, `None`, `123`, `"10"`, dicts → all `[]` |
| No shell-out primitive in the module | AST scan for `subprocess`/`pty` imports and `os.system/popen/execv/spawnv` attributes |
| Capture queue is bounded | 200 × 64 KiB → buffered ≤ 4 MiB, drops counted, `last_drop_reason == "buffer_full"` |
| Drop accounting is exact | `kept + dropped_bytes == total pushed` |
| Producer never blocks | 400 × 32 KiB completes well under 2 s with the consumer stopped |
| Oversized single buffer rejected | > 1 MiB → not queued, reason `oversized_buffer` |
| Import does not mutate `sys.path` | 7 representative modules, each in a fresh interpreter: path-length delta must be 0 |
| No module-level `sys.path.insert` remains | AST scan; only `__main__`-guarded inserts and the two documented `bot_dir` inserts are allowed |
| Script mode still works | Positive assertion that the four script entry points keep a `__main__`-guarded bootstrap |
| Toolhelp handles are pointer-safe | AST assertion that `CreateToolhelp32Snapshot`, `Process32FirstW`, `Process32NextW`, `CloseHandle` all set `restype` **and** `argtypes` |
| The typed Toolhelp call works for real | Live `_process_table()` call: rows are `(int, int, str)` and the current PID appears |

### Control plane

26 cases cover token enforcement, rejection of any `Origin`-bearing request (empty
allowlist), request-size bounds, and loopback-only fixture behaviour.

### Configuration

10 cases cover the `QQAI_BOT_CONFIG` / `QQAI_BOT_DIR` mutual exclusion, the
ambiguous-config case, exact camera-name matching, and refusal to fall back to
another device.

---

## What the suite does **not** prove

This is the part that matters most when reading a green result.

1. **No real audio device is opened.** `pyaudiowpatch`, `sounddevice`, and the
   WASAPI process-loopback path are never exercised against hardware. Only object
   construction and queueing are tested.
2. **No real ASR inference.** `faster-whisper` is replaced by a stub; the model is
   never loaded and no audio is transcribed.
3. **No real speech synthesis.** `audio_out.synthesize()` (which starts a
   PowerShell SAPI child) is not run.
4. **No real QQ interaction.** Building the module stub sets an error can be
   asserted, but no QQ process is contacted, captured, or controlled.
5. **No network is contacted.** The model-endpoint call is not exercised; nothing
   is sent anywhere during the suite.
6. **No end-to-end call.** A full capture → transcribe → dialogue → speak loop on a
   real call has **not** been performed or signed off for Core.
7. **No long-run stability.** Queue tests are burst tests; there is no multi-minute
   soak, no memory-growth measurement, no sustained-latency measurement.
8. **No performance claims.** The ~13–16 FPS behaviour is unexplained and this
   suite says nothing about it.

A green suite therefore means: **the security invariants hold and the logic
regressions are caught**. It does not mean the product works on a live call.

---

## Reproducing

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt   # numpy only; optional deps not needed

cd core
python -B -X utf8 -m unittest discover -s r3/tests -p "test_*.py"
```

If you do not get `Ran 90 tests ... OK`, check the tree hash against the release
manifest first: a modified tree invalidates this result.

---

## Environment used

| Item | Value |
|---|---|
| Python | 3.12.0 |
| numpy | 2.5.2 |
| OS | Windows 11 build 26200 |
| Optional packages present | `faster-whisper` 1.2.1, `PyAudioWPatch` 0.2.12.9, `sounddevice` 0.5.6, `voice_out` absent |
| Network during tests | none |
