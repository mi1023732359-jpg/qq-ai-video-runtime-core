# INSTALL · qqai-high-risk-call-control

> ⚠️ 先读 [`DANGER.md`](DANGER.md)。本包默认关闭；安装不会自动启用任何能力。

## 环境要求

- Windows 10/11（UI 自动化依赖 Win32 API；非 Windows 可导入但无法真正点击）
- Python ≥ 3.10
- 本包**无第三方依赖**（`pyproject.toml` 的 `dependencies = []`）

## 方式 A：pip 可编辑安装

```powershell
cd packages/qqai-high-risk-call-control
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
python -B -X utf8 -m unittest discover -s high_risk_call_control/tests -p "test_*.py"
```

## 方式 B：不安装，直接放 PYTHONPATH

```powershell
$env:PYTHONPATH = "<repo>\packages\qqai-high-risk-call-control"
python -c "import high_risk_call_control as m; print(m.DEFAULT_ENABLED)"   # 应为 False
```

## 与 Core 一起用

本包不修改 Core、也不被 Core 导入。需要时由你的启动脚本：

1. 用 Core 自己的方式创建 `SessionCoordinator`；
2. `from high_risk_call_control.core_bridge import CoreBridge`，把协调器包进去；
3. 用 `CallRuntime` 单步驱动（自己控制循环，扩展不做无限扫描）。

## 验证安装

```powershell
python -B -X utf8 -c "from high_risk_call_control.config import load_config, effective_flags; cfg=load_config(env={}); print(effective_flags(cfg))"
```
期望输出：`{'extension': False, 'auto_answer': False, 'auto_hangup': False, ...}`

## 卸载

```powershell
pip uninstall qqai-high-risk-call-control
```
或直接删除目录；Core 不受影响。
