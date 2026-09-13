# 超级助手架构审查交接包

状态：供外部 Agent 审查的交接材料。本文不代表功能已经实现，也不替代源码、测试、数据库迁移和项目级 `AGENTS.md` 的事实权威。

## 1. 审查上下文

本次工作是在 OpenOntology 即将商用的背景下，重新审视“超级助手”的整体架构。目标不是立即改代码，而是先形成一套能够指导后续开发的顶层设计，并明确哪些语义必须先冻结。

当前设计工作区（用环境变量表示，避免将个人绝对路径提交到仓库）：

```text
$OPENONTOLOGY_REVIEW_ROOT
```

当前分支：

```text
codex/super-assistant-commercial-20260913
```

该分支从本地 `nano-ontoprompt` 创建，当前只包含架构设计文档，没有超级助手业务代码改动。原始 OpenOntology 工作树和参考 Harness 的位置如下：

```text
OpenOntology 基线工作树：
$OPENONTOLOGY_BASE_ROOT

参考 Rust Harness：
$RUST_DEEPSEEK_HARNESS_ROOT
```

参考 Harness 当前已同步到 `origin/main`，审查时只借鉴其插件化、注册表、任务循环、请求重建和恢复等架构思想，不把其实现直接视为 OpenOntology 的迁移方案。

## 2. 原始需求的审查版整理

以下内容保留用户原始意图，但按架构审查需要重新组织，并非逐字转录：

### 产品目标

将超级助手升级为面向商用的、懂用户且可持续扩展的 Agent 平台入口。它应能结合用户私人知识和长期交互记忆，调用平台内部助手、用户自建插件和外部 Agent，完成短任务与长尾任务，并在上下文、权限、执行状态和故障恢复方面保持稳定。

### 用户提出的核心能力

1. **知识图谱**：用户可以添加私人领域知识，使超级助手在回答和执行任务时更了解用户。
2. **记忆宫殿**：通过持续交互积累用户偏好、事实和工作经验，使助手逐步变得更适用。
3. **外部集成**：支持用户安装、使用和卸载自定义插件；已有 Multica 接入和文件夹同步能力应能纳入统一扩展模型。
4. **智能体调用**：超级助手主要负责调度平台内助手和外部 Agent，避免把所有业务能力堆进一个巨大、臃肿且上下文消耗过高的 Agent。
5. **长任务执行**：支持流式进度、取消、审批回调、结构化 Artifact、浏览器断开后继续、失败恢复和合理的 Agent loop。

### 已给出的约束与偏好

- 当前按单用户部署设计，但一个用户可以同时拥有多个会话和多个运行中的任务。
- 外部 Agent 是本轮重点；暂不要求超级助手被外部系统调用。
- 外部调用至少需要考虑流式进度、取消、审批回调和结构化 Artifact；其他协议和能力由架构自行补足。
- 插件允许用户自建、安装、停用和卸载，灵活性重要，但不能因此绕过平台权限、凭据和核心执行状态机。
- 平台内部助手可能有前置条件。例如本体助手必须选择本体；业务澄清助手必须选择目标本体及其可编辑执行版本。
- 遇到缺少条件或业务不确定性时，应主动向用户询问并给出推荐，不能猜测、静默漂移或阻塞整个会话。
- 可以进行重构，版本迁移数量不是当前约束；先完成正确的分析和架构设计。
- Rust DeepSeek Harness 只作为架构思想参考，不复制其具体运行时和存储假设。

## 3. 已确认的关键决策

下列决策已经由用户确认，审查意见可以挑战其实现方式，但不能无提示地改写其产品意图：

| 决策 | 当前约束 | 文档依据 |
|---|---|---|
| 委派必须绑定业务上下文 | 业务澄清委派在创建前必须绑定目标本体和编辑中的草稿版本；缺失任一条件时进入 `waiting_input`，不创建无绑定子会话 | `super-assistant-top-level.md`、`super-assistant-agent-connector-model.md` |
| 多运行并行 | 一个 Conversation 可包含多个 Run；一个 Run 等待输入、审批或外部结果时，不锁住整个会话 | `super-assistant-execution-model.md` |
| 事实与传输分离 | PostgreSQL 保存执行事实和事件；NATS 负责派发与唤醒；SSE 断开不等于取消 | `super-assistant-top-level.md`、`super-assistant-data-event-model.md` |
| 不伪造远端能力 | 远端不支持真实流式、取消或 Artifact 时，Connector 必须如实声明，不得通过统一接口伪造 | `super-assistant-agent-connector-model.md`、`super-assistant-capability-plugin-model.md` |
| 调用与尝试分离 | 逻辑 `Call` 与实际 `Attempt` 分离；未知副作用不能因超时自动重复 | `super-assistant-execution-model.md` |
| 上下文按需构建 | 每次模型请求生成有预算、有来源、有权限边界的 Context Pack；不把全部记忆、图谱、工具 schema 或事件日志常驻注入 | `super-assistant-context-memory-model.md` |
| 来源分工 | 原始私人资料、图谱推导、长期记忆、当前任务状态分别管理；图谱推导不自动高于原始来源 | `super-assistant-context-memory-model.md` |
| 插件受控扩展 | 用户插件只能提供受控 Tool、Resource、Skill 或 Agent Connector；事件存储、状态机、权限、密钥、Outbox 和 Artifact 校验属于平台内核 | `super-assistant-capability-plugin-model.md`、`super-assistant-implementation-blueprint.md` |
| 迁移按执行版本隔离 | 旧 Run 按旧路径收尾，新 Run 才进入新 Kernel；不对同一个 Run 双写两套权威状态 | `super-assistant-migration-validation-plan.md` |
| 本轮先做设计 | 当前产出是架构设计和开发依据，不是已完成的代码重构；具体字段、端点、协议版本、预算和隔离实现尚未全部冻结 | 全部架构文档 |

## 4. 当前架构主张

设计将超级助手拆成以下职责边界，并计划继续放在现有后端业务域中，不为概念分层强行增加微服务：

```text
会话与任务应用服务
        ↓
执行内核（Run / Turn / Step / Call / Attempt / Inbox）
        ├── 上下文组织（Context Pack、记忆、私人知识、图谱、压缩）
        ├── 能力目录与调用服务（Capability、Tool、Agent、Plugin）
        ├── 授权与审批
        ├── Agent / Tool Connector
        ├── Artifact 服务
        └── 执行记录、事件、Outbox、租约与投影
```

推荐的首轮实现包结构、Protocol、Runtime 拆分顺序和不可插件化边界见 `super-assistant-implementation-blueprint.md`。该蓝图是开发拆分依据，不是要求一次性完成的重构清单。

## 5. 相关文档与阅读顺序

完整架构文档目录：

```text
$OPENONTOLOGY_REVIEW_ROOT/docs/architecture
```

建议审查顺序：

1. `README.md`：文档入口、讨论规则和阅读顺序。
2. `super-assistant-top-level.md`：目标、边界、核心概念、不变量和待决事项。
3. `super-assistant-execution-model.md`：Run、Turn、Step、Call、Attempt、等待、取消、恢复和租约。
4. `super-assistant-capability-plugin-model.md`：Capability、Tool、Agent、Skill、Plugin、权限和插件生命周期。
5. `super-assistant-agent-connector-model.md`：内部助手、RAP、A2A、Agent Client Protocol、MCP 和委派前置条件。
6. `super-assistant-context-memory-model.md`：Context Pack、私人知识、记忆、图谱、压缩和 Artifact。
7. `super-assistant-data-event-model.md`：逻辑实体、事件封套、Outbox、幂等和兼容投影。
8. `super-assistant-implementation-blueprint.md`：现有 Python 包的推荐拆分和最小 Protocol。
9. `super-assistant-migration-validation-plan.md`：迁移阶段、回滚、故障注入、协议验收和上线门禁。
10. `../../AGENTS.md`：仓库业务域边界、兼容契约、测试门禁和文档责任。

当前实现事实应同时抽查以下入口：

```text
backend/app/super_assistant/runtime.py
backend/app/super_assistant/remote_agent_service.py
backend/app/super_assistant/memory_service.py
backend/app/super_assistant/palace_service.py
backend/app/super_assistant/conversation_service.py
backend/app/assistant_hub/contract.py
backend/app/data_channel/
backend/app/ontologies/
backend/alembic/versions/
```

参考项目路径变量：

```text
$RUST_DEEPSEEK_HARNESS_ROOT
```

## 6. 请审查 Agent 重点回答的问题

- 顶层边界是否足以支撑长任务、多个并行 Run、外部 Agent 调度和未来演进？
- `Run / Turn / Step / Call / Attempt / Event / Inbox` 是否语义清晰，是否存在重复状态机或无法恢复的竞态？
- 委派绑定目标本体和编辑草稿版本的决策，是否已贯穿 Connector、Run、Call、权限和版本漂移处理？
- Context Pack、私人知识、图谱和长期记忆的权威关系是否可执行，是否会造成错误记忆、过度注入或删除传播不完整？
- 用户自建插件的信任边界、进程隔离、凭据范围、卸载和在途调用处理是否达到商用所需水平？
- 流式进度、取消、审批、迟到回调、未知外部结果和 Artifact 是否都有可验证的事件与状态语义？
- A2A、RAP、Agent Client Protocol、MCP 的边界是否清楚，是否存在把不同协议混称或过早绑定某一协议的问题？
- 迁移策略是否能保持现有 HTTP、SSE、NATS、数据库、菜单和旧会话兼容，并且具备可回滚性？
- 哪些字段、API、事件 payload、权限规则、SLO 或隔离机制必须在开发前冻结？
- 相比 Rust Harness，哪些借鉴是合理的，哪些部分存在不应复制的安全或一致性假设？

请将结论分为：**必须修改、建议修改、可以保留、需要产品确认**，并给出文档路径和具体依据。审查阶段不要直接修改源码或架构文档。

## 7. 可直接交给另一位 Agent 的简要 Prompt

```text
请对 OpenOntology 超级助手商用升级方案做一次独立架构审查，不要直接改代码或文档。

主审查工作区：
$OPENONTOLOGY_REVIEW_ROOT

OpenOntology 基线工作树：
$OPENONTOLOGY_BASE_ROOT

参考 Rust Harness（只借鉴架构思想）：
$RUST_DEEPSEEK_HARNESS_ROOT

先阅读：
1. <主审查工作区>/docs/architecture/super-assistant-review-packet.md
2. <主审查工作区>/docs/architecture/README.md
3. 该目录下其余 8 份 super-assistant-*.md 架构文档
4. <主审查工作区>/AGENTS.md
5. 结合审查包列出的现有源码入口核对实现事实

重点审查：顶层边界、Run/Turn/Step/Call/Attempt 状态机、事件与 Outbox、长任务恢复、外部 Agent Connector、插件信任边界、上下文/记忆/知识权威关系、业务澄清委派必须绑定目标本体和编辑草稿版本、迁移回滚、协议边界和测试验收闭环。

输出格式：
- 总体判断：是否足以进入详细契约设计
- 必须修改
- 建议修改
- 可以保留
- 需要产品确认
- 进入开发前必须冻结的字段、API、事件、权限和验收项

每条意见都请引用具体文件、章节或源码路径；明确区分“设计提案”和“当前已实现事实”。
```
