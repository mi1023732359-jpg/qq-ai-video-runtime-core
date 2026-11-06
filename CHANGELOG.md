# Changelog

All notable changes to QQ AI Video Runtime Core.

This is the first published form of the tree. There is no earlier public release
to compare against — entries below describe how the current Core snapshot was
derived, not a history of shipped versions.

## Unreleased — Core v0.1.0 (Experimental Source Preview)

### Added

- Core extraction: 20 runtime modules (13 under `core/r2/`, 7 under
  `core/video_runtime/`) plus a start-only `core/launch_core.py`.
- Unit test suite: 90 cases across 6 files under `core/r3/tests/`.
- Documentation set: `INSTALLATION.md`, `CONFIGURATION.md`, `KNOWN_ISSUES.md`,
  `TEST_REPORT.md`, `PRIVACY.md`, `DEPENDENCIES.md`, `SECURITY.md`,
  `CONTRIBUTING.md`, `CHANGELOG.md`, `ROADMAP.md`.
- Dependency declaration: `requirements.txt` (hard) and
  `requirements-optional.txt` (lazy).
- **License**: `LICENSE` (MIT, `Copyright (c) 2026 Melarizan`), plus
  `THIRD_PARTY_NOTICES.md` separating third-party rights from the project's own
  grant.
- Provenance and decision records: `LICENSE_FINAL_AUDIT.md`,
  `LICENSE_HUMAN_DECISION.md`.

### Fixed

- **FIX-INJECT-1** (`core/r2/peer_audio_asr.py`) — command injection. `pids` were
  interpolated into a PowerShell command string. The entire subprocess path was
  removed and replaced by a read-only Toolhelp32 snapshot via `ctypes`;
  `process_names()` now validates strictly and returns `[]` on invalid input.
- **FIX-QUEUE-1** (`core/r2/proc_capture_fix.py`) — unbounded capture buffer. The
  queue is now capped at 4 MiB with 1 MiB per-buffer limit, drop-oldest policy,
  and drop counters exposed through `stats()`.
- **FIX-SYSPATH-1** — module-level `sys.path.insert(...)` removed from
  `asr_service.py`, `conversation_loop.py`, `doctor.py`, `mini_control.py`,
  `peer_audio_asr.py`, and `video_runtime/process_loopback_stream.py`. Bootstrap
  now lives behind `if __name__ == "__main__":`. Importing Core no longer mutates
  `sys.path`.
- **FIX-HANDLE-1** (`core/r2/peer_audio_asr.py`) — Toolhelp32 handle truncation.
  `CreateToolhelp32Snapshot`, `Process32FirstW`, `Process32NextW`, and
  `CloseHandle` now declare explicit `restype`/`argtypes` (HANDLE is
  pointer-sized; the ctypes default is C `int`). The invalid-handle check now
  compares against `INVALID_HANDLE_VALUE` instead of `-1`, and
  `th32DefaultHeapID` is typed `c_size_t`.

### Security

- Empty `ALLOWED_ORIGINS`: any request carrying an `Origin` header to the control
  plane is rejected.
- PID input validated as positive integers only; `bool` explicitly rejected.
- Full data-flow disclosure in `PRIVACY.md`, including local transcript storage,
  the outbound model request, and user-code loading via `QQAI_BOT_DIR`.

### Known limitations carried into this release

- No auto-answer, no physical input, no camera termination, no watchdog, no
  one-key runtime, no plugin loader.
- A real QQ video call has not been accepted end-to-end from this tree.
- The ~13–16 FPS behaviour is unexplained; no fix is claimed.
- Long-utterance ASR latency has not been adequately validated.
- The full runtime's auto-answer / camera features are not part of this tree.

### Notes

- `core/r2/asr_service.py` exits with code 42 when the ASR worker stalls beyond
  `--stuck-timeout`. This is the designed watchdog-restart contract; Core ships no
  supervisor.
- `video_runtime/process_loopback.py` still uses `wmic` for process-tree expansion;
  `wmic` is deprecated and may be absent on newer Windows builds.
