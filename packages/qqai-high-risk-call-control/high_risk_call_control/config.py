# -*- coding: utf-8 -*-
"""高风险通话控制扩展 · 配置（默认全部关闭，fail-closed）。

设计原则：
1. 默认关闭：不显式开启时，扩展不做任何 UI 操作、不注册任何东西。
2. 三层独立授权：扩展开关 → 自动接听 → 自动挂断，缺一不可。
3. 任何无法确认的配置（缺项/类型错/路径不可写）都按"关闭"处理。
4. 不读取、不记录任何账号密码 / API Key / token。

配置来源优先级（高 → 低）：
  A. 显式传入的 dict（测试 / 管理员代码）
  B. 配置文件（JSON，UTF-8）
  C. 环境变量 QQAI_HRCC_*
  D. 内置默认值（全部 False）
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, fields
from typing import Any

ENV_PREFIX = "QQAI_HRCC_"
DEFAULT_CONFIG_NAME = "high_risk_call_control.json"

# 允许出现在配置里的键（白名单；未知键被忽略并记录）
ALLOWED_KEYS = {
    "enabled",
    "enable_auto_answer",
    "enable_auto_hangup",
    "admin_uins",
    "allow_uins",
    "deny_uins",
    "dnd_windows",
    "require_single_account",
    "max_call_seconds",
    "ring_timeout_seconds",
    "heartbeat_timeout_seconds",
    "tick_interval_seconds",
    "hangup_max_retries",
    "hangup_retry_backoff_seconds",
    "max_actions_per_minute",
    "qq_process_names",
    "qq_window_classes",
    "qq_window_title_keywords",
    "state_dir",
    "audit_log_path",
    "stop_file",
    "answer_confirm_file",
    "window_fingerprint_file",
    "require_confirmation_file",
    "require_env_click_flag",
    "require_confirm_binding",
    "confirm_max_age_seconds",
    "env_click_flag",
}


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("1", "true", "yes", "on", "y", "是", "开"):
            return True
        if v in ("0", "false", "no", "off", "n", "否", "关", ""):
            return False
    return default


def _as_int(value: Any, default: int, minimum: int = 0, maximum: int | None = None) -> int:
    try:
        out = int(float(value))
    except (TypeError, ValueError):
        return default
    if out < minimum:
        return minimum
    if maximum is not None and out > maximum:
        return maximum
    return out


def _as_float(value: Any, default: float, minimum: float = 0.0, maximum: float | None = None) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    if out < minimum:
        return minimum
    if maximum is not None and out > maximum:
        return maximum
    return out


def _as_uins(value: Any) -> tuple[str, ...]:
    """账号名单：接受逗号分隔字符串或列表；非数字内容全部丢弃（防注入）。"""
    if value is None:
        return ()
    if isinstance(value, str):
        items = value.replace("，", ",").split(",")
    elif isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        return ()
    out = []
    for item in items:
        s = str(item).strip()
        if s.isdigit():
            out.append(s)
    return tuple(sorted(set(out)))


def _as_windows(value: Any) -> tuple[tuple[int, int], ...]:
    """免打扰时段：接受 "22:00-08:00" 或 [[22,0],[8,0]]。"""
    if value is None:
        return ()
    pairs = []
    if isinstance(value, str):
        for chunk in value.split(";"):
            chunk = chunk.strip()
            if not chunk or "-" not in chunk:
                continue
            a, b = chunk.split("-", 1)
            try:
                pairs.append(((int(a.split(":")[0]), int(a.split(":")[1])),
                              (int(b.split(":")[0]), int(b.split(":")[1]))))
            except (ValueError, IndexError):
                continue
    elif isinstance(value, (list, tuple)):
        for item in value:
            try:
                pairs.append(((int(item[0][0]), int(item[0][1])),
                              (int(item[1][0]), int(item[1][1]))))
            except (TypeError, IndexError, ValueError):
                continue
    return tuple(pairs)


def _as_str_tuple(value, default=()):
    """字符串按逗号切分；列表按元素；其余丢弃。"""
    if value is None:
        value = default
    if isinstance(value, str):
        items = value.replace("，", ",").split(",")
    elif isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        items = list(default)
    out = [str(x).strip() for x in items if str(x).strip()]
    return tuple(out) if out else tuple(default)


@dataclass(frozen=True)
class HighRiskConfig:
    """不可变配置。构造失败或关键项缺失时保持全部关闭。"""

    enabled: bool = False
    enable_auto_answer: bool = False
    enable_auto_hangup: bool = False

    admin_uins: tuple[str, ...] = ()
    allow_uins: tuple[str, ...] = ()
    deny_uins: tuple[str, ...] = ()
    dnd_windows: tuple[tuple[tuple[int, int], tuple[int, int]], ...] = ()

    require_single_account: bool = True
    max_call_seconds: int = 1800
    ring_timeout_seconds: int = 45
    heartbeat_timeout_seconds: float = 25.0
    tick_interval_seconds: float = 0.5

    hangup_max_retries: int = 2
    hangup_retry_backoff_seconds: float = 1.0
    max_actions_per_minute: int = 12

    qq_process_names: tuple[str, ...] = ("QQ.exe", "QQNT.exe")
    qq_window_classes: tuple[str, ...] = ()
    qq_window_title_keywords: tuple[str, ...] = ("QQ",)

    state_dir: str = ""
    audit_log_path: str = ""
    stop_file: str = ""
    answer_confirm_file: str = ""
    window_fingerprint_file: str = ""

    require_confirmation_file: bool = True
    require_env_click_flag: bool = True
    env_click_flag: str = "VIDEO_AUTO_ANSWER_CLICK"
    require_confirm_binding: bool = True
    confirm_max_age_seconds: int = 86400

    warnings: tuple[str, ...] = field(default_factory=tuple)

    # ---------- 派生 ----------
    @property
    def auto_answer_active(self) -> bool:
        # 接听必须在三道闸门全部启用时才可能生效；关掉任何一道都等于关闭自动接听
        return bool(self.enabled and self.enable_auto_answer
                    and self.require_confirmation_file and self.require_env_click_flag)

    @property
    def auto_hangup_active(self) -> bool:
        return bool(self.enabled and self.enable_auto_hangup)

    def to_public_dict(self) -> dict:
        """给日志/文档用的安全视图：不含任何凭据，只含开关与策略。"""
        return {
            "enabled": self.enabled,
            "enable_auto_answer": self.enable_auto_answer,
            "enable_auto_hangup": self.enable_auto_hangup,
            "admin_uins": len(self.admin_uins),
            "allow_uins": len(self.allow_uins),
            "deny_uins": len(self.deny_uins),
            "dnd_windows": len(self.dnd_windows),
            "require_single_account": self.require_single_account,
            "max_call_seconds": self.max_call_seconds,
            "ring_timeout_seconds": self.ring_timeout_seconds,
            "heartbeat_timeout_seconds": self.heartbeat_timeout_seconds,
            "hangup_max_retries": self.hangup_max_retries,
            "max_actions_per_minute": self.max_actions_per_minute,
            "require_confirmation_file": self.require_confirmation_file,
            "require_env_click_flag": self.require_env_click_flag,
            "require_confirm_binding": self.require_confirm_binding,
            "confirm_max_age_seconds": self.confirm_max_age_seconds,
        }


def _merge_layers(explicit: dict | None, file_cfg: dict, env: dict) -> dict:
    merged: dict[str, Any] = {}
    for layer in (file_cfg, explicit or {}, env):
        for k, v in (layer or {}).items():
            if k in ALLOWED_KEYS:
                merged[k] = v
    return merged


def _load_json_file(path: str) -> tuple[dict, list[str]]:
    warns = []
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            warns.append("配置文件根节点不是对象，已忽略")
            return {}, warns
        return data, warns
    except FileNotFoundError:
        return {}, warns
    except Exception as exc:  # 读不了就当没配
        warns.append(f"配置文件读取失败（{type(exc).__name__}），按关闭处理")
        return {}, warns


def _env_layer(env: dict | None = None) -> dict:
    env = os.environ if env is None else env
    out = {}
    mapping = {
        "ENABLED": "enabled",
        "ENABLE_AUTO_ANSWER": "enable_auto_answer",
        "ENABLE_AUTO_HANGUP": "enable_auto_hangup",
        "ADMIN_UINS": "admin_uins",
        "ALLOW_UINS": "allow_uins",
        "DENY_UINS": "deny_uins",
        "DND_WINDOWS": "dnd_windows",
        "MAX_CALL_SECONDS": "max_call_seconds",
        "RING_TIMEOUT_SECONDS": "ring_timeout_seconds",
        "HEARTBEAT_TIMEOUT_SECONDS": "heartbeat_timeout_seconds",
        "HANGUP_MAX_RETRIES": "hangup_max_retries",
        "MAX_ACTIONS_PER_MINUTE": "max_actions_per_minute",
        "STATE_DIR": "state_dir",
        "AUDIT_LOG_PATH": "audit_log_path",
        "STOP_FILE": "stop_file",
        "ANSWER_CONFIRM_FILE": "answer_confirm_file",
        "WINDOW_FINGERPRINT_FILE": "window_fingerprint_file",
        "REQUIRE_CONFIRMATION_FILE": "require_confirmation_file",
        "REQUIRE_CONFIRM_BINDING": "require_confirm_binding",
        "CONFIRM_MAX_AGE_SECONDS": "confirm_max_age_seconds",
        "REQUIRE_ENV_CLICK_FLAG": "require_env_click_flag",
        "ENV_CLICK_FLAG": "env_click_flag",
        "QQ_PROCESS_NAMES": "qq_process_names",
        "QQ_WINDOW_TITLE_KEYWORDS": "qq_window_title_keywords",
    }
    for suffix, key in mapping.items():
        raw = env.get(ENV_PREFIX + suffix)
        if raw is not None:
            out[key] = raw
    return out


def load_config(explicit: dict | None = None, config_path: str | None = None,
                env: dict | None = None) -> HighRiskConfig:
    """加载配置。任何异常都退化为"全关"而不是抛到调用方。"""
    warnings: list[str] = []
    file_cfg: dict = {}
    path = config_path or os.environ.get(ENV_PREFIX + "CONFIG_FILE", "")
    if path:
        file_cfg, w = _load_json_file(path)
        warnings.extend(w)
    env_cfg = _env_layer(env)
    merged = _merge_layers(explicit, file_cfg, env_cfg)
    unknown = set(file_cfg) - ALLOWED_KEYS
    if unknown:
        warnings.append("忽略未知配置项：" + ",".join(sorted(unknown)[:8]))

    cfg = HighRiskConfig(
        enabled=_as_bool(merged.get("enabled")),
        enable_auto_answer=_as_bool(merged.get("enable_auto_answer")),
        enable_auto_hangup=_as_bool(merged.get("enable_auto_hangup")),
        admin_uins=_as_uins(merged.get("admin_uins")),
        allow_uins=_as_uins(merged.get("allow_uins")),
        deny_uins=_as_uins(merged.get("deny_uins")),
        dnd_windows=_as_windows(merged.get("dnd_windows")),
        require_single_account=_as_bool(merged.get("require_single_account"), True),
        max_call_seconds=_as_int(merged.get("max_call_seconds"), 1800, 1, 86400),
        ring_timeout_seconds=_as_int(merged.get("ring_timeout_seconds"), 45, 1, 600),
        heartbeat_timeout_seconds=_as_float(merged.get("heartbeat_timeout_seconds"), 25.0, 1.0, 3600.0),
        tick_interval_seconds=_as_float(merged.get("tick_interval_seconds"), 0.5, 0.05, 10.0),
        hangup_max_retries=_as_int(merged.get("hangup_max_retries"), 2, 0, 5),
        hangup_retry_backoff_seconds=_as_float(merged.get("hangup_retry_backoff_seconds"), 1.0, 0.0, 30.0),
        max_actions_per_minute=_as_int(merged.get("max_actions_per_minute"), 12, 1, 120),
        qq_process_names=_as_str_tuple(merged.get("qq_process_names"), ("QQ.exe", "QQNT.exe")),
        qq_window_classes=_as_str_tuple(merged.get("qq_window_classes"), ()),
        qq_window_title_keywords=_as_str_tuple(merged.get("qq_window_title_keywords"), ("QQ",)),
        state_dir=str(merged.get("state_dir") or ""),
        audit_log_path=str(merged.get("audit_log_path") or ""),
        stop_file=str(merged.get("stop_file") or ""),
        answer_confirm_file=str(merged.get("answer_confirm_file") or ""),
        window_fingerprint_file=str(merged.get("window_fingerprint_file") or ""),
        require_confirmation_file=_as_bool(merged.get("require_confirmation_file"), True),
        require_env_click_flag=_as_bool(merged.get("require_env_click_flag"), True),
        require_confirm_binding=_as_bool(merged.get("require_confirm_binding"), True),
        confirm_max_age_seconds=_as_int(merged.get("confirm_max_age_seconds"), 86400, 60, 31536000),
        env_click_flag=str(merged.get("env_click_flag") or "VIDEO_AUTO_ANSWER_CLICK"),
        warnings=tuple(warnings),
    )

    # 自动接听/挂断要求扩展本身已开启；否则显式降级并留痕，绝不"看起来开着"
    if (cfg.enable_auto_answer or cfg.enable_auto_hangup) and not cfg.enabled:
        warnings.append("enable_* 为真但 enabled 为假，自动能力已按关闭处理")
    if cfg.enable_auto_answer and not (cfg.require_confirmation_file and cfg.require_env_click_flag):
        warnings.append("自动接听要求三道闸门；闸门被配置关闭 → 自动接听按关闭处理")
    if warnings:
        import dataclasses
        cfg = dataclasses.replace(cfg, warnings=tuple(warnings))
    return cfg


def effective_flags(cfg: HighRiskConfig) -> dict:
    """真正生效的开关（供运行前自检/文档使用）。"""
    return {
        "extension": cfg.enabled,
        "auto_answer": cfg.auto_answer_active,
        "auto_hangup": cfg.auto_hangup_active,
        "answer_needs_confirm_file": cfg.require_confirmation_file,
        "answer_needs_env_flag": cfg.require_env_click_flag,
    }
