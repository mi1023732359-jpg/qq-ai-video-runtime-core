# ⚠️ qqai-high-risk-call-control

> **危险：本包会模拟鼠标点击 QQ 窗口，并可能自动接起 / 挂断 QQ 视频通话。**
> 默认全部关闭，必须三层显式授权才会动作；**未在真实 QQ 环境完成端到端验证**。
> 使用前必读 [`DANGER.md`](DANGER.md)、[`SECURITY.md`](high_risk_call_control/SECURITY.md)、
> [`OPERATION_GUIDE.md`](high_risk_call_control/OPERATION_GUIDE.md)。

独立打包的高风险通话控制扩展：从 [QQ AI Video Runtime Core](https://github.com/mi1023732359-jpg/qq-ai-video-runtime-core)
中拆出，作为**可选、默认关闭**的外部包分发。Core 本身不依赖本包。

## 它做什么

- QQ 视频来电检测（只读枚举窗口 + 人工确认过的窗口指纹）
- 自动接听（三道闸门 + 点击后复核真实状态）
- 自动拒接（策略拒绝，不点击）
- 自动挂断（最大时长 / 管理员 / 用户要求 / 心跳丢失 / 窗口消失）
- 通话超时挂断、异常断线收尾
- 与 Core 的 `SessionCoordinator` 对接（通过显式注入的窄接口，不 import core）

## 它不做什么

- 不做协议层接听（NapCat 无视频通话接听接口，这是平台已知约束）
- 不注入 DLL / 不改 QQ 客户端 / 不绕过平台安全机制
- 不盲点固定坐标（点击前校验窗口类名、尺寸、点位；不一致就不点）
- 不重新实现会话状态机（状态以 Core 的协调器为唯一来源）
- 默认不启用任何自动动作

## 安装

无第三方依赖，只需 Python ≥ 3.10（Windows）。

```powershell
# 方式 A：直接安装本目录（可编辑）
pip install -e .

# 方式 B：不安装，直接把本目录加入 PYTHONPATH
$env:PYTHONPATH = "<本目录>"
```

## 最小用法

```python
from high_risk_call_control.config import load_config
from high_risk_call_control.core_bridge import CoreBridge
from high_risk_call_control.qq_ui_controller import Win32WindowProbe, Win32ClickActuator
from high_risk_call_control.runtime import CallRuntime

# 1) Core 的协调器由你自己创建（本包不 import core）
from core.video_runtime.session_coordinator import SessionCoordinator
coord = SessionCoordinator(...)

# 2) 配置：默认全关；开启需要你自己改
cfg = load_config(config_path="high_risk_call_control.json")
if not cfg.enabled:
    raise SystemExit("扩展未启用（默认关闭）")

# 3) 装配
probe = Win32WindowProbe()
runtime = CallRuntime(cfg, CoreBridge(coord), probe=probe, actuator=Win32ClickActuator(probe))
runtime.initialize()

# 4) 单步驱动（自己控制节奏，扩展不做无限循环）
runtime.notify_incoming("10002")     # 来电事件由你的消息层提供
runtime.step()

# 5) 管理员控制
runtime.stop()          # 立即停手
runtime.force_hangup()  # 强制挂断
```

## 三层开关（缺一不可）

| 开关 | 默认 | 作用 |
|---|---|---|
| `enabled` | `false` | 扩展总开关；关闭时不枚举窗口、不调用协调器 |
| `enable_auto_answer` | `false` | 自动接听；另有指纹 + 管理员确认文件 + `VIDEO_AUTO_ANSWER_CLICK=1` 三道闸门 |
| `enable_auto_hangup` | `false` | 自动挂断 |

`enable_*` 为真但 `enabled` 为假 → 自动能力按关闭处理并写警告。

## 测试

```powershell
python -B -X utf8 -m unittest discover -s high_risk_call_control/tests -p "test_*.py"
```
64 项全部为 Mock（假窗口/假点击器/假协调器），不启动 QQ、不点击真实窗口。

## 文档

| 文件 | 内容 |
|---|---|
| [`DANGER.md`](DANGER.md) | 危险项逐条（自动接听/挂断/UI 自动化/合规/免责） |
| [`high_risk_call_control/README.md`](high_risk_call_control/README.md) | 扩展功能、架构、启用/关闭 |
| [`high_risk_call_control/SECURITY.md`](high_risk_call_control/SECURITY.md) | 权限清单、误操作可能、故障处理 |
| [`high_risk_call_control/OPERATION_GUIDE.md`](high_risk_call_control/OPERATION_GUIDE.md) | 启动检查、开启步骤、紧急停止、异常恢复 |
| [`high_risk_call_control/CONFIGURATION.md`](high_risk_call_control/CONFIGURATION.md) | 全部配置项与默认值 |
| [`high_risk_call_control/ACCEPTANCE_REPORT.md`](high_risk_call_control/ACCEPTANCE_REPORT.md) | 验收报告（测试/回归/遗留） |

## 已知限制

- 真实 QQ 环境未验证（窗口类名/坐标需人工采集）。
- 来电号码来自上层事件；拿不到号码 → 按未知处理并拒绝接听。
- 单账号统一未实现；`requires_account_handover` 拓扑下默认拒接。
- 多显示器/DPI 缩放需实测；QQ UI 改版需重采指纹。

## 许可

MIT © 2026 Melarizan。见 [`LICENSE`](LICENSE)。
本包按 **AS IS** 提供，无任何担保；使用风险自负。
