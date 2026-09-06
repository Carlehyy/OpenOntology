"""助手委派枢纽 — 平台助手目录与统一委派契约。

超级助手（用户"分身"）经本包委派平台内其他助手：每个助手一个薄
adapter，职责限定为权限检查、建/续子会话、跑一个回合、事件归一；
委派引擎（super_assistant/delegation.py）只依赖本契约，不含任何
具体助手的信息。

依赖方向固定：assistant_hub → 各助手域（白名单符号，见
tests/architecture/test_assistant_delegation_boundaries.py 规则 3/4）；
本包不持有自有数据模型，子会话仍归属各助手域，委派映射归
super_assistant 域。
"""
