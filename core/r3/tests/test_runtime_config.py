#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""S3 tests: configuration precedence, paths and camera selection (offline)."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "r2"))

import runtime_config as rc  # noqa: E402


class ConfigTests(unittest.TestCase):
    def test_missing_config_is_an_error(self):
        with self.assertRaises(rc.ConfigError) as cm:
            rc.resolve_bot_config({})
        self.assertEqual(cm.exception.code, "config_missing")

    def test_both_env_vars_conflict(self):
        with self.assertRaises(rc.ConfigError) as cm:
            rc.resolve_bot_config({"QQAI_BOT_CONFIG": "a.json",
                                   "QQAI_BOT_DIR": "dir"})
        self.assertEqual(cm.exception.code, "config_conflict")

    def test_config_file_with_spaces_and_chinese(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "我的 机器人" / "config.json"
            p.parent.mkdir()
            p.write_text("{}", encoding="utf-8")
            out = rc.resolve_bot_config({"QQAI_BOT_CONFIG": str(p)})
            self.assertEqual(out["config"], p)
            self.assertEqual(out["source"], "QQAI_BOT_CONFIG")

    def test_bot_dir_with_spaces_and_chinese(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "用户 数据" / "bot" / "config.json"
            p.parent.mkdir(parents=True)
            p.write_text("{}", encoding="utf-8")
            out = rc.resolve_bot_config({"QQAI_BOT_DIR": str(p.parent.parent)})
            self.assertEqual(out["config"], p)
            self.assertEqual(out["bot_dir"], p.parent)

    def test_bot_dir_ambiguous_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "config.json").write_text("{}", encoding="utf-8")
            (d / "bot").mkdir()
            (d / "bot" / "config.json").write_text("{}", encoding="utf-8")
            with self.assertRaises(rc.ConfigError) as cm:
                rc.resolve_bot_config({"QQAI_BOT_DIR": str(d)})
            self.assertEqual(cm.exception.code, "config_ambiguous")

    def test_camera_missing_never_falls_back(self):
        with self.assertRaises(rc.CameraConfigError) as cm:
            rc.choose_camera_index(["Other Camera"], "OBS Virtual Camera")
        self.assertEqual(cm.exception.code, "camera_not_found")
        self.assertIn("available", cm.exception.detail)

    def test_duplicate_camera_is_rejected(self):
        with self.assertRaises(rc.CameraConfigError) as cm:
            rc.choose_camera_index(["OBS Virtual Camera", "OBS Virtual Camera"],
                                   "OBS Virtual Camera")
        self.assertEqual(cm.exception.code, "camera_ambiguous")

    def test_device_indices_can_change(self):
        first = rc.choose_camera_index(["OBS Virtual Camera", "Live2D"], "OBS Virtual Camera")
        second = rc.choose_camera_index(["Live2D", "OBS Virtual Camera"], "OBS Virtual Camera")
        self.assertEqual(first["device"], 0)
        self.assertEqual(second["device"], 1)

    def test_camera_name_is_explicit_or_default(self):
        self.assertEqual(rc.requested_camera_name({}), "OBS Virtual Camera")
        self.assertEqual(rc.requested_camera_name({"QQAI_CAMERA_NAME": "My Cam"}), "My Cam")

    def test_doctor_redacts_secrets_and_reports_missing_config(self):
        env = {"OBS_WS_PASSWORD": "super-secret-value"}
        report = rc.doctor_report(env, camera_names=["OBS Virtual Camera"])
        text = json.dumps(report, ensure_ascii=False)
        self.assertNotIn("super-secret-value", text)
        self.assertFalse(report["checks"]["bot_config"]["ok"])
        self.assertEqual(report["checks"]["secrets"]["OBS_WS_PASSWORD_set"], True)
        self.assertTrue(report["checks"]["camera"]["ok"])


if __name__ == "__main__":
    unittest.main()
