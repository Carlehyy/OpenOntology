# 超级助手能力与插件模型（开发基线 v1.0）

状态：开发基线 v1.0 的能力与插件专题说明

本文细化[顶层架构](./super-assistant-top-level.md)中的 Capability Registry、插件安装和工具调用边界。插件市场属于 post-v1 deferred；UI、数据库和默认资源限制以开发基线 v1.0 为准。

## 1. 概念分工

| 概念 | 作用 | 例子 |
|---|---|---|
| Capability | 运行时可调用的能力描述和执行入口 | `web_search`、发送邮件、调用 Agent |
| Tool | 有输入 schema 的单次能力调用 | MCP tool、内置读文件 |
| Agent | 能持有上下文并完成多步工作的能力 | 本体助手、远程 Agent |
| Skill | 按需加载的行为说明和流程知识 | 用户 Skill、平台 Skill |
| Connector | 把某一传输或业务域映射到统一调用语义 | MCP、RAP、A2A adapter |
| Plugin | 可安装、可版本化、可卸载的能力包 | 本地进程插件、插件包 |
| Service | 内核或领域服务提供的非模型调用依赖 | Artifact store、Memory provider |

Skill 可以指导模型选择能力，但不是自动获得执行权限的插件。Agent 也不能因为描述了某个能力，就跳过平台授权和前置条件检查。

## 2. 注册表作用域

能力目录按作用域叠加，不允许一个会话卸载或覆盖另一个会话的能力：

```text
platform fixed capabilities
    + user installed revisions
    + session enable / disable view
    + run snapshot
```

Run 创建或激活时固定可见能力的 revision、schema、授权和信任元数据。插件升级只影响新 Run；恢复旧 Run 时使用原 revision，除非用户明确迁移。

平台固定能力不能由用户插件覆盖。插件工具使用命名空间，冲突在安装或挂载前失败，不能留下半安装状态。

## 3. Capability Manifest

Manifest 的逻辑字段包括：

```text
name
version
revision
source
entrypoint
kind
trust_level
permissions
session_scope
input_schema
output_schema
side_effect_class
supports_stream
supports_cancel
supports_approval
supports_artifact
timeout
max_calls
healthcheck
secret_refs
workspace_scope
network_scope
```

`side_effect_class` 至少区分：

```text
read_only
idempotent_write
non_idempotent_write
external_async
```

Manifest 是声明，不是授权。平台根据当前用户、Run、目标资源、数据出境规则和审批结果重新裁决。插件不能声明 `read_only` 后获得更高信任。

## 4. 插件生命周期

### 安装

```text
inspect source
→ validate manifest and schema
→ freeze immutable revision
→ resolve dependencies
→ request approval if needed
→ install
→ healthcheck
→ enable for future Runs
```

安装失败必须回滚到未安装状态。启用和安装是两个动作，停用不删除审计和版本记录。

### 挂载

挂载只发生在明确的平台、用户、会话或 Run 作用域。挂载时创建能力快照、策略快照和资源预算；任何注册失败都不能留下部分工具。

### 卸载与撤销

```text
disable new calls
→ drain or cancel in-flight calls
→ revoke secret access
→ stop plugin process
→ close capability scope
→ keep audit record
```

在途外部调用被强制中止时，只能结束本地等待；其外部结果仍然需要按 `outcome_unknown` 处理。权限撤销优先于恢复旧调用，恢复必须重新进行当前授权判断。

## 5. 执行隔离

能力执行器不得直接写 Event Store、修改租约、决定审批结果或改变 Kernel 策略。它只能通过宿主提供的受限上下文请求：

- 允许的输入和文件引用；
- 受限的凭据引用；
- 取消令牌和截止时间；
- 资源预算；
- 结果和 Artifact 回传接口。

可信平台实现可以进程内运行。用户提供的可执行代码默认由独立进程宿主运行，使用允许的工作区和网络范围；本项目的进程插件协议依赖独立宿主；生产启用 user_untrusted 前必须具备独立 uid/container，否则保持 disabled。

模型不能直接把任意 shell 命令变成持久插件。安装、启用、动态挂载和使用高风险能力都经过用户确认或既有策略。

## 6. Connector 与协议边界

- **内置工具和 Skill**：优先使用进程内实现，避免序列化和不必要的依赖。
- **MCP**：承担工具、资源和提示集成；如果双方协商支持 MCP Tasks，可使用任务句柄、查询和合作式取消，但仍通过本地 Call/Run 记录。
- **用户进程插件**：参考 Harness 的 `initialize / tools/list / tools/call / progress / shutdown` 生命周期；工具仍经过 schema、权限、超时、取消、审计和有序结果处理。
- **远程工具**：通过 HTTPS/MCP Connector 接入，不把远程声明视为可信身份。
- **Agent**：走 Agent Connector，不把长任务 Agent 简化成一次 Tool Call。

协议能力必须通过握手或适配器实现确认。统一 Capability 接口不能补造远端不支持的流式、取消或 Artifact。

能力只能在安装、启用或明确挂载时按不可变 revision 注册；`tools/call`、Agent 结果或插件运行时响应只能返回数据、进度和 Artifact 引用，不能偷偷扩展当前能力目录。新增能力必须重新走 manifest、schema、权限和审批检查。

## 7. 调用策略

调用前顺序固定为：

```text
resolve capability revision
→ validate input schema
→ resolve current context and resource scope
→ classify side effect
→ evaluate policy
→ request approval if required
→ persist Call intent and idempotency key
→ dispatch Attempt
→ persist result and projection
```

只读调用可按依赖关系并行。需要审批的调用和可能产生写副作用的调用默认串行。并行结果按调用索引进入下一次模型请求，实际完成顺序保留在事件流中。

调用失败必须是模型可见的结构化结果，同时保留面向用户的诊断、重试建议和是否可能已经产生外部副作用的判断。

## 8. 用户安装和运行体验

首版管理流程只需要支持：

- 查看 manifest、来源、revision 和权限；
- 安装前测试连接或健康检查；
- 启用、停用、卸载；
- 查看最近调用、错误、耗时和资源使用；
- 看到当前 Run 实际使用的能力版本；
- 在审批卡片中看到目标、参数摘要和数据范围。

不把插件目录、原始凭据或进程日志直接注入模型上下文。需要用户排障时，通过脱敏诊断和 Artifact 引用展示。

## 9. 观测与验收

每次 Call 至少需要能关联：

```text
run_id
turn_id
step_id
call_id
attempt_id
capability revision
policy decision
approval id
idempotency key
started / completed time
result status
artifact ids
```

验收包括：manifest 原子校验、版本冻结、命名冲突、权限拒绝、审批超时、进程崩溃恢复、重复回调、卸载 drain、旧 Run 不切换新版本、凭据不进入 prompt、不支持的协议能力不会被伪造，以及插件调用响应不能注册或扩大新的 Capability。
