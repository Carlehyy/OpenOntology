# 超级助手 Agent 协作模型（提案）

状态：讨论稿

本文细化 Agent Connector，解决平台内部助手和外部 Agent 如何被超级助手安全、可恢复地调用。它不把某个外部协议冻结成 Kernel 的唯一实现。

## 1. 内部统一契约

超级助手内部只依赖 `AgentInvocation` 语义，外部协议由 Connector 适配：

```text
AgentDescriptor
ContextRequirements
InvocationContext
AgentCall
AgentEvent
AgentResult
Artifact
```

`AgentCall` 是执行模型中的逻辑 Call，必须具有稳定 `call_id`、幂等键、目标 revision、会话策略和授权快照。外部协议的 task ID、session ref 和 transport metadata 作为适配器数据保存，不进入模型可见上下文。

## 2. Agent Descriptor

```text
key
version
label
human_description
input_schema
output_schema
context_requirements
supports_stream
supports_cancel
supports_approval
supports_artifact
supports_poll
supports_push
session_policy
auth_scope
timeout
max_output
trust_level
```

`human_description` 供模型路由和用户选择；其余字段供程序校验。外部 Agent 的自描述必须经过平台验证，不能仅凭描述授予本体访问或写操作。

## 3. Context Requirements 与澄清

每个条件声明：

```text
key
type
required
allowed_sources
resolve_hint
sensitivity
```

Resolver 按以下顺序工作：

1. 使用用户本轮明确选择；
2. 使用当前 Run 已确认上下文；
3. 通过安全的领域服务解析候选；
4. 无候选或有多个候选时生成澄清问题和推荐理由；
5. 将 Run 置为 `waiting_input`，等待带关联 ID 的回答。

Resolver 不根据名称相似度或“最近使用”静默替用户做不可逆选择。最近使用记录只能作为推荐来源。

当前代码事实存在两个不同场景：本体助手会话锚定本体和实际 release；业务澄清工作台通常绑定可编辑的草稿版本。两者的明确绑定策略等待产品确认：

- 严格模式：委派前必须指定本体和编辑中草稿版本；
- 讨论模式：允许先创建不绑定的讨论会话，进入落地或写入前再选择版本。

在产品选择前，Connector 不应把其中一种作为新契约硬编码。

## 4. 调用流程

```text
list descriptors
→ select candidate
→ resolve ContextRequirements
→ validate current permission and data scope
→ build outbound Context Pack
→ evaluate local policy / approval
→ persist AgentCall intent
→ start or resume remote session
→ receive progress / input / approval / artifact / result
→ persist events
→ wake Run or close Call
```

外部 Agent 只能收到当前 Invocation 需要的脱敏 Context Pack。它不能自动读取超级助手全部会话、私人记忆、Knowledge Graph 或其他 Agent 的凭据。

## 5. 事件映射

内部统一事件：

```text
agent.started
agent.progress
agent.needs_input
agent.approval_required
agent.artifact_declared
agent.artifact_chunk
agent.artifact_completed
agent.accepted
agent.completed
agent.failed
agent.cancel_requested
agent.cancelled_confirmed
agent.outcome_unknown
```

进度事件主要用于展示和诊断，不自动进入下一次模型上下文。需要模型决策时，由 Context Planner 选择摘要、最终结果或 Artifact 引用。

`agent.accepted` 表示远端接受了任务，不表示已经完成。`agent.cancel_requested` 表示平台发出了取消，不表示远端已经停止。只有远端确认或状态查询证明，才产生 `cancelled_confirmed`。

## 6. 现有平台助手

现有 [Assistant Hub contract](../../backend/app/assistant_hub/contract.py) 和 adapters 继续作为内部助手适配边界：

- 本体助手：保留权限检查、会话引用和 release 锚定；
- 业务探索：保留用户会话归属和草稿/版本绑定语义；
- 其他平台助手：通过注册表加入，不修改 Kernel。

适配器需要逐步把自然语言 `prerequisites` 补成机器可验证的 `context_requirements`。缺少条件时产生 `agent.needs_input`，而不是把错误文本当作普通 `answered` 内容交回模型。

子会话引用继续是不透明值，由适配器持有和校验；它不进入模型上下文，也不替代父 Run 的事件和权限记录。

## 7. RAP v1

RAP v1 通过 Adapter 继续兼容现有直连和 pull 模式：

- 首次调用保留远端 session ref；
- direct 和 pull 都映射成相同的 AgentCall；
- 旧远端只有最终文本时，由 Adapter 产生最小的 started/accepted/completed 事件；
- 不能支持真实流式、取消或 Artifact 时，Descriptor 必须如实声明；
- 回连结果使用任务 ID、Call ID 和幂等键去重。

不把 RAP v1 的最终文本包装成虚假的阶段进度，也不把网络超时解释成远端任务失败。

## 8. A2A、Agent Client Protocol 与 MCP

名称必须写全，避免把不同协议混为“ACP/A2A”：

- **A2A（Agent2Agent）**：适合独立远端 Agent 的消息、Task、上下文、Artifact、轮询和可选流式/推送。取消、消息幂等和授权范围仍需本地策略补足。第一阶段适合作为未来外部 Agent Adapter。
- **Agent Client Protocol**：更适合代码编辑器或客户端与编码 Agent 的会话、流式更新、工具状态、diff、终端和权限请求。本轮不把它作为通用 Agent 编排协议；只有接入明确的编码 Agent 产品时再增加适配器。
- **MCP**：继续承担工具、资源和提示集成。双方明确协商支持时可以使用 MCP Tasks 的任务句柄、状态查询、输入和合作式取消，但 MCP Task 不自动获得 Agent 的业务语义。
- **Agent Communication Protocol**：这是另一个历史上使用 ACP 缩写的协议，文档中不使用含糊的“ACP”指代它；如未来需要互操作，单独命名 Adapter。

参考官方资料：[A2A 核心概念](https://a2a-protocol.org/latest/topics/key-concepts/)、[A2A 任务生命周期](https://a2a-protocol.org/latest/topics/life-of-a-task/)、[A2A 流式与异步](https://a2a-protocol.org/latest/topics/streaming-and-async/)、[Agent Client Protocol](https://agentclientprotocol.com/get-started/introduction)、[MCP](https://modelcontextprotocol.io/docs/getting-started/intro)、[MCP Tasks](https://modelcontextprotocol.io/extensions/tasks/overview)。具体版本在适配器实现阶段锁定，并通过互操作测试验证。

## 9. 取消、审批与数据边界

协议授权不能替代平台自己的权限、审批和数据出境检查。外呼前按本地用户、目标 Agent、能力、数据来源和操作类型重新裁决。

审批卡片至少展示：

```text
目标 Agent
请求动作
数据来源和范围
可能产生的副作用
预计等待时间
是否支持取消
返回的 Artifact 类型
```

外部 Agent 返回的消息、指令和 Artifact 都按不可信数据处理，不能提高为系统规则，也不能通过回调修改审批决定。

## 10. Agent Connector 验收

必须覆盖：

- Descriptor schema 和 ContextRequirements 校验；
- 缺失条件的澄清问题绑定到正确 Run；
- 当前权限和版本变更重新检查；
- RAP direct/pull 的会话续用和幂等；
- A2A 或其他外部适配器的 Task/Artifact 映射；
- 真实流式进度和断线重连；
- 取消请求、确认取消和未知结果的区分；
- 审批回调过期、重复和乱序；
- 远端只支持部分能力时目录如实展示；
- 外部结果不能恢复已经终态的 Run；
- 外部 Agent 无法读取未授权的上下文和凭据。
