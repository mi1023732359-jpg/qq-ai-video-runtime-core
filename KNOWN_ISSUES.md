# Known Issues and Limitations

QQ AI Video Runtime Core — Experimental Source Preview — v0.1

This list is **intentional and authoritative**. "Experimental Source Preview"
means these are not bugs to be fixed by a support ticket — they describe what this
tree is not.

---

## 1. What Core does not contain

| Not included | Consequence |
|---|---|
| **Auto-answer** | Core never accepts or rejects a call. Call lifecycle decisions exist in `session_coordinator.py` as logic and tests, but nothing in this tree drives QQ to answer. |
| **Physical input control** | No `SetCursorPos`, `mouse_event`, `SendInput`, `keybd_event`. Core cannot click or type. |
| **Camera process termination** | No `TerminateProcess` / `taskkill` / `os.kill` against other programs. |
| **Watchdog auto-recovery** | There is no supervisor in this tree. `launch_core.py` starts processes and deliberately never stops or restarts them. |
| **A one-key runtime** | No installer, no service, no tray app, no GUI. You run Python scripts. |
| **The Live2D model, the camera driver, the web page** | Out of scope for this tree. The control plane emits expression/mouth state; the renderer is separate software you supply. |
| **Any plugin or add-on loader** | Core loads no plugins. Auto Answer / Camera Management add-ons are not imported. |

If you have seen a "complete runtime" of this project elsewhere, **that is not this
tree**. Do not assume any capability of the full runtime exists here.

---

## 2. Unverified behaviour (explicitly)

1. **A real QQ video call has not been accepted end-to-end from this tree.**
   Capture, transcription, dialogue, and TTS exist as runnable components, but a
   full real call has not been signed off for Core.
2. **The ~15 FPS frame-rate problem is not explained.** The cause of the observed
   ~13–16 FPS behaviour has **not** been determined. No fix is claimed.
3. **Long-utterance ASR latency has not been adequately validated.** Chunking
   exists and is unit-tested at the math level; sustained multi-minute latency
   under load has not been measured.
4. **COM/WASAPI capture paths are not covered by automated tests.** They require a
   real audio stream; the suite exercises construction and queueing only.
5. **`faster-whisper` inference is not exercised by the tests.** A stub
   transcriber is used.

---

## 3. Operational limitations

| Area | Limitation |
|---|---|
| Platform | Windows only. No fallback for non-Windows. |
| Process loopback | Requires Windows build ≥ 19041. Older builds cannot use it. |
| Stopping | Core ships **no stop logic**. Stop children yourself. |
| Audio device selection | Core follows the OS default device; it does not offer per-device capture selection. |
| Camera selection | Exact FriendlyName match only; no fuzzy match, no index selection. |
| Concurrency | One dialogue loop and one ASR worker are assumed. Multiple concurrent calls are not supported. |
| Recovery | A crashed process is not restarted. `session_coordinator` can detect a stale state file on next start, but it does not resurrect a call. |
| `wmic` usage | `video_runtime/process_loopback.py:103` queries the process tree with `wmic`, which is deprecated and may be absent on newer Windows builds. If it is missing, tree expansion degrades to the root PIDs. |

---

## 4. Data-handling limitations

| Area | Limitation |
|---|---|
| Logging | **No off switch.** Core always writes `core/logs/`. See `PRIVACY.md` §7. |
| Retention | Nothing deletes logs. `logs/` grows until you remove it. |
| Sensitive content | `logs/conversation_trace.jsonl`, `logs/conversation_loop.json`, `logs/peer_asr.json`, and the session `history` files contain full conversation text. `logs/control_token` holds a local control credential. The complete per-file inventory is in `PRIVACY.md` §2. |
| Raw audio on disk | `logs/pcm_ring.bin` holds a rolling raw-PCM buffer; `logs/audio/*.wav` holds synthesised speech; `--wav` writes capture audio. |
| Temporary WAV | `logs/asr_tmp/*.wav` is deleted in `finally`; a hard crash may leave one behind. |
| Unbounded base queue | `process_loopback_stream.ProcessLoopbackSource._frames` has no cap. The `proc_capture_fix` subclass used by the capture helpers **is** capped at 4 MiB. |

---

## 5. Security posture (accepted, documented)

1. **Not a sandbox.** Core runs with your user rights.
2. **User-code execution.** `QQAI_BOT_DIR` code is imported and executed (see
   `PRIVACY.md` §6). Point it only at code you trust.
3. **Read-only process observation.** Core enumerates `QQ.exe` and captures its
   audio output; it never injects, reads memory, or terminates the target.
4. **Loopback-only ports.** `8770` and `3010` bind `127.0.0.1`. The control token
   is a convenience, not an isolation boundary.
5. **Fixed PowerShell use.** One constant read-only CIM query
   (`proc_capture_fix.QQ_PROCESS_QUERY`) and one SAPI synthesis call that passes
   text via **stdin**. No caller data is concatenated into a command line.

---

## 6. Code-level carry-over items (reviewed, not fixed)

These were reviewed and accepted as non-blocking for a preview:

| ID | Item | Why it stays |
|---|---|---|
| `CORE-SUP-1` | `r2/asr_service.py` calls `os._exit(42)` when the ASR worker exceeds `--stuck-timeout` | It is the designed watchdog-restart contract: exit code 42 tells an external supervisor to restart. Core ships no supervisor, so in this tree it simply exits. |
| `CORE-QQENUM-1` | Core enumerates `QQ.exe` processes (PID, parent PID, name; CIM also selects `CommandLine`) | Required to find the capture target. Documented in `PRIVACY.md` §4. |
| `CORE-LOG-1` | Traces and WAVs may contain sensitive conversation content | Needed for debugging; disclosed in `PRIVACY.md`. No off switch in v0.1. |

---

## 7. Things that will *look* broken but are not

| Observation | Explanation |
|---|---|
| Tests pass but no audio is heard or captured | By design: the suite never opens real devices. |
| `doctor.py` exits non-zero on a fresh machine | `config_missing` is expected until you set `QQAI_BOT_DIR`/`QQAI_BOT_CONFIG`. |
| The launcher leaves processes running | Start-only by design; it prints PIDs and refuses to manage them. |
| The brain replies "（大脑未配置）" | No `providers` entry configured in `bot/config.json`. |
| The brain replies "（网络不太好）" | The provider request failed; the error is in the reply dict, not logged. |
| Transcription hallucinates text during silence | Mitigated by `VadTranscriber` (VAD on) in `conversation_loop`, but not eliminated in every path. |

---

## 8. Reporting

This is an experimental preview without a support commitment. If you report an
issue, include:

- the exact command line,
- the Windows build and Python version,
- the relevant `core/logs/*.json` evidence file,

and **remove conversation text and any API keys first** — logs contain both.
