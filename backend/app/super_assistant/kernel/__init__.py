"""超级助手 kernel.v1 的纯合同与执行基础。

本包只承载不依赖数据库、HTTP 或具体连接器的状态和事件语义，避免把
Conversation 级 legacy runtime 的状态机复制进新路径。
"""
