# -*- coding: utf-8 -*-
"""QQ AI Video Runtime · 高风险通话控制扩展（**默认关闭，不属于 Core 默认能力**）。

这个包把"QQ 视频来电检测 / 自动接听 / 自动拒接 / 自动挂断 / 超时挂断 /
异常收尾 / QQ UI 自动化"从主项目里独立出来，作为需要单独安装、配置与授权的
实验性扩展。

红线（README / SECURITY.md 有完整说明）：
- Core 默认不 import 本包、不启动任何自动接听/挂断。
- import 本包不产生任何副作用：不枚举窗口、不点击、不写文件、不改 sys.path。
- 只能通过显式注入的协调器对象（core_bridge.CoreBridge）与 Core 交互。
- 自动接听、自动挂断各自独立授权；任一未开启即不执行。
- 任何条件无法确认 → 拒绝（fail-closed）。

最小用法（由调用方自己决定是否启用）：
    from extensions.high_risk_call_control.config import load_config
    from extensions.high_risk_call_control.runtime import CallRuntime
    cfg = load_config(config_path="high_risk_call_control.json")   # 默认全关
    if cfg.enabled:
        ...  # 由调用方创建 probe/actuator/bridge 后交给 CallRuntime
"""
from __future__ import annotations

__version__ = "0.1.0"
EXTENSION_NAME = "high_risk_call_control"
DEFAULT_ENABLED = False

__all__ = [
    "__version__",
    "EXTENSION_NAME",
    "DEFAULT_ENABLED",
]
