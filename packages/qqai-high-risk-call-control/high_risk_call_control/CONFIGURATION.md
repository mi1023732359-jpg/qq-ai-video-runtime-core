# CONFIGURATION · 高风险通话控制扩展

> 所有键的默认值都是**关闭 / 最保守**。配置文件读不出来时，扩展按"关闭"处理。

## 1. 加载顺序（高 → 低）

1. 代码里显式传入的 dict（测试/管理员程序）
2. 配置文件 JSON（UTF-8，可用 `QQAI_HRCC_CONFIG_FILE` 指定路径）
3. 环境变量（前缀 `QQAI_HRCC_`）
4. 内置默认值（本文件第 2 节）

未知键会被忽略并写进 `warnings`；敏感键（password/token/api_key/secret…）不会进入公开视图与审计日志。

## 2. 配置项

### 2.1 总开关（三层独立授权）

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `enabled` | bool | `false` | 扩展总开关；为假时**不探测、不点击、不调用协调器** |
| `enable_auto_answer` | bool | `false` | 自动接听；必须 `enabled=true` 才生效 |
| `enable_auto_hangup` | bool | `false` | 自动挂断；必须 `enabled=true` 才生效 |

> 只把 `enable_auto_answer` 打开、`enabled` 仍为假时：实际不生效，并写入警告
> "enable_* 为真但 enabled 为假，自动能力已按关闭处理"。

### 2.2 权限与名单

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `admin_uins` | 数字列表/逗号串 | 空 | 管理员账号；可跳过免打扰、可强制挂断/拒绝 |
| `allow_uins` | 数字列表/逗号串 | 空 | 白名单；为空表示"只允许管理员" |
| `deny_uins` | 数字列表/逗号串 | 空 | 黑名单（优先级高于白名单） |
| `dnd_windows` | `"22:00-08:00"` 或 `[[22,0],[8,0]]` | 空 | 免打扰时段；非管理员在此时间段一律拒接 |
| `require_single_account` | bool | `true` | 单账号互斥未满足时拒接 |

非纯数字的名单项会被丢弃（防注入）；不要在这里写昵称。

### 2.3 时序与限流

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `max_call_seconds` | int | `1800` | 最大通话时长（秒），到点自动挂断 |
| `ring_timeout_seconds` | int | `45` | 振铃超时（供上层使用） |
| `heartbeat_timeout_seconds` | float | `25.0` | 心跳超时判定 |
| `tick_interval_seconds` | float | `0.5` | 单步间隔（`run()` 使用） |
| `hangup_max_retries` | int | `2` | 挂断重试次数（0–5）；**含首次的总尝试次数硬上限为 5** |
| `hangup_retry_backoff_seconds` | float | `1.0` | 重试退避基数（秒） |
| `max_actions_per_minute` | int | `12` | 全部高风险动作的滑动窗口上限 |

### 2.4 UI 识别与三道接听闸门

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `qq_process_names` | 列表/逗号串 | `["QQ.exe","QQNT.exe"]` | 只接受这些进程的窗口 |
| `qq_window_title_keywords` | 列表/逗号串 | `["QQ"]` | 窗口标题关键字（辅助过滤） |
| `qq_window_classes` | 列表/逗号串 | 空 | 窗口类名白名单（可选） |
| `window_fingerprint_file` | 路径 | 空 | 人工确认的窗口指纹（含 call_candidates / 坐标） |
| `answer_confirm_file` | 路径 | 空 | 管理员确认文件（automation_id + confirmed_by） |
| `require_confirmation_file` | bool | `true` | 是否强制要求上面两个文件（**别关**） |
| `require_env_click_flag` | bool | `true` | 是否强制要求环境变量点击开关（关闭即等于关闭自动接听） |
| `require_confirm_binding` | bool | `true` | 确认文件必须绑定当前指纹 SHA256 |
| `confirm_max_age_seconds` | int | `86400` | 确认文件有效期（秒），过期即拒绝接听 |
| `env_click_flag` | string | `VIDEO_AUTO_ANSWER_CLICK` | 点击开关的环境变量名 |

### 2.5 路径与日志

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `audit_log_path` | 路径 | 空（运行时默认 `high_risk_call_control_audit.jsonl`） | 审计日志文件 |
| `stop_file` | 路径 | 空 | 紧急停止文件；存在即停 |
| `state_dir` | 路径 | 空 | 扩展自身状态目录（预留） |

## 3. 环境变量对照（前缀 `QQAI_HRCC_`）

| 环境变量 | 对应键 |
|---|---|
| `QQAI_HRCC_ENABLED` | `enabled` |
| `QQAI_HRCC_ENABLE_AUTO_ANSWER` | `enable_auto_answer` |
| `QQAI_HRCC_ENABLE_AUTO_HANGUP` | `enable_auto_hangup` |
| `QQAI_HRCC_ADMIN_UINS` / `ALLOW_UINS` / `DENY_UINS` | 对应名单 |
| `QQAI_HRCC_DND_WINDOWS` | `dnd_windows` |
| `QQAI_HRCC_MAX_CALL_SECONDS` / `HANGUP_MAX_RETRIES` / `MAX_ACTIONS_PER_MINUTE` | 对应数值 |
| `QQAI_HRCC_AUDIT_LOG_PATH` / `STOP_FILE` | 对应路径 |
| `QQAI_HRCC_WINDOW_FINGERPRINT_FILE` / `ANSWER_CONFIRM_FILE` | 三道闸门文件 |
| `QQAI_HRCC_REQUIRE_ENV_CLICK_FLAG` / `ENV_CLICK_FLAG` | 环境变量闸门 |

注意：`VIDEO_AUTO_ANSWER_CLICK` 是**独立的点击开关**，不属于 `QQAI_HRCC_` 前缀。

## 4. 配置冲突与处理

| 情况 | 处理 |
|---|---|
| `enable_*` 为真但 `enabled` 为假 | 自动能力按关闭处理，写警告 |
| `enable_auto_answer` 为真但任一道闸门被关 | 自动接听按关闭处理，写警告 |
| 确认文件缺 `issued_at` / 已过期 | 拒绝接听（`confirm_missing_issued_at` / `confirm_expired`） |
| 确认文件 `fingerprint_sha256` 与当前指纹不一致 | 拒绝接听（`confirm_binding_mismatch`） |
| `allow_uins` 为空 | 只允许 `admin_uins` |
| 同一号码同时在白名单和黑名单 | 黑名单优先（拒绝） |
| `dnd_windows` 格式非法 | 该时段被忽略，并写 `warnings` |
| 数值越界（如 max_call_seconds=0） | 收敛到允许范围（最小 1 秒） |
| 配置文件不是合法 JSON | 整体按默认（全关）处理 |
| 未知配置键 | 忽略，写 `warnings` |
| 同时传 dict 与配置文件 | 显式 dict 优先 |

## 5. 默认关闭说明

- 全新安装后：`enabled=false`、`enable_auto_answer=false`、`enable_auto_hangup=false`。
- 扩展关闭时：不枚举窗口、不点击、不调用协调器、不写审计（`init_skipped_disabled` 除外）。
- 卸载扩展：直接删除目录；Core 无需改动。
