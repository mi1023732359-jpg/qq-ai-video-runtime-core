# Security

QQ AI Video Runtime Core — Experimental Source Preview — v0.1

Core is a **local developer tool**, not a hardened service. This page states the
threat model honestly so you can judge whether it fits your use.

---

## 1. Trust model

| Assumption | Reality |
|---|---|
| Runs as you | Yes — **same user privileges**, no elevation, no service account |
| Sandboxed | **No.** Core is a normal Python process. It is not isolated from your files or network. |
| Multi-user safe | **No.** Nothing in Core is hardened against other users or other processes on the same machine |
| Network-facing | **No.** All listening sockets bind `127.0.0.1` |
| Audited | Reviewed statically by a second model-based reviewer, plus a unit suite. Not a formal security audit |

**Anything running as your user can do what you can do, including call Core's
loopback ports.** The control token is a guard against accidental cross-origin
requests, not an authentication boundary between processes.

---

## 2. Security-relevant design decisions

1. **No shell-out from data.** `proc_capture_fix.QQ_PROCESS_QUERY` is a module
   **constant**; the SAPI synthesis script receives text via **stdin**. No
   caller-controlled value is interpolated into a command line anywhere in the
   tree.
2. **Strict PID validation.** `process_names()` accepts only a sequence of
   positive integers and returns an empty list for anything else. `bool` is
   rejected explicitly. It never spawns a process.
3. **Bounded capture queue.** The capture helper drops the *oldest* buffers beyond
   4 MiB and counts drops; the producer never blocks on capacity.
4. **Import does not mutate `sys.path`.** Bootstrap inserts live behind
   `if __name__ == "__main__":`. The only call-time inserts are the documented
   `QQAI_BOT_DIR` ones.
5. **No origin allowance.** `ALLOWED_ORIGINS` is empty: any request carrying an
   `Origin` header is rejected. Requests without one (local CLI) are allowed.
6. **Secrets are never printed.** `doctor.py` reports only booleans about shape;
   the API key is read from your file and used, never logged.

---

## 3. Known exposure you are accepting

| Exposure | Notes |
|---|---|
| **User-code execution** | `QQAI_BOT_DIR` modules are imported and run with your privileges. This is the largest risk in the project and it is by design. See `PRIVACY.md` §6. |
| **Process enumeration** | Core lists `QQ.exe`; the CIM query also selects `CommandLine`. Read-only. |
| **Sensitive logs** | Transcripts, replies, and a rolling raw-PCM buffer are written to `core/logs/` with no off switch and no retention limit. |
| **Audio capture scope** | Process-loopback captures the target process tree exactly; device-loopback captures everything the device plays. |
| **Loopback port reachability** | Same-user processes can reach `8770`/`3010` and can read `logs/control_token`. |
| **Outbound transcripts** | The dialogue loop sends transcripts to the model endpoint you configure. Core cannot control that provider's handling. |
| **Watchdog exit** | `asr_service` exits with code 42 when the worker stalls > `--stuck-timeout`; an external supervisor is assumed. Core ships none. |

---

## 4. What Core deliberately does *not* do

- Does not answer, click, type, or control QQ windows.
- Does not inject into, read memory of, or terminate other processes.
- Does not start or stop cameras.
- Does not auto-discover or load plugins.
- Does not accept connections from non-loopback interfaces.
- Does not ship a stop/kill path for its children.

If a future change adds any of these, it must not be presented as "Core v0.1".

---

## 5. Hardening suggestions for operators

1. Run in a dedicated Windows user account with minimal files.
2. Point `QQAI_BOT_DIR` only at code you have read.
3. Do not forward ports `8770` / `3010`, including through containers or proxies.
4. Treat `core/logs/` as sensitive: restrict ACLs on it if other users share the host.
5. Rotate the model API key if you share logs or screenshots of `bot/config.json`.
6. Use a local model endpoint if you do not want transcripts leaving the machine.
7. Delete `core/logs/` before archiving or sharing a directory.

---

## 6. Reporting a vulnerability

This is an **experimental preview with no support commitment**. If you find a
security defect:

1. Do **not** open a public issue containing exploit details, logs with transcripts, or API keys.
2. Report the affected file and line, the exact input that triggers it, and the observed effect.
3. Expect that fixes may take time, or may not happen at all for a preview.

Please scrub conversation text and credentials from anything you share — the logs
very likely contain both.

---

## 7. Security invariants that must not regress

Any future change to Core that breaks one of these is a regression, not a
refactor:

| Invariant | Checked by |
|---|---|
| `process_names()` never spawns a process and never accepts non-int PIDs | `test_core_hardening.InjectionTests` |
| No module-level `sys.path.insert` outside the `__main__` guard | `test_core_hardening.SysPathCleanTests` |
| Capture queue enforces its byte cap and counts drops | `test_core_hardening.BoundedQueueTests` |
| Toolhelp HANDLE calls declare `restype` and `argtypes` | `test_core_hardening.InjectionTests.test_toolhelp_handles_have_explicit_prototypes` |
| No physical input, no foreign process termination | enforced by design; a code scan for those primitives returns zero hits |
