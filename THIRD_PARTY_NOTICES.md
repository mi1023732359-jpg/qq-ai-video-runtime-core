# Third-Party Notices

QQ AI Video Runtime Core — Experimental Source Preview — v0.1

This file exists so that the project's own MIT license is not read as covering
anything the project does not own. **The MIT license in `LICENSE` applies only to
the source code and documentation written for this project.** It does not grant
rights to any third-party component listed below.

---

## 1. Scope of the project's own copyright

`LICENSE` covers the files in this repository that were authored for this project:
the Python modules under `core/`, the test suite, and the Markdown documentation.

It does **not** cover:

- third-party Python packages installed at runtime,
- model weights downloaded at runtime,
- any application this software interacts with (QQ, OBS, Live2D, etc.),
- any module placed in a user-supplied `QQAI_BOT_DIR`.

---

## 2. Runtime dependencies (not vendored)

No third-party source code is copied into this repository. These packages are
installed separately by you (or by `pip`) and remain under their own licenses.

| Package | License (from package metadata) | Notes |
|---|---|---|
| `numpy` | `BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0` | Hard dependency |
| `faster-whisper` | `MIT` | Optional; lazy import |
| `PyAudioWPatch` | `Apache-2.0` | Optional; lazy import |
| `sounddevice` | `MIT` | Optional; lazy import |

Versions used for the release test run are recorded in `DEPENDENCIES.md` §5.

### Apache-2.0 notice requirement

`PyAudioWPatch` is licensed under **Apache-2.0**. If you distribute a **bundle**
that includes it (for example a PyInstaller build), Apache-2.0 requires that you:

1. include a copy of the Apache-2.0 license text,
2. include any `NOTICE` file the package ships, if present, and
3. state any modifications you made to those files.

This repository does **not** bundle it, so nothing here satisfies that obligation
for you.

### MIT / BSD notice requirement

`numpy`, `faster-whisper`, and `sounddevice` require their copyright notice and
permission text to accompany redistributions of those packages. Again: only
relevant if you bundle them.

---

## 3. Model weights

`faster-whisper` downloads speech-model weights on first use. Those weights are
**not** part of this repository and are **not** licensed by this project's MIT
license. Their publisher's terms govern their use and redistribution. Check that
license yourself before shipping weights inside a distribution.

---

## 4. User-supplied content

Anything you place in the directory referenced by `QQAI_BOT_DIR` (the modules
`memory_archive`, `facts`, `main_persona`, `persona_layers`, `persona_history`,
`reply_style`, `world`, and `voice_out`) is **not** part of this project. Core
imports and executes it, but does not license it and does not claim any rights
over it.

The same applies to any Live2D model, camera driver, or web page you pair this
code with. They are not included in this repository.

---

## 5. Deliberately absent components

For the avoidance of doubt, this repository contains **none** of the following, and
therefore carries no obligation regarding them:

- Live2D Cubism Core or the Cubism SDK,
- any Live2D model file (`.moc3`, `.model3.json`, textures, motions),
- NapCat or any injection kit,
- VCamNetSample or DirectN / DirectNCore code,
- the camera driver project (`.csproj`) or the web UI (`index.html`).

The words "Live2D", "QQ", "OBS", and "DeepSeek" appear in this repository only as
descriptive references to third-party software that the code interoperates with.
They remain the trademarks of their respective owners.

---

## 6. Windows API definitions

The `ctypes` declarations in `peer_audio_asr.py`, `process_loopback.py`,
`process_loopback_stream.py`, `p2_ds_enum.py`, and `audio_out.py` (structure
layouts, constants, COM GUIDs) are interface definitions required to call the
Windows API. They are not third-party source code and carry no attribution
obligation.

---

## 7. AI-assisted development

Parts of this codebase were written with the assistance of AI tools: **DeepSeek**
and **OpenAI Codex**. This is disclosed so that users can weigh it. The copyright
holder of this repository is `Melarizan`; the AI tools are not authors and are not
copyright holders. See `README.md` and `CONTRIBUTING.md`.

---

## 8. Reporting an attribution problem

If you believe this repository contains material that is misattributed or used
without permission, report it with the specific file and the source you believe it
came from. See `SECURITY.md` §6 for how to report without publishing sensitive
details.
