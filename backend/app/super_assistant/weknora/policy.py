"""WeKnora 内置 MCP 的固定只读策略。

本模块刻意不依赖数据库、网络或 kernel：runtime、kernel connectors 与
注册服务共享同一份无副作用常量，避免为了拿白名单而形成反向 import。
"""
from __future__ import annotations

WEKNORA_BUILTIN_KEY = "weknora"
WEKNORA_SERVER_NAME = "platform_weknora"
WEKNORA_DISPLAY_NAME = "知识图谱(WeKnora)"
WEKNORA_DESCRIPTION = (
    "平台内置 WeKnora 知识图谱只读检索：列目录、检索分块、读文档与 "
    "Wiki 检索。工具清单由平台冻结，不接受写库工具。"
)

# 平台内置 WeKnora 的固定只读工具清单。远端 endpoint 也只发布这些工具；
# 注册、手工测试与调用前都按此白名单收敛，任何写 KB 工具都不得进入目录。
# ``ask`` 被排除：它可切换 Agent 并间接写其它工具，超出平台只读边界。
WEKNORA_ALLOWED_TOOLS: tuple[str, ...] = (
    "list_knowledge_bases",
    "search_knowledge",
    "grep_chunks",
    "list_documents",
    "read_document",
    "wiki_search",
    "wiki_read_page",
    "wiki_index",
)
WEKNORA_ALLOWED_TOOL_SET = frozenset(WEKNORA_ALLOWED_TOOLS)

# Bootstrap/state 验证的最小必需工具集；与允许清单同属一个事实源，避免
# service.py / bootstrap.py 各维护一份。
WEKNORA_REQUIRED_TOOLS = frozenset({
    "list_knowledge_bases", "search_knowledge", "read_document",
})

__all__ = [
    "WEKNORA_ALLOWED_TOOLS",
    "WEKNORA_ALLOWED_TOOL_SET",
    "WEKNORA_BUILTIN_KEY",
    "WEKNORA_DESCRIPTION",
    "WEKNORA_DISPLAY_NAME",
    "WEKNORA_REQUIRED_TOOLS",
    "WEKNORA_SERVER_NAME",
]
