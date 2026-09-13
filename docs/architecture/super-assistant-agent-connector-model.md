# 超级助手 Agent 协作模型（开发基线 v1.0）

状态：开发基线 v1.0 的 Agent 协作专题说明

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
supports_query_status
supports_push
session_policy
auth_scope
timeout
max_output
trust_level
```

`human_description` 供模型路由和用户选择；其余字段供程序校验。外部 Agent 的自描述必须经过平台验证，不能仅凭描述授予本体访问或写操作。

`supports_query_status` 在本模型中表示 Connector 能够实现 `query_status()` 的只读远端状态查询；`supports_push` 表示能够接收远端主动事件。两者都不能由适配器自报而缺少真实握手或可执行验收。

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

当前代码事实存在两个不同场景：本体助手会话锚定本体和实际 release；业务澄清工作台绑定可编辑的草稿版本。架构决策已经冻结：

- 业务澄清通过超级助手委派时，必须同时提供目标本体和编辑中的草稿版本；
- 缺少任一条件时，Connector 不创建无绑定会话，而是生成带候选和推荐理由的 `agent.needs_input`；
- 绑定后，业务澄清子会话、后续 Call 和草稿写入都固定在该本体版本上；版本失效、权限变化或版本漂移必须重新询问或终止，不能静默切换；
- 不支持通过超级助手先创建无绑定业务澄清会话。

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

Connector 收到的 `agent.*` 是协议/适配器输入，不是第二套持久化事件注册表。持久化只使用执行模型和数据事件模型定义的 canonical 事件；同一事实只能写一次，适配器映射如下：

| Connector 输入 | 持久化 canonical 事件 | 主要状态变化 |
|---|---|---|
| `agent.started` | `attempt.started` + `call.outcome_changed` | 远端已报告开始，Call 进入 `running, outcome=remote_running` |
| `agent.accepted` | `call.outcome_changed` | `status=waiting_external, outcome=accepted`，等待远端继续 |
| `agent.progress` | `call.progress` + `call.outcome_changed` | 更新远端观测，Call 保持 `waiting_external` 和 `accepted/remote_running` outcome |
| `agent.needs_input` | `inbox.appended` + `call.outcome_changed` + `run.status_changed` | Call 进入 `waiting_external, outcome=remote_running`，以 `question_id` 关联问题，Run 进入 `waiting_input` |
| `agent.approval_required` | `approval.requested` + `call.outcome_changed` + `run.status_changed` | Call 进入 `waiting_external, outcome=remote_running`，以 `approval_id` 关联审批，Run 进入 `waiting_approval` |
| `agent.artifact_declared/chunk/completed` | `artifact.declared/chunked/completed` | Artifact 按校验和和引用落库 |
| `agent.completed/failed` | `attempt.result` + `call.outcome_changed` | `status=closed`，写入对应 outcome |
| `agent.cancel_requested` | `call.outcome_changed` | `status=cancel_requested`，保留此前 outcome，不写入 `cancel_requested` outcome |
| `agent.cancelled_confirmed` | `attempt.result` + `call.outcome_changed` | `status=closed, outcome=cancelled_confirmed` |
| `agent.outcome_unknown` | `call.outcome_changed` | `status=reconciling, outcome=outcome_unknown` |

进度事件主要用于展示和诊断，不自动进入下一次模型上下文。需要模型决策时，由 Context Planner 选择摘要、最终结果或 Artifact 引用。

`agent.accepted` 表示远端接受了任务，不表示已经完成。`agent.cancel_requested` 表示平台发出了取消，不表示远端已经停止。只有远端确认或状态查询证明，才产生 `cancelled_confirmed`。

## 6. 现有平台助手

现有 [Assistant Hub contract](../../backend/app/assistant_hub/contract.py) 和 adapters 继续作为内部助手适配边界：

- 本体助手：保留权限检查、会话引用和 release 锚定；
- 业务探索：保留用户会话归属和草稿/版本绑定语义；
- 其他平台助手：通过注册表加入，不修改 Kernel。

适配器需要逐步把自然语言 `prerequisites` 补成机器可验证的 `context_requirements`。缺少条件时产生 `agent.needs_input`，而不是把错误文本当作普通 `answered` 内容交回模型。

子会话引用继续是不透明值，由适配器持有和校验；它不进入模型上下文，也不替代父 Run 的事件和权限记录。

### 6.1 现有行为拆除清单

已确认的绑定决策需要穿透到现有适配器和领域服务，不能只在 Connector 外层增加一次校验。进入开发前必须逐条处置：

1. `assistant_hub/adapters/ontology_agent.py` 中缺少 `ontology_id` 时按最近使用本体回退的路径必须删除或改为候选推荐，不能静默选择；`AssistantSpec.prerequisites` 文案和断言该回退的旧测试也必须同步改为“缺失→needs_input/不建子会话”。
2. `assistant_hub/adapters/exploration.py` 的 `start()` 不能再无条件创建无绑定会话；`context_requirements` 必须声明 `ontology_id` 和编辑草稿版本。
3. 业务探索的**委派创建和委派恢复路径**（即 `binding_mode=delegated`）必须显式使用绑定入口，强制检查目标本体、草稿版本的 `draft + editing` 生命周期、归属和写权限；列表端点的过滤不能替代写路径校验。direct UI 入口保留空绑定和 `ontology_id`→current release 的既有契约；本次只收紧 `binding_mode=delegated`。
4. 委派会话的领域 `apply` 在绑定版本不再是 `draft + editing` 时不得静默分叉新草稿并重锚会话，必须返回版本失效并由 Run 重新询问或终止。直接 UI 会话继续遵循其既有绑定语义，但不能被伪装成委派绑定。
5. 绑定版本删除、晋级或被替代时，不能只通过拉取漂移摘要提示；领域服务应发出带版本引用的生命周期事件（例如 `promoted`、`superseded`、`deleted`），由 Inbox 唤醒关联 Run。
6. 本体助手的 HTTP 直聊入口也必须执行按本体的访问校验，不能因为绕过 Assistant Hub 就只依赖菜单级权限；Hub 路径和直聊路径的权限语义需要在契约中对齐。

以上清单是迁移任务中**超级助手委派路径**的拆除和改造范围。业务事实仍由本体/探索领域服务裁决，Connector 不能单独伪造“可编辑版本”。委派调用必须把 `ontology_id`、`draft_version_id` 和 `binding_mode=delegated` 一并写入 Call/子会话绑定快照；直接 UI 流程的兼容行为不得被这条规则隐式改变。

## 7. RAP v1

RAP v1 通过 Adapter 继续兼容现有直连和 pull 模式：

- 首次调用保留远端 session ref；
- direct 和 pull 都映射成相同的 AgentCall；
- 旧远端只有最终文本时，由 Adapter 产生最小的 started/accepted/completed 事件；
- 不能支持真实流式、取消或 Artifact 时，Descriptor 必须如实声明；
- pull 模式在存在远端任务 ID 时按任务 ID、Call ID 和幂等键去重；direct v1 没有平台幂等字段时不能声称可安全重试，必须等待明确结果或进入 `outcome_unknown`。

不把 RAP v1 的最终文本包装成虚假的阶段进度，也不把网络超时解释成远端任务失败。

RAP v1 的单端点回合模型不能凭最终文本可靠判断远端是否需要澄清或审批，因此能力必须按协议实际提供的字段声明：

| RAP 能力 | v1 默认值 | 允许声明为支持的条件 |
|---|---:|---|
| 同一 `session_ref` 继续输入 | `false` | 远端明确支持追加回合且 Adapter 能验证会话归属 |
| `agent.needs_input` | `false` | RAP minor 扩展提供结构化 `input_request`；纯文本问题不能自动升级为该事件 |
| `agent.approval_required` | `false` | 远端提供结构化审批请求和决定回送通道 |
| 取消 | `false` | 远端提供取消请求和可验证状态查询 |
| 幂等重试 | `false` | 请求携带可选 `call_id`、`idempotency_key`，远端承诺按其去重 |

如果远端只支持 `{message, session_ref}`，用户回答只能作为新的、明确关联的回合输入；Adapter 不得把普通最终文本伪装成 `needs_input`，也不得在没有远端幂等字段时自动重发可能产生副作用的请求。RAP minor 演进沿用现有版本化规则；kernel.v1 使用 `call_id`、`idempotency_key`、`input_request`、`approval_request`、`decision`、`result_status` 的 additive 字段和固定去重范围。

## 8. A2A、Agent Client Protocol 与 MCP

名称必须写全，避免把不同协议混为“ACP/A2A”：

- **A2A（Agent2Agent）**：适合独立远端 Agent 的消息、Task、上下文、Artifact、轮询和可选流式/推送。取消、消息幂等和授权范围仍需本地策略补足。列为 post-v1 deferred，不进入 kernel.v1 首发适配器。
- **Agent Client Protocol**：更适合代码编辑器或客户端与编码 Agent 的会话、流式更新、工具状态、diff、终端和权限请求。本轮不把它作为通用 Agent 编排协议；只有接入明确的编码 Agent 产品时再增加适配器。
- **MCP**：继续承担工具、资源和提示集成。双方明确协商支持时可以使用 MCP Tasks 的任务句柄、状态查询、输入和合作式取消，但 MCP Task 不自动获得 Agent 的业务语义。
- **Agent Communication Protocol**：这是另一个历史上使用 ACP 缩写的协议，文档中不使用含糊的“ACP”指代它；如未来需要互操作，单独命名 Adapter。

参考官方资料：[A2A 核心概念](https://a2a-protocol.org/latest/topics/key-concepts/)、[A2A 任务生命周期](https://a2a-protocol.org/latest/topics/life-of-a-task/)、[A2A 流式与异步](https://a2a-protocol.org/latest/topics/streaming-and-async/)、[Agent Client Protocol](https://agentclientprotocol.com/get-started/introduction)、[MCP](https://modelcontextprotocol.io/docs/getting-started/intro)、[MCP Tasks](https://modelcontextprotocol.io/extensions/tasks/overview)。首发 RAP 固定 v1，MCP 使用仓库已支持的 transport；A2A、ACP 和超级助手被外部调用属于 post-v1 deferred。

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
- RAP/MCP 的 Task/Artifact 映射；A2A 仅保留 deferred contract；
- 真实流式进度和断线重连；
- 取消请求、确认取消和未知结果的区分；
- 审批回调过期、重复和乱序；
- 远端只支持部分能力时目录如实展示；
- 外部结果不能恢复已经终态的 Run；
- 外部 Agent 无法读取未授权的上下文和凭据。
- RAP v1 无结构化输入/审批能力时不会伪造 `needs_input` 或 `approval_required`；具备 minor 扩展时，用户回答和审批决定能回送同一远端会话并按幂等键去重。
