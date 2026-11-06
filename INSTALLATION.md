# Installation

QQ AI Video Runtime Core — Experimental Source Preview — v0.1

Core is **not** an installer, not a service, and not a one-click runtime. It is a
source tree you run with your own Python. This page covers exactly what you need
to get it running and how to prove it works.

**Supported platform:** Windows only (build 19041 / Win10 2004 or newer for
process-loopback capture). Verified on Windows 11 build 26200 with CPython 3.12.0.
See `DEPENDENCIES.md` for the full dependency inventory.

---

## 1. Create a virtual environment

From the directory that contains this `README.md`:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -V
```

`Activate.ps1` may be blocked by execution policy. If so, either use
`.\.venv\Scripts\python.exe` explicitly for every command below, or allow the
script for the current user:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

---

## 2. Install core dependencies

```powershell
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

That installs **`numpy`** only. No version pin is declared because no minimum
version has been verified by this project.

---

## 3. Verify the installation

Run these three checks. All three must pass before you go further.

```powershell
# a) dependency present
python -c "import numpy; print('numpy', numpy.__version__)"

# b) every module imports (no audio device, no network needed)
cd core
python -B -X utf8 -c "import sys; sys.path[:0]=['.', './r2']; import asr_client, asr_service, avatar_decision, conversation_loop, doctor, listening_feedback, mini_control, p2_ds_enum, pcm_ring, peer_audio_asr, proc_capture_fix, qqai_memory, runtime_config; import video_runtime.asr_in, video_runtime.audio_capture, video_runtime.audio_out, video_runtime.dialogue, video_runtime.process_loopback, video_runtime.process_loopback_stream, video_runtime.session_coordinator; print('imports OK')"
```

```powershell
# c) the full unit test suite
python -B -X utf8 -m unittest discover -s r3/tests -p "test_*.py"
```

Expected on a clean install: `Ran 90 tests` … `OK`.
These tests use fakes and loopback fixtures; they do **not** start real audio,
open real devices, or contact any network service.

---

## 4. Install optional ASR / TTS

Only needed if you want local speech-to-text or speech playback.

```powershell
python -m pip install -r requirements-optional.txt
```

| Package | Enables | If you skip it |
|---|---|---|
| `faster-whisper` | Local transcription | No ASR; only the stub transcriber |
| `PyAudioWPatch` | Device-level loopback capture | That capture path unavailable |
| `sounddevice` | Audio playback | No speaker output |

**First use of `faster-whisper` downloads model weights over the network.** Plan
for a few hundred MB and a working connection. See `PRIVACY.md` §5.3.

Verify optional pieces individually:

```powershell
python -c "import faster_whisper; print('faster-whisper', faster_whisper.__version__)"
python -c "import pyaudiowpatch; print('pyaudiowpatch OK')"
python -c "import sounddevice; print(sounddevice.query_devices())"
```

`voice_out` is **not** installable from PyPI. It must exist inside the directory
you point `QQAI_BOT_DIR` at. Without it, the QQAI TTS path is skipped.

---

## 5. What you must supply yourself

Core ships none of this. You are responsible for obtaining it and for having the
legal right to use it.

| Need | Notes |
|---|---|
| **A virtual camera**, if capture-by-name is wanted | `QQAI_CAMERA_NAME` defaults to `OBS Virtual Camera`. Core selects by exact FriendlyName and **refuses to fall back** to another device |
| **A Live2D model + renderer/page** | Not included in this tree. The control plane only emits expression/mouth state |
| **A QQ client**, if you want process-loopback audio | Core never launches or controls it; it only observes the process list and captures that process tree's audio |
| **A model API and key** | Only for the dialogue loop. Without it the brain returns a placeholder and sends nothing |
| **PowerShell** | Present on Windows; used for one fixed read-only CIM query and for SAPI synthesis |

If your camera is not named `OBS Virtual Camera`, set it explicitly:

```powershell
$env:QQAI_CAMERA_NAME = "Your Camera Name"
```

---

## 6. Run the existing self-checks

```powershell
cd core

# 1) environment report (never prints secrets; exit code 0 only when unambiguous)
python r2\doctor.py --json

# 2) control-plane self-test (starts a loopback server, then stops it)
python r2\mini_control.py --selftest

# 3) OS capability report for process loopback (read-only; no capture)
python video_runtime\process_loopback.py

# 4) what the launcher would start, without starting anything
python launch_core.py --dry-run
```

Notes:

- `doctor.py` requires `QQAI_BOT_CONFIG` **or** `QQAI_BOT_DIR` to be set, and will
  report `config_missing` otherwise. That is expected on a fresh machine.
- `process_loopback.py` prints the real Windows build and whether it meets
  `MIN_BUILD = 19041`. On older builds it exits non-zero with the reason.
- `mini_control.py --selftest` writes its evidence to `logs/mini_control_selftest.json`.

---

## 7. First real run

```powershell
cd core
python launch_core.py
```

This starts child processes and **prints their PIDs**. Core deliberately contains
**no stop/kill logic** — stop them with `Ctrl+C` or Task Manager. See
`KNOWN_ISSUES.md` before expecting a working call.

---

## 8. Troubleshooting quick table

| Symptom | Likely cause |
|---|---|
| `ModuleNotFoundError: numpy` | venv not activated, or step 2 skipped |
| `ConfigError: config_missing` | neither `QQAI_BOT_CONFIG` nor `QQAI_BOT_DIR` set |
| `ConfigError: config_conflict` | both set — set exactly one |
| `camera_not_found` | `QQAI_CAMERA_NAME` does not match any DirectShow FriendlyName exactly |
| `process loopback not supported` | Windows build < 19041 |
| `ModuleNotFoundError: faster_whisper` | optional step 4 skipped; ASR unavailable |
| Tests pass but no audio | expected — the suite never opens real devices |

---

## 9. Uninstall / clean up

```powershell
deactivate
Remove-Item -Recurse -Force .venv
Remove-Item -Recurse -Force core\logs      # deletes captured audio and transcripts
```

`core\logs` is where captured PCM, temporary WAVs, transcripts, and the control
token live. Read `PRIVACY.md` §7 before deleting it if you want to keep evidence.
