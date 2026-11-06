# -*- coding: utf-8 -*-
"""进程级音频采集（E2）：采 **QQ 进程树** 的声音，与系统默认输出设备无关。

为什么需要：QQ 客户端没有音视频设备选择项，跟随 Windows 设备；
如果按"默认输出 loopback"去采，一旦 QQ 把声音送到别的设备（蓝牙/虚拟线）就采不到——
2026-10-31 实测正是这样：通话中 Realtek loopback 峰值 **0.000**。

本模块用 Windows 自带的
    ActivateAudioInterfaceAsync + AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK
（Win10 2004 / build 19041 起提供）直接采**指定进程（含子进程）**的渲染音频，
不需要切系统扬声器、不需要装驱动、不需要 ProcTap 的 MSVC 编译。

设计约束（与项目红线一致）：
  · **只读采集**：只创建 AudioClient，绝不写目标进程、不注入、不改设备；
  · **失败即降级**：不支持/失败时明确返回原因，由调用方决定退回设备级 loopback；
  · **可诊断**：暴露 check_support()/setup_report()，任何异常都带原因，不静默。

用法：
    python video_runtime\\process_loopback.py            # 能力自检 + 找 QQ 进程树
    python video_runtime\\process_loopback.py --probe    # 自检后尝试开流 1 秒并报能量
"""
import ctypes
import json
import os
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

TARGET_RATE = 16000
# Win10 2004 = build 19041：进程 loopback 从这一版开始提供
MIN_BUILD = 19041

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "logs" / "process_loopback.json"


# ---------------------------------------------------------------- 能力判定
def windows_build():
    """返回 (build, 原始版本串)。"""
    try:
        ver = sys.getwindowsversion()
        return int(ver.build), "%d.%d.%d" % (ver.major, ver.minor, ver.build)
    except Exception:
        try:
            raw = subprocess.run(["cmd", "/c", "ver"], capture_output=True,
                                 text=True, errors="replace").stdout
        except Exception as exc:
            return 0, "unknown(%s)" % exc
        build = 0
        for token in raw.replace("]", " ").split():
            if token.replace(".", "").isdigit() and "." in token:
                try:
                    build = int(token.split(".")[-1])
                except Exception:
                    build = 0
        return build, raw.strip()


def check_support():
    """只读判断本机是否支持进程级 loopback。不创建任何音频对象。"""
    build, raw = windows_build()
    out = {"windows_build": build, "windows_version": raw,
           "min_build": MIN_BUILD, "supported": build >= MIN_BUILD,
           "reasons": []}
    if build < MIN_BUILD:
        out["reasons"].append("Windows build %s < %s（进程级 loopback 不可用）"
                              % (build, MIN_BUILD))
    # AUDIOCLIENT_ACTIVATION_PARAMS 相关导出只在 mmdevapi 里；这里只做存在性判断
    try:
        ctypes.WinDLL("mmdevapi.dll")
    except Exception as exc:
        out["supported"] = False
        out["reasons"].append("mmdevapi.dll 加载失败：%s" % exc)
    out["fallback"] = "audio_capture.LoopbackSource（设备级 WASAPI loopback）"
    return out


# ---------------------------------------------------------------- 进程树
def qq_pids():
    """列出所有 QQ.exe 的 pid（只读）。"""
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq QQ.exe", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, errors="replace").stdout
    except Exception as exc:
        return {"pids": [], "error": str(exc)}
    pids = []
    for line in out.splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) >= 2 and parts[0].lower().startswith("qq.exe"):
            try:
                pids.append(int(parts[1]))
            except Exception:
                continue
    return {"pids": pids, "count": len(pids), "error": None}


def process_tree(root_pids):
    """根 pid + 全部子进程（QQ 是 Chromium 多进程，渲染/音频在子进程里）。只读。"""
    try:
        out = subprocess.run(
            ["wmic", "process", "get", "ProcessId,ParentProcessId", "/format:csv"],
            capture_output=True, text=True, errors="replace").stdout
    except Exception as exc:
        return {"pids": sorted(set(root_pids)), "error": str(exc)}
    parent = {}
    for line in out.splitlines():
        cells = [c.strip() for c in line.split(",")]
        if len(cells) >= 3 and cells[-1].isdigit() and cells[-2].isdigit():
            try:
                parent[int(cells[-1])] = int(cells[-2])
            except Exception:
                continue
    roots = set(int(p) for p in root_pids)
    pids = set(roots)
    changed = True
    while changed:
        changed = False
        for pid, ppid in parent.items():
            if ppid in pids and pid not in pids:
                pids.add(pid)
                changed = True
    return {"pids": sorted(pids), "roots": sorted(roots), "error": None}


def main(argv):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            continue
    support = check_support()
    qq = qq_pids()
    tree = process_tree(qq["pids"])
    payload = {"at": time.strftime("%Y-%m-%d %H:%M:%S"),
               "support": support, "qq": qq, "tree": tree,
               "note": "E2 第一层：能力与进程树（只读）。音频流实现见下轮。"}
    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"supported": support["supported"],
                      "build": support["windows_build"],
                      "qq_pids": qq["count"], "tree_size": len(tree["pids"]),
                      "evidence": str(EVIDENCE)}, ensure_ascii=False))
    return 0 if support["supported"] else 3


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
