# -*- coding: utf-8 -*-
"""把视频通话接进 QQAI 的记忆体系：**共用**人设与事实，**独立**归档通话记录。

用户确认的语义（2026-10-31）：
    · 人设（persona）、事实（facts）—— 与 QQAI 主机器人**共用一套**
    · 通话对话记录 —— **单独归档**，不混进主机器人上下文
    · 单通电话内的短期上下文 —— 通话内独立

实现要点：
    归档用独立 `etype="video_call"`。QQAI 的 `memory_archive` 本来就按 etype 分流，
    所以主机器人按自己的 etype 读取时天然读不到通话记录——**不需要改 QQAI 一行代码**，
    比"加 source 字段再过滤"更干净。

约束：QQAI 只**只读导入**，绝不修改其文件。

用法：
    from qqai_memory import load_persona, facts_for, archive_call_turn
"""
from __future__ import annotations

import json
import os
import sys
import traceback
from pathlib import Path

R2_DIR = Path(__file__).resolve().parent
ROOT = R2_DIR.parent
QQAI_BOT = None
LOG_DIR = ROOT / "logs"

CALL_ETYPE = "video_call"          # 通话记录专用类型；主机器人不会读它

_state: dict = {"persona": None, "errors": []}


def _bot():
    """把 QQAI\\bot 加进 sys.path 并返回模块集合（只读导入）。失败返回空 dict。"""
    global QQAI_BOT
    if QQAI_BOT is None:
        try:
            from runtime_config import resolve_bot_config
            QQAI_BOT = resolve_bot_config()["bot_dir"]
        except Exception as exc:  # noqa: BLE001
            _state["errors"].append(f"QQAI bot 配置错误: {type(exc).__name__}: {exc}")
            return {}
    if not QQAI_BOT.is_dir():
        _state["errors"].append(f"QQAI bot 目录不存在: {QQAI_BOT}")
        return {}
    if str(QQAI_BOT) not in sys.path:
        sys.path.insert(0, str(QQAI_BOT))
    mods = {}
    for name in ("memory_archive", "facts", "main_persona", "persona_layers",
                 "persona_history", "reply_style", "world"):
        try:
            mods[name] = __import__(name)
        except Exception as exc:  # noqa: BLE001
            mods[name] = None
            _state["errors"].append(f"{name}: {type(exc).__name__}: {exc}")
    return mods


_MODS = None


def mods() -> dict:
    global _MODS
    if _MODS is None:
        _MODS = _bot()
    return _MODS


# ------------------------------------------------------------------ 人设

def load_persona(max_chars: int = 1200) -> str:
    """取 QQAI 的人设文本（共用同一套）。取不到就返回空串，不编造。

    2026-10-31 实测入口：
        main_persona.core_text()            -> 人设正文
        main_persona.enabled()              -> 是否启用
        persona_layers.compose(scope)       -> dict，含场景/临时层覆盖
    """
    if _state["persona"] is not None:
        return _state["persona"]
    m = mods()
    text = ""

    mp = m.get("main_persona")
    if mp is not None:
        try:
            on = getattr(mp, "enabled", None)
            if (not callable(on)) or on():
                fn = getattr(mp, "core_text", None)
                val = fn() if callable(fn) else ""
                if isinstance(val, str):
                    text = val.strip()
        except Exception as exc:  # noqa: BLE001
            _state["errors"].append(f"main_persona.core_text: {exc}")

    if not text:
        pl = m.get("persona_layers")
        if pl is not None:
            try:
                fn = getattr(pl, "compose", None)
                val = fn("video_call") if callable(fn) else None
                if isinstance(val, dict):
                    for k in ("text", "prompt", "composed", "core"):
                        if isinstance(val.get(k), str) and val[k].strip():
                            text = val[k].strip()
                            break
                elif isinstance(val, str):
                    text = val.strip()
            except Exception as exc:  # noqa: BLE001
                _state["errors"].append(f"persona_layers.compose: {exc}")

    _state["persona"] = text[:max_chars]
    if not text:
        _state["errors"].append("没取到人设文本")
    return _state["persona"]


# ------------------------------------------------------------------ 事实

def facts_for(scope: str, text: str, max_n: int = 4) -> list[str]:
    """按当前话题取与 QQAI 共用的事实条目。失败返回空列表。"""
    mod = mods().get("facts")
    if mod is None or not scope:
        return []
    out: list[str] = []
    try:
        fn = getattr(mod, "relevant_facts", None)
        if callable(fn):
            got = fn(scope, text, max_n=max_n)
            out = [_flatten(x) for x in (got or [])]
        if not out:
            fn2 = getattr(mod, "recent_facts", None)
            if callable(fn2):
                out = [_flatten(x) for x in (fn2(scope, max_n=max_n) or [])]
    except Exception as exc:  # noqa: BLE001
        _state["errors"].append(f"facts_for: {type(exc).__name__}: {exc}")
    return [x for x in out if x]


def _flatten(item) -> str:
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, dict):
        for k in ("text", "value", "fact", "content"):
            if isinstance(item.get(k), str):
                return item[k].strip()
        return json.dumps(item, ensure_ascii=False)[:200]
    return str(item).strip()


# ------------------------------------------------------------------ 归档

def archive_call_turn(user_text: str, reply: str, *, uid: str | None = None,
                      emotion: str = "", motion: str = "", session: str = "",
                      extra: dict | None = None) -> dict:
    """把一轮通话记录写进 QQAI 记忆，**etype=video_call**，与主机器人上下文隔离。"""
    mod = mods().get("memory_archive")
    if mod is None:
        return {"ok": False, "error": "memory_archive 不可用"}
    payload = {"source": "video_call", "emotion": emotion, "motion": motion,
               "session": session}
    if extra:
        payload.update(extra)
    text = f"【视频通话】对方：{user_text}\nAI：{reply}"
    try:
        fn = getattr(mod, "append_event", None)
        if not callable(fn):
            return {"ok": False, "error": "memory_archive 没有 append_event"}
        fn(CALL_ETYPE, text, uid=uid, gid=None, extra=payload)
        return {"ok": True, "etype": CALL_ETYPE, "uid": uid}
    except Exception as exc:  # noqa: BLE001
        _state["errors"].append(f"archive_call_turn: {type(exc).__name__}: {exc}")
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def recent_call_turns(uid: str | None = None, limit: int = 5) -> list[str]:
    """读回通话记录（只读 etype=video_call，证明归档生效且可检索）。"""
    mod = mods().get("memory_archive")
    if mod is None:
        return []
    try:
        q = getattr(mod, "query_text", None) or getattr(mod, "query", None)
        if not callable(q):
            return []
        rows = q(uid=uid, etype=CALL_ETYPE, limit=limit) or []
        if isinstance(rows, str):
            return [rows[i:i + 160] for i in range(0, min(len(rows), 800), 160)]
        out = []
        for r in rows:
            if isinstance(r, dict):
                out.append(str(r.get("text") or r)[:120])
            else:
                out.append(str(r)[:120])
        return out
    except Exception as exc:  # noqa: BLE001
        _state["errors"].append(f"recent_call_turns: {type(exc).__name__}: {exc}")
        return []


def diagnostics() -> dict:
    exists = bool(QQAI_BOT and QQAI_BOT.is_dir())
    return {"qqai_bot": str(QQAI_BOT) if QQAI_BOT else None, "exists": exists,
            "persona_chars": len(_state["persona"] or ""),
            "errors": _state["errors"][-10:]}


if __name__ == "__main__":
    print(json.dumps(diagnostics(), ensure_ascii=False, indent=2))
    p = load_persona()
    print(f"[人设] {len(p)} 字：{p[:120]!r}")
    print(f"[事实] {facts_for('test', '你好')}")
    print(f"[归档] {archive_call_turn('自测一句话', '自测回复', uid='selftest')}")
    print(f"[读回] {recent_call_turns(uid='selftest', limit=3)}")
