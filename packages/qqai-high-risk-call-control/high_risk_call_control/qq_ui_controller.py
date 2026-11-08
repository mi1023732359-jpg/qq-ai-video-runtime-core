# -*- coding: utf-8 -*-
"""QQ 官方客户端 UI 自动化（只做"有验证的单次点击"）。

不使用：DLL 注入、修改 QQ 文件、模拟协议封包、绕过平台安全机制。
不使用：固定坐标盲点。每次点击前都校验
  · 目标 hwnd 仍存在且标题/类名与识别时一致
  · 窗口仍可见且矩形没变（避免点空/点到别的窗口）
  · 点击点落在该窗口内部
点击后由调用方（answer/hangup controller）再次探测真实状态确认结果。

本模块可在非 Windows 环境导入（仅定义类）；真正调用 Win32 时才 import ctypes。
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from .call_detector import WindowInfo, WindowProbe


class UiAutomationError(RuntimeError):
    pass


@dataclass
class ActionResult:
    ok: bool
    reason: str
    clicked: bool = False
    detail: dict | None = None


class Win32WindowProbe(WindowProbe):
    """只读枚举顶层窗口。"""

    def __init__(self, max_windows: int = 400):
        self.max_windows = max_windows
        self._ensure_dpi()

    @staticmethod
    def _ensure_dpi() -> None:
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            try:
                import ctypes
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass

    def list_windows(self) -> list[WindowInfo]:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        out: list[WindowInfo] = []

        EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def _cb(hwnd, _lparam):
            try:
                if not user32.IsWindowVisible(hwnd):
                    return True
                length = user32.GetWindowTextLengthW(hwnd)
                title = ""
                if length > 0:
                    buf = ctypes.create_unicode_buffer(length + 1)
                    user32.GetWindowTextW(hwnd, buf, length + 1)
                    title = buf.value
                cls_buf = ctypes.create_unicode_buffer(256)
                user32.GetClassNameW(hwnd, cls_buf, 256)
                rect = wintypes.RECT()
                user32.GetWindowRect(hwnd, ctypes.byref(rect))
                pid = wintypes.DWORD(0)
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                proc_name = ""
                try:
                    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
                    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
                    if h:
                        size = wintypes.DWORD(1024)
                        pbuf = ctypes.create_unicode_buffer(1024)
                        if kernel32.QueryFullProcessImageNameW(h, 0, pbuf, ctypes.byref(size)):
                            proc_name = pbuf.value.split("\\")[-1]
                        kernel32.CloseHandle(h)
                except Exception:
                    proc_name = ""
                out.append(WindowInfo(hwnd=int(hwnd), title=title, class_name=cls_buf.value,
                                      pid=int(pid.value), left=rect.left, top=rect.top,
                                      right=rect.right, bottom=rect.bottom,
                                      visible=True, process_name=proc_name))
            except Exception:
                pass
            return True

        user32.EnumWindows(EnumWindowsProc(_cb), 0)
        return out[: self.max_windows]


class Win32ClickActuator:
    """受控单次点击：校验窗口 → 校验点 → 点击一次。绝不循环点击。"""

    def __init__(self, probe: Win32WindowProbe, settle_seconds: float = 0.05, clock=time.monotonic):
        self.probe = probe
        self.settle_seconds = settle_seconds
        self.clock = clock
        self.clicks = 0

    def _window_by_hwnd(self, hwnd: int) -> WindowInfo | None:
        for w in self.probe.list_windows():
            if w.hwnd == hwnd:
                return w
        return None

    def click_point(self, window: WindowInfo, offset: tuple[int, int]) -> ActionResult:
        x, y = int(offset[0]), int(offset[1])
        if x < 0 or y < 0 or x >= window.width or y >= window.height:
            return ActionResult(False, "point_outside_window")
        current = self._window_by_hwnd(window.hwnd)
        if current is None:
            return ActionResult(False, "window_gone")
        if (current.class_name != window.class_name
                or current.width != window.width
                or current.height != window.height):
            return ActionResult(False, "window_changed",
                                detail={"was": window.public(), "now": current.public()})
        # 句柄可能被系统复用：进程/进程名不一致一律拒绝（防误点其它程序）
        if current.pid != window.pid:
            return ActionResult(False, "window_pid_changed",
                                detail={"was_pid": window.pid, "now_pid": current.pid})
        if (window.process_name and current.process_name
                and window.process_name.lower() != current.process_name.lower()):
            return ActionResult(False, "window_process_changed",
                                detail={"was": window.process_name,
                                        "now": current.process_name})

        abs_x = current.left + x
        abs_y = current.top + y
        try:
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            user32.SetForegroundWindow(current.hwnd)
            time.sleep(self.settle_seconds)
            user32.SetCursorPos(int(abs_x), int(abs_y))
            MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0002, 0x0004
            user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
            user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
            self.clicks += 1
            return ActionResult(True, "clicked", clicked=True,
                                detail={"hwnd": current.hwnd, "x": abs_x, "y": abs_y})
        except Exception as exc:
            return ActionResult(False, f"click_error:{type(exc).__name__}")
