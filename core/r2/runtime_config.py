#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Single configuration entry point for the open-source runtime.

Rules:
  * No personal absolute fallback.  QQAI_BOT_CONFIG or QQAI_BOT_DIR must be set.
  * QQAI_BOT_CONFIG and QQAI_BOT_DIR are mutually exclusive: setting both is a
    conflict and is reported, never silently resolved.
  * Camera selection is by explicit FriendlyName (QQAI_CAMERA_NAME, default
    "OBS Virtual Camera").  Missing or duplicate names are errors; this module
    never falls back to another device or to an arbitrary index.
  * Error text never contains credential values.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

DEFAULT_CAMERA_NAME = "OBS Virtual Camera"


class ConfigError(RuntimeError):
    """Configuration error with a stable code for doctor/tests."""

    def __init__(self, code: str, message: str, **detail):
        super().__init__(message)
        self.code = code
        self.detail = detail


class CameraConfigError(ConfigError):
    pass


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def resolve_bot_config(env: dict | None = None) -> dict:
    """Resolve the bot config path and bot directory.

    Priority is intentionally not a silent precedence: both environment
    variables set is an error, because the two names can point to different
    installations and silently choosing one could use the wrong account.
    """
    env = os.environ if env is None else env
    cfg = _clean(env.get("QQAI_BOT_CONFIG"))
    bdir = _clean(env.get("QQAI_BOT_DIR"))
    if cfg and bdir:
        raise ConfigError(
            "config_conflict",
            "QQAI_BOT_CONFIG and QQAI_BOT_DIR are both set; set exactly one",
        )
    if cfg:
        p = Path(cfg).expanduser()
        if not p.is_file():
            raise ConfigError("config_missing", "QQAI_BOT_CONFIG does not point to a file")
        return {"config": p, "bot_dir": p.parent, "source": "QQAI_BOT_CONFIG"}
    if bdir:
        d = Path(bdir).expanduser()
        candidates = [d / "config.json", d / "bot" / "config.json"]
        found = [p for p in candidates if p.is_file()]
        if len(found) > 1:
            raise ConfigError(
                "config_ambiguous",
                "QQAI_BOT_DIR contains both config.json and bot/config.json",
                candidates=[str(p) for p in candidates],
            )
        if not found:
            raise ConfigError(
                "config_missing",
                "QQAI_BOT_DIR does not contain config.json or bot/config.json",
                candidates=[str(p) for p in candidates],
            )
        p = found[0]
        return {"config": p, "bot_dir": p.parent, "source": "QQAI_BOT_DIR"}
    raise ConfigError(
        "config_missing",
        "set QQAI_BOT_CONFIG (file) or QQAI_BOT_DIR (installation directory)",
    )


def requested_camera_name(env: dict | None = None) -> str:
    env = os.environ if env is None else env
    return _clean(env.get("QQAI_CAMERA_NAME")) or DEFAULT_CAMERA_NAME


def choose_camera_index(names: list[str], requested: str | None = None) -> dict:
    """Choose exactly one camera by FriendlyName; never fall back."""
    req = _clean(requested) or DEFAULT_CAMERA_NAME
    matches = [i for i, name in enumerate(names)
               if str(name or "").strip().casefold() == req.casefold()]
    if not matches:
        raise CameraConfigError(
            "camera_not_found",
            f"camera {req!r} not found; refusing to choose another device",
            requested=req, available=[str(n) for n in names],
        )
    if len(matches) > 1:
        raise CameraConfigError(
            "camera_ambiguous",
            f"camera {req!r} appears {len(matches)} times; refusing to choose",
            requested=req, indexes=matches,
            available=[str(n) for n in names],
        )
    return {"device": matches[0], "name": names[matches[0]],
            "source": "configured_name", "requested": req}


def enumerate_camera_names() -> list[str]:
    """Read DirectShow FriendlyNames; raise CameraConfigError on failure."""
    try:
        import p2_ds_enum  # noqa: PLC0415
        res = p2_ds_enum.enumerate_dshow_cameras()
        if not res.get("ok"):
            raise CameraConfigError("camera_enum_failed",
                                    res.get("error") or "DirectShow enumeration failed")
        return [str(d.get("friendly_name") or "") for d in res.get("devices", [])]
    except CameraConfigError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise CameraConfigError("camera_enum_failed",
                                f"{type(exc).__name__}: {exc}") from exc


def resolve_camera(env: dict | None = None, enum_fn=None) -> dict:
    requested = requested_camera_name(env)
    names = enum_fn() if enum_fn is not None else enumerate_camera_names()
    return choose_camera_index(list(names), requested)


def _read_json_keys(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else {}
    except Exception as exc:  # noqa: BLE001
        return {"__error__": f"{type(exc).__name__}: {exc}"}


def doctor_report(env: dict | None = None, camera_names: list[str] | None = None) -> dict:
    """Offline-friendly environment report; never includes secrets."""
    env = os.environ if env is None else env
    out: dict = {"python": os.sys.version.split()[0], "checks": {}, "blocked": []}
    try:
        cfg = resolve_bot_config(env)
        data = _read_json_keys(cfg["config"])
        out["checks"]["bot_config"] = {
            "ok": "__error__" not in data,
            "source": cfg["source"],
            "config": str(cfg["config"]),
            "bot_dir": str(cfg["bot_dir"]),
            "has_providers": isinstance(data.get("providers"), dict),
            "error": data.get("__error__"),
        }
    except ConfigError as exc:
        out["checks"]["bot_config"] = {"ok": False, "code": exc.code,
                                        "error": str(exc), **exc.detail}
    try:
        req = requested_camera_name(env)
        if camera_names is None:
            out["checks"]["camera"] = {"ok": None, "requested": req,
                                        "note": "not enumerated in doctor_report"}
        else:
            cam = choose_camera_index(camera_names, req)
            out["checks"]["camera"] = {"ok": True, **cam,
                                        "available": list(camera_names)}
    except CameraConfigError as exc:
        out["checks"]["camera"] = {"ok": False, "code": exc.code,
                                    "error": str(exc), **exc.detail}
    out["checks"]["secrets"] = {
        "OBS_WS_PASSWORD_set": bool(env.get("OBS_WS_PASSWORD")),
        "note": "values are never printed",
    }
    out["checks"]["files"] = {
        "driver_present": (Path.cwd() / "r2" / "driver" / "Live2DCamDriver.csproj").is_file(),
        "web_present": (Path.cwd() / "web" / "index.html").is_file(),
    }
    if not out["checks"]["files"]["driver_present"]:
        out["blocked"].append("camera driver source is not in this staging tree")
    if not out["checks"]["files"]["web_present"]:
        out["blocked"].append("web UI is not in this staging tree")
    out["ok"] = all(c.get("ok") is not False for c in out["checks"].values()
                    if isinstance(c, dict) and "ok" in c)
    return out


if __name__ == "__main__":
    print(json.dumps(doctor_report(), ensure_ascii=False, indent=2))
