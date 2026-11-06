# Privacy

QQ AI Video Runtime Core — Experimental Source Preview — v0.1

This document describes **what this code actually does with data**, derived by
reading the source in this tree. It is not a marketing summary and it does not
describe software this tree does not contain.

Where a behaviour could not be established from the code, it is marked
**UNKNOWN** rather than guessed.

> Scope note: this document covers **Core only**. The QQ client, your AI model
> provider, OBS, the Live2D renderer, and any module inside `QQAI_BOT_DIR` are
> separate software with their own data practices. Their behaviour is not
> described — and not vouched for — here.

---

## 0. Short version

- Audio is captured locally and transcribed **locally** by `faster-whisper`.
  **No audio file is uploaded by Core.**
- The **transcribed text** is sent over HTTPS to the model endpoint **you**
  configure in `bot/config.json`, authenticated with **your** API key. That is the
  only outbound application data flow.
- Transcription results, full conversation turns, and a rolling raw-PCM buffer are
  written to `core/logs/` on disk and are **not** deleted automatically.
- A **control credential** (`logs/control_token`) is stored on disk.
- `QQAI_BOT_DIR` causes code from that directory to be imported and executed with
  your user privileges.
- There is **no runtime switch to turn logging off** in v0.1. You control it by
  filesystem permissions or by deleting `logs/`.

---

## 1. Audio

### 1.1 Where audio comes from

| Path | Source | Code |
|---|---|---|
| Process loopback | The audio of the target process **tree** (intended: a running QQ client), captured through `ActivateAudioInterfaceAsync` + `PROCESS_LOOPBACK` | `core/video_runtime/process_loopback_stream.py`, `core/r2/proc_capture_fix.py` |
| Device loopback | The default playback device's loopback stream (falls back to whatever the device mixes) | `core/video_runtime/audio_capture.py` |

Process loopback captures exactly the process tree you point it at. Device
loopback captures everything the device plays, which may include other
applications. Choose deliberately.

### 1.2 In-memory handling

- Captured buffers are queued in RAM as raw bytes for the ASR consumer.
- `proc_capture_fix.FixedProcessLoopbackSource` bounds its queue at **4 MiB**
  (~10.9 s at 48 kHz stereo int16) and drops the **oldest** buffers when full.
- `process_loopback_stream.ProcessLoopbackSource` (the base class) **does not
  bound** its `_frames` list. The bounded subclass is the one used by the capture
  helpers. Using the base class directly with a stalled consumer can grow memory.

### 1.3 Is audio sent anywhere?

**No.** Audio is written to a local WAV and passed to the local `faster-whisper`
model. Core performs no audio upload.

**Exception to be aware of:** `faster-whisper` **downloads** model weights over
the network on first use (see §5.3). That is a download, not an upload of your
audio.

---

## 2. Everything Core writes to disk

The table below lists the **default** locations of the files **Core itself**
creates. `core/logs/` is the default root for all of them; a `--wav` argument or
one of the constructor overrides below moves specific outputs elsewhere.

Not every write on this machine comes from Core: §2.9 covers files written by
dependencies, whose location Core does not choose.

**Paths are not fixed.** Several public constructors accept a caller-supplied
output location, so code that instantiates them can move data outside `logs/`:

| Constructor | Overridable location |
|---|---|
| `dialogue.DialogueService(..., log_path=...)` | dialogue log file |
| `session_coordinator.SessionCoordinator(store_root=..., log_path=...)` | session store root and coordinator log |
| `asr_in.WhisperTranscriber(..., tmp_dir=..., download_root=...)` | temporary WAV directory and model cache |
| `asr_in.AsrGate(..., log_path=...)` | ASR gate log |
| `pcm_ring.PcmRingWriter(path=..., stats_path=...)` | raw PCM ring file and its stats file |
| `listening_feedback` / `avatar_decision.AvatarRuntime(..., log_path=...)` | feedback and avatar log files |

`mini_control`, `asr_service`, `peer_audio_asr`, and the `process_loopback*`
evidence writers use a module-level `logs/` constant and are **not** relocatable
without editing the code.

`doctor.py` writes **no file at all** — it only prints a report. `qqai_memory.py`
defines a `LOG_DIR` constant but never writes through it; its archive writes go to
your QQAI installation instead (§2.8).

The tables in §2.1–§2.5 are the complete inventory of write sites found in Core's
own code.

### 2.1 Audio and audio-derived content

| File | Written by | Content | Sensitive | Lifecycle |
|---|---|---|---|---|
| `logs/pcm_ring.bin` | `core/r2/pcm_ring.py` (instantiated at `conversation_loop.py:447`) | Rolling **raw PCM** buffer of captured audio | **Yes — raw audio** | Persists after exit |
| `logs/pcm_ring_stats.json` | `core/r2/pcm_ring.py:126` | Counters, rate, timestamps | Low | Persists |
| `logs/asr_tmp/<uuid>.wav` | `core/video_runtime/asr_in.py:118` | One **raw WAV** per transcription, passed to `faster-whisper` | **Yes — raw audio** | Deleted in `finally`; a hard crash can leave one behind |
| `logs/audio/<uuid>.wav` | `core/video_runtime/audio_out.py` | Synthesised **speech output** | **Yes — speech** | Persists (never cleaned up) |
| `logs/audio/<uuid>.ps1` | `core/video_runtime/audio_out.py:168` | Temporary SAPI script; **no user text** (text goes via stdin) | No | Deleted in `finally` |
| `logs/tts_tmp/` | `core/r2/mini_control.py:331` | Temporary directory for the control plane's TTS path | Yes — speech | Managed by that code path |
| a path you pass to `--wav` | `core/r2/peer_audio_asr.py:224` | **Raw WAV** of captured audio | **Yes — raw audio** | Persists where you put it |

### 2.2 Transcription output

| File | Written by | Content | Sensitive | Lifecycle |
|---|---|---|---|---|
| `logs/peer_asr.json` | `core/r2/peer_audio_asr.py:228` | `{"at", "pids", "texts"}` — **the transcribed text array** | **Yes — transcript** | Overwritten per run; persists |

### 2.3 Conversation records

| File | Content | Sensitive |
|---|---|---|
| `logs/conversation_trace.jsonl` | Event stream: `heard` / `reply` / `dropped` records with the **full transcript text** and the AI reply (`conversation_loop.py:412-423, 810, 833`) | **Yes — full conversation** |
| `logs/conversation_loop.json` | Session summary with `rounds` (`user`, `reply`), `speak_log`, feedback snapshot, avatar history (`conversation_loop.py:830, 864`) | **Yes — full conversation** |
| `logs/dialogue_loop.jsonl` | Dialogue turn log (`conversation_loop.py:491`) | Turn metadata, may include text |
| `logs/dialogue.jsonl` | Dialogue service log (`dialogue.py:61`) | Turn metadata |
| `logs/dialogue_store/sessions/<scope>/<key>/<kind>.jsonl` | Session store: `history`, `world`, `archive`, `summary`, `facts` (`session_coordinator.py:115-146`; root set at `conversation_loop.py:462`) | **Yes — `history` holds user and AI text** |
| `logs/coordinator_store/sessions/video/<sid>/history.jsonl` | One record per turn: `{"turn","user","ai","emotion","at"}` (`session_coordinator.py:401-410`) | **Yes — full conversation** |
| `logs/coordinator.jsonl` | Call lifecycle events, caller id, call id, decisions (`session_coordinator.py:162`) | Caller identifiers |
| `logs/coordinator_store/coordinator_state.json` | Current call state and send lease (written atomically via `.tmp` + `os.replace`) | Caller identifiers |

### 2.4 Service, diagnostic and evidence files

| File | Written by | Content | Sensitive |
|---|---|---|---|
| `logs/asr_service.json` | `core/r2/asr_service.py:35,71` | Service state (`initializing`/`ready`/`failed`), pid, port, `queue_max`, error text | Low |
| `logs/asr_queue_stats.json` | `core/r2/asr_service.py:60` | Queue depth, in-flight timing, `maxsize` | Low |
| `logs/asr_in.jsonl` | `core/video_runtime/asr_in.py:33,168` | ASR gate state transitions, frame counts, error strings | Mostly metrics |
| `logs/listening_feedback.jsonl` | `core/r2/listening_feedback.py:33` | Listening-feedback state events | State metadata |
| `logs/avatar_runtime.jsonl` | `core/r2/avatar_decision.py:113` | Expression/action decisions, HTTP failures | Low |
| `logs/process_loopback.json` | `core/video_runtime/process_loopback.py:36,141` | Capability probe evidence: Windows build, support verdict, **PID lists** | Low–medium |
| `logs/process_loopback_stream.json` | `core/video_runtime/process_loopback_stream.py:30,400` | Stream probe evidence: native rate/channels, **PID** | Low–medium |
| `logs/p2_ds_enum.json` | `core/r2/p2_ds_enum.py:166` | DirectShow camera **FriendlyName list** | Low |
| `logs/mini_control_selftest.json` | `core/r2/mini_control.py:637` | Control-plane self-test result (endpoint reachability) | Low |

### 2.5 Credential on disk

| File | Content | Protection |
|---|---|---|
| `logs/control_token` | The control-plane token, read and written by `core/r2/mini_control.py:52,74`. Sent as the `X-ADI-Control-Token` header. | Written with `os.chmod(..., 0o600)` (`mini_control.py:76`) — owner-only on filesystems that honour POSIX modes. **On Windows NTFS that flag is not equivalent to an ACL**; treat the file as readable by any process running as you. |

This token is a local convenience guard, **not** an authentication boundary. Any
same-user process can read it and call `127.0.0.1:8770`.

### 2.6 Credentials that may appear in conversation logs

Core does **not** log your API key, and does not read `OBS_WS_PASSWORD`'s value.
But the conversation logs record **whatever text was spoken or returned**:

- if you speak a password, card number, or address, it is transcribed and stored
  in `conversation_trace.jsonl`, `conversation_loop.json`, and the session
  `history` files;
- if you paste a secret into a prompt, it can appear in the model's reply and be
  stored the same way;
- if the model echoes a credential, it is stored as part of the reply.

**Treat everything under `core/logs/` as potentially containing secrets.** There
is no redaction and no PII filtering anywhere in Core.

### 2.7 Retention

**Nothing in Core deletes these files.** No rotation, no TTL, no size cap.
`logs/` grows until you remove it. The only automatic deletion is the transient
`asr_tmp/*.wav` and the temporary `.ps1` script, both in `finally` blocks.

### 2.8 Third-party storage

Your QQAI installation (the directory behind `QQAI_BOT_DIR`) also receives call
records: `qqai_memory.archive_call_turn()` writes
`【视频通话】对方：<user_text>\nAI：<reply>` into the QQAI memory archive, tagged
`etype=video_call`. That store belongs to your QQAI installation, and its
retention rules are **not** governed by Core.

### 2.9 Files written by dependencies, not by Core

| What | Written by | Where | User data? |
|---|---|---|---|
| `faster-whisper` model weights | the `faster_whisper` library, on the first transcription | `download_root` if you passed one, otherwise the library's own default cache directory. **This is not necessarily under `core/logs/`**, and Core does not choose it. | No — model weights, not your audio |
| Installed packages | `pip` at install time | your environment's `site-packages` | No |

Core neither controls nor cleans these locations.

---

## 3. Conversation egress

### 3.1 What is sent to the model

`core/r2/conversation_loop.py:65-79` (`DeepSeekBrain.respond`) sends an HTTP
`POST` to the `base_url` from your `bot/config.json` provider entry, with
`Authorization: Bearer <your api_key>`, and this JSON body:

```json
{
  "model": "<provider model or 'deepseek-chat'>",
  "messages": [
    {"role": "system", "content": "<persona, plus injected 【已知事实】 lines>"},
    {"role": "user",   "content": "<the transcribed speech of the caller>"}
  ],
  "temperature": 0.8,
  "max_tokens": 120
}
```

So the following leave your machine:

1. **The transcribed speech** — i.e. what was said, as text.
2. **The persona text** (a default persona string or one taken from your QQAI
   installation).
3. **Injected "known facts"** — `qqai_memory.facts_for(scope_uid, text)` appends
   fact lines from your QQAI fact store to the system message before the call
   (`conversation_loop.py:815-819`).
4. **Your API key** in the `Authorization` header.

There is no redaction, filtering, or PII scrubbing. If no provider is configured,
`DeepSeekBrain` returns a placeholder reply and sends nothing
(`conversation_loop.py:66-68`).

---

## 4. Process information

### 4.1 What is enumerated

Core lists processes named `QQ.exe` to find the audio capture target:

| Method | Location | Fields |
|---|---|---|
| Toolhelp32 snapshot (`ctypes`) | `core/r2/peer_audio_asr.py:_process_table` | PID, parent PID, executable name |
| `tasklist /FI "IMAGENAME eq QQ.exe"` | `core/video_runtime/process_loopback.py:84` | PID, name |
| `wmic process get ProcessId,ParentProcessId` | `core/video_runtime/process_loopback.py:103` | PID, parent PID |
| PowerShell CIM constant query | `core/r2/proc_capture_fix.py:51` | `ProcessId`, `ParentProcessId`, **`CommandLine`** |

The CIM query selects `CommandLine`. A command line can contain paths, user
names, and arguments. Core parses it for PID selection and **does not write it to
disk**; only PIDs reach the evidence files. Treat it as sensitive if you add
logging around `qq_processes()`.

### 4.2 Is it stored or transmitted?

- PIDs are stored in `logs/process_loopback.json` /
  `logs/process_loopback_stream.json` and echoed to stdout.
- Process information is **not** sent to any external service.

### 4.3 What is *not* done

Core does not open, read, inject into, or terminate the target process. It does
not read the target's memory or files. It only observes the process list and
captures that process tree's audio output.

---

## 5. External services

### 5.1 Local (loopback only)

| Endpoint | Purpose | Bound to |
|---|---|---|
| `127.0.0.1:8770` | Live2D expression/mouth control plane | loopback |
| `127.0.0.1:3010` | ASR service | loopback |

See §2.5 about the control token.

### 5.2 Your model provider (network egress)

The only outbound **application data** is the HTTPS `POST` described in §3.1, to
the `base_url` **you** configured. Core does not pick a provider, has no default
endpoint, and proxies nothing else.

Separately, dependency code performs a **download** (not an upload) on first use —
see §5.3. That is not application data leaving your machine; it is a library
fetching model weights.

What that provider retains, and whether it trains on your data, is governed by
**that provider's policy** — **UNKNOWN** from Core's perspective.

### 5.3 Model download

`faster-whisper` downloads model weights on first use. The destination is
`download_root` if configured, otherwise the library's default location. The
download is performed by that library under its own terms.

### 5.4 Third-party software with its own data handling

| Software | Relationship to Core |
|---|---|
| QQ client | Core only observes its process list and captures its audio; QQ's own data practices are separate |
| OBS Virtual Camera | A device Core may select by name; OBS's practices are separate |
| Live2D model / renderer | **not included** in this tree; separate software |
| Modules in `QQAI_BOT_DIR` | user-supplied; see §6 |

Core makes **no claim** about any of these parties' privacy practices.

---

## 6. User-supplied code (`QQAI_BOT_DIR` / `QQAI_BOT_CONFIG`)

This is the highest-risk feature in Core and it is **by design**.

1. `runtime_config.resolve_bot_config()` resolves a directory from
   `QQAI_BOT_DIR` or `QQAI_BOT_CONFIG` (mutually exclusive; both set is an error).
   There is no fallback to a hard-coded personal path.
2. `core/r2/qqai_memory.py` inserts that directory into `sys.path` and imports
   these fixed module names: `memory_archive`, `facts`, `main_persona`,
   `persona_layers`, `persona_history`, `reply_style`, `world`.
3. `core/r2/mini_control.py` inserts the same directory into `sys.path`, changes
   the working directory to it, and imports `voice_out`.

**Code in that directory is imported and executed with your user privileges.** It
can read your files, open network connections, and modify anything you can. Core
performs no sandboxing, no signature check, and no review of that code.

Control it by pointing the variable only at code you trust, or leaving it unset
(Core still imports; QQAI-dependent features degrade). The insertion happens at
**call time**, so importing Core modules does not execute that code.

---

## 7. Your controls

| Goal | How, in v0.1 |
|---|---|
| Stop sending text to an AI provider | Do not configure `providers` in `bot/config.json`, or point `base_url` at a local model you run yourself |
| Stop audio capture | Do not run the capture paths (`conversation_loop`, `peer_audio_asr`, `launch_core`) |
| Avoid persisting audio | Make `logs/` read-only, or delete `logs/pcm_ring.bin`, `logs/audio/`, `logs/tts_tmp/`, `logs/asr_tmp/` |
| Avoid persisting transcripts | Delete `logs/peer_asr.json`, `logs/conversation_trace.jsonl`, `logs/conversation_loop.json`, `logs/dialogue_store/`, `logs/coordinator_store/` |
| Remove the control credential | Delete `logs/control_token`; a new one is generated next run |
| Delete all local data Core produced | Delete the whole `core/logs/` directory while the runtime is stopped |
| Disable user-code loading | Unset both `QQAI_BOT_DIR` and `QQAI_BOT_CONFIG` |
| Keep the control plane off the network | It binds loopback only; do not expose or forward `8770` / `3010` |

### 7.1 What cannot be controlled from Core

- **There is no configuration flag that turns logging off.** v0.1 always writes
  the files in §2.
- Core cannot control what your model provider retains.
- Core cannot control what QQ, OBS, or your `QQAI_BOT_DIR` code does.
- Audio routed through the OS mixer (device-loopback mode) may be observed by
  other software with audio access; that is an operating-system property.
- A crashed process may leave a partial `logs/asr_tmp/*.wav`; Core runs no cleanup
  sweep at startup.
- The `0o600` mode on `logs/control_token` does not on its own restrict access on
  Windows NTFS.

---

## 8. Explicitly marked UNKNOWN

| Question | Status |
|---|---|
| What your AI provider does with transcripts and API keys | **UNKNOWN** — see their policy |
| Whether your QQAI installation syncs `video_call` archive entries elsewhere | **UNKNOWN** — outside this tree |
| Exact default cache path used by `faster-whisper` when `download_root` is unset | **UNKNOWN** — supplied by that library |
| Whether the OS retains audio in pagefile, hibernation files, or crash dumps | **UNKNOWN** — operating-system behaviour, out of scope |
| Whether any QQ client version writes its own copy of call audio | **UNKNOWN** — QQ behaviour, not observable from this code |
| Whether any module inside `QQAI_BOT_DIR` transmits the data it receives | **UNKNOWN** — user-supplied code |

---

## 9. Honest summary of the risk

If you run the dialogue loop with a remote AI provider configured, then:

- what the other party says is transcribed locally,
- the transcript plus persona plus injected facts are sent to that provider,
- and the full conversation is written to `core/logs/` indefinitely,
- alongside a rolling raw-audio buffer and the synthesised speech files,
- and a control credential sits in the same directory.

If that is not acceptable, configure a local model, restrict or delete `logs/`, or
do not run the dialogue loop. Nothing in Core mitigates this for you — it is the
intended function.
