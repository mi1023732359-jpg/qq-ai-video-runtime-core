#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Stage B 验证：用 DirectShow 实际枚举视频采集设备并读出 FriendlyName（纯 ctypes COM）。

为什么必须做这一步：
    注册表里"有键"不等于"滤镜能被加载"。只有真正 CoCreateInstance 出 System Device Enumerator、
    枚举 CLSID_VideoInputDeviceCategory、再对每个 moniker 取 FriendlyName，才能证明：
      - DLL 的 DllGetClassObject 能加载
      - 滤镜能在采集设备类别里实例化
      - 应用侧选设备时能看到这个名字

⚠️ 口径：DirectShow 可见 **不代表** Media Foundation 可见（QQ/Chromium 走 MF）。
    两者分别验证：本文件 = DS，`r2\\p2_mf_enum.py` = MF。

用法（在 QQAI_video_runtime 根目录）：
    python r2\\p2_ds_enum.py
退出码：0 枚举成功；1 失败。
"""
from __future__ import annotations

import ctypes
import json
import sys
from ctypes import wintypes
from datetime import datetime
from pathlib import Path

R2_ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = R2_ROOT / "logs"

ole32 = ctypes.WinDLL("ole32", use_last_error=True)

CLSCTX_INPROC_SERVER = 1
S_OK, S_FALSE = 0, 1


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

    @classmethod
    def parse(cls, text: str) -> "GUID":
        g = cls()
        ole32.CLSIDFromString(ctypes.c_wchar_p(text), ctypes.byref(g))
        return g


class VARIANT_UNION(ctypes.Union):
    _fields_ = [("bstrVal", ctypes.c_wchar_p), ("punkVal", ctypes.c_void_p),
                ("ulVal", wintypes.ULONG), ("llVal", ctypes.c_longlong),
                ("p", ctypes.c_void_p)]


class VARIANT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("vt", wintypes.WORD), ("wReserved1", wintypes.WORD),
                ("wReserved2", wintypes.WORD), ("wReserved3", wintypes.WORD),
                ("u", VARIANT_UNION)]


CLSID_SystemDeviceEnum = GUID.parse("{62BE5D10-60EB-11d0-BD3B-00A0C911CE86}")
IID_ICreateDevEnum = GUID.parse("{29840822-5B84-11D0-BD3B-00A0C911CE86}")
CLSID_VideoInputDeviceCategory = GUID.parse("{860BB310-5D01-11d0-BD3B-00A0C911CE86}")
IID_IPropertyBag = GUID.parse("{55272A00-42CB-11CE-8135-00AA004BB851}")


def _fn(ptr, index: int, restype, *argtypes):
    vtable = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(vtable[index])


def release(ptr) -> None:
    if ptr:
        _fn(ptr, 2, ctypes.c_ulong)(ptr)


def read_friendly_name(moniker) -> str | None:
    """IMoniker::BindToStorage（vtable 9）→ IPropertyBag::Read（vtable 3）取 FriendlyName。"""
    bag = ctypes.c_void_p()
    hr = _fn(moniker, 9, ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p,
             ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p))(
        moniker, None, None, ctypes.byref(IID_IPropertyBag), ctypes.byref(bag))
    if hr != 0 or not bag:
        return None
    try:
        var = VARIANT()
        ctypes.memset(ctypes.byref(var), 0, ctypes.sizeof(var))
        hr = _fn(bag, 3, ctypes.c_long, ctypes.c_wchar_p,
                 ctypes.POINTER(VARIANT), ctypes.c_void_p)(
            bag, "FriendlyName", ctypes.byref(var), None)
        if hr != 0:
            return None
        name = var.bstrVal if var.vt == 8 else None  # VT_BSTR
        if name:
            try:
                ole32.VariantClear(ctypes.byref(var))
            except Exception:  # noqa: BLE001
                pass
        return name
    finally:
        release(bag)


def enumerate_dshow_cameras() -> dict:
    dev_enum = ctypes.c_void_p()
    hr = ole32.CoCreateInstance(ctypes.byref(CLSID_SystemDeviceEnum), None,
                                CLSCTX_INPROC_SERVER, ctypes.byref(IID_ICreateDevEnum),
                                ctypes.byref(dev_enum))
    if hr != 0 or not dev_enum:
        return {"ok": False, "stage": "CoCreateInstance", "hr": hex(hr & 0xFFFFFFFF)}

    enum_moniker = ctypes.c_void_p()
    try:
        cat = CLSID_VideoInputDeviceCategory
        hr = _fn(dev_enum, 3, ctypes.c_long, ctypes.POINTER(GUID),
                 ctypes.POINTER(ctypes.c_void_p), wintypes.DWORD)(
            dev_enum, ctypes.byref(cat), ctypes.byref(enum_moniker), 0)
        # S_FALSE 表示"类别存在但 0 个设备"，此时 enum_moniker 为 NULL
        if hr not in (S_OK, S_FALSE):
            return {"ok": False, "stage": "CreateClassEnumerator", "hr": hex(hr & 0xFFFFFFFF)}
        if hr == S_FALSE or not enum_moniker:
            return {"ok": True, "count": 0, "devices": [],
                    "note": "类别枚举器为空（S_FALSE）"}

        devices = []
        while True:
            moniker = ctypes.c_void_p()
            fetched = wintypes.ULONG(0)
            hr = _fn(enum_moniker, 3, ctypes.c_long, wintypes.ULONG,
                     ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.ULONG))(
                enum_moniker, 1, ctypes.byref(moniker), ctypes.byref(fetched))
            if hr != 0 or fetched.value == 0 or not moniker:
                break
            devices.append({"index": len(devices), "friendly_name": read_friendly_name(moniker)})
            release(moniker)
        return {"ok": True, "count": len(devices), "devices": devices}
    finally:
        release(enum_moniker)
        release(dev_enum)


def main() -> int:
    ole32.CoInitializeEx(None, 0x2)
    try:
        res = enumerate_dshow_cameras()
    finally:
        ole32.CoUninitialize()

    names = [d.get("friendly_name") or "" for d in res.get("devices", [])]
    obs = [n for n in names if "obs" in n.lower()]
    print(f"[DirectShow 枚举] {'成功' if res.get('ok') else '失败'}  设备数={res.get('count', 'n/a')}")
    for d in res.get("devices", []):
        print(f"  #{d['index']} {d['friendly_name']!r}")
    if not res.get("ok"):
        print(f"  失败阶段={res.get('stage')} hr={res.get('hr')}")
    print(f"\n  OBS Virtual Camera 在 DirectShow 中: {'**可见**' if obs else '不可见'} {obs}")

    payload = {
        "at": datetime.now().isoformat(timespec="seconds"),
        "api": "DirectShow ICreateDevEnum + IPropertyBag",
        "result": res,
        "obs_visible_to_dshow": bool(obs),
        "names": names,
        "caveat": "DS 可见 ≠ MF 可见；QQ(Chromium) 走 MF，需对照 logs\\p2_mf_enum.json。",
    }
    out = LOG_DIR / "p2_ds_enum.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"证据：{out}")
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
