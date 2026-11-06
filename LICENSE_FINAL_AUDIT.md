# License Final Audit

QQ AI Video Runtime Core — Experimental Source Preview — v0.1

**Tree**: `core_v0.1_final_20261106` — this package. Its exact file count, byte
size and tree hash are recorded in the release manifest, not here, so that editing
this document cannot invalidate its own fingerprint.
**Date**: 2026-11-06
**Scope**: provenance and licensing of every file in the release candidate.

> **Update (2026-11-06):** the licensing decision was made after this audit was
> written. `LICENSE` now exists with the MIT text and
> `Copyright (c) 2026 Melarizan`; third-party rights are kept separate in
> `THIRD_PARTY_NOTICES.md`. Sections below that describe the holder as undecided
> are the **pre-decision** state and are preserved as the audit trail.

---

## 1. What is in the tree

| Kind | Count | Notes |
|---|---:|---|
| Python source | 27 | 20 runtime modules + `launch_core.py` + 6 test files |
| Markdown documentation | 13 | includes 12 added this round |
| Requirement files | 2 | `requirements.txt`, `requirements-optional.txt` |
| `.gitignore` | 1 | generic |
| Binary / model / image / DLL / EXE / IDE project | **0** | no assets of any kind |

Extension census confirms it: only `.py`, `.md`, `.txt`, `.gitignore` — **no
binary content anywhere in the tree**.

---

## 2. Copyright headers

Scanned the first lines of all 27 `.py` files:

| Finding | Result |
|---|---|
| `# Copyright` lines | **0** |
| SPDX identifiers | **0** |
| Author/company attribution lines | **0** |
| License notices inside source | **0** |

Every file starts with a shebang and/or a docstring describing what the module
does. There is no per-file licensing metadata to reconcile.

---

## 3. Third-party code provenance

| Check | Result |
|---|---|
| Vendored library code (copied classes/functions from another project) | **none found** |
| Attribution comments (`based on`, `ported from`, `source:`, `adapted`) | **none found** |
| Links to external repositories in comments | **none found** |
| Copied test fixtures from another project | **none found** |

### 3.1 Windows API definitions are not third-party source

`peer_audio_asr.py`, `process_loopback.py`, `process_loopback_stream.py`,
`p2_ds_enum.py`, and `audio_out.py` contain `ctypes` declarations:
structure layouts (`PROCESSENTRY32W`, `WAVEFORMATEX`, `PROPERTYKEY`-like blobs,
`VARIANT` unions), constants (`TH32CS_SNAPPROCESS`, `AUDCLNT_*`, `CLSCTX_*`), and
COM GUIDs.

These are **interface facts** taken from published Windows API documentation —
the numeric values and binary layouts that the operating system requires in order
to interoperate. They are not copied from another project's source, and they
carry no third-party attribution obligation. Every declaration was written in this
project.

### 3.2 The SAPI PowerShell script

`video_runtime/audio_out.py` embeds a small PowerShell script that instantiates
`System.Speech.Synthesis.SpeechSynthesizer`. It is written here (a few lines of
standard SAPI boilerplate), receives the text via **stdin**, and is deleted after
use. Not copied from a third-party sample.

### 3.3 Explicitly excluded components

| Component | Present in tree? | Evidence |
|---|---|---|
| **Live2D Cubism Core / Cubism SDK** | **No** | "Live2D" appears only as a *description* of what the control plane drives (`mini_control.py`, `listening_feedback.py` docstrings) and as a test string. No Cubism header, no `live2dcubismcore` binary, no SDK binding. |
| **Public ("公皮") Live2D model files** | **No** | No `.moc3`, `.model3.json`, `.png`, `.mtn`, or any asset file exists. Zero binary files in the tree. |
| **NapCat / injection kit** | **No** | The string appears only twice, in `session_coordinator.py` docstring/comments stating that Core does **not** import QQAI, NapCat, or ADI. No NapCat code, no injection logic. |
| **VCamNetSample** | **No** | Zero occurrences of the name or related API usage. |
| **DirectN / DirectNCore** | **No** | Zero occurrences. DirectShow access is `ctypes` COM written here, not a DirectN binding. |
| **Camera driver (`.csproj`)** | **No** | `runtime_config.py` only *checks for the presence* of `r2/driver/Live2DCamDriver.csproj` to report it as missing. The file is not shipped. |
| **Web UI (`index.html`)** | **No** | Same: only a presence check. The page is not shipped. |

---

## 4. Dependency licensing

Runtime dependencies are **not vendored** — nothing is copied into this tree.

| Package | Version tested | License (package metadata) |
|---|---|---|
| `numpy` | 2.5.2 | `BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0` |
| `faster-whisper` | 1.2.1 | `MIT` |
| `PyAudioWPatch` | 0.2.12.9 | `Apache-2.0` |
| `sounddevice` | 0.5.6 | `MIT` |
| `voice_out` | not installed | **UNKNOWN** — user-supplied, outside this tree |

**NOTICE obligations:** distributing the *source tree* alone triggers none.
Distributing a *bundle* that includes these packages (e.g. a PyInstaller build)
would require carrying each project's license text and, for Apache-2.0
(`PyAudioWPatch`), its NOTICE if one is present.

`faster-whisper` downloads model weights at runtime. Those weights have their own
license terms (from the model publisher), which this project does not grant and
cannot audit. **Anyone shipping weights must check that license separately.**

---

## 5. Authorship facts relevant to copyright

Two facts must be on the record before a copyright line is written.

### 5.1 AI-assisted code is present

`video_runtime/audio_out.py` carries the comments `# 由 Codex 实现/审查`
(implemented/reviewed by Codex). Other modules were developed with AI assistance
across the project's history.

Copyright law in several jurisdictions requires **human authorship** for a work to
be protected. How much human authorship is required, and whether AI-assisted
portions are protected at all, is a legal question this audit cannot answer. It
is a decision item, not a technical check.

### 5.2 No in-tree authorship record

The tree contains no git history, no `AUTHORS` file, and no contributor list. Who
wrote what must therefore be established from the project's own records, not from
the release candidate.

---

## 6. Answers to the six required questions

**Q1. Which code can the copyright holder license?**
All 27 Python files, 13 Markdown files, 2 requirement files, and `.gitignore`, to
the extent the holder actually owns them. No third-party code is embedded, so
there is no foreign copyright to clear for the source tree itself. The AI-assisted
portions (see §5.1) need a human legal judgment before the whole can be licensed
as a unified work.

**Q2. Are there other contributors?**
**No contributor attribution exists in the tree, and none was found in the code.**
That is not proof there are no contributors — it means the tree carries no record
either way. This must be answered by the human from project history.

**Q3. Is there code of unknown origin?**
**None found.** Every file was authored within this project's development; the
only "foreign" material is Windows API interface facts (§3.1), which are not
third-party source. Confidence: high for the Python code, because the files are
recent, self-contained, and stylistically consistent.

**Q4. Must any third-party NOTICE be preserved?**
**Not for the source tree.** If a binary distribution bundles `numpy` (BSD-3),
`faster-whisper` (MIT), `PyAudioWPatch` (Apache-2.0), or `sounddevice` (MIT), then
yes — each license text (and Apache-2.0 NOTICE) must accompany the bundle. Model
weights from `faster-whisper` carry a separate license.

**Q5. Which license is recommended?**
See `LICENSE_HUMAN_DECISION.md` for the options and trade-offs. This audit
recommends **not** deciding until the two facts in §5 are settled, and offers
Apache-2.0 and MIT as the two defensible candidates for a preview of this shape.

**Q6. How should the copyright line read?**
It cannot be written yet. The form is
`Copyright (c) <year> <holder>`, and both fields are unresolved. **No placeholder
has been inserted** and none should be — a wrong holder is worse than no license.

---

## 7. Non-copyright considerations for public release

Not legal advice, but these belong in the record:

1. **Interoperability and terms of service.** Core captures another application's
   audio and drives an AI avatar during a call. Whether that is permitted by the
   relevant service's terms is a question about *that service*, not about
   copyright, and this tree does not answer it.
2. **Recording consent.** In many jurisdictions, capturing and transcribing
   another person's speech requires their consent. `PRIVACY.md` discloses what the
   software does; obtaining consent is the operator's obligation.
3. **Model weights.** Whatever `faster-whisper` downloads has its own license and
   acceptable-use terms.
4. **Trademarks.** "Live2D", "QQ", "OBS", "DeepSeek" are third-party marks. Naming
   them descriptively is not a license to use them as branding.

---

## 8. Audit conclusion

| Item | Status |
|---|---|
| Vendored third-party code | none |
| Required third-party NOTICE (source tree) | none |
| Excluded components confirmed absent | Live2D Cubism, model files, NapCat, VCamNetSample, DirectN |
| Copyright headers present | none |
| Copyright holder determined | **YES — `Melarizan`** (real name not published) |
| LICENSE file created | **YES — MIT** |
| Third-party rights separated | **YES — `THIRD_PARTY_NOTICES.md`** |
| Blocking issue for public release | **none from licensing** |
