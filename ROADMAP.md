# Roadmap

QQ AI Video Runtime Core — Experimental Source Preview — v0.1

This is **not a commitment**. No dates, no promised features. Items are listed so
readers know what has been *considered*, not what will happen.

## Licensing — resolved

MIT, `Copyright (c) 2026 Melarizan`. This no longer blocks anything. See
`LICENSE`, `THIRD_PARTY_NOTICES.md`, and `LICENSE_HUMAN_DECISION.md` for the
decision record.

## Genuinely open, in rough order of value

1. **Replace `wmic` with Toolhelp** in `video_runtime/process_loopback.py`
   (deprecated command; `peer_audio_asr._process_table` already shows the pattern).
2. **Bound the base `ProcessLoopbackSource._frames`** the way
   `proc_capture_fix` does — the base class is currently uncapped.
3. **Diagnose the ~13–16 FPS behaviour.** Currently unexplained. No cause has been
   identified, so no work is scoped.
4. **Measure long-utterance ASR latency** under sustained load, and publish the
   numbers or admit they are absent.
5. **Give logging a switch.** `PRIVACY.md` §7 documents that v0.1 always logs.
   A documented off switch would make the privacy story much stronger.
6. **End-to-end call acceptance** on a real QQ call, with evidence — this is what
   would move Core from "experimental preview" to "usable".

## Explicitly not planned for Core

These belong to separate products and are listed so nobody waits for them:

- auto-answer / call acceptance automation,
- physical input control (mouse/keyboard injection),
- camera process termination or lifecycle management,
- watchdog / auto-restart supervision,
- a plugin or add-on loader,
- a GUI, installer, tray app, or one-click runtime.

Core is the safe slice. If any of the above appears later, it will be a different
distribution with its own review, not a "Core v0.2 feature".

## Versioning

No versioning policy is defined yet. The tree is identified as
"Experimental Source Preview v0.1"; this may be abandoned rather than continued.
