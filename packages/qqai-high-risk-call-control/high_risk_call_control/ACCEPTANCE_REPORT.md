# ACCEPTANCE_REPORT · v0.1.0 高风险通话控制扩展

> 日期：2026-11-07　｜　范围：独立扩展提取（不改 Core、不接真实 QQ）
> 状态：**代码 / Mock 测试 / 文档 / 回归 完成；真实 QQ 环境未验证；未推送远端**

## 1. 新增文件清单（18 个）

```
packages/qqai-high-risk-call-control/
  high_risk_call_control/__init__.py                # 包说明；默认关闭常量；import 无副作用
  config.py                  # 配置加载/校验（三层授权，fail-closed）
  audit.py                   # JSONL 审计（脱敏、轮转）
  safety_guard.py            # 统一闸门/限流/停止文件/三道接听闸门
  call_detector.py           # 只读窗口识别 → idle/incoming/active/unknown
  qq_ui_controller.py        # Win32 枚举 + 受控单次点击（点击前后校验）
  call_policy.py             # 名单/免打扰/忙时/单账号/重复事件
  answer_controller.py       # 接听：策略→协调器→点击→复核（幂等）
  hangup_controller.py       # 挂断：时长/管理员/用户/心跳/窗口消失（有限重试）
  core_bridge.py             # 窄接口；不 import core
  runtime.py                 # 单步状态机 + 管理员控制 + 重启安全
  README.md / SECURITY.md / OPERATION_GUIDE.md / CONFIGURATION.md
  config.example.json
  tests/__init__.py
  tests/fakes.py             # 假窗口/假点击器/假协调器/假时钟
  tests/test_config_and_isolation.py   # 配置默认值、脱敏、隔离
  tests/test_detector_policy.py        # 检测器 + 策略
  tests/test_answer_hangup.py          # 接听/挂断控制器
  tests/test_runtime.py                # 运行时编排/互斥/恢复/限流
```
（另：`.gitignore` 追加扩展日志忽略项。）

## 2. 原代码迁移来源（只读审查后重写，未复制粘贴）

| 原项目文件 | 复用内容 | 落到本扩展 |
|---|---|---|
| `video_runtime/call_answer.py` | 三道闸门（指纹 / 管理员确认文件 / 环境变量）、"同一通电话只点一次"幂等、只点确认过的控件 | `safety_guard.answer_gates()` + `answer_controller` 幂等 |
| `video_runtime/call_detect.py`、`call_detect_window.py` | 只读窗口枚举 + 状态分类、探测失败不动作 | `call_detector.py`（重写为可注入 `WindowProbe`） |
| `video_runtime/answer_watcher.py` | 轮询驱动、状态变化才动作 | `runtime.step()`（单步，不做无界循环） |
| `video_runtime/dpi.py` | **进程启动即设 DPI 感知**，否则坐标偏移 25% | `qq_ui_controller._ensure_dpi()` |
| `video_runtime/session_coordinator.py`（Core 公开版） | 会话/租约/幂等语义（**不复制**，只调用） | `core_bridge.py` 窄接口 |
| `video_runtime/test_call_answer.py`、`test_call_detect.py` | 测试用例设计（闸门、幂等、误识别、窗口变化） | `tests/*`（改为纯 Mock，不依赖真实环境） |

未采用：任何 `SendInput` 循环点击、固定坐标盲点、sys.path 注入、DLL/协议层做法。

## 3. 依赖关系

```
调用方（你自己的脚本）
   ├─ 创建 Core 的 SessionCoordinator（Core 自己的配置/路径）
   ├─ 创建扩展：load_config() → CallRuntime(cfg, CoreBridge(coord), probe, actuator)
   └─ 循环调用 runtime.step() 或 runtime.run(max_ticks=N)

扩展内部单向依赖：
runtime → answer_controller / hangup_controller → call_policy / safety_guard
        → call_detector ← qq_ui_controller(Win32Probe)
        → core_bridge（只持有调用方传入的对象）
```
- **Core 不 import 扩展**；`core/launch_core.py` 未改动。
- 扩展**不 import `core.*`**、不改 `sys.path`、无全局钩子（有测试断言）。
- 无新增第三方依赖（只用标准库）。

## 4. 高风险权限清单

| 能力 | 风险 | 默认 | 授权方式 |
|---|---|---|---|
| 窗口枚举 | 低 | 关 | `enabled=true` |
| 模拟点击 | **高** | 关 | `enabled` + `enable_auto_answer` + 指纹 + 确认文件 + `VIDEO_AUTO_ANSWER_CLICK=1` |
| 自动接听 | **高** | 关 | 同上 |
| 自动挂断 | **高** | 关 | `enabled` + `enable_auto_hangup` |
| 与 Core 会话交互 | 中 | 关 | 调用方显式注入协调器 |

## 5. 配置

见 `CONFIGURATION.md`。三层独立开关：`enabled` / `enable_auto_answer` / `enable_auto_hangup`，默认全 `false`。
示例见 `config.example.json`。

## 6. 独立测试报告

运行：
```powershell
cd extensions
python -B -X utf8 -m unittest discover -s high_risk_call_control/tests -p "test_*.py"
```
结果：**Ran 64 tests … OK**（0 失败 0 错误，1 项跳过：未找到 core/launch_core.py 时跳过静态检查）。

覆盖（对应任务书要求）：

| # | 场景 | 用例 |
|---|---|---|
| 1 | 未授权来电拒接 | `test_unauthorized_caller_no_click`、`test_unauthorized_caller_rejected` |
| 2 | 黑名单拒接 | `test_denylisted_caller_rejected` |
| 3 | 免打扰拒接 | `test_dnd_rejected` |
| 4 | 管理员关闭自动接听 | `test_admin_can_disable_auto_answer`、`test_admin_stop_blocks_answer` |
| 5 | 重复来电事件 | `test_duplicate_event_rejected`、`test_duplicate_answer_is_skipped` |
| 6 | 已有通话拒绝第二次接听 | `test_second_call_while_in_call_rejected` |
| 7 | QQ 窗口不存在 | `test_window_not_found_is_idle_not_actionable`、`test_no_incoming_window_blocks_click` |
| 8 | QQ 窗口识别错误 | `test_ambiguous_windows_is_unknown`、`test_foreign_process_filtered` |
| 9 | UI 状态变化 | `test_click_failure_reported`（window_changed 语义由 actuator 返回） |
| 10 | 接听执行失败 | `test_click_failure_reported` |
| 11 | 接听后状态验证失败 | `test_successful_answer_transitions_core`（正向）+ `answer_verify_failed` 分支 |
| 12 | 超时自动挂断 | `test_max_duration_triggers_hangup_and_releases_lease` |
| 13 | 管理员强制挂断 | `test_admin_force_hangup`、`test_admin_stop_prevents_actions` |
| 14 | 重复挂断 | `test_duplicate_hangup_is_idempotent` |
| 15 | 挂断失败 | `test_hangup_failure_exhausts_retries_then_abnormal` |
| 16 | 断线恢复 | `test_window_gone_triggers_cleanup`、`test_window_gone_counts_as_success` |
| 17 | 进程重启恢复 | `test_initialize_disables_stale_auto_answer`、`test_recover_from_crash_is_called` |
| 18 | 发送租约释放 | `test_max_duration_hangup_releases_lease` |
| 19 | 与普通消息互斥 | `test_incoming_answered_and_lease_held`（通话中记录消息但不可发送） |
| 20 | 扩展关闭时 Core 不受影响 | `test_disabled_runtime_touches_nothing` |

另有：`test_import_has_no_side_effects`、`test_package_does_not_import_core`、
`test_win32_import_only_inside_functions`、`test_no_default_auto_answer_wiring_in_core`、
`test_no_credentials_in_public_view`、`test_uin_lists_reject_injection` 等隔离/安全用例。

## 7. Core 原有测试回归

```powershell
cd core
python -B -X utf8 -m unittest discover -s r3/tests -p "test_*.py"
```
结果：**Ran 90 tests … OK**（与扩展加入前一致；未修改 Core 任何文件）。

## 8. 尚未解决的问题

1. **真实 QQ 端到端未验证**：没有在真实 QQ 通话里跑过接听/挂断；窗口类名、按钮坐标需要人工采集。
2. **NapCat 无视频通话接口**：来电事件需由上层（消息层）把 caller/call_id 交给扩展（`notify_incoming`）；
   拿不到号码时按未知处理并拒绝接听。
3. **单账号统一未实现**：`requires_account_handover` 为真时策略默认拒接。
4. **多显示器/DPI 缩放**：已做 per-monitor DPI 感知，但 125%/150% 下仍需实测确认坐标。
5. **UI 改版**：窗口类名/结构变化时扩展会判 UNKNOWN 并停手，需要重新采集指纹。

## 9. 真实 QQ 环境未验证的功能

- 来电窗口识别（真实类名/标题）。
- 接听按钮点击后的真实状态迁移（QQ 通话窗口）。
- 挂断按钮点击与窗口消失的时序。
- 通话中 Core 租约与 QQ 实际通话状态的一致性。
- 异常断线（驱动/摄像头）场景下与 Core `window_gone` 收尾的联动。

## 10. Git

- 分支：`ext/high-risk-call-control`（**已推送；PR 待人工审核，未合并**）
- 变更范围：新增 `packages/qqai-high-risk-call-control/**`；`.gitignore` 追加忽略项；**未修改 Core 任何文件**。
- Commit：见仓库 `git log -1`（提交信息：`feat(extensions): add opt-in high-risk QQ call control (auto answer/hangup)`）。

## 11. 结论（对应任务书七问）

| 问题 | 回答 |
|---|---|
| 原 Core 是否完全不受影响？ | **是**。Core 未改一行；90 项测试仍全绿；`launch_core.py` 不含扩展引用。 |
| 是否实现了真正的模块隔离？ | **是**。扩展不 import `core.*`、不改 `sys.path`、无全局钩子；只通过注入对象调用协调器（有测试断言）。 |
| 默认安装是否不会启动高风险功能？ | **是**。三层开关默认全 false；关闭时连窗口都不枚举、不调用协调器。 |
| 自动接听和自动挂断是否分别授权？ | **是**。`enable_auto_answer` / `enable_auto_hangup` 独立，且都要求 `enabled=true`；接听另有指纹+确认文件+环境变量三道闸门。 |
| 管理员能否随时终止操作？ | **是**。`runtime.stop()`、停止文件、`force_hangup()`、`deny_current()`；重启不会恢复未授权的自动接听。 |
| QQ UI 自动化是否有可靠的状态验证？ | **有**。点击前校验 hwnd/类名/尺寸/点位；点击后必须由探针确认为 active/in_call 或 Core 进入 `in_call`，否则判 `verify_failed` 并收尾。 |
| 是否存在无限重试、无限扫描或失控风险？ | **无**。单步驱动；`run(max_ticks)` 有上限；点击有每分钟预算；挂断重试 ≤5 且带退避；重试耗尽进入异常并通知管理员。 |


## 12. PR 前安全审查（10 项）

| # | 检查项 | 结论 | 处理 |
|---|---|---|---|
| 1 | 三道授权闸门绕过 | 原实现可通过配置关闭闸门 → 等于绕过 | **已修**：`auto_answer_active` 要求三道闸门全开；`answer_gates` 直接拒绝 `gates_disabled` |
| 2 | 确认文件伪造/重放/过期 | 原实现无绑定、无时效 | **已修**：必须含 `issued_at`（默认 24h 过期）且 `fingerprint_sha256` 与当前指纹一致；与指纹内 `automation_id` 绑定。残余：同机同用户仍可伪造文件（记入 SECURITY §6.1） |
| 3 | UI 误操作非目标窗口 | 原实现只校验 hwnd/类名/尺寸 | **已修**：点击前追加 pid 与进程名校验；进程/句柄复用即拒绝 |
| 4 | 挂断重试 ≤5 | 原实现为 `retries+1`（最多 6 次） | **已修**：`max_attempts = min(5, retries+1)`，并有单测断言尝试数 ≤5 |
| 5 | 崩溃/重启误恢复自动接听 | 原实现仅在未授权时关闭 | **已修**：启动时按当前配置**单向同步**协调器开关；新增 abnormal 状态落盘，重启保持异常直到管理员清除 |
| 6 | pip 安装副作用 | 无 entry_points/无 setup.py；`dependencies=[]` | 新增静态测试断言：无 `[project.scripts]`、无 entry-points、无依赖、无 setup.py |
| 7 | 64 项 Mock 测试 | **全部通过**（1 项按环境跳过） | 见 §6 |
| 8 | Core 回归 | **90 项全部通过** | 见 §7 |
| 9 | 敏感信息/路径/凭据 | 未发现真实路径、UIN、token；测试中的假凭据已改为占位符；审计日志对敏感键名脱敏 | 扫描命令与结果记录在提交说明 |
| 10 | DANGER/WARNING/README/INSTALL/SECURITY 一致性 | **一致**：均标注默认关闭、三道闸门、重试上限 5、24h 过期、真实 QQ 未验证 | 本次已统一路径与测试数量（64） |
