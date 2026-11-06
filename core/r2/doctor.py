#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Environment doctor for the open-source staging tree.

Usage:
    python r2\\doctor.py
    python r2\\doctor.py --json

Never prints OBS password, API keys, tokens or provider values.
Exit code 0 only when configuration and camera selection are unambiguous.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

R2_DIR = Path(__file__).resolve().parent
ROOT = R2_DIR.parent

import runtime_config  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    try:
        names = runtime_config.enumerate_camera_names()
    except runtime_config.ConfigError as exc:
        report = runtime_config.doctor_report(os.environ, camera_names=[])
        report["checks"]["camera"] = {"ok": False, "code": exc.code,
                                      "error": str(exc), **exc.detail}
        report["ok"] = False
    else:
        report = runtime_config.doctor_report(os.environ, camera_names=names)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"python: {report['python']}")
        for name, check in report["checks"].items():
            print(f"[{name}] ok={check.get('ok')}")
            for key in ("source", "config", "bot_dir", "requested", "device",
                        "name", "available", "code", "error", "note"):
                if key in check and check[key] is not None:
                    print(f"  {key}: {check[key]}")
        if report["blocked"]:
            print("blocked:")
            for item in report["blocked"]:
                print(f"  - {item}")
        print("RESULT:", "PASS" if report.get("ok") else "FAIL")
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    # Script mode only (FIX-SYSPATH-1): importing this module must not mutate
    # sys.path.  Direct `python core/r2/doctor.py` keeps working.
    sys.path.insert(0, str(R2_DIR))
    sys.exit(main())
