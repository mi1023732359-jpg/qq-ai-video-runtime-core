# OPERATION_GUIDE · 高风险通话控制扩展

> 适用于"已经装好 Core、想试自动接听/挂断"的运维人员。
> 每一步都可回滚；不确定时不要开启自动接听。

## 1. 启动前检查（5 分钟）

- [ ] QQ 官方客户端已登录**测试账号**（不要用主号）。
- [ ] Core 的协调器可单独跑起来（`cd core && python launch_core.py --dry-run`）。
- [ ] 扩展目录存在：`packages/qqai-high-risk-call-control/high_risk_call_control/`。
- [ ] 配置里 `enabled=false`（先别开）。
- [ ] 扩展测试全绿：
      `cd packages/qqai-high-risk-call-control && python -B -X utf8 -m unittest discover -s high_risk_call_control/tests -p "test_*.py"` → 应显示 64 项 OK。
- [ ] 确认没有其它自动化工具同时在操作 QQ（避免抢焦点）。

## 2. 如何开启自动挂断（比接听安全，建议先开这个）

1. 配置 `enabled=true`、`enable_auto_hangup=true`，其它保持关闭。
2. 设置 `max_call_seconds`（建议先 120 秒）、`hangup_max_retries=1`。
3. 启动并观察审计日志：应出现 `init`，不应出现任何点击记录（因为还没开接听）。
4. 手动打一通电话给测试账号并手动接听，确认到时间能被挂断；日志出现 `hangup_done`。

## 3. 如何开启自动接听（高风险，最后做）

1. **采集窗口指纹**（人工、一次）：
   - 让测试账号给你打视频电话，使 QQ 弹出"来电"窗口；
   - 记录窗口类名/尺寸/按钮相对坐标，写入 `window_fingerprint_file`：
     ```json
     {
       "call_candidates": [{"hwnd": 0}],
       "call_classes": ["<来电窗口类名>"],
       "active_classes": ["<通话窗口类名>"],
       "call_point": [x, y],
       "hangup_point": [x, y]
     }
     ```
   - **注意**：`hwnd=0` 只是占位；扩展用的是类名+尺寸，不是固定句柄。
2. **生成管理员确认文件** `answer_confirm_file`：
   ```json
   {
     "automation_id": "ctrl-answer-001",
     "confirmed_by": "你的名字",
     "issued_at": 1762500000,
     "fingerprint_sha256": "<指纹文件的 SHA256>"
   }
   ```
   - `issued_at` 是 Unix 秒；过期时间由 `confirm_max_age_seconds` 控制（默认 24 小时），过期即拒绝接听。
   - `fingerprint_sha256` 必须等于当前指纹文件的 SHA256（默认强制绑定），命令：
     ```powershell
     Get-FileHash <window_fingerprint_file> -Algorithm SHA256 | Select-Object -ExpandProperty Hash
     ```
3. 配置 `enabled=true`、`enable_auto_answer=true`、`allow_uins=["你的测试号"]`。
4. 启动进程时带环境变量：`VIDEO_AUTO_ANSWER_CLICK=1`（Windows PowerShell：`$env:VIDEO_AUTO_ANSWER_CLICK="1"`）。
5. 用测试账号打进来，观察：
   - 日志出现 `answer_decision` → `answer_click` → `answered`；
   - Core 状态变 `in_call`，发送租约被持有；
   - 手动挂断后出现 `hangup_done`，租约释放。
6. 若日志出现 `answer_gate_blocked` / `no_incoming_window` / `verify_failed`，说明闸门或 UI 校验挡住了——**不要绕过**，先修指纹。

## 4. 如何手动挂断

- 优先在 QQ 里人工点挂断（最稳）。
- 需要程序挂断：`runtime.force_hangup()`，或让 Core 调 `force_hangup()`。
- 手动挂断后扩展会在下一次 `step()` 探测到 IDLE，按 `window_gone` 收尾（释放租约）。

## 5. 紧急停止

三种任选其一，立即生效：

1. 创建停止文件：`echo stop > <stop_file 路径>`（内容不重要，存在即停）。
2. 代码调用 `runtime.stop()`。
3. 直接停扩展进程（Core 不受影响；视频会话会因心跳缺失超时收尾）。

停止后**不会**再有任何点击；已在进行中的通话保持现状，由人工处理。

## 6. 如何检查日志

```powershell
# 最近的审计记录
Get-Content <audit_log_path> -Tail 20 | ForEach-Object { $_ }
```

重点看：

| 事件 | 含义 |
|---|---|
| `init` | 启动自检，含 flags/warnings |
| `answer_decision` | 策略结论（allow / 拒绝原因） |
| `answer_gate_blocked` | 三道闸门没过（不会点击） |
| `answer_click` | 点击动作结果 |
| `answered` / `answer_verify_failed` | 成功 / 点完状态不对 |
| `hangup_done` / `hangup_failed` | 挂断成功 / 重试耗尽 |
| `abnormal_state` | 进入异常，需人工确认 |
| `admin_stop` / `admin_force_hangup` | 管理员动作 |

## 7. 如何从异常状态恢复

1. 先人工确认 QQ 当前是否还在通话；如仍在，手动挂断。
2. 查看日志最后几条，确认异常原因（`answer_verify_failed` / `hangup_failed`）。
3. 修好原因（UI 改版就重采指纹；配置错就改配置）。
4. 调用 `runtime.clear_abnormal()`（或重启进程；重启不会自动恢复自动接听）。
5. 先用"只开自动挂断"跑一轮，再考虑重开自动接听。

## 8. 回滚

- 改 `enabled=false` 即可，Core 不需要任何改动。
- 完全移除：删掉 `packages/qqai-high-risk-call-control/` 目录。
