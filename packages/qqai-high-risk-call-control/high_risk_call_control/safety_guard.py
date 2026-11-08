# -*- coding: utf-8 -*-
"""安全闸门：所有高风险动作的统一出入口。

职责：
1. fail-closed：任何"不确定"都返回拒绝原因，而不是放行。
2. 管理员最高优先级：管理员停止 / 关闭开关永远优先于任何自动流程。
3. 动作预算：滑动窗口限流，防止"无限点击/无限扫描"。
4. 紧急停止文件：运维放置 stop 文件即可立刻停手，不需要重启进程。
5. 三道接听闸门（沿用原项目已验证的做法）：
   a. 窗口指纹文件里存在 call_candidates
   b. 管理员确认文件含 automation_id 且 confirmed_by 非空
   c. 显式环境变量点击开关（默认 VIDEO_AUTO_ANSWER_CLICK=1）
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Decision:
    allowed: bool
    reason: str
    detail: dict = field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self.allowed)


class RateWindow:
    """滑动窗口限流：最多 n 次 / period 秒。"""

    def __init__(self, limit: int, period_seconds: float = 60.0, clock=time.monotonic):
        self.limit = max(1, int(limit))
        self.period = float(period_seconds)
        self.clock = clock
        self._events: list[float] = []

    def _prune(self, now: float) -> None:
        cutoff = now - self.period
        self._events = [t for t in self._events if t >= cutoff]

    def try_acquire(self) -> tuple[bool, str]:
        now = self.clock()
        self._prune(now)
        if len(self._events) >= self.limit:
            return False, f"动作超限（{self.limit}/{self.period:.0f}s）"
        self._events.append(now)
        return True, ""

    def remaining(self) -> int:
        now = self.clock()
        self._prune(now)
        return max(0, self.limit - len(self._events))


def _read_json(path: str | os.PathLike) -> tuple[dict | None, str]:
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            return None, "根节点不是对象"
        return data, ""
    except FileNotFoundError:
        return None, "文件不存在"
    except Exception as exc:
        return None, f"读取失败：{type(exc).__name__}"


class SafetyGuard:
    """高风险动作的唯一入口。构造时不做任何 IO，只有调用才读状态。"""

    def __init__(self, cfg, audit, clock=time.monotonic, wall=time.time, env=None):
        self.cfg = cfg
        self.audit = audit
        self.clock = clock
        self.wall = wall
        self.env = os.environ if env is None else env
        self.rate = RateWindow(cfg.max_actions_per_minute, 60.0, clock)
        self.admin_stopped = False
        self.abnormal = False

    # ---------- 管理员 ----------
    def admin_request_stop(self, by: str = "admin", reason: str = "manual_stop") -> None:
        self.admin_stopped = True
        self.audit.write("admin_stop", by=by, reason=reason)

    def admin_clear_stop(self, by: str = "admin") -> None:
        self.admin_stopped = False
        self.audit.write("admin_clear_stop", by=by)

    def is_admin(self, caller: str) -> bool:
        return str(caller) in set(self.cfg.admin_uins)

    # ---------- 基础状态 ----------
    def stop_file_present(self) -> bool:
        path = self.cfg.stop_file
        if not path:
            return False
        try:
            return Path(path).exists()
        except Exception:
            return True  # 无法判断 → 视为存在（fail-closed）

    def stopped_reason(self) -> str:
        if self.admin_stopped:
            return "admin_stopped"
        if self.abnormal:
            return "abnormal_state"
        if self.stop_file_present():
            return "stop_file_present"
        return ""

    def base_gate(self, action: str) -> Decision:
        """所有动作共用的前置检查。"""
        if not self.cfg.enabled:
            return Decision(False, "extension_disabled")
        stop = self.stopped_reason()
        if stop:
            return Decision(False, stop)
        ok, why = self.rate.try_acquire()
        if not ok:
            self.audit.write("rate_limited", action=action, reason=why)
            return Decision(False, "rate_limited", {"why": why})
        return Decision(True, "ok")

    # ---------- 接听三道闸门 ----------
    def answer_gates(self) -> Decision:
        """接听前的三道闸门。任一不满足 → 不点击。"""
        if not self.cfg.auto_answer_active:
            return Decision(False, "auto_answer_off")
        # 硬性要求：三道闸门必须都在开启状态；任何一道被配置关闭都视为禁止接听
        if not (self.cfg.require_confirmation_file and self.cfg.require_env_click_flag):
            return Decision(False, "gates_disabled")

        detail = {"fingerprint": False, "confirm": False, "env_flag": False,
                  "binding": False, "fresh": False}

        fp_path = self.cfg.window_fingerprint_file
        fp, why = _read_json(fp_path) if fp_path else (None, "未配置指纹文件")
        candidates = (fp or {}).get("call_candidates") if isinstance(fp, dict) else None
        detail["fingerprint"] = bool(candidates)
        if not candidates:
            return Decision(False, "fingerprint_missing", {"why": why, **detail})

        # 指纹内容哈希：把确认文件绑定到这一份指纹，防止拿旧确认配新指纹
        try:
            with open(fp_path, "rb") as fh:
                fp_sha = hashlib.sha256(fh.read()).hexdigest()
        except Exception:
            return Decision(False, "fingerprint_unreadable", detail)

        confirm, cwhy = _read_json(self.cfg.answer_confirm_file) if self.cfg.answer_confirm_file else (None, "未配置确认文件")
        if not isinstance(confirm, dict):
            return Decision(False, "confirm_missing", {"why": cwhy, **detail})

        auto_id = str(confirm.get("automation_id") or "").strip()
        confirmed_by = str(confirm.get("confirmed_by") or "").strip()
        if not auto_id or not confirmed_by:
            return Decision(False, "confirm_missing", {"why": "缺少 automation_id / confirmed_by", **detail})

        # 与指纹里的 automation_id 绑定（若指纹里写了）
        fp_auto = str(fp.get("automation_id") or "").strip()
        if fp_auto and fp_auto != auto_id:
            return Decision(False, "confirm_id_mismatch",
                            {"fingerprint_automation_id": fp_auto, **detail})

        # 绑定指纹哈希（默认强制）
        detail["binding"] = True
        if self.cfg.require_confirm_binding:
            bound = str(confirm.get("fingerprint_sha256") or "").strip().lower()
            if not bound:
                return Decision(False, "confirm_not_bound", detail)
            if bound != fp_sha:
                return Decision(False, "confirm_binding_mismatch",
                                {"expected": fp_sha[:12], "got": bound[:12], **detail})

        # 过期检查（默认 24 小时）
        issued = confirm.get("issued_at")
        try:
            issued_s = int(float(issued))
        except (TypeError, ValueError):
            return Decision(False, "confirm_missing_issued_at", detail)
        age = self.wall() - issued_s
        if age < -300:       # 允许 5 分钟时钟漂移
            return Decision(False, "confirm_from_future", {"age_s": round(age, 1), **detail})
        if age > self.cfg.confirm_max_age_seconds:
            return Decision(False, "confirm_expired",
                            {"age_s": round(age, 1), "max_age_s": self.cfg.confirm_max_age_seconds, **detail})
        detail["fresh"] = True
        detail["confirm"] = True

        flag = str(self.env.get(self.cfg.env_click_flag, ""))
        detail["env_flag"] = flag == "1"
        if flag != "1":
            return Decision(False, "env_flag_missing", detail)

        return Decision(True, "ok", detail)

    # ---------- 挂断闸门 ----------
    def hangup_gate(self, reason: str) -> Decision:
        if not self.cfg.auto_hangup_active:
            return Decision(False, "auto_hangup_off")
        return Decision(True, "ok", {"reason": reason})

    # ---------- 异常 ----------
    def mark_abnormal(self, reason: str) -> None:
        self.abnormal = True
        self.audit.write("abnormal_state", reason=reason)

    def clear_abnormal(self, by: str = "admin") -> None:
        self.abnormal = False
        self.audit.write("abnormal_cleared", by=by)
