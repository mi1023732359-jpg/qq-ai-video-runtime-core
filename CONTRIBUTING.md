# Contributing

QQ AI Video Runtime Core — Experimental Source Preview — v0.1

## License and contribution terms

This project is **MIT licensed** — see [`LICENSE`](LICENSE) (`Copyright (c) 2026
Melarizan`). By submitting a contribution you agree that it may be distributed
under the same MIT terms.

There is currently **no separate CLA and no DCO sign-off requirement**. Do not
assume that will stay true forever; if terms are introduced later, they will be
announced in this file before they take effect.

The MIT license covers this project's own code and documentation only. It does not
grant rights over third-party packages, model weights, or the software this code
interoperates with — see [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).

### AI-assisted contributions

Parts of this codebase were written with the assistance of **DeepSeek** and
**OpenAI Codex**. Disclosure is appreciated but not required for contributions. The
copyright holder remains Melarizan; AI tools are not authors.

---

## What is in scope

Fixes and small improvements that keep Core within its stated boundary:

- correctness bugs in existing modules,
- security hardening that removes capability (not adds it),
- test coverage for existing behaviour,
- documentation accuracy.

## What is out of scope (by design, not by policy)

These are **separate products**, not Core:

- auto-answer / call acceptance,
- physical mouse or keyboard control,
- camera process termination / lifecycle management,
- watchdog / auto-restart supervisors,
- a plugin loader or add-on system,
- a one-click runtime or GUI.

A change that adds any of the above is not a Core change. Do not submit it here.

---

## Project invariants — do not regress these

These were established by security review and are guarded by tests. A change that
breaks one is a regression regardless of how useful it looks.

1. **No shell-out from data.** No caller-controlled value may reach a command
   line. Existing shell usage must stay argv-separated, and text must go via
   stdin, never interpolated into script source.
2. **`process_names()` stays pure.** It accepts only a sequence of positive
   integers, rejects `bool`, returns `[]` on invalid input, and spawns nothing.
3. **The capture queue stays bounded.** Byte cap enforced on every enqueue path,
   oldest-first drop, drops counted, producer never blocks on capacity.
4. **Import must not mutate `sys.path`.** Bootstrap inserts belong inside
   `if __name__ == "__main__":`. The only call-time inserts are the documented
   `QQAI_BOT_DIR` ones in `qqai_memory.py` and `mini_control.py`.
5. **No physical input, no foreign process termination, no camera control.**
6. **Loopback only.** Nothing may bind a non-loopback interface.
7. **No new hard dependency without justification.** `numpy` is the only one.
   Anything else must be a lazy import inside a function.
8. **Tests must keep passing.** The suite is the contract.

---

## How to run the checks locally

```powershell
cd core

# full suite (must be green)
python -B -X utf8 -m unittest discover -s r3/tests -p "test_*.py"

# import sanity for all 20 modules (each in its own interpreter)
python -B -X utf8 -c "import sys; sys.path[:0]=['.', './r2']; import asr_client, asr_service, avatar_decision, conversation_loop, doctor, listening_feedback, mini_control, p2_ds_enum, pcm_ring, peer_audio_asr, proc_capture_fix, qqai_memory, runtime_config; print('r2 OK')"
python -B -X utf8 -c "import video_runtime.asr_in, video_runtime.audio_capture, video_runtime.audio_out, video_runtime.dialogue, video_runtime.process_loopback, video_runtime.process_loopback_stream, video_runtime.session_coordinator; print('video_runtime OK')"
```

Do not weaken a test to make it pass. If an existing test encodes an invariant you
believe is wrong, explain why in the issue instead of deleting the assertion.

---

## Reporting a defect

Include:

1. the exact command line,
2. Windows build and Python version,
3. the smallest reproduction you can manage,
4. the relevant evidence file from `core/logs/`.

**Scrub conversation text and API keys before sharing** — the logs contain both.

For security issues, read `SECURITY.md` §6 first.

---

## Style

- Keep the existing module boundaries: `r2/` for runtime modules,
  `video_runtime/` for the audio/COM layer, `r3/tests/` for tests.
- Prefer standard library over new dependencies.
- Comments that explain *why* (especially a hardware/COM quirk) are valued over
  comments that restate the code.
- Do not add a feature flag that silently enables a capability the project has
  deliberately excluded.
