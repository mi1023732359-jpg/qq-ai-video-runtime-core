#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""完整对话闭环：对方说话 → ASR → DialogueService → 表情 + TTS（口型）→ 循环。

复用（不重写）：
    video_runtime.process_loopback_stream.ProcessLoopbackSource   进程级采集
    video_runtime.asr_in.WhisperTranscriber                       faster-whisper
    video_runtime.dialogue.DialogueService + PlaceholderBrain      轮次/记忆/播报回调
    video_runtime.session_coordinator.Store                        视频命名空间记忆
    r2/mini_control.py (:8770)                                   表情/口型/QQAI 语音

大脑：
    默认 placeholder（回声，**不是 AI**，用于验证闭环）；
    --brain deepseek 时读 QQAI 的配置文件（只读）调用其 DeepSeek 接口，给出真实回复。

用法：
    python r2\\conversation_loop.py --pid <pid> --seconds 30
    python r2\\conversation_loop.py --qq --brain deepseek --seconds 300
    python r2\\conversation_loop.py --pid <pid> --seconds 20 --no-speak   # 只转写不播报
退出码：0 正常；1 参数/依赖错误；2 采集失败。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import urllib.request
from pathlib import Path

import numpy as np

R2_DIR = Path(__file__).resolve().parent
R2_ROOT = R2_DIR.parent

from pcm_ring import PcmRingWriter  # noqa: E402

LOG_DIR = R2_ROOT / "logs"
CTL = "http://127.0.0.1:8770"


# ------------------------------------------------------------------ 大脑

class DeepSeekBrain:
    """读 QQAI 的 bot/config.json（只读）调用其 OpenAI 兼容接口，做真实回复。"""

    name = "qqai-deepseek"
    is_real_ai = True

    def __init__(self, persona: str = "你是「大肥鱼」，一个活泼可爱的虚拟主播。回答要简短口语化，1-2 句。"):
        self.cfg = None
        self.persona = persona
        try:
            import json as _json
            from runtime_config import resolve_bot_config
            p = resolve_bot_config()["config"]
            data = _json.loads(p.read_text(encoding="utf-8-sig"))
            prov = data.get("providers") or {}
            self.cfg = prov.get("DeepSeek-chat") or next(iter(prov.values()), None)
        except Exception as exc:  # noqa: BLE001
            self.err = str(exc)

    def respond(self, text, ctx=None):
        if not self.cfg:
            return {"reply": "（大脑未配置）", "emotion": "confused", "brain": self.name,
                    "is_real_ai": False}
        import json as _json
        body = _json.dumps({
            "model": self.cfg.get("model") or "deepseek-chat",
            "messages": [{"role": "system", "content": self.persona},
                         {"role": "user", "content": str(text)}],
            "temperature": 0.8, "max_tokens": 120,
        }).encode()
        req = urllib.request.Request(self.cfg["base_url"], data=body, headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + self.cfg["api_key"],
        })
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                data = _json.loads(r.read().decode())
            reply = (data.get("choices") or [{}])[0].get("message", {}).get("content", "").strip()
            return {"reply": reply or "嗯？", "emotion": "neutral", "brain": self.name,
                    "is_real_ai": True}
        except Exception as exc:  # noqa: BLE001
            return {"reply": "（网络不太好）", "emotion": "confused", "brain": self.name,
                    "is_real_ai": True, "error": str(exc)}


# ------------------------------------------------------------------ 播报

def post_json(path: str, obj: dict, timeout: float = 30.0):
    try:
        from mini_control import read_token  # local, avoids a client-side token cache
        token = read_token() or ""
    except Exception:  # noqa: BLE001
        token = ""
    req = urllib.request.Request(
        CTL + path, data=json.dumps(obj).encode(),
        headers={"Content-Type": "application/json",
                 "X-ADI-Control-Token": token})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


class VadTranscriber:
    """在 WhisperTranscriber 之上**打开 VAD**（不改旧模块）。

    为什么必须开：静音/噪声段 whisper 会凭空转出"字幕by索兰娅""阿彌陀佛"这类幻觉文本，
    再喂给大脑就会胡说八道。faster-whisper 自带 vad_filter，能把这些段直接滤掉。
    """

    name = "faster-whisper+vad"

    def __init__(self, base, prompt: str | None = None):
        self.base = base
        # 人物设定名等专有词，作为 initial_prompt 给 whisper 一个上下文。
        # 2026-10-31 21:0x A/B 实测（同一段真实朗读）：
        #   prompt=None            → "你好呀 我是大飛魚現場正掛車時與一時別..."
        #   prompt="大肥鱼,直播间,你好呀" → "你好呀 我是大肥鱼..."
        # 人名识别正确，断句也更合理。
        self.prompt = prompt

    def __getattr__(self, item):
        return getattr(self.base, item)

    def transcribe(self, pcm, rate=16000):
        return self.transcribe_detail(pcm, rate)[0]

    def transcribe_detail(self, pcm, rate=16000):
        """返回 (文本, 质量指标)。质量指标用来判断这一段到底是不是**真的有人在说话**。

        2026-10-31 18:34 那通真实电话的教训：11 轮对话里 ASR 转出的是
        "字幕by索兰娅""阿彌陀佛""掌聲""1,2,3" —— 全是 whisper 对静音/噪声的幻觉，
        AI 对着这些乱码一本正经地回答，用户听到的就是"AI 没有正常回答"。
        光开 vad_filter 不够，必须把每段的置信度指标拿回来做二次门禁。
        """
        import uuid
        import wave
        import numpy as _np
        model = self.base._load()
        self.base.calls += 1
        self.base.tmp_dir.mkdir(parents=True, exist_ok=True)
        path = self.base.tmp_dir / (uuid.uuid4().hex + ".wav")
        try:
            with wave.open(str(path), "wb") as wav:
                wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(rate)
                wav.writeframes(_np.asarray(pcm, dtype=_np.int16).tobytes())
            segments, _info = model.transcribe(
                str(path), language=self.base.language, beam_size=1,
                vad_filter=True,
                vad_parameters={"min_silence_duration_ms": 300, "threshold": 0.5},
                no_speech_threshold=0.6, condition_on_previous_text=False,
                initial_prompt=self.prompt)
            segs = list(segments)
            text = "".join(s.text for s in segs).strip()
            quality = {
                "segments": len(segs),
                "speech_s": round(sum(max(0.0, s.end - s.start) for s in segs), 2),
                "avg_logprob": (round(float(_np.mean([s.avg_logprob for s in segs])), 3)
                                if segs else None),
                "no_speech_prob": (round(float(_np.max([s.no_speech_prob for s in segs])), 3)
                                   if segs else None),
                "compression_ratio": (round(float(_np.max([s.compression_ratio for s in segs])), 3)
                                      if segs else None),
                "chars": len(text),
            }
            return text, quality
        finally:
            try:
                path.unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass


#: whisper 在中文静音/噪声音频上的经典幻觉串。命中即丢弃。
HALLUCINATION_PATTERNS: tuple[str, ...] = (
    "字幕by索兰娅", "字幕由索兰娅", "索兰娅", "字幕志愿者", "中文字幕",
    "请不吝点赞", "订阅转发", "打赏支持", "明镜与点点", "点点栏目",
    "谢谢观看", "感谢观看", "感谢收看", "本字幕", "amara", "yoyo television",
    "优优独播", "字幕制作人", "字幕組", "字幕组",
)


def _squash(text: str) -> str:
    """去掉空白与标点，便于做幻觉串匹配。"""
    import re
    return re.sub(r"[\s\W_]+", "", text or "").lower()


def judge_transcript(text: str, quality: dict, duration_s: float,
                     min_speech_s: float = 0.6, min_chars: int = 2,
                     prev_text: str | None = None) -> tuple[bool, str]:
    """判断一段转写是否可信。返回 (是否采纳, 丢弃原因)。"""
    if not text:
        return False, "empty"
    n = _squash(text)
    q = quality or {}
    # 短问候必须能回：实测 2026-11-01 18:49 对方说“好”，ASR 出字但 chars=1 被丢，
    # 导致整通电话“无回话”。对明确问候词放行，但仍要求有一定语音时长和置信度。
    SHORT_GREETINGS = {"喂", "你好", "您好", "在吗", "在么", "在不在", "好", "嗯",
                       "哈喽", "hello", "hi", "能听到吗", "听得到吗", "听到吗"}
    if n in SHORT_GREETINGS:
        speech_s = float(q.get("speech_s") or 0)
        alp = q.get("avg_logprob")
        if speech_s >= 0.25 and (alp is None or float(alp) > -1.6):
            return True, "short_greeting"
    if len(n) < min_chars:
        return False, f"chars={len(n)}"
    low = (text or "").lower()
    hit = next((p for p in HALLUCINATION_PATTERNS if _squash(p) in n or p in low), None)
    if hit:
        return False, f"hallucination_pattern:{hit}"
    if q.get("segments", 0) == 0:
        return False, "no_segments"
    if float(q.get("speech_s") or 0) < min_speech_s:
        return False, f"speech_too_short:{q.get('speech_s')}s"
    # 置信度门禁（2026-10-31 20:33 放宽）：
    # 原来 no_speech_prob 门槛定在 0.55，实测把**真实语音**判成非语音
    # （"你好呀,我是大飛" no_speech=0.794 但 avg_logprob=-0.449 明显可信；
    #   "能听到我说话吗" no_speech=0.594 也被丢）。任务明确禁止"静默丢弃有效音频"。
    # 现在只对**两项都不好**的情况丢弃；单看 no_speech_prob 不再单独否决。
    nsp = q.get("no_speech_prob")
    alp = q.get("avg_logprob")
    if nsp is not None and float(nsp) > 0.85 and (alp is None or float(alp) < -0.6):
        return False, f"no_speech_prob={nsp},avg_logprob={alp}"
    if alp is not None and float(alp) < -1.2:
        return False, f"avg_logprob={alp}"
    if q.get("compression_ratio") is not None and float(q["compression_ratio"]) > 2.4:
        return False, f"compression_ratio={q['compression_ratio']}"
    if prev_text and _squash(prev_text) == n:
        return False, "repeat_of_previous"
    if duration_s and q.get("speech_s") and float(q["speech_s"]) > duration_s * 1.5:
        return False, "speech_longer_than_audio"
    return True, ""


def make_speak_cb(enabled: bool, log: list, avatar=None, speaking: dict | None = None,
                  on_speak=None, feedback=None):
    """播报回调。

    2026-10-31 修复两点：
      1) 返回真值（DialogueService 用 bool(返回值) 记 `spoken`，返回 None 会记成 false）；
      2) 情绪/动作交给 `AvatarRuntime` 决定并执行（`/set` + `/motion`），
         本函数只负责 **播报**，不再用固定的 `neutral` 覆盖表情；
      3) 顺带设置半双工闸门窗口（`speaking["speaking_until"]`），
         AI 说话期间不采集，避免把回声当对方输入。
    R2.1：播报开始/结束事件交给 ListeningFeedback；普通回复和可选等待语音用
    `meta={"kind": ...}` 区分，展示层不会把 waiting voice 当成 reply。
    """
    def speak(reply: str, emotion: str = "neutral", meta: dict | None = None):
        if not enabled or not reply:
            return {"spoken": False, "reason": "disabled" if not enabled else "empty_reply"}
        meta = meta or {}
        kind = str(meta.get("kind") or "reply")
        try:
            text = reply
            emo = emotion
            if avatar is not None:
                plan = avatar.speak_plan(reply, tag_emotion=emotion if emotion != "neutral" else None)
                text, emo = plan["text"], plan["emotion"]
            else:
                post_json("/set", {"emotion": emotion})
            r = post_json("/speak", {"text": text})
            # 半双工窗口：中文约 4 字/秒，再留 1.2s 余量；上限 20s 防呆
            est = min(20.0, max(1.5, len(text) / 4.0 + 1.2))
            if speaking is not None:
                speaking["speaking_until"] = time.time() + est
            if on_speak is not None:
                try:
                    on_speak({"text": text[:40], "emotion": emo, "kind": kind,
                              "seconds": round(est, 2)})
                except Exception:  # noqa: BLE001
                    pass
            if feedback is not None:
                try:
                    feedback.emit("reply_started", text=text[:80], emotion=emo,
                                  seconds=est, kind=kind)
                except Exception:  # noqa: BLE001
                    pass
            log.append({"at": time.strftime("%H:%M:%S"), "speak": text[:60], "emotion": emo,
                        "kind": kind,
                        "half_duplex_s": round(min(20.0, max(1.5, len(text) / 4.0 + 1.2)), 1)})
            return {"spoken": True, "emotion": emo, "ok": (r or {}).get("ok")}
        except Exception as exc:  # noqa: BLE001
            log.append({"at": time.strftime("%H:%M:%S"), "speak_error": str(exc)})
            if feedback is not None:
                try:
                    feedback.emit("reply_finished", reason="speak_error")
                except Exception:  # noqa: BLE001
                    pass
            return {"spoken": False, "error": str(exc)}
    return speak


# ------------------------------------------------------------------ 采集

def qq_pids() -> list[int]:
    from peer_audio_asr import qq_pids as _qp
    return _qp()


def _tail_silent(audio: np.ndarray, tail_s: float = 0.45, rel: float = 0.10) -> bool:
    """判断音频尾部是否是静音（用于"说完一句就切"，而不是硬切 3.5 秒）。

    2026-10-31 实测：固定 3.5s 分片会把句子拦腰截断（自测里"你好呀，我是大肥鱼"
    整段被切掉，只剩后半句），对话质量明显受损。改成"尾部静音就收"。
    """
    if audio.size < int(tail_s * 16000):
        return False
    tail = audio[-int(tail_s * 16000):]
    tail_rms = float(np.sqrt(np.mean(tail ** 2)))
    head_rms = float(np.sqrt(np.mean(audio ** 2))) or 1e-9
    return tail_rms < max(0.006, head_rms * rel)


def split_asr_audio(audio: np.ndarray, rate: int = 16000,
                    max_s: float = 5.0, min_s: float = 2.5) -> list[np.ndarray]:
    """把长语音切成 <=max_s 的块，尽量切在低能量处；不丢任何样本。

    只用于 ASR 请求，不改变 VAD/切句；所有块都会送 ASR，失败会显式记录。
    """
    a = np.asarray(audio, dtype=np.float32).reshape(-1)
    n = int(a.size)
    maxn = max(1, int(max_s * rate))
    minn = max(1, int(min_s * rate))
    if n <= maxn:
        return [a] if n else []
    chunks: list[np.ndarray] = []
    start = 0
    win = max(1, int(0.02 * rate))          # 20ms 能量帧
    while start < n:
        end = min(n, start + maxn)
        if end < n and end - start >= minn:
            lo = max(start + minn, end - int(1.2 * rate))
            lo = (lo // win) * win
            hi = (end // win) * win
            if hi - lo >= win:
                frames = a[lo:hi].reshape(-1, win)
                rms = np.sqrt(np.mean(frames ** 2, axis=1))
                cut = lo + int(np.argmin(rms)) * win
                if start + minn <= cut < end:
                    end = cut
        if end <= start:
            end = min(n, start + maxn)
        chunks.append(a[start:end])
        start = end
    return chunks


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="视频对话闭环（采集→ASR→对话→语音）")
    ap.add_argument("--pid", type=int, action="append", default=None)
    ap.add_argument("--pids", default=None)
    ap.add_argument("--qq", action="store_true")
    ap.add_argument("--brain", default="placeholder", choices=["placeholder", "deepseek"])
    ap.add_argument("--seconds", type=float, default=30.0)
    ap.add_argument("--chunk-seconds", type=float, default=3.5,
                    help="最短分片时长（低于它不判句）")
    ap.add_argument("--peer-uid", default=None,
                    help="通话对方的记忆作用域（默认 video_call）")
    ap.add_argument("--asr-prompt", default="大肥鱼, 直播间, 你好呀",
                    help="whisper initial_prompt：给专有名词一个上下文（人名识别明显更准）")
    ap.add_argument("--max-seconds", type=float, default=9.0,
                    help="最长分片上限；连续说话到这么长就强制切")
    ap.add_argument("--min-rms", type=float, default=0.004)
    ap.add_argument("--no-speak", action="store_true")
    ap.add_argument("--no-listening-feedback", action="store_true",
                    help="关闭 R2.1 聆听/思考轻量视觉反馈")
    ap.add_argument("--listening-voice", action="store_true",
                    help="允许 ASR 等待超过 delay 时的一句短反馈（默认关闭）")
    ap.add_argument("--listening-voice-delay", type=float, default=8.0,
                    help="ASR 等待多少秒后才允许反馈语音")
    ap.add_argument("--asr-service-port", type=int, default=3010,
                    help="常驻 ASR 服务端口（默认 3010；服务未 ready 时回退本地模型）")
    ap.add_argument("--asr-timeout", type=float, default=12.0,
                    help="单块 ASR 客户端超时；长语音已拆块，不再用一个 60s 请求")
    ap.add_argument("--asr-max-audio", type=float, default=5.0,
                    help="单个 ASR 请求的最大音频秒数；长语音拆块，不丢弃")
    ap.add_argument("--no-asr-service", action="store_true", help="强制本地 ASR（会冷启动）")
    ap.add_argument("--greet", default=None,
                    help="接通后立刻说一句（无需对方先开口就能验证声音链路）")
    args = ap.parse_args(argv)

    pids: list[int] = []
    if args.qq:
        # 用 **QQ 根进程** 作为采集目标（INCLUDE_TARGET_PROCESS_TREE 会覆盖整棵树）。
        # 旧实现取全部 QQ pid 再逐个试激活，实测选中了 utility 子进程 → 永远采不到通话音频。
        from proc_capture_fix import qq_root_pids
        pids = qq_root_pids()
        if not pids:
            from video_runtime.process_loopback import qq_pids as _qp
            pids = _qp().get("pids", [])
    elif args.pids:
        pids = [int(x) for x in args.pids.split(",") if x.strip()]
    elif args.pid:
        pids = list(args.pid)
    if not pids:
        print("[FAIL] 需要 --pid / --pids / --qq")
        return 1

    # ── 先把采集拉起来，再做重量级 import ──────────────────────────────────
    # 2026-10-31 21:0x：原来 `from video_runtime.asr_in import WhisperTranscriber`
    # （会拉起 faster-whisper/torch 依赖）排在 `src.start()` **之前**，
    # 实测进程起来到真正开始采集要 3–6 秒 —— 对方接通后开口的头几句直接漏掉。
    # 采集只依赖轻量的 ctypes/COM，必须最先启动。
    from proc_capture_fix import FixedProcessLoopbackSource, stats as cap_stats

    capture_mode = "qq" if args.qq else "pids"     # qq=真实通话；pids=离线自测/调试
    session_id = "video-" + time.strftime("%Y%m%d-%H%M%S")
    trace_path = LOG_DIR / "conversation_trace.jsonl"
    _t_begin = time.time()

    def trace(entry: dict) -> None:
        """每一段音频的去向都记一笔——不允许"静默丢弃有效音频"。

        `t` 是毫秒级 epoch，用来算端到端延迟（听到→回复→开始播报）。
        只靠 `at`（秒级字符串）算不出几百毫秒级的差异。
        """
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            with trace_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"at": time.strftime("%H:%M:%S"),
                                     "t": round(time.time(), 3), **entry,
                                     "session": session_id, "mode": capture_mode},
                                    ensure_ascii=False) + "\n")
        except Exception:  # noqa: BLE001
            pass

    src = FixedProcessLoopbackSource(pids=pids, rate=48000)
    try:
        src.start()
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] 进程级采集启动失败：{type(exc).__name__}: {exc}")
        return 2
    boot_s = round(time.time() - _t_begin, 2)
    print(f"[采集] open_pid={getattr(src,'open_pid',None)} 候选={getattr(src,'candidates',None)} "
          f"native {src.native_rate}Hz/{src.native_channels}ch → 16k 单声道；"
          f"分片 {args.chunk_seconds}s（进程启动→采集就绪 {boot_s}s）")
    trace({"event": "capture_start", "boot_s": boot_s, **cap_stats(src)})

    # P0：本进程是该 QQ 进程树音频的**唯一所有者**。把原始 48k 单声道 PCM 写入有界环，
    # 诊断/探针只读环，不再各自打开 ActivateAudioInterfaceAsync。
    ring = None
    try:
        ring = PcmRingWriter(rate=int(getattr(src, "native_rate", 48000) or 48000),
                             channels=1, seconds=10, owner_pid=os.getpid())
        trace({"event": "pcm_owner", "owner_pid": os.getpid(),
               "rate": ring.rate, "capacity_frames": ring.capacity})
    except Exception as exc:  # noqa: BLE001
        trace({"event": "pcm_owner_failed", "error": f"{type(exc).__name__}: {exc}"})

    # ── 之后再拉重量级依赖（whisper / dialogue）──────────────────────────
    from video_runtime.asr_in import WhisperTranscriber
    from video_runtime.dialogue import DialogueService, PlaceholderBrain
    from video_runtime.session_coordinator import Store
    from avatar_decision import AvatarRuntime
    from listening_feedback import ListeningFeedback
    import qqai_memory

    store = Store(LOG_DIR / "dialogue_store")
    if args.brain == "placeholder":
        brain = PlaceholderBrain()
        base_persona = ""
    else:
        # 人设与 QQAI 主机器人**共用同一套**（main_persona.core_text）
        base_persona = qqai_memory.load_persona()
        brain = DeepSeekBrain(persona=(base_persona or None))
    scope_uid = (args.peer_uid or "video_call")
    print(f"[记忆] 人设 {len(base_persona)} 字；作用域 uid={scope_uid}")
    avatar = AvatarRuntime()
    speak_log: list[dict] = []
    state = {"speaking_until": 0.0}

    # R2.1：只做展示层调度；事件非阻塞入有界队列，worker 独立线程消费。
    feedback = ListeningFeedback(
        avatar=avatar, speaking=state, session_id=session_id,
        enabled=not args.no_listening_feedback,
        voice_enabled=bool(args.listening_voice),
        voice_delay=args.listening_voice_delay,
    )
    speak_cb = make_speak_cb(not args.no_speak, speak_log,
                             avatar=avatar, speaking=state, feedback=feedback,
                             on_speak=lambda info: trace({"event": "speak_start", **info}))
    # 反馈语音必须复用现有 TTS/半双工/授权链，不直接调用 audio_out。
    feedback.set_voice_cb(
        lambda text: speak_cb(text, "serious", meta={"kind": "feedback_voice"}))
    dlg = DialogueService(store, session_id=session_id, brain=brain,
                          speak=lambda reply, emotion="neutral": speak_cb(reply, emotion),
                          log_path=LOG_DIR / "dialogue_loop.jsonl")
    print(f"[对话] session={session_id} brain={brain.name} is_real_ai={getattr(brain,'is_real_ai',None)}")

    # P1：优先用常驻 ASR 服务（Runtime 启动即预热），未 ready 时显式回退本地模型。
    asr_backend = "local"
    asr_reason = "disabled" if args.no_asr_service else "state_not_ready"
    tr = None
    if not args.no_asr_service:
        try:
            from asr_client import AsrClient
            svc_state = {}
            try:
                svc_state = json.loads((LOG_DIR / "asr_service.json").read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                pass
            client = AsrClient(port=args.asr_service_port, timeout=args.asr_timeout)
            if svc_state.get("state") == "ready" and client.available():
                tr = client
                asr_backend = "service"
                asr_reason = ""
                trace({"event": "asr_backend", "backend": "service",
                       "model": svc_state.get("model"), "load_s": svc_state.get("load_s"),
                       "port": args.asr_service_port})
        except Exception as exc:  # noqa: BLE001
            asr_reason = f"{type(exc).__name__}: {exc}"
    if tr is None:
        tr = VadTranscriber(WhisperTranscriber(model_size="base", language="zh",
                                               cpu_threads=8),
                            prompt=args.asr_prompt)
        trace({"event": "asr_backend", "backend": "local", "reason": asr_reason})

    # 模型预热：本地模型才需要；常驻服务在 runtime 启动时已经 ready。
    def _warmup():
        t_w = time.time()
        try:
            tr.transcribe_detail(np.zeros(8000, dtype=np.int16), 16000)
            print(f"[预热] ASR 模型就绪，用时 {time.time() - t_w:.1f}s "
                  f"（进程启动→模型就绪 {time.time() - _t_begin:.1f}s）")
            trace({"event": "asr_warmup", "seconds": round(time.time() - t_w, 2),
                   "ready_s": round(time.time() - _t_begin, 2), "backend": asr_backend})
        except Exception as exc:  # noqa: BLE001
            trace({"event": "asr_warmup_failed", "error": str(exc), "backend": asr_backend})

    if asr_backend == "service":
        trace({"event": "asr_warmup", "seconds": 0.0,
               "ready_s": round(time.time() - _t_begin, 2), "backend": "service",
               "note": "already warmed"})
    else:
        threading.Thread(target=_warmup, daemon=True).start()

    if args.greet and not args.no_speak:
        # 接通即开口：对方不需要说话就能确认"AI 的声音真的到了他那头"
        speak_cb(args.greet, "happy")
        print(f"[打招呼] {args.greet}")
        time.sleep(1.2)          # 让 TTS 先播完，避免把自己人声当对方输入

    buffered: list[np.ndarray] = []
    samples = 0
    seg = int(args.chunk_seconds * 16000)
    max_seg = int(max(args.chunk_seconds, args.max_seconds) * 16000)
    t0 = time.time()
    rounds: list[dict] = []
    dropped: list[dict] = []
    prev_text: str | None = None
    last_status = t0
    silent_segments = 0
    last_data_ts = time.time()
    restarts = 0
    # R2.1 展示层“实时起/停”检测：只用于 Listening Feedback，不参与 ASR 切句。
    user_live = False
    last_voice_ts = 0.0
    voice_threshold = max(0.004, args.min_rms)
    # P2：AI 问候/播报期间的对方语音不丢弃，暂存在有界缓冲；问候结束后并入下一段送 ASR。
    deferred: list[np.ndarray] = []
    deferred_samples = 0
    max_deferred = 16000 * 8

    try:
        while time.time() - t0 < args.seconds:
            avatar.tick()
            chunk = src.read()
            if chunk is None:
                # 异常恢复：采集超过 6 秒没有数据（QQ 重启/音频设备切换/线程死掉）→ 重开流
                idle_for = time.time() - last_data_ts
                if idle_for > 6.0 and restarts < 5:
                    restarts += 1
                    trace({"event": "capture_restart", "idle_s": round(idle_for, 1),
                           "attempt": restarts, **cap_stats(src)})
                    print(f"[自愈] 采集 {idle_for:.1f}s 无数据，重启流（第 {restarts} 次）")
                    try:
                        src.stop()
                    except Exception:  # noqa: BLE001
                        pass
                    try:
                        src = FixedProcessLoopbackSource(
                            pids=(qq_root_pids() if args.qq else pids), rate=48000).start()
                        last_data_ts = time.time()
                        trace({"event": "capture_restarted", **cap_stats(src)})
                    except Exception as exc:  # noqa: BLE001
                        trace({"event": "capture_restart_failed", "error": str(exc)})
                        print(f"[自愈] 重启失败：{type(exc).__name__}: {exc}")
                        time.sleep(1.0)
                time.sleep(0.02)
                continue
            last_data_ts = time.time()
            if ring is not None:
                try:
                    ring.write(np.asarray(chunk, dtype=np.float32).reshape(-1))
                except Exception as exc:  # noqa: BLE001
                    trace({"event": "pcm_ring_error", "error": f"{type(exc).__name__}: {exc}"})
                    ring = None
            # read() 已归一化到 [-1,1]；48k→16k 用 3:1 盒式平均（模块自带重采样会破坏音质）
            a = np.asarray(chunk, dtype=np.float32).reshape(-1)
            if a.size:
                if float(np.max(np.abs(a))) > 2.0:      # 兼容未归一化的老实现
                    a = a / 32768.0
                n = (a.size // 3) * 3
                if n:
                    a = a[:n].reshape(-1, 3).mean(axis=1)
            buffered.append(a)
            samples += a.size
            # 实时反馈：只发轻量事件；真正切句仍由下面的 seg/尾静音决定。
            chunk_rms = float(np.sqrt(np.mean(a ** 2))) if a.size else 0.0
            now_chunk = time.time()
            if chunk_rms >= voice_threshold:
                last_voice_ts = now_chunk
                if not user_live:
                    feedback.emit("user_speech_started", rms=round(chunk_rms, 4),
                                  realtime=True)
                    user_live = True
            elif user_live and now_chunk - last_voice_ts >= 0.5:
                feedback.emit("user_speech_paused", rms=round(chunk_rms, 4),
                              realtime=True)
                user_live = False
            if samples < seg:
                continue
            audio = np.concatenate(buffered)
            # 自由说话：到最短分片后，等尾部静音再收句（最多等 max_seg）
            hit_max = samples >= max_seg
            if not hit_max and not _tail_silent(audio):
                continue
            buffered, samples = [], 0
            trace({"event": "segment_flush", "seconds": round(audio.size / 16000.0, 2),
                   "why": "max_len" if hit_max else "tail_silence"})
            now = time.time()
            rms = float(np.sqrt(np.mean(audio ** 2))) if audio.size else 0.0
            had_deferred = False

            # 半双工闸门：AI 正在说话时不采集，避免把回声/自激当成对方输入。
            # 2026-11-01 加 rms：只记 samples 无法判断被丢的是静音还是对方抢话。
            if now < state["speaking_until"]:
                trace({"event": "half_duplex_skip", "samples": int(audio.size),
                       "rms": round(rms, 5)})
                # 不直接丢：有界暂存，等问候/播报结束后并入下一段（P2 首句缓存）。
                if rms >= args.min_rms:
                    deferred.append(audio.copy())
                    deferred_samples += audio.size
                    while deferred_samples > max_deferred and deferred:
                        dropped_a = deferred.pop(0)
                        deferred_samples -= dropped_a.size
                        trace({"event": "deferred_overflow", "dropped_s": round(dropped_a.size / 16000.0, 2)})
                continue

            if deferred:
                merged = np.concatenate(deferred + [audio])
                deferred, deferred_samples = [], 0
                merged_rms = float(np.sqrt(np.mean(merged ** 2))) if merged.size else 0.0
                trace({"event": "deferred_flush", "seconds": round(merged.size / 16000.0, 2),
                       "rms": round(merged_rms, 5)})
                audio = merged
                rms = merged_rms
                had_deferred = True

            if rms < args.min_rms:
                silent_segments += 1
                trace({"event": "below_min_rms", "rms": round(rms, 5),
                       "min": args.min_rms, "samples": int(audio.size)})
                if time.time() - last_status >= 15:
                    print(f"[状态] 已收 {int(time.time()-t0)}s，静音段 {silent_segments}，"
                          f"当前 rms={rms:.5f}（<{args.min_rms} 视为静音）")
                    last_status = time.time()
                continue
            # 按能量裁掉首尾静音；有效语音太短就丢弃（whisper 对静音会幻觉）
            win = int(0.02 * 16000)
            if audio.size < win * 5:
                trace({"event": "segment_too_short", "samples": int(audio.size)})
                continue
            nfr = audio.size // win
            frames = audio[: nfr * win].reshape(-1, win)
            fr = np.sqrt(np.mean(frames ** 2, axis=1))
            thr = max(0.01, float(np.max(fr)) * 0.12)
            act = np.where(fr > thr)[0]
            if act.size == 0:
                silent_segments += 1
                trace({"event": "all_silent_after_trim", "rms": round(rms, 5)})
                continue
            a0, a1 = int(act[0]) * win, int(act[-1] + 1) * win
            trimmed = audio[a0:a1]
            dur = trimmed.size / 16000.0
            if dur < 0.3:
                trace({"event": "speech_too_short", "dur": round(dur, 2)})
                continue
            audio = trimmed
            rms = float(np.sqrt(np.mean(audio ** 2)))

            pcm16 = np.clip(audio * 32767.0, -32768, 32767).astype(np.int16)
            dur_s = pcm16.size / 16000.0
            feedback.emit("user_speech_paused", rms=round(rms, 4), reason="asr")
            chunks = split_asr_audio(audio, rate=16000, max_s=args.asr_max_audio)
            feedback.emit("asr_processing", audio_s=round(dur_s, 2), chunks=len(chunks))
            t_asr = time.time()
            texts: list[str] = []
            qualities: list[dict] = []
            errors: list[str] = []
            chunk_stats: list[dict] = []
            for ci, chunk in enumerate(chunks):
                part = np.clip(chunk * 32767.0, -32768, 32767).astype(np.int16)
                last_exc = None
                for attempt in (1, 2):
                    ct0 = time.time()
                    try:
                        txt, q = tr.transcribe_detail(part, 16000)
                        q = dict(q or {})
                        qualities.append(q)
                        if txt:
                            texts.append(str(txt).strip())
                        chunk_stats.append({"index": ci,
                                            "audio_s": round(part.size / 16000.0, 2),
                                            "asr_s": round(time.time() - ct0, 2),
                                            "queue_s": q.get("queue_s"),
                                            "chars": len(str(txt or "")),
                                            "error": None})
                        last_exc = None
                        break
                    except Exception as exc:  # noqa: BLE001
                        last_exc = exc
                        if attempt == 1:
                            time.sleep(0.15)
                            continue
                        errors.append(f"chunk{ci}: {type(exc).__name__}: {exc}")
                        chunk_stats.append({"index": ci,
                                            "audio_s": round(part.size / 16000.0, 2),
                                            "asr_s": round(time.time() - ct0, 2),
                                            "queue_s": None, "chars": 0,
                                            "error": f"{type(exc).__name__}: {exc}"})
                        trace({"event": "asr_chunk_error", "chunk": ci,
                               "chunks": len(chunks), "dur": round(part.size / 16000.0, 2),
                               "attempts": attempt,
                               "error": f"{type(exc).__name__}: {exc}"})
                        print(f"[ASR错误] chunk {ci+1}/{len(chunks)} "
                              f"{type(exc).__name__}: {exc}（已重试，仍失败）")
            asr_s = round(time.time() - t_asr, 2)
            trace({"event": "asr_chunks", "chunks": len(chunks), "errors": len(errors),
                   "audio_s": round(dur_s, 2), "asr_s": asr_s, "detail": chunk_stats})
            if errors:
                # Fail closed: joining surviving chunks around a failed middle
                # chunk can change the sentence's meaning.  Do not submit a
                # corrupted transcript; ask the caller to repeat instead.
                err = errors[-1]
                feedback.emit("asr_error", error=err, dur=round(dur_s, 2))
                trace({"event": "asr_error", "error": err, "partial": bool(qualities),
                       "dur": round(dur_s, 2), "rms": round(rms, 4),
                       "chunks": len(chunks), "failed_chunks": len(errors)})
                print(f"[ASR错误] {len(errors)}/{len(chunks)} 块失败，整段不提交：{err}")
                continue
            if not qualities:
                feedback.emit("asr_error", error="no_asr_result", dur=round(dur_s, 2))
                trace({"event": "asr_error", "error": "no_asr_result",
                       "dur": round(dur_s, 2), "rms": round(rms, 4),
                       "chunks": len(chunks)})
                print(f"[ASR错误] 所有 {len(chunks)} 块均失败：no_asr_result（跳过本段，继续通话）")
                continue
            text = "".join(texts).strip()
            quality = {
                "segments": sum(int(q.get("segments") or 0) for q in qualities),
                "speech_s": round(sum(float(q.get("speech_s") or 0) for q in qualities), 2),
                "avg_logprob": (round(float(np.mean([q.get("avg_logprob") for q in qualities
                                                      if q.get("avg_logprob") is not None])), 3)
                                if any(q.get("avg_logprob") is not None for q in qualities)
                                else None),
                "no_speech_prob": (round(max(float(q.get("no_speech_prob") or 0)
                                             for q in qualities), 3) if qualities else None),
                "compression_ratio": (round(max(float(q.get("compression_ratio") or 0)
                                                for q in qualities), 3) if qualities else None),
                "chars": len(text),
                "asr_s": asr_s,
                "audio_s": round(dur_s, 2),
                "chunks": len(chunks),
                "failed_chunks": len(errors),
                "queue_s": round(sum(float(q.get("queue_s") or 0) for q in qualities), 2),
            }
            if errors:
                trace({"event": "asr_partial", "errors": errors,
                       "chunks": len(chunks), "text": text})
            # 首句缓存合并后，防止把 AI 自己的问候整句当成对方（只拦长句全匹配，短“你好”放行）。
            if had_deferred and text and speak_log:
                last_spoken = _squash(str(speak_log[-1].get("speak") or ""))
                t_norm = _squash(text)
                if (last_spoken and t_norm and len(t_norm) >= 6
                        and (t_norm in last_spoken or last_spoken in t_norm)):
                    feedback.emit("asr_completed", text=text, asr_s=asr_s,
                                  accepted=False, reason="echo_of_ai_speech")
                    trace({"event": "dropped", "reason": "echo_of_ai_speech",
                           "text": text, "quality": quality, "dur": round(dur_s, 2)})
                    print(f"[丢弃] echo_of_ai_speech  文本={text!r}")
                    continue
            ok, why = judge_transcript(text, quality, dur_s, prev_text=prev_text)
            if not ok:
                feedback.emit("asr_completed", text=text, asr_s=asr_s,
                              accepted=False, reason=why)
                dropped.append({"at": time.strftime("%H:%M:%S"), "text": text,
                                "reason": why, "quality": quality})
                trace({"event": "dropped", "reason": why, "text": text,
                       "quality": quality, "dur": round(dur_s, 2), "rms": round(rms, 4)})
                if text:
                    print(f"[丢弃] {why}  文本={text!r}  质量={quality}")
                continue
            prev_text = text
            feedback.emit("asr_completed", text=text, asr_s=asr_s, accepted=True)
            trace({"event": "heard", "text": text, "quality": quality,
                   "dur": round(dur_s, 2), "rms": round(rms, 4),
                   "asr_s": asr_s})
            print(f"[听到] {text}  质量={quality}")
            # 事实：从 QQAI 共用的事实库按当前话题取，注入到 system
            if base_persona:
                try:
                    fcts = qqai_memory.facts_for(scope_uid, text)
                    extra = ("\n【已知事实】\n" + "\n".join("- " + x for x in fcts)) if fcts else ""
                    brain.persona = base_persona + extra
                except Exception:  # noqa: BLE001
                    pass
            feedback.emit("dialogue_processing", text=text)
            res = dlg.hear(text)
            if res.get("ok"):
                reply = (res.get("reply") or "")
                emo = (res.get("emotion") or "").strip()
                # 情绪：优先用大脑标注，否则按回复文本推断（旧实现恒为 neutral）
                plan = avatar.speak_plan(reply, tag_emotion=emo if emo and emo != "neutral" else None)
                print(f"[回复] ({plan['emotion']}/动作={plan['action'].get('motion')!r}) {plan['text']}")
                rounds.append({"at": time.strftime("%H:%M:%S"), "user": text,
                               "reply": plan["text"], "emotion": plan["emotion"],
                               "motion": plan["action"].get("motion")})
                trace({"event": "reply", "user": text, "reply": plan["text"],
                       "emotion": plan["emotion"], "motion": plan["action"].get("motion")})
                # 归档：写进 QQAI 记忆，**独立 etype=video_call**，不污染主机器人上下文
                try:
                    arc = qqai_memory.archive_call_turn(
                        text, plan["text"], uid=scope_uid, emotion=plan["emotion"],
                        motion=str(plan["action"].get("motion") or ""), session=session_id)
                    trace({"event": "memory_archive", **arc})
                except Exception as exc:  # noqa: BLE001
                    trace({"event": "memory_archive_failed", "error": str(exc)})
                # 播报由 DialogueService 内部调用 speak 回调（勿在此重复调用，否则会说两遍）
    finally:
        try:
            feedback.emit("call_ended", why="session_end")
            feedback.close("session_end")
        except Exception:  # noqa: BLE001
            pass
        try:
            avatar.emergency_stop("session_end")
        except Exception:  # noqa: BLE001
            pass
        trace({"event": "capture_stop", **cap_stats(src)})
        try:
            if ring is not None:
                ring.close()
        except Exception:  # noqa: BLE001
            pass
        src.stop()

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    out = LOG_DIR / "conversation_loop.json"
    out.write_text(json.dumps({"at": time.strftime("%Y-%m-%d %H:%M:%S"),
                               "session": session_id, "brain": brain.name,
                               "pids": pids, "open_pid": getattr(src, "open_pid", None),
                               "rounds": rounds, "dropped": dropped,
                               "silent_segments": silent_segments,
                               "speak_log": speak_log,
                               "listening_feedback": feedback.snapshot(),
                               "avatar_history": avatar.history[-40:]},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[结束] {len(rounds)} 轮；丢弃 {len(dropped)} 段；静音段 {silent_segments}；证据 {out}")
    return 0


if __name__ == "__main__":
    # Script mode only (FIX-SYSPATH-1): importing this module must not mutate
    # sys.path.  Direct `python core/r2/conversation_loop.py` keeps working.
    sys.path.insert(0, str(R2_ROOT))
    sys.path.insert(0, str(R2_DIR))
    sys.exit(main())



