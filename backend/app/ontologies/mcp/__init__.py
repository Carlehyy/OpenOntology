"""本体 MCP 的路由装配与传输生命周期入口。

transport/auth 归平台接线所有（``server.py``/``auth.py``），工具业务实现
归业务域所有（``service.py``/``schemas.py``）。入口统一从
``app.ontologies.mcp.server`` 导入 ``register_routes``/``transport``：
包 ``__init__`` 保持零导入，避免与子模块形成导入环（架构测试强制）。
"""
