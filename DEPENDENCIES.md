# Dependencies

QQ AI Video Runtime Core — Experimental Source Preview — v0.1

This document records **what the code actually imports**, verified by parsing every
module in this tree, not by summarising older reports.

- Inventory method: AST scan of all 27 `.py` files, separating module-level imports
  (hard) from imports inside functions (lazy).
- The only unconditional third-party import in the whole tree is `numpy`.

---

## 1. Hard dependencies

Required for Core to import at all.

| Package | Where | Why |
|---|---|---|
| `numpy` | `core/r2/conversation_loop.py`, `core/r2/pcm_ring.py`, `core/r2/peer_audio_asr.py`, `core/r2/proc_capture_fix.py`, `core/video_runtime/asr_in.py`, `core/video_runtime/audio_capture.py` | PCM buffers, resampling, WAV frames |

Install:

```powershell
python -m pip install -r requirements.txt
```

**No version pin is declared.** No minimum version has been verified by this
project, so none is claimed. The version used for the released test run is
recorded in section 5.

---

## 2. Optional dependencies (lazy imports)

These are imported **inside functions**. Core imports, and the full unit test
suite passes, without any of them installed. Missing one degrades one feature
and raises only when that feature is used.

| Package | Import site | Feature it enables | Without it |
|---|---|---|---|
| `faster-whisper` | `core/video_runtime/asr_in.py:105` | Local speech-to-text (`WhisperTranscriber`) | No local ASR; only the stub transcriber used by tests |
| `PyAudioWPatch` | `core/video_runtime/audio_capture.py:129,153` | Device-level WASAPI loopback capture | Device-loopback capture unavailable; process loopback path is separate and does not need it |
| `sounddevice` | `core/video_runtime/audio_out.py:32,71,263,413` | Audio playback of synthesised speech | No playback device output |
| `voice_out` | `core/r2/mini_control.py:226` | QQAI-side TTS (`voice_out.synthesize`) | That TTS path is skipped; Core's own SAPI path is unaffected |

Install:

```powershell
python -m pip install -r requirements-optional.txt
```

### `voice_out` is not a PyPI package

`voice_out` must exist **inside the directory** you point `QQAI_BOT_DIR` or
`QQAI_BOT_CONFIG` at. It is loaded from there at call time. It is not listed as
an installable requirement and this project does not distribute it.

### Model weights are downloaded at first use

`faster-whisper` does **not** ship model weights. The first successful
`WhisperModel(...)` call downloads weights (hundreds of MB, model-size dependent)
over the network and caches them. `download_root` can be supplied to control the
location; otherwise the library default is used. This is a network access you
should expect on first run — see `PRIVACY.md`.

---

## 3. Python version

| Item | Value |
|---|---|
| Verified interpreter | CPython **3.12.0** (the only version this project has actually run) |
| Practical floor | **3.8** |
| Basis for the floor | The source uses `Path.unlink(missing_ok=...)` in 8 places (`missing_ok=` is 3.8+). No 3.9/3.10/3.11-only standard-library API was found (no `removeprefix`, `functools.cache`, `zoneinfo`, `graphlib`, `tomllib`, `match`, or runtime `X | Y` in `isinstance`). All 54 PEP 604 unions (`str | None`) sit in files that carry `from __future__ import annotations`, so they are never evaluated at runtime. |

**Not verified:** any interpreter other than 3.12.0. If you run 3.8–3.11 you are
outside the tested configuration.

---

## 4. Operating system

| Item | Value |
|---|---|
| OS | Windows only |
| Verified OS | Windows 11 Home China, build **26200** |
| Process-loopback capture | Requires Windows **build 19041** (Win10 2004) or newer; `video_runtime/process_loopback.py` defines `MIN_BUILD = 19041` and `check_support()` reports the real build |
| Tested paths | Device-level WASAPI (`PyAudioWPatch`), process loopback via `ActivateAudioInterfaceAsync` + PROCESS_LOOPBACK, DirectShow enumeration via `ctypes` COM, SAPI synthesis via PowerShell |

Components that call Windows APIs through `ctypes` (`kernel32`, `ole32`,
`mmdevapi`) are **not** portable to other operating systems. DirectShow
enumeration and process loopback have no cross-platform fallback.

**PowerShell is required** for two features and is assumed present on Windows:

1. `core/r2/proc_capture_fix.py` — one **fixed constant** read-only CIM query
   (`QQ_PROCESS_QUERY`). No caller input is concatenated into it.
2. `core/video_runtime/audio_out.py` — SAPI speech synthesis; the text is passed
   through **stdin**, never interpolated into the script source.

---

## 5. Versions present in the released test environment

Recorded for reproducibility. These are observations, not minimums.

| Package | Version |
|---|---|
| `numpy` | 2.5.2 |
| `faster-whisper` | 1.2.1 |
| `PyAudioWPatch` | 0.2.12.9 |
| `sounddevice` | 0.5.6 |
| `voice_out` | not installed (lazy import; not needed by the tests) |

---

## 6. System and external software you must supply

Core does **not** ship, install, or license any of these. You are responsible for
obtaining and legally using them.

| Need | Supplied by | Notes |
|---|---|---|
| A virtual camera, if you want capture by name | e.g. OBS Virtual Camera | `QQAI_CAMERA_NAME` defaults to `OBS Virtual Camera`; Core refuses to silently pick another device |
| A Live2D model and the renderer/page | **not included** | The control plane emits expression/mouth state only; the model and driver are out of scope for this tree |
| A QQ client | you | Core never launches, answers, clicks, or controls it |
| A model API + credentials | you, in your `bot/config.json` | Only used when the dialogue loop is running |

---

## 7. What is deliberately *not* a dependency

- No `pywin32` / `win32api` / `win32gui` — Windows APIs are called through `ctypes`.
- No web framework — the control plane uses `http.server`.
- No cloud SDK — the only outbound HTTP is `urllib.request` against the model
  endpoint you configure.
- No plugin/auto-discovery packages. Core loads no plugins.

---

## 8. Re-check this document yourself

The inventory above was produced from the source in this tree. To reproduce it:

```powershell
cd core
python -B -X utf8 -c "import ast,pathlib;[print(f'{f.name}:', sorted({n.names[0].name.split('.')[0] for n in ast.walk(ast.parse(f.read_text(encoding='utf-8-sig'))) if isinstance(n, ast.Import)})) for f in sorted(pathlib.Path('.').rglob('*.py'))]"
```
