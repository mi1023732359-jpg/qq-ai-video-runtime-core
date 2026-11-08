# -*- coding: utf-8 -*-
"""接听/挂断策略（纯逻辑，可单测；不调用 UI、不写文件）。

任何一项无法确认，结论都是拒绝（fail-closed）。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class PolicyInput:
    caller: str
    now_hhmm: tuple[int, int]
    in_call: bool
    ringing: bool
    core_authorized: bool | None = None     # None = 协调器未裁决
    account_confirmed: bool | None = None   # None = 无法确认登录账号
    single_account_ok: bool | None = None   # None = 无法确认互斥条件
    duplicate: bool = False
    admin_override: bool | None = None      # True=管理员要求接听, False=管理员拒绝


@dataclass
class PolicyResult:
    allow: bool
    reason: str
    detail: dict = field(default_factory=dict)


def _in_windows(now: tuple[int, int], windows) -> bool:
    for start, end in windows:
        if start <= end:
            if start <= now < end:
                return True
        else:  # 跨零点，如 22:00-08:00
            if now >= start or now < end:
                return True
    return False


class CallPolicy:
    def __init__(self, cfg, clock=time.time):
        self.cfg = cfg
        self.clock = clock

    def _now_hhmm(self) -> tuple[int, int]:
        t = time.localtime(self.clock())
        return (t.tm_hour, t.tm_min)

    def decide(self, inp: PolicyInput) -> PolicyResult:
        caller = str(inp.caller)
        detail = {"caller_is_admin": caller in set(self.cfg.admin_uins)}

        # 0) 管理员最高优先级
        if inp.admin_override is False:
            return PolicyResult(False, "admin_denied", detail)
        if inp.admin_override is True:
            return PolicyResult(True, "admin_forced", detail)

        # 1) 扩展开关
        if not self.cfg.enabled:
            return PolicyResult(False, "extension_disabled", detail)
        if not self.cfg.enable_auto_answer:
            return PolicyResult(False, "auto_answer_off", detail)

        # 2) 黑名单优先于白名单
        if caller in set(self.cfg.deny_uins):
            return PolicyResult(False, "caller_in_denylist", detail)

        # 3) 白名单（管理员默认放行，但仍走其余检查）
        if caller not in set(self.cfg.admin_uins):
            if not self.cfg.allow_uins or caller not in set(self.cfg.allow_uins):
                return PolicyResult(False, "caller_not_allowlisted", detail)

        # 4) 免打扰
        if self.cfg.dnd_windows and _in_windows(inp.now_hhmm, self.cfg.dnd_windows):
            if caller not in set(self.cfg.admin_uins):
                return PolicyResult(False, "dnd_window", detail)

        # 5) 重复事件
        if inp.duplicate:
            return PolicyResult(False, "duplicate_event", detail)

        # 6) 已有通话
        if inp.in_call:
            return PolicyResult(False, "already_in_call", detail)
        if not inp.ringing and inp.core_authorized is None:
            # 既不在振铃也不在通话：没有可接的来电
            return PolicyResult(False, "not_ringing", detail)

        # 7) 协调器可否建立视频会话
        if inp.core_authorized is False:
            return PolicyResult(False, "core_rejected", detail)

        # 8) 账号条件（无法确认 → 拒绝）
        if not inp.account_confirmed:
            return PolicyResult(False, "account_unconfirmed", detail)
        if self.cfg.require_single_account and inp.single_account_ok is not True:
            return PolicyResult(False, "single_account_unsatisfied", detail)

        return PolicyResult(True, "allowed", detail)

    # ---------- 挂断策略 ----------
    def should_hangup(self, *, reason: str, elapsed_seconds: float,
                      admin: bool = False) -> PolicyResult:
        if not self.cfg.auto_hangup_active and not admin:
            return PolicyResult(False, "auto_hangup_off")
        if admin:
            return PolicyResult(True, "admin_hangup")
        if reason == "max_duration":
            if elapsed_seconds >= self.cfg.max_call_seconds:
                return PolicyResult(True, "max_duration")
            return PolicyResult(False, "not_yet")
        if reason in ("heartbeat_timeout", "window_gone", "user_request", "ring_timeout"):
            return PolicyResult(True, reason)
        return PolicyResult(False, "unknown_reason")
