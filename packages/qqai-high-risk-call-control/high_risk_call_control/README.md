# 高风险通话控制扩展（high_risk_call_control）

> **这不是 Core 的默认能力。**
> 本目录是一个需要**单独安装、单独配置、单独授权**的实验性高风险扩展。
> 只安装 / 只运行 Core 时，你不会得到、也不会触发任何自动接听或自动挂断能力。

## 1. 这个扩展做什么

把原开发项目里与 QQ 视频通话相关的高风险操作单独提取出来，作为可选模块：

| 能力 | 说明 |
|---|---|
| QQ 视频来电检测 | 只读枚举窗口，按人工确认过的"窗口指纹"识别来电/通话窗口 |
| 自动接听 | 三道闸门全通过才点击一次接听按钮，点后复核真实状态 |
| 自动拒接 | 管理员/策略拒绝时，不点击（只通知协调器拒绝） |
| 自动挂断 | 最大时长、管理员、用户要求、心跳丢失、窗口消失时挂断 |
| 通话超时挂断 | 超过 `max_call_seconds` 自动挂断 |
| 异常断线收尾 | 窗口消失但 Core 仍认为在通话 → 触发 `window_gone` 收尾 |
| QQ UI 自动化 | 只对明确识别到、且点击前再次校验的官方 QQ 窗口做**单次**点击 |
| 与 Core 对接 | 只通过显式注入的协调器对象（`CoreBridge`）调用 SessionCoordinator |

## 2. 明确不做什么

- 不实现 QQ 协议层接听（NapCat 没有可用的视频通话接听接口，这是已知平台约束）。
- 不注入 DLL、不修改 QQ 客户端、不绕过平台安全机制。
- 不枚举/点击非目标窗口；不盲点固定坐标。
- 不重新实现一套状态机（会话状态以 Core 的 SessionCoordinator 为唯一来源）。
- 默认不启用任何自动动作；未授权时一律 fail-closed。

## 3. 架构

```
packages/qqai-high-risk-call-control/high_risk_call_control/
  config.py            # 配置加载（默认全关，三档独立开关，fail-closed）
  audit.py             # JSONL 审计日志（脱敏、轮转、写失败不阻断）
  safety_guard.py      # 统一闸门：管理员优先、限流、停止文件、三道接听闸门
  call_detector.py     # 只读检测：窗口 → idle / incoming / active / unknown
  qq_ui_controller.py  # Win32 只读枚举 + 受控单次点击（点击前后都校验）
  call_policy.py       # 白/黑名单、免打扰、单账号、忙时、重复事件
  answer_controller.py # 策略 → 协调器 → 点击 → Core 状态复核（幂等）
  hangup_controller.py # 时长/管理员/用户/心跳/窗口消失（幂等 + 有限重试）
  core_bridge.py       # 窄接口：只调用协调器的公开方法，不 import core
  runtime.py           # 单步状态机 + 管理员控制 + 重启安全
  tests/               # 64 项纯 Mock 测试（不碰真实 QQ）
```

依赖方向是**单向**的：本扩展 → (显式传入的) 协调器对象。
Core 不 import 本扩展；本扩展不 import `core.*`、不改 `sys.path`、不装全局钩子。

## 4. 安装

不需要新依赖：只使用 Python 标准库（`ctypes` 用于窗口枚举/点击）。

```powershell
git clone https://github.com/mi1023732359-jpg/qq-ai-video-runtime-core.git
cd qq-ai-video-runtime-core
# 包就在 packages/qqai-high-risk-call-control，随仓库分发，无需 pip install
```

## 5. 配置

最小配置（默认值全部为 `false`）：

```json
{
  "enabled": false,
  "enable_auto_answer": false,
  "enable_auto_hangup": false,
  "admin_uins": [],
  "allow_uins": []
}
```

完整字段、默认值与冲突规则见 `CONFIGURATION.md`。
也可以用环境变量（前缀 `QQAI_HRCC_`）临时覆盖，见同一文档。

## 6. 启用（三步，缺一不可）

1. 把 `enabled` 设为 `true`（扩展总开关）。
2. 需要自动接听时，再把 `enable_auto_answer` 设为 `true`，并完成：
   - 采集并人工确认窗口指纹（`window_fingerprint_file`）
   - 生成管理员确认文件（`answer_confirm_file`，含 `automation_id` 与 `confirmed_by`）
   - 启动进程时设置环境变量 `VIDEO_AUTO_ANSWER_CLICK=1`
3. 需要自动挂断时，再把 `enable_auto_hangup` 设为 `true`。

任一条件缺失：扩展不会点击，只会记录"为什么没点"。

## 7. 关闭

- 临时停手：放停止文件（`stop_file` 指向的路径存在即停）或调用 `runtime.stop()`。
- 永久关闭：把 `enabled` 改回 `false`，或删掉扩展目录——Core 完全不受影响。

## 8. 已知限制 / 验证状态

- **未在真实 QQ 账号上做过端到端验证**：本轮只有 Mock 测试（64 项）与静态检查。
- 窗口指纹需要人工采集确认；QQ UI 结构变化时扩展会判 `UNKNOWN` 并停手，不会盲点。
- 来电号码来源依赖上层事件（`notify_incoming(caller)`）；拿不到号码时按未知处理 → 拒绝接听。
- 单账号统一尚未实现，`split_sessions` 拓扑下按"互斥条件不满足"处理（默认拒绝）。
- 本扩展不解决长语音 ASR、摄像头帧率、黑屏等已知运行时问题。

## 9. 测试

```powershell
cd extensions
python -B -X utf8 -m unittest discover -s high_risk_call_control/tests -p "test_*.py" -v
```

64 项全部为 Fake（假窗口、假执行器、假协调器）；不会启动 QQ、不会点击任何真实窗口、不写真实协调器 store。
Core 既有 90 项测试与扩展无关，可独立运行：

```powershell
cd core
python -B -X utf8 -m unittest discover -s r3/tests -p "test_*.py"
```
