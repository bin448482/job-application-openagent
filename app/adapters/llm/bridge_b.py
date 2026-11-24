"""实现架构 B：显式 argv 的本机进程桥。"""

from __future__ import annotations

import app.adapters.llm.support as support


class LocalProcessBridge(support.SubprocessBridge):
    """以 `m4b-local-v1` 协议调用本机进程，只依赖固定角色分区请求。"""

    provider_id = "local-process"
    bridge_name = "bridge_b"
    content_key = "content"
