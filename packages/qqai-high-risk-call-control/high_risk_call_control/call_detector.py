# -*- coding: utf-8 -*-
"""来电/通话检测（只读）。

硬约束：
- 只读：枚举窗口、读标题/类名/矩形/进程名，绝不发送输入、绝不点击。
- 识别失败 → UNKNOWN；UNKNOWN 永不触发任何自动动作。
- 只认定"明确匹配指纹"的窗口才可能是通话窗口；模糊匹配一律判 UNKNOWN。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Protocol


class CallState(str, Enum):
    IDLE = "idle"            # 没有来电、没有通话
    INCOMING = "incoming"    # 有匹配的来电窗口
    ACTIVE = "active"        # 有匹配的通话中窗口
    UNKNOWN = "unknown"      # 无法判断（探测失败/多个候选/指纹缺失）


@dataclass(frozen=True)
class WindowInfo:
    hwnd: int
    title: str
    class_name: str
    pid: int
    left: int
    top: int
    right: int
    bottom: int
    visible: bool = True
    process_name: str = ""

    @property
    def width(self) -> int:
        return max(0, self.right - self.left)

    @property
    def height(self) -> int:
        return max(0, self.bottom - self.top)

    def public(self) -> dict:
        return {"hwnd": self.hwnd, "class": self.class_name, "pid": self.pid,
                "rect": [self.left, self.top, self.right, self.bottom],
                "visible": self.visible, "process": self.process_name}


class WindowProbe(Protocol):
    def list_windows(self) -> list[WindowInfo]:  # pragma: no cover - 协议
        ...


@dataclass
class DetectionResult:
    state: CallState
    reason: str
    window: WindowInfo | None = None
    candidates: list[WindowInfo] = field(default_factory=list)
    detail: dict = field(default_factory=dict)

    @property
    def actionable(self) -> bool:
        return self.state in (CallState.INCOMING, CallState.ACTIVE)


@dataclass
class Fingerprint:
    """窗口指纹：来自人工确认过的采集结果（不是猜的）。"""

    call_classes: tuple[str, ...] = ()
    call_title_keywords: tuple[str, ...] = ()
    active_classes: tuple[str, ...] = ()
    active_title_keywords: tuple[str, ...] = ()
    automation_id: str = ""
    call_point: tuple[int, int] | None = None      # 相对窗口左上角的像素
    hangup_point: tuple[int, int] | None = None
    confirmed_by: str = ""

    @property
    def usable(self) -> bool:
        return bool((self.call_classes or self.call_title_keywords) and self.confirmed_by)


def load_fingerprint(fingerprint_path: str, confirm_path: str = "") -> tuple[Fingerprint, str]:
    """读指纹 + 管理员确认文件；任一缺失/不可用就返回不可用指纹。"""
    if not fingerprint_path:
        return Fingerprint(), "未配置指纹文件"
    try:
        with open(fingerprint_path, "r", encoding="utf-8-sig") as fh:
            fp = json.load(fh)
    except FileNotFoundError:
        return Fingerprint(), "指纹文件不存在"
    except Exception as exc:
        return Fingerprint(), f"指纹读取失败：{type(exc).__name__}"
    if not isinstance(fp, dict):
        return Fingerprint(), "指纹根节点不是对象"

    confirm = {}
    if confirm_path:
        try:
            with open(confirm_path, "r", encoding="utf-8-sig") as fh:
                confirm = json.load(fh) or {}
        except Exception:
            confirm = {}

    def _tup(*keys):
        for k in keys:
            v = fp.get(k)
            if isinstance(v, str):
                return tuple(x.strip() for x in v.split(",") if x.strip())
            if isinstance(v, (list, tuple)):
                return tuple(str(x).strip() for x in v if str(x).strip())
        return ()

    point = fp.get("call_point") or confirm.get("call_point")
    hp = fp.get("hangup_point") or confirm.get("hangup_point")
    auto_id = str(confirm.get("automation_id") or fp.get("automation_id") or "")
    confirmed_by = str(confirm.get("confirmed_by") or fp.get("confirmed_by") or "")
    out = Fingerprint(
        call_classes=_tup("call_classes", "incoming_classes"),
        call_title_keywords=_tup("call_title_keywords", "incoming_title_keywords"),
        active_classes=_tup("active_classes", "in_call_classes"),
        active_title_keywords=_tup("active_title_keywords", "in_call_title_keywords"),
        automation_id=auto_id,
        call_point=(int(point[0]), int(point[1])) if isinstance(point, (list, tuple)) and len(point) == 2 else None,
        hangup_point=(int(hp[0]), int(hp[1])) if isinstance(hp, (list, tuple)) and len(hp) == 2 else None,
        confirmed_by=confirmed_by,
    )
    if not out.usable:
        return out, "指纹或确认信息不完整"
    return out, ""


def _match(window: WindowInfo, classes: Iterable[str], keywords: Iterable[str]) -> bool:
    cls = (window.class_name or "").strip()
    title = (window.title or "").strip()
    if classes and cls not in set(classes):
        return False
    if keywords and not any(k and k in title for k in keywords):
        return False
    return bool(window.visible and window.width > 0 and window.height > 0)


class CallDetector:
    def __init__(self, probe: WindowProbe, fingerprint: Fingerprint,
                 allowed_process_names: tuple[str, ...] = ("QQ.exe", "QQNT.exe")):
        self.probe = probe
        self.fingerprint = fingerprint
        self.allowed_process_names = tuple(allowed_process_names)

    def detect(self) -> DetectionResult:
        if not self.fingerprint.usable:
            return DetectionResult(CallState.UNKNOWN, "fingerprint_unusable")
        try:
            windows = self.probe.list_windows()
        except Exception as exc:
            return DetectionResult(CallState.UNKNOWN, f"probe_error:{type(exc).__name__}")
        if windows is None:
            return DetectionResult(CallState.UNKNOWN, "probe_none")

        allowed = {p.lower() for p in self.allowed_process_names if p}
        usable = [w for w in windows
                  if w.pid > 0 and w.hwnd > 0
                  and (not w.process_name or w.process_name.lower() in allowed)]

        incoming = [w for w in usable
                    if _match(w, self.fingerprint.call_classes, self.fingerprint.call_title_keywords)]
        active = [w for w in usable
                  if _match(w, self.fingerprint.active_classes, self.fingerprint.active_title_keywords)]

        if len(incoming) > 1 or len(active) > 1:
            return DetectionResult(CallState.UNKNOWN, "ambiguous_candidates",
                                   candidates=incoming + active)
        if active:
            return DetectionResult(CallState.ACTIVE, "active_window", window=active[0])
        if incoming:
            return DetectionResult(CallState.INCOMING, "incoming_window", window=incoming[0])
        return DetectionResult(CallState.IDLE, "no_call_window")
