# QQ AI Video Runtime Core

**Experimental Source Preview**
**v0.1**

A local Windows runtime slice for a video-call digital human: it captures a target
application's audio, transcribes it locally, drives a dialogue loop, and emits
Live2D expression/mouth state.

> **Status: experimental source preview. MIT licensed. Maintained by Melarizan.
> Not a release, and not ready for production use.**
>
> **A real QQ video call has not been accepted end-to-end from this tree.** Do not
> read "runtime" as "working product". See *What this is not* below.

---

## What this is

The safe, self-contained part of the project:

- **ASR** — resident service (`asr_service.py`, `asr_client.py`) plus local
  transcription (`video_runtime/asr_in.py`, `faster-whisper`, optional)
- **Dialogue loop** — `conversation_loop.py`, calling an OpenAI-compatible
  endpoint you configure
- **TTS playback** — `video_runtime/audio_out.py` (SAPI child, bounded, own child)
- **Avatar control plane** — `mini_control.py` + `avatar_decision.py` on
  `127.0.0.1:8770` with a per-run token
- **Audio capture** — device loopback (`audio_capture.py`) and **process-tree
  loopback** (`process_loopback*.py`, `proc_capture_fix.py`, `peer_audio_asr.py`)
- **Session state** — `session_coordinator.py`, `pcm_ring.py`, `qqai_memory.py`
- **Configuration and diagnostics** — `runtime_config.py`, `doctor.py`
- **DirectShow enumeration only** — `p2_ds_enum.py` (no capture, no control)

20 runtime modules plus a start-only launcher. Windows only.

---

## What this is not

1. **No auto-answer.** Core never accepts or rejects a call.
2. **No physical input control.** No `SetCursorPos`, `mouse_event`, `SendInput`,
   `keybd_event`.
3. **No camera process termination.** No `TerminateProcess` / `taskkill` /
   `os.kill` against other programs.
4. **No watchdog auto-recovery.** `launch_core.py` starts processes and
   deliberately never stops or restarts them.
5. **No one-key runtime.** No installer, no service, no GUI, no tray app.
6. **Not library-complete either.** Load no plugins; there is no add-on loader.

If you have seen a "full runtime" of this project, **that is not this tree**.

---

## Unverified / open behaviour

- **A real QQ video call has not been accepted end-to-end from this tree.**
- **The ~13–16 FPS behaviour is unexplained.** No cause identified, no fix claimed.
- **Long-utterance ASR latency has not been adequately validated.**
- COM/WASAPI capture paths and `faster-whisper` inference are not covered by the
  automated suite (it uses stubs and never opens a real device).

---

## Quick start

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt          # numpy only

cd core
python -B -X utf8 -m unittest discover -s r3/tests -p "test_*.py"   # 90 tests, OK
python launch_core.py --dry-run                     # show the plan, start nothing
```

Full instructions, including optional ASR/TTS and every self-check, are in
`INSTALLATION.md`.

---

## Documentation

| Document | What it covers |
|---|---|
| `INSTALLATION.md` | venv, dependencies, verification, optional parts, self-checks |
| `CONFIGURATION.md` | environment variables, `bot/config.json` fields, ports, CLI reference |
| `DEPENDENCIES.md` | verified dependency inventory, Python/Windows floors, tested versions |
| `PRIVACY.md` | **what data is captured, stored, and sent — traced from the code** |
| `SECURITY.md` | trust model, accepted exposures, reporting, invariants |
| `KNOWN_ISSUES.md` | the authoritative limitation list |
| `TEST_REPORT.md` | what the suite does and does not prove |
| `CONTRIBUTING.md` | contribution terms (MIT) and project invariants |
| `CHANGELOG.md` | how this snapshot was derived |
| `ROADMAP.md` | non-committal list of open work |
| `LICENSE_FINAL_AUDIT.md` | provenance audit: what is and is not in this tree |
| `LICENSE_HUMAN_DECISION.md` | the licensing decision record (now resolved) |
| `THIRD_PARTY_NOTICES.md` | third-party licenses; what MIT does *not* cover |
| `DEPENDENCIES.md` | also: what you must supply yourself |

---

## Security notes (read before use)

1. **Same-user process, not a sandbox.** Core runs with your user rights.
2. **User-code loading.** `qqai_memory.py` and `mini_control.py` add the directory
   configured by `QQAI_BOT_DIR` to `sys.path` and import fixed module names from
   it. **That code runs with your privileges.** Point it only at code you trust.
3. **Read-only process enumeration.** Toolhelp / `tasklist` / `wmic` / a fixed CIM
   query are used to find `QQ.exe`. Nothing is injected, terminated, or read from
   the target.
4. **No shell-out from data.** The only PowerShell uses are one constant read-only
   query and SAPI synthesis with text passed via stdin.
5. **Local ports.** Control plane `127.0.0.1:8770` (token in `logs/control_token`),
   ASR `127.0.0.1:3010`. Same-user processes can reach them; a token is not
   process isolation.
6. **Origin policy.** Core serves no web UI, so `ALLOWED_ORIGINS` is empty: any
   request carrying an `Origin` header is rejected.
7. **Logs contain sensitive text and audio.** Transcripts, replies, a rolling raw
   PCM buffer, and synthesised WAVs are written to `core/logs/`. **There is no off
   switch and no retention limit in v0.1.** See `PRIVACY.md`.
8. **Transcripts leave the machine** when you configure a remote model endpoint —
   that is the intended function, not a side effect.

---

## Run

```powershell
cd core

# show what would start; spawns nothing
python launch_core.py --dry-run

# start ASR (:3010), control plane (:8770) and the dialogue loop
python launch_core.py
```

The launcher starts processes and prints their PIDs. **It will not stop them** —
use `Ctrl+C` or Task Manager. Core deliberately ships no stop/kill logic.

---

## Test

```powershell
cd core
python -B -X utf8 -m unittest discover -s r3/tests -p "test_*.py"
# Ran 90 tests ... OK
```

The suite uses fakes and loopback fixtures only: no real audio device, no network,
no QQ process. See `TEST_REPORT.md` for what that does and does not prove.

---

## Layout

```
core/
  launch_core.py          start-only launcher (no stop, no plugins)
  r2/                     13 runtime modules
  video_runtime/          7 audio/COM-layer modules
  r3/tests/               6 test files (90 cases)
  logs/                   created at runtime — contains sensitive data
```

---

## License

**MIT** — see [`LICENSE`](LICENSE).

```text
Copyright (c) 2026 Melarizan
```

Maintainer: **Melarizan** (GitHub). No real name is published.

The MIT license covers the code and documentation authored for this project. It
does **not** cover third-party packages, model weights, or the software this code
interoperates with — see [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).

### AI-assisted development

Parts of this codebase were written with the assistance of **DeepSeek** and
**OpenAI Codex**. Those tools are not authors and hold no copyright; the copyright
holder is Melarizan. This is disclosed so users can weigh it.

Provenance detail is in `LICENSE_FINAL_AUDIT.md`; the decision record is in
`LICENSE_HUMAN_DECISION.md`.
