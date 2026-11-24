"""实现架构 A：显式 argv 的外部 agent 子进程桥。"""

from __future__ import annotations

import app.adapters.llm.support as support


class ExternalAgentBridge(support.SubprocessBridge):
    """以 `m4b-agent-v1` 协议调用外部 agent 程序，只依赖固定角色分区请求。"""

    provider_id = "external-agent"
    bridge_name = "bridge_a"
    content_key = "output"
