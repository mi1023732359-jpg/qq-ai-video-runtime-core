# Configuration

QQ AI Video Runtime Core — Experimental Source Preview — v0.1

Core has **no configuration file of its own**. It reads environment variables plus
one file that belongs to your QQAI installation (`bot/config.json`, read-only).

---

## 1. Environment variables

| Variable | Required | Meaning |
|---|---|---|
| `QQAI_BOT_CONFIG` | one of these two | Absolute/relative path to a **file** (`config.json`). The bot directory becomes that file's parent. |
| `QQAI_BOT_DIR` | one of these two | Path to an **installation directory**. Core looks for `config.json` or `bot/config.json` inside it. |
| `QQAI_CAMERA_NAME` | optional | DirectShow **FriendlyName** to select. Default: `OBS Virtual Camera`. |
| `QQAI_CONTROL_TOKEN` | optional | Overrides the per-run control-plane token. If unset, a token is generated and stored in `logs/control_token`. |
| `OBS_WS_PASSWORD` | not used by Core | Only its **presence** is reported by `doctor.py`; the value is never read, printed, or stored by Core. |

### `QQAI_BOT_CONFIG` and `QQAI_BOT_DIR` are mutually exclusive

Setting both raises `ConfigError("config_conflict")`. This is intentional: the two
names can point at different installations, and silently picking one could drive
the wrong account. There is **no fallback to a hard-coded personal path** — if
neither is set, Core reports `config_missing` instead of guessing.

```powershell
# correct: exactly one
$env:QQAI_BOT_DIR = "D:\path\to\your\qqai"
# or
$env:QQAI_BOT_CONFIG = "D:\path\to\your\qqai\bot\config.json"
```

`QQAI_BOT_DIR` search order is `config.json`, then `bot/config.json`. If **both**
exist it raises `config_ambiguous` rather than choosing.

### Camera selection never falls back

`runtime_config.choose_camera_index()` matches the requested name
case-insensitively against DirectShow FriendlyNames. Zero matches →
`camera_not_found`. More than one match → `camera_ambiguous`. Core will not
silently pick index 0 or "the other" camera.

---

## 2. The `bot/config.json` fields Core reads

Core reads this file **read-only** and uses one section:

```jsonc
{
  "providers": {
    "DeepSeek-chat": {
      "base_url": "https://<your-provider>/<path>",
      "api_key": "<your key>",
      "model": "<model name>"
    }
  }
}
```

- `providers` — a dict. Core prefers the key `DeepSeek-chat`; if absent it takes
  the first provider entry.
- `base_url` — the OpenAI-compatible chat-completions endpoint to POST to.
- `api_key` — sent as `Authorization: Bearer <key>`.
- `model` — sent in the request body; falls back to `deepseek-chat` if missing.

If `providers` is absent or the file cannot be read, the brain returns a
placeholder reply and **sends nothing** to the network.

> The API key lives in your file, not in this tree. Core never writes it back and
> never logs it. Error paths return an exception string, and `doctor.py` reports
> only booleans about config shape.

Other keys in that file belong to your QQAI installation; Core ignores them.

---

## 3. Local ports

| Port | Service | Bound to | Auth |
|---|---|---|---|
| `8770` | Live2D control plane (`mini_control.py`) | `127.0.0.1` | `X-ADI-Control-Token` header on state-changing requests; `Origin`-bearing requests rejected (`ALLOWED_ORIGINS` is empty) |
| `3010` | ASR service (`asr_service.py`) | `127.0.0.1` | none (loopback, length-prefixed JSON protocol) |

Both are loopback-only. Do not forward or expose them. Same-user processes can
reach them; the token is not process isolation.

---

## 4. Command-line reference

All commands are run from the `core/` directory.

### `launch_core.py` — start-only launcher

```
--dry-run          print the plan, start nothing
--no-asr           skip the ASR service
--no-control       skip the control plane
--no-conversation  skip the dialogue loop
```

There is no `--stop`, no restart, and no plugin loading.

### `r2/asr_service.py` — resident ASR service

```
--port 3010            listen port
--model small          faster-whisper model size
--device cpu           cpu | cuda
--compute-type int8    quantization
--cpu-threads N        thread count (0 = library default)
--queue-max 8          bounded request queue
--stuck-timeout 30     worker watchdog threshold (seconds)
--prompt TEXT          initial_prompt hint for proper nouns
--status               print service state and exit
```

### `r2/mini_control.py` — control plane

```
--port 8770        listen port
--selftest         run the built-in loopback self-test and exit
--tts-device NAME  output device for TTS
--no-tts           disable TTS
```

### `r2/conversation_loop.py` — dialogue loop

```
--pid / --pids / --qq      capture target selection
--brain NAME               brain implementation
--seconds N                run duration
--chunk-seconds N          ASR segment length
--peer-uid UID             archive scope
--asr-prompt TEXT          ASR initial prompt
--max-seconds N            hard stop
--min-rms F                silence threshold
--no-speak                 disable TTS output
--no-listening-feedback    disable listening feedback
--listening-voice          enable voice acknowledgements
--listening-voice-delay S  delay before voice ack
--asr-service-port N       ASR service port
--asr-timeout S            ASR request timeout
--asr-max-audio S          maximum audio accepted per request
--no-asr-service           use the in-process transcriber instead
--greet                    greet on start
```

### `r2/peer_audio_asr.py` — capture-to-text helper

```
--qq                   select all QQ.exe processes
--pid N                single PID (repeatable)
--pids "1,2,3"         comma-separated PIDs
--seconds N            capture duration
--chunk-seconds N      segment length
--wav PATH             record to WAV instead of transcribing
--min-rms F            silence threshold
```

PID input is validated strictly: only positive integers. Anything else returns an
empty selection and executes nothing.

### `r2/doctor.py` — environment report

```
--json   machine-readable output
```

### `video_runtime/process_loopback.py` — OS capability probe

Takes no options. Prints the Windows build, `MIN_BUILD`, whether process loopback
is supported, and the fallback that exists. Read-only; creates no audio objects.

---

## 5. Where data is written

Everything Core writes goes under `core/logs/` unless you pass an explicit path
(`--wav`). The full inventory, with what each file contains and whether it is
sensitive, is in `PRIVACY.md` §2.

There is **no log-level or log-off switch** in v0.1.

---

## 6. Configuration errors and what they mean

| Code | Meaning | Fix |
|---|---|---|
| `config_missing` | neither `QQAI_BOT_CONFIG` nor `QQAI_BOT_DIR` set, or the named file does not exist | set exactly one |
| `config_conflict` | both set | unset one |
| `config_ambiguous` | `QQAI_BOT_DIR` contains both `config.json` and `bot/config.json` | keep one |
| `camera_not_found` | no DirectShow device matches `QQAI_CAMERA_NAME` | check the exact FriendlyName |
| `camera_ambiguous` | more than one device matches | rename or select a unique camera |
| `camera_enum_failed` | DirectShow enumeration itself failed | check the DirectShow stack / run `python video_runtime/process_loopback.py` for diagnostics |
