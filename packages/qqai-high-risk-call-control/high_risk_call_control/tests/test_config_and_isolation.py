# -*- coding: utf-8 -*-
"""配置/隔离/默认关闭 相关测试。"""
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

from high_risk_call_control.tests import fakes

PKG_DIR = Path(__file__).resolve().parents[1]


class TestConfigDefaults(unittest.TestCase):
    def test_defaults_are_all_off(self):
        from high_risk_call_control.config import load_config, effective_flags
        cfg = load_config(env={})
        self.assertFalse(cfg.enabled)
        self.assertFalse(cfg.enable_auto_answer)
        self.assertFalse(cfg.enable_auto_hangup)
        self.assertFalse(cfg.auto_answer_active)
        self.assertFalse(cfg.auto_hangup_active)
        flags = effective_flags(cfg)
        self.assertEqual(flags["extension"], False)

    def test_auto_flags_without_master_switch_degrade(self):
        from high_risk_call_control.config import load_config
        cfg = load_config(env={"QQAI_HRCC_ENABLE_AUTO_ANSWER": "1",
                               "QQAI_HRCC_ENABLE_AUTO_HANGUP": "1"})
        self.assertFalse(cfg.auto_answer_active)
        self.assertFalse(cfg.auto_hangup_active)
        self.assertTrue(any("enabled" in w for w in cfg.warnings))

    def test_env_enables_all_three_layers(self):
        from high_risk_call_control.config import load_config
        cfg = load_config(env={"QQAI_HRCC_ENABLED": "1",
                               "QQAI_HRCC_ENABLE_AUTO_ANSWER": "1",
                               "QQAI_HRCC_ENABLE_AUTO_HANGUP": "1",
                               "QQAI_HRCC_ALLOW_UINS": "10002,10003",
                               "QQAI_HRCC_DND_WINDOWS": "22:00-08:00"})
        self.assertTrue(cfg.auto_answer_active)
        self.assertTrue(cfg.auto_hangup_active)
        self.assertEqual(cfg.allow_uins, ("10002", "10003"))
        self.assertEqual(len(cfg.dnd_windows), 1)

    def test_bad_config_file_fails_closed(self):
        from high_risk_call_control.config import load_config
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "bad.json")
            with open(p, "w", encoding="utf-8") as fh:
                fh.write("{not json")
            cfg = load_config(config_path=p, env={})
            self.assertFalse(cfg.enabled)
            self.assertTrue(cfg.warnings)

    def test_uin_lists_reject_injection(self):
        from high_risk_call_control.config import load_config
        cfg = load_config(env={"QQAI_HRCC_ALLOW_UINS": "10002; rm -rf /,abc,10003"})
        # 非纯数字项一律丢弃（fail-closed），注入内容不会进入名单
        self.assertEqual(cfg.allow_uins, ("10003",))
        self.assertNotIn("rm", "".join(cfg.allow_uins))

    def test_gate_disable_turns_auto_answer_off(self):
        from high_risk_call_control.config import load_config
        cfg = load_config(env={"QQAI_HRCC_ENABLED": "1",
                               "QQAI_HRCC_ENABLE_AUTO_ANSWER": "1",
                               "QQAI_HRCC_REQUIRE_ENV_CLICK_FLAG": "0"})
        self.assertFalse(cfg.auto_answer_active)
        self.assertTrue(any("三道闸门" in w for w in cfg.warnings))

    def test_no_credentials_in_public_view(self):
        from high_risk_call_control.config import load_config
        cfg = load_config(explicit={"enabled": True, "api_key": "DUMMY_CREDENTIAL_VALUE",
                                    "password": "p"}, env={})
        dumped = json.dumps(cfg.to_public_dict(), ensure_ascii=False)
        self.assertNotIn("DUMMY_CREDENTIAL_VALUE", dumped)
        self.assertNotIn("password", dumped)


class TestIsolation(unittest.TestCase):
    def test_package_does_not_import_core(self):
        offenders = []
        pattern = re.compile(r"sys\.path\s*(\.|\[)")
        for path in PKG_DIR.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            if re.search(r"^\s*(import|from)\s+core(\b|\.)", text, re.M):
                offenders.append(path.name)
            for line in text.splitlines():
                stripped = line.strip()
                if stripped.startswith("#") or "tests" in str(path):
                    continue
                if pattern.search(stripped) or "PYTHONPATH" in stripped:
                    offenders.append(path.name + ":sys.path")
        self.assertEqual(offenders, [], f"扩展不得 import core 或改 sys.path：{offenders}")

    def test_import_has_no_side_effects(self):
        import subprocess
        code = (
            "import sys;"
            f"sys.path.insert(0, {str(PKG_DIR.parent)!r});"
            "import high_risk_call_control as m;"
            "print(m.DEFAULT_ENABLED);"
            "print('high_risk_call_control.qq_ui_controller' in sys.modules);"
        )
        out = subprocess.run([sys.executable, "-B", "-X", "utf8", "-c", code],
                             capture_output=True, text=True, encoding="utf-8",
                             env={**os.environ, "PYTHONPATH": ""})
        lines = [x.strip() for x in (out.stdout or "").splitlines() if x.strip()]
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(lines[0], "False")
        # import 时不得把 UI 控制器也拉起来
        self.assertEqual(lines[1], "False")

    def test_win32_import_only_inside_functions(self):
        text = (PKG_DIR / "qq_ui_controller.py").read_text(encoding="utf-8")
        top_level = [ln for ln in text.splitlines()
                     if ln.startswith("import ctypes") or ln.startswith("from ctypes")]
        self.assertEqual(top_level, [], "ctypes 只能在函数内部 import")

    def test_pyproject_has_no_entrypoints_or_runtime_hooks(self):
        pyproject = PKG_DIR.parent / "pyproject.toml"
        text = pyproject.read_text(encoding="utf-8")
        self.assertNotIn("[project.scripts]", text)
        self.assertNotIn("entry-points", text)
        self.assertNotIn("dependencies = [\n", text.split("[project]")[1].split("[tool")[0])
        self.assertIn("dependencies = []", text)
        # 不能有 setup.py / install hook 之类的安装期脚本
        self.assertFalse((PKG_DIR.parent / "setup.py").exists())

    def test_no_default_auto_answer_wiring_in_core(self):
        """Core 的启动器不引用本扩展；扩展不存在时 Core 行为不变。"""
        launch = PKG_DIR.parents[1] / "core" / "launch_core.py"
        if not launch.exists():
            self.skipTest("未找到 core/launch_core.py")
        text = launch.read_text(encoding="utf-8")
        self.assertNotIn("high_risk_call_control", text)


if __name__ == "__main__":
    unittest.main()
