# 超级助手架构审查交接包

状态：开发基线 v1.0 的审查记录和资料索引。本文不代表功能已经实现；代码完成度以源码、测试、数据库迁移和项目级 `AGENTS.md` 为准。

## 1. 审查上下文

本次工作是在 OpenOntology 即将商用的背景下，重新审视“超级助手”的整体架构。目标是形成能够直接指导后续开发的顶层设计、详细契约和验收门禁。冻结合同见 [开发基线 v1.0](./super-assistant-development-baseline.md)。

当前设计工作区（用环境变量表示，避免将个人绝对路径提交到仓库）：

```text
$OPENONTOLOGY_REVIEW_ROOT
```

当前分支：

```text
codex/super-assistant-commercial-20260913
```

该分支从本地 `nano-ontoprompt` 创建，已包含 kernel.v1 的执行内核、Connector、Artifact、插件生命周期和前端任务投影实现；实现状态以本文件第 10 节和最终测试证据为准。原始 OpenOntology 工作树和参考 Harness 的位置如下：

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
| 本轮先做设计再实现 | 设计合同已冻结，并已在本分支逐步落地；未完成项必须以源码、测试和 staging 证据标记，不得由文档推断完成 | 全部架构文档 |

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

1. `README.md`：文档入口、基线使用规则和阅读顺序。
2. `super-assistant-development-baseline.md`：唯一开发合同。
3. `super-assistant-top-level.md`：目标、边界、核心概念、不变量和实现顺序。
4. `super-assistant-execution-model.md`：Run、Turn、Step、Call、Attempt、等待、取消、恢复和租约。
5. `super-assistant-capability-plugin-model.md`：Capability、Tool、Agent、Skill、Plugin、权限和插件生命周期。
6. `super-assistant-agent-connector-model.md`：内部助手、RAP、A2A、Agent Client Protocol、MCP 和委派前置条件。
7. `super-assistant-context-memory-model.md`：Context Pack、私人知识、记忆、图谱、压缩和 Artifact。
8. `super-assistant-data-event-model.md`：逻辑实体、事件封套、Outbox、幂等和兼容投影。
9. `super-assistant-implementation-blueprint.md`：现有 Python 包的推荐拆分和最小 Protocol。
10. `super-assistant-migration-validation-plan.md`：迁移阶段、回滚、故障注入、协议验收和上线门禁。
11. `../../AGENTS.md`：仓库业务域边界、兼容契约、测试门禁和文档责任。

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

请按“已闭环、部分闭环、未闭环、post-v1 deferred”分类，并给出文档路径和具体依据。审查阶段不要直接修改源码或架构文档。

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
3. 该目录下的开发基线 v1.0、专题架构文档和审查记录
4. <主审查工作区>/AGENTS.md
5. 结合审查包列出的现有源码入口核对实现事实

重点审查：顶层边界、Run/Turn/Step/Call/Attempt 状态机、事件与 Outbox、长任务恢复、外部 Agent Connector、插件信任边界、上下文/记忆/知识权威关系、业务澄清委派必须绑定目标本体和编辑草稿版本、迁移回滚、协议边界和测试验收闭环。

输出格式：
- 总体判断：是否足以进入详细契约设计
- 必须修改
- 建议修改
- 可以保留
- post-v1 deferred
- 进入开发前必须冻结的字段、API、事件、权限和验收项

每条意见都请引用具体文件、章节或源码路径；明确区分“开发基线目标”和“当前已实现事实”。
```

## 8. 已收到的独立审查结论

独立审查结论为：架构方向可以进入详细契约设计，但在冻结字段、事件和 API 前必须闭环五项接缝问题：Run 状态机缺边与取消超时、委派绑定现状的拆除清单、多 Run 与旧 HTTP/SSE/回收器契约映射、RAP 的输入/审批回送与幂等，以及委派恢复和唯一索引的 Run 作用域。

该结论已回写到执行模型、Agent Connector、数据事件模型、迁移验收方案和开发基线 v1.0。插件信任、A2A 首发范围、记忆默认值、断线继续、多 Run UI 和文档治理已经冻结；A2A、ACP、超级助手被外部调用、多租户和插件市场列为 post-v1 deferred。

## 9. 二次审查范围

二次审查不应重复确认文档是否“看起来完整”，而应验证初审结论是否已经真正穿透到架构不变量、现有代码接缝和开发前契约。重点核对：

1. M1：Run 状态机是否补齐排队取消/过期、等待失效、暂停 Inbox、取消超时和 stuck-run；
2. M2：业务澄清绑定是否有现状行为拆除清单，并且域服务真正负责 `draft + editing + write` 校验；
3. M3：旧 HTTP、SSE、600 秒回收器、cancel 和 tool decision 是否有按 `execution_version` 的映射；
4. M4：RAP v1 是否诚实声明 `needs_input`、审批、取消和幂等能力，是否定义回送与 minor 演进；
5. M5：委派恢复、唯一索引、子会话和远端引用是否已绑定到 Run/Call，是否存在跨 Run 串线；
6. 初审建议项是否已形成可执行契约：`seq` 串行化、Inbox claim/consume、未知结果 reconciler、结构化 `source_ref`、Artifact 存储边界、插件能力不可动态扩展和兼容 facade 退役条件。

二次审查记录已将每一条意见标记为已闭环、部分闭环、未闭环或 post-v1 deferred，并在开发基线和迁移验收文档中给出章节、源码证据和测试用例。本文本身是审查记录，不得把设计合同误写成运行时已经实现。

## 10. 二次审查后的当前门槛

当前实现门槛应按以下事实读取：M1 的状态/事件/幂等/租约、跨进程 heartbeat、stuck 扫描、Inbox TTL 和 parent 子结果 Artifact 归并已有实现与专项测试；M2 的执行表、迁移、Outbox、NATS 基础、历史显式回填/回滚、结构化 DLQ 发布与重放入口已实现，真实 PostgreSQL/NATS 验收仍未闭环；M3 已具备多步 activation、等待输入/审批/外部结果、恢复、Artifact 结果证据，以及通过 `sa.execution.call.<owner>` durable consumer 进入 ConnectorRegistry 的直连外部 Call 派发；Assistant Hub 委派现在先创建 Kernel `assistant_child` 子 Run，子 Run 完成后通过父子 fan-in 归并结果，适配器内部仍保留 legacy delegation 行以兼容既有子会话语义；连接器按 owner 解析，并在首个调用前冻结 `CapabilityRevision`，不可用或异常进入 `outcome_unknown/reconciling`；Multica 已支持 opaque remote ref、周期查询、远端取消、终态 Artifact 和 Run 唤醒，仍不提供 provider 流式传输；M4 已有不可变 Capability/Connector、Remote Agent HTTP 适配、MCP 工具 Connector、RAP pull、HMAC callback ingress、provider event 幂等、MCP manifest 变更/禁用/卸载撤销、`plugin_host.py` 的独立 JSON-lines 进程边界，以及持久化用户进程插件的安装/启停/卸载 drain；真实 OS 沙箱、secret broker 和插件 staging 证据仍未闭环；M5 已接入 Memory/Palace ContextSource、来源字段、tombstone 持久化和结构化外部 Artifact 归档；M6 已有 kernel.v1 API/SSE、输入/审批/Artifact、深链、失败重试 API、多 Run 列表/任务卡、410 游标恢复、事件 reducer 和 inline/object Artifact 下载，真实浏览器验收仍需补齐；M7/M8 的真实 staging、迁移升级/downgrade、发布和回滚证据尚未完成。

本次整理已将这些门禁、事件、枚举、API、默认配置和直接 UI/委派范围边界回写到开发基线 v1.0。问题 TTL、`outcome_unknown/remote_running` 的用户呈现和人工升级已分别冻结为 `reask_once/fail_branch/fail_run` 与结果待确认+对账上限。直接 UI 的空绑定/current release 兼容行为保持不变，本轮只收紧超级助手委派的 `binding_mode=delegated`。

## 11. 当前实现证据（2026-09-14）

本节只记录已经执行过的证据，不把设计目标当成完成事实。当前分支最近的实现提交包括：Kernel Artifact 下载 `7c1bdf9c`、外部插件与异步委派 `6d42792b`、进程插件健康/清理 `9f317068`、外部回调/恢复/DLQ/子结果归并 `b5775974`、Assistant Hub Kernel 子 Run `fd7323d9`、结构化外部 Artifact `1737edab`。

| 里程碑 | 当前证据 | 状态 |
|---|---|---|
| M0 | 源码、迁移、路由、事件、前端和参考 Harness 已完成差距审计；本文件与开发基线已修正实现状态 | 已完成 |
| M1 | `tests/super_assistant/kernel/` 专项回归；lease heartbeat、Inbox TTL、stuck recovery、父子结果 Artifact 均有测试 | 已完成代码闭环，需长时 staging 压测 |
| M2 | `alembic heads` 单 head；执行 Outbox、DLQ 发布与 replay 测试；staging 数据库已执行 `alembic upgrade head` | 已完成代码闭环 |
| M3 | 多步 activation、等待/恢复、统一外部 Call、Assistant Hub `assistant_child` 子 Run、fan-in 结果归并均有专项测试 | 已完成首版；Hub 内部 legacy 子会话行保留兼容 |
| M4 | RAP direct/pull、MCP、Multica、Process Plugin、HMAC callback、provider event 去重和 secret allowlist 有代码/测试 | 已完成首版；OS 沙箱、secret broker 需部署层证据 |
| M5 | Context Pack、Memory/Palace source provenance、tombstone 排除、结构化 Artifact 和 checksum 校验有代码/测试 | 已完成首版 |
| M6 | Kernel API/SSE、输入/审批、Artifact inline/object 下载、前端任务卡和 reducer 已有单测/build | 已完成代码闭环，需真实浏览器验收 |
| M7 | PostgreSQL、Neo4j、NATS JetStream、MinIO staging 探针全部通过；staging 数据库已升级至 `0110_super_assistant_process_plugins` | 依赖探针通过，完整 E2E 待执行 |
| M8 | 静态门禁和专项测试通过；前端 color-token 门禁仍有既有 `pages/login/login.css` 基线失败，完整发布/回滚演练尚未完成 | 未完成 |

已执行的 staging 依赖探针命令为：

```bash
uv run python scripts/super_assistant_kernel_live_e2e.py --output .artifacts/super-assistant-kernel-live-e2e-staging.json
```

在隔离 Compose 网络中，PostgreSQL `SELECT 1`、NATS `SA_EXECUTION_V1` subjects、MinIO `assistant-workspace` bucket、Neo4j `RETURN 1` 均通过；并使用当前分支源码短时启动 `nats_executor`，确认 durable consumers `sa-kernel-v1`、`sa-call-v1`、`sa-reconciler-v1` 已注册，随后已停止该临时进程。该证据不等价于完整业务 E2E 或生产发布批准。
