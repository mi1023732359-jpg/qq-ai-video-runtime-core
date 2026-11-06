#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""QQ AI Video Runtime — Core launcher (start-only).

Design constraints (2026-11-06 modular split):

* **Start only.** This launcher never stops, kills, restarts or supervises
  another process. Stopping is the user's job (Ctrl+C / Task Manager).
* **No add-on loading.** Auto Answer / Camera Management are *not* imported,
  discovered or spawned here. There is no plugin scan and no plugin path.
* **No physical input.** Nothing in this file moves the cursor or synthesises
  clicks.
* **Local only.** Services bind 127.0.0.1 by their own configuration.

Usage:
    python core/launch_core.py --dry-run
    python core/launch_core.py                 # start ASR + control plane + dialogue
    python core/launch_core.py --no-asr --no-conversation
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

CORE = Path(__file__).resolve().parent
R2 = CORE / "r2"
ROOT = CORE

SERVICES = {
    "asr": ("asr_service.py", ["--port", "3010"]),
    "control": ("mini_control.py", ["--port", "8770"]),
    "conversation": ("conversation_loop.py", []),
}


def _python() -> str:
    return sys.executable


def build_plan(start_asr: bool, start_control: bool, start_conversation: bool) -> list[dict]:
    plan = []
    if start_asr:
        plan.append({"name": "asr", "script": "asr_service.py",
                     "argv": [*SERVICES["asr"][1]], "port": 3010})
    if start_control:
        plan.append({"name": "control", "script": "mini_control.py",
                     "argv": [*SERVICES["control"][1]], "port": 8770})
    if start_conversation:
        plan.append({"name": "conversation", "script": "conversation_loop.py",
                     "argv": [*SERVICES["conversation"][1]], "port": None})
    return plan


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Core launcher (start-only, no add-ons)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the start plan and exit; spawns nothing")
    ap.add_argument("--no-asr", action="store_true")
    ap.add_argument("--no-control", action="store_true")
    ap.add_argument("--no-conversation", action="store_true")
    args = ap.parse_args(argv)

    plan = build_plan(not args.no_asr, not args.no_control, not args.no_conversation)
    missing = [p["script"] for p in plan if not (R2 / p["script"]).is_file()]

    print("core:", CORE)
    print("python:", _python())
    for item in plan:
        print(f"  plan {item['name']:<12} {item['script']:<24} port={item['port']}")
    if missing:
        print("missing scripts:", ", ".join(missing), file=sys.stderr)
        return 2
    if args.dry_run:
        print("dry-run: nothing started")
        return 0

    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    started = []
    try:
        for item in plan:
            cmd = [_python(), str(R2 / item["script"]), *item["argv"]]
            proc = subprocess.Popen(cmd, cwd=str(ROOT), env=env)
            started.append((item["name"], proc.pid))
            print(f"  started {item['name']:<12} pid={proc.pid}")
    except KeyboardInterrupt:
        print("\ninterrupted by user")
    finally:
        if started:
            print("\nstarted processes (this launcher will NOT stop them):")
            for name, pid in started:
                print(f"  {name:<12} pid={pid}  (stop it yourself)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
