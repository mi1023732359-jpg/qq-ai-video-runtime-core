# CHANGELOG · qqai-high-risk-call-control

## 0.1.0 — 2026-11-07

首个独立打包版本（**默认关闭，未在真实 QQ 验证**）。

- 从 QQ AI Video Runtime Core 原 `extensions/high_risk_call_control`（现已迁移为 `packages/qqai-high-risk-call-control`）抽出为独立包。
- 三层开关：`enabled` / `enable_auto_answer` / `enable_auto_hangup`（默认全 false）。
- 自动接听三道闸门：窗口指纹 + 管理员确认文件 + `VIDEO_AUTO_ANSWER_CLICK=1`。
- 受控 UI 自动化：只读枚举 + 点击前校验窗口/点位 + 点击后状态复核，单次点击。
- 自动挂断：最大时长 / 管理员 / 用户要求 / 心跳丢失 / 窗口消失；幂等 + 有限重试 + 退避。
- 与 Core 对接仅通过显式注入的 `CoreBridge`（不 import core、不改 sys.path）。
- 审计日志：JSONL、脱敏、轮转；写失败不阻断主流程。
- 64 项 Mock 测试（含闸门绕过/确认文件绑定与过期/句柄复用/挂断上限/重启同步）；Core 90 项测试零影响。
- 新增 `DANGER.md` / `WARNING.txt`：逐条列明自动接听、自动挂断、UI 点击、合规与免责风险。
- 已知限制：真实 QQ 未验证；来电号码需上层事件注入；单账号统一未实现；DPI/多屏需实测。
