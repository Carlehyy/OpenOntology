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
| 委派必须绑定业务上下文 | 本体助手委派创建前必须绑定 `ontology_id`；业务澄清委派还必须绑定编辑中的草稿版本；缺失条件时进入 `waiting_input`，不创建无绑定子会话。直接 UI 的历史兼容回退独立保留 | `super-assistant-top-level.md`、`super-assistant-agent-connector-model.md` |
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

当前实现门槛应按以下事实读取：M1 的状态/事件/幂等/租约、跨进程 heartbeat、stuck 扫描、Inbox TTL 和 parent 子结果 Artifact 归并已有实现与专项测试；M2 的执行表、迁移、Outbox、NATS 基础、历史显式回填/回滚、结构化 DLQ 发布与重放入口已实现，隔离 PostgreSQL/NATS 依赖和迁移往返已有证据，现存业务库升级与完整业务 E2E 仍未闭环；M3 已具备多步 activation、等待输入/审批/外部结果、恢复、Artifact 结果证据，以及通过 `sa.execution.call.<owner>` durable consumer 进入 ConnectorRegistry 的直连外部 Call 派发；Assistant Hub 委派现在先创建 Kernel `assistant_child` 子 Run，子 Run 完成后通过父子 fan-in 归并结果，适配器内部仍保留 legacy delegation 行以兼容既有子会话语义；连接器按 owner 解析，并在首个调用前冻结 `CapabilityRevision`，不可用或异常进入 `outcome_unknown/reconciling`；Multica 已支持 opaque remote ref、周期查询、远端取消、终态 Artifact 和 Run 唤醒，仍不提供 provider 流式传输；M4 已有不可变 Capability/Connector、Remote Agent HTTP 适配、MCP 工具 Connector、RAP pull、HMAC callback ingress、provider event 幂等、MCP manifest 变更/禁用/卸载撤销、`plugin_host.py` 的独立 JSON-lines 进程边界，以及持久化用户进程插件的安装/启停/卸载 drain；真实 OS 沙箱、secret broker 和插件 staging 证据仍未闭环；M5 已接入 Memory/Palace ContextSource、来源字段、tombstone 持久化和结构化外部 Artifact 归档；M6 已有 kernel.v1 API/SSE、输入/审批/Artifact、深链、失败重试 API、多 Run 列表/任务卡、410 游标恢复、事件 reducer 和 inline/object Artifact 下载，真实浏览器验收仍需补齐；M7/M8 的完整 staging、现存库迁移、发布和回滚证据尚未完成。

本次整理已将这些门禁、事件、枚举、API、默认配置和直接 UI/委派范围边界回写到开发基线 v1.0。问题 TTL、`outcome_unknown/remote_running` 的用户呈现和人工升级已分别冻结为 `reask_once/fail_branch/fail_run` 与结果待确认+对账上限。直接 UI 的空绑定/current release 兼容行为保持不变，本轮只收紧超级助手委派的 `binding_mode=delegated`。

## 11. 当前实现证据（2026-09-14，基线提交 `d5bf6fb9`）

本节只记录已经执行过的证据，不把设计目标当成完成事实。此前的功能提交已汇入当前分支；主要对抗式修复收敛在 `461847a1`，其后又增加进程插件信任字段篡改防护、完整 manifest 指纹校验、生产环境别名 fail-closed、回调/远程响应/MCP 结果边界、运行时 SSRF 复核、生产容器加固，以及外部 Artifact 声明大小和存储引用长度边界。本轮进一步收紧探索委派绑定：服务端生成并校验写权限指纹，Kernel 子 Run 强制保留可信来源标记，委派恢复每轮复核实时草稿和写权限。相关提交包含 reconciliation Outbox payload、RAP 结构化 Artifact 持久化、输入消费事务、能力 revision 栅栏、HTTP/SSE 契约、NATS 重投与外部结果边界修复和对应回归测试。

| 里程碑 | 当前证据 | 状态 |
|---|---|---|
| M0 | 源码、迁移、路由、事件、前端和参考 Harness 已完成差距审计；本文件与开发基线已修正实现状态 | 已完成 |
| M1 | `tests/super_assistant/kernel/` 专项回归；lease heartbeat、Inbox TTL、stuck recovery、父子结果 Artifact 均有测试 | 已完成代码闭环，需长时 staging 压测 |
| M2 | `alembic heads` 唯一 head 为 `0113_remote_agent_result_artifacts`；迁移链为 `0111 → 0112_reconcile_outbox_payload → 0113_remote_agent_result_artifacts`；执行 Outbox、DLQ 发布与 replay 测试 | 已完成代码闭环；现存业务库升级仍需 staging 证据 |
| M3 | 多步 activation、等待/恢复、统一外部 Call、Assistant Hub `assistant_child` 子 Run、fan-in 结果归并均有专项测试 | 已完成首版；Hub 内部 legacy 子会话行保留兼容 |
| M4 | RAP direct/pull、MCP、Multica、Process Plugin、HMAC callback、provider event 去重和 secret allowlist 有代码/测试；配置变更按 revision/hash 栅栏，旧 revision 进入人工处理 | 已完成首版；OS 沙箱、secret broker 需部署层证据 |
| M5 | Context Pack、Memory/Palace source provenance、tombstone 排除、结构化 Artifact 和 checksum 校验有代码/测试 | 已完成首版 |
| M6 | Kernel API/SSE、输入/审批、Artifact inline/object 下载、If-Match/Idempotency-Key、UTF-8 请求上限、前端任务卡和 reducer 已有单测/build | 已完成代码闭环，需真实浏览器验收 |
| M7 | 当前 `oo-rearch` Compose 的 `/api/health` 依赖探针通过（PostgreSQL、Redis、Neo4j、MinIO、Browser、NATS、n8n 均健康）；但该 staging backend 旧镜像内 `alembic current` 无法定位数据库 revision `0110_super_assistant_process_plugins`，不能证明当前分支迁移已部署。将当前分支源码挂载到该旧数据库启动时，schema guard 明确拒绝缺少 `super_assistant_mcp_servers.manifest_revision/manifest_hash`，证明现存业务库必须先执行迁移；本轮未直接改动该共享数据库。最新隔离临时 Compose 已使用当前分支执行 PostgreSQL `upgrade head`（到 `0113`）、`downgrade 0113 -> 0110`、再次 `upgrade head`，并通过 PostgreSQL/NATS JetStream/MinIO/Neo4j 依赖探针；NATS executor 短时启动并成功注册 `sa-kernel-v1`、`sa-call-v1`、`sa-reconciler-v1`。该证据仍未覆盖现存业务数据库升级、完整 kernel live E2E、浏览器/外部副作用和发布回滚 | 当前分支临时迁移往返和依赖探针通过，现存业务库升级和完整 staging 验收待执行 |
| M8 | 静态门禁、前端 color-token 和专项测试通过；完整发布/回滚演练尚未完成 | 未完成 |

已执行的 staging 依赖探针命令为：

```bash
uv run python scripts/super_assistant_kernel_live_e2e.py --output .artifacts/super-assistant-kernel-live-e2e-staging.json
```

在隔离 Compose 网络中，PostgreSQL `SELECT 1`、NATS `SA_EXECUTION_V1` subjects、MinIO `assistant-workspace` bucket、Neo4j `RETURN 1` 均通过；并使用当前分支源码短时启动 `nats_executor`，确认 durable consumers `sa-kernel-v1`、`sa-call-v1`、`sa-reconciler-v1` 已注册，随后已停止该临时进程。该证据不等价于完整业务 E2E 或生产发布批准。

## 12. 对抗式代码审查（2026-09-14）

本轮按恶意输入、并发竞态、迟到结果、进程泄漏和资源耗尽路径复核 Kernel、Assistant Hub、远程 Agent 与进程插件边界，确认并修复以下问题：

- 业务探索委派原先可在缺少本体/编辑草稿绑定时先创建 `assistant_child`；现在由探索域服务在创建子 Run 前校验 `ontology_id + draft_version_id + editing + write_permission_hash`，缺失条件转为 `waiting_input`，不留下无绑定子会话。
- 业务探索委派的写权限指纹现在由服务端从明确的本体/草稿选择计算；伪造或过期指纹会被拒绝，委派恢复的每一轮会重新检查实时权限和草稿生命周期，失效时不会静默 fork 到最新版本。
- 本轮复查发现本体助手首次 Kernel 委派曾可缺少 `ontology_id` 并依赖“最近本体”回退；现在 Kernel 委派入口缺少显式本体绑定时直接返回 `needs_input`，不创建子 Run。直接 UI 的历史回退语义保持不变。
- Run 在取消宽限期后进入 `cancelled/expired` 时，原调度器会停止远程 Call 对账；现在带远端句柄的未决 Call 继续执行取消或状态查询，终态 Run 保持不可重开但 Call 可收敛到真实终态。
- 回连 Agent 长轮询原先会持有请求级数据库连接；现在认证/心跳事务在等待前结束，任务认领使用短会话。
- 远程 callback payload 增加 64 KiB 上限；超限内容必须以 Artifact 引用传递。
- 发现并修复了四条可触发的闭环缺口：`ENVIRONMENT=Production`/带空格时进程插件生产禁用曾被绕过（现在统一规范化）；输入和审批把 Run 唤醒为 `active` 却未写 activation Outbox（现在与状态事实同事务写入）；活动 Run 的已终态 Call 曾可被迟到 `running` 观察重开（现在 Call 终态优先忽略）；取消中的 Call 也曾被迟到 `running` 观察改回普通等待（现在保持 `cancel_requested`）。审批决策同时增加 Run 等待态和过期校验，callback 对 `status=closed + outcome=completed` 的传输封套按语义结果处理，并拒绝 payload 跨 Call/Attempt 引用。

新增专项回归为 `33 passed`（router/reconciler/recovery/callback/process-plugin），前端静态门禁和 unit/build 通过；`test:e2e:mocked` 当前为 `253 passed, 53 failed`，失败集中在既有导航/场景/登录等跨域规格，不能作为超级助手商用验收通过证据。独立 rootless plugin-runner/secret broker、真实 staging 外部副作用、迁移升级与回滚仍未完成，因此本轮审查不构成商用发布批准。

当前仍有多项对商用安全和可运维性有直接影响的未闭环问题：用户进程插件的 `network_scope`、`workspace_scope`、`secret_refs` 仍是元数据，尚未由独立 rootless runner、网络/secret broker 和工作区挂载真正执行；本轮已增加 `plugin-runner.v1` 有界调用信封，并把运行时默认改为 `disabled`，仅 development/test 允许 `direct_dev`，但 `nats` 独立 runner 尚未部署，不能把契约当成隔离执行完成；插件信任等级缺少可验证的签名信任根（运行时现在会重算完整 manifest 并对 entrypoint/权限字段篡改 fail-closed，但不能替代签名验证）；MCP/外部 HTTP 以及浏览器导航的配置期 DNS 校验与实际建连之间仍存在 DNS rebinding TOCTOU 窗口。浏览器的 `context.route("**/*", _route_guard)` 会逐请求重检 URL，因此重定向已受应用层 URL 检查；但它不能证明最终连接使用的 IP，也不能替代网络层 egress/private-CIDR 策略。当前镜像仍需 `--no-sandbox`；生产部署已经强制 `BROWSER_IMAGE` 使用 digest，但其他基础镜像的全局 `STRICT_IMAGE_DIGESTS` 仍允许关闭。browser Dockerfile 的字体包已锁定 Debian 版本，生产集成 HTTPS 仍需完成既有端点迁移、证书和回调兼容性验收。生产环境配置现已在 Settings 入口统一规范化 `prod`、大小写和外围空白，并对空值/未知值 fail-closed；私网浏览器目标默认关闭且生产 Compose 显式固定为 `false`。剩余问题必须在 staging 攻击验收与发布门禁中闭环。

## 13. 最新对抗式代码审查证据（提交 `62661863`，绑定收口 `47a6a4d9`）

本轮重点检查了“写入成功但派发丢失”“重复或迟到外部结果”“配置漂移误调用”“输入丢失”“HTTP 并发覆盖”和“SSE 客户端按错误形状解析”等故障路径，并补充了以下不变量：

- callback 与 scheduler 只写 reconciliation Outbox；状态转换仍由 `sa-reconciler-v1` durable consumer 执行，重复 provider event 以 payload hash 拒绝冲突。
- pending user Inbox 在模型结果成功落库的同一事务中才标记 consumed；模型失败或进程崩溃会保留 pending，等待下一次 activation。
- MCP、Remote Agent 和 Multica 的 Call 固定 `capability_revision` 与 manifest hash。配置或凭据变更撤销当前 revision；旧 Call 不会重定向到新端点，而是进入 `outcome_unknown/manual_attention`。
- mutation API 要求 body/header 幂等键一致；控制、重试、输入和审批使用 `If-Match`，SSE snapshot 使用 `data.run`，Artifact 下载声明 `application/octet-stream`。
- 新增的结构化 Artifact、UTF-8 请求大小、Outbox payload 和迁移链均有专项测试；此前记录的 Kernel 专项为 `151 passed`，本轮包含新增连接器/callback/插件回归的完整 `backend/tests/super_assistant/kernel/` 为 `161 passed`，架构/OpenAPI/时长门禁为 `12 passed`。

本轮另执行了完整超级助手业务域回归 `uv run pytest -q tests/super_assistant --disable-warnings`，结果为 `609 passed`，覆盖 Kernel、远程助手、MCP、记忆宫殿、同步、路由和兼容接口；该证据仍不替代真实 staging 的外部依赖、浏览器副作用与发布回滚验收。

前端完整静态门禁已复核：`npm run test:unit` 为 `481 passed`（155 suites），feature-boundaries、component-convergence、color-tokens、lint 和 `npm run build` 均通过；构建仅报告既有 Vite/Tailwind 警告，不影响退出码。

前端超级助手专属浏览器验收（自主模式、MCP 降级、工作台、多会话、知识图谱、Artifact/集成入口）执行 36 个用例，结果 `36 passed`（Chromium，3 workers）；其中修正了旧占位文案断言和目录刷新后的 busy 竞态等待。

rootless browser 探针已覆盖 Compose 精确 healthcheck、CDP `/json/version`、`PUT /json/new` 页面创建、Chromium/socat 子进程 UID/GID 和错误日志检查；这些结果证明容器权限加固可运行，但不替代完整真实浏览器外部副作用验收。

本轮没有把局部专项结果扩大解释为商用验收。修复后的完整后端回归已实际执行：`3597 passed, 6 skipped`；时长表重录后的覆盖守卫单独复核通过；此前暴露的迁移 head、能力版本表和 manifest 列问题均已修复并复验。新增的 NATS 失败重投、远端调用崩溃恢复、终态取消、超长引用收口、ContextPack 上限、RAP Artifact、callback 白名单、JetStream 策略漂移和进程插件 manifest 篡改测试均已通过；核心定向集合和真实隔离栈证据仍不替代完整 staging。真实隔离栈探针已通过 PostgreSQL、NATS `SA_EXECUTION_V1`、MinIO bucket、Neo4j；NATS durable consumers `sa-kernel-v1`、`sa-call-v1`、`sa-reconciler-v1` 注册并清空积压，真实 MinIO round-trip 和 NATS executor E2E 各 `1 passed`。当前分支启动的 API `/api/health` 返回 503 的唯一不可用项是隔离栈未提供 n8n，因此浏览器 E2E、真实外部 Agent、rootless 插件隔离、DNS rebinding 攻击验证和发布回滚演练仍是 M7/M8 的阻断项。

最新隔离 E2E 栈证据（2026-09-14）为：PostgreSQL 当前分支从空库升级到 `0113_remote_agent_result_artifacts`，再降级到 `0110_super_assistant_process_plugins` 并重新升级到 `0113`；依赖探针返回 PostgreSQL、NATS JetStream、MinIO bucket、Neo4j 全部 `ok=true`；短时 nats executor 成功注册三个 kernel durable consumer。临时容器、卷和网络已在验证后销毁；这仍不等价于现存业务库升级或完整外部副作用验收。

迁移报告运维入口已补齐脚本自举：在 `backend` 目录直接执行 `uv run python scripts/super_assistant_migration_report.py`（无需手工设置 `PYTHONPATH`）可输出 `kernel.v1.legacy-disposition.v1` 只读报告；`--help` 在无数据库配置时也可用。该入口修复已用隔离 PostgreSQL 实际执行并确认 `mutated=false`，不改变现存数据。

本次最新收口后的定向回归为 `77 passed`，探索适配器完整回归为 `14 passed`，委派边界架构测试为 `4 passed`，时长覆盖守卫为 `1 passed`；新增验证覆盖伪造权限指纹、服务端重算指纹、Kernel 子 Run 来源标记不可被上下文覆盖，以及草稿生命周期变化后恢复委派会话会明确失败。该专项证据只证明代码级绑定不变量，不改变 M7/M8 的 staging 和发布阻断状态。

用户进程插件的最新专项回归为 `28 passed`，生产配置回归为 `84 passed`；新增验证覆盖 runner 信封字段/大小/摘要/回复主题校验，以及 `disabled | nats | direct_dev` 的 fail-closed 解析。当前 `nats` 只冻结了独立 runner 的消息契约，尚未提供 rootless 执行服务，因此不能据此解除插件商用阻断。

对抗式审查新增的代码修复包括：NATS handler 在状态未持久化时 NAK 而非 ACK；RUNNING 状态的重复外部调用进入对账/人工介入路径且不二次触发 provider；父 Run 终态后仍对带远端句柄的 Call 执行取消；provider 引用和结果文本在落库前限长；人工介入 Call 不再被 scheduler 无限轮询；外部 Artifact 的对象存储引用必须落在 owner/run/artifact 命名空间；SSE callback 事件只保留稳定字段和受限 Artifact 引用。上述修复已经通过对应专项测试，但不替代真实 provider、对象存储和浏览器副作用验收。

直连 Remote Agent 的响应读取也已改为流式并设置 256 KiB 硬上限，避免远端在 Kernel 处理前用超大 JSON 响应造成内存压力；超限响应进入失败/对账路径，新增连接器回归已通过（`11 passed`）。

旧 `/remote-agents` 兼容适配器也增加同一 256 KiB 响应体上限，避免旁路契约绕过 Kernel 的输入边界；兼容层完整回归 `21 passed`。

MCP `call_tool` 的序列化结果现在也限制为 256 KiB，覆盖 HTTP、SSE、streamable HTTP 和 stdio 共用出口；超限结果在进入 Kernel 事件或 Artifact 持久化前即被拒绝，MCP 客户端回归 `13 passed`。

此外，Kernel 直连连接器在每次真实外呼前重新执行共享 SSRF/URL 校验，避免配置变更或 DNS 变化后继续使用已失效的网络边界；注入 transport 的测试路径不参与 DNS 解析。该校验不能消除 DNS 解析与 TCP 建连之间的全部 rebinding 窗口，最终仍需网络层 egress policy 和攻击性 staging 验证。

本轮额外发现并修复了 web_fetch 与 Multica 客户端的重定向边界：此前 HTTP 客户端会自动跟随未经逐跳 SSRF 校验的 Location，可能把请求转向内网地址，Multica 还可能将 Bearer 凭据带到重定向主机。现在 web_fetch 关闭隐式跟随并对每一跳重新校验，限制最多 3 次；Multica 对重定向直接 fail-closed。对应 web 与 Multica 回归已通过。

同一入口的响应体也改为流式读取，先检查 `Content-Length`，并在实际字节累计超过 256 KiB 时立即中止；这避免 chunked 或错误声明长度的远端响应在 HTML 解析前造成内存压力。

独立 DNS 审查还发现 MCP legacy SSE 路径会使用 SDK 默认的 `follow_redirects=True`，存在跨主机重定向和请求头泄露风险；现在注入 `follow_redirects=False` 的客户端工厂，SSE 与 streamable HTTP 的重定向策略保持一致。DNS 解析与 TCP 建连之间的 rebinding TOCTOU 仍需网络层 egress policy 或固定 IP transport 解决。

同一审查确认 HTTPX 默认会读取进程环境代理；所有超级助手外部 HTTP 出口现在显式设置 `trust_env=False`，避免 `HTTP(S)_PROXY/NO_PROXY` 在校验后改变解析或路由。若生产必须使用代理，应由受控 egress proxy 负责 DNS/IP 策略并通过明确的应用配置接入。

生产外部集成端点现由 `SUPER_ASSISTANT_EXTERNAL_HTTPS_REQUIRED=true` 默认强制 HTTPS；本地 development/test 保留 HTTP fixture 兼容。既有生产端点迁移、证书轮换和 callback 回连验收仍属于 M7 staging 门禁。

生产 Compose 的 browser、`python_kernel_gateway`、backend 与 `pipeline_executor` 已增加 `no-new-privileges`、`cap_drop: ALL` 和独立 `/tmp` tmpfs，降低容器内提权与临时目录持久化风险；这属于通用容器纵深防御，不能替代用户插件所需的 rootless runner、独立 namespace、网络/工作区隔离和资源配额。

对 browser 镜像的运行态检查显示，基础镜像缺少可用的 Chromium SUID sandbox，去掉 `--no-sandbox` 会退出；本轮已据探针结果把镜像和 Compose 固定到 UID/GID 10001，保留 `--no-sandbox`，并验证 CDP 健康检查和新页面创建均成功。该项降低了容器被攻破后的权限，但仍需换用带内部 sandbox 的固定 digest 镜像并完成浏览器攻击面 staging，才能解除纵深防御风险。

browser 基础镜像默认值现已固定为已验证的 SHA-256 digest，部署守卫会拒绝恢复 `latest`；容器以 UID/GID 10001 运行，启用只读根文件系统，缓存收口到专用 `/tmp/browser-cache` 子目录；生产部署现已强制所有镜像启用 `STRICT_IMAGE_DIGESTS=true`，显式传入 `BROWSER_IMAGE` 仍属于运维变更，必须重新执行镜像构建、CDP 健康检查和安全回归。

部署脚本同时清除宿主环境中的 `SUPER_ASSISTANT_PROCESS_PLUGIN_RUNNER_MODE`，防止 shell 变量覆盖已验证的生产配置；插件 runner 模式只能来自服务器 `.env`。

`scripts/ci/test-deploy-guards.sh` 已增加对上述四个服务和三项配置的服务级守卫，部署守卫自测通过，后续 Compose 修改若移除任一选项会在 CI 阶段失败。

本轮对用户进程插件做了额外的反向检查：`plugin_host.py` 目前只是受限 JSON-lines 子进程，`network_scope`、`workspace_scope`、`secret_refs` 没有被 OS/网络策略执行；生产 Compose 已增加通用的 capability drop、no-new-privileges、固定非 root browser、只读 browser 根文件系统和 hardened `/tmp`，但插件仍没有独立 runner/namespace。运行时不会把 secret 值直接传给插件，因此当前插件能力是 fail-closed 的，不能作为“已支持凭据注入的商用插件”宣称。该事实与 `process_plugin_service.py`、`kernel/runtime.py`、`kernel/plugin_host.py` 和生产 Compose 配置一致，必须以独立 runner、secret broker 和攻击性 staging 验收完成后才可解除 M7/M8 阻断。

本次新增的对抗式复查确认了四个 P1 风险中的三项已完成代码级收口：浏览器私网目标默认拒绝并由生产 Compose 固定关闭，`ENVIRONMENT=prod`、大小写、外围空白和未知值在 Settings 入口统一处理，browser Dockerfile 的字体包固定到版本号。浏览器已有逐请求 route URL 检查，但 DNS 解析与实际建连之间仍有 TOCTOU 窗口，且尚无网络层 egress/private-CIDR 隔离；生产部署现已无论全局开关取值都拒绝浮动 `BROWSER_IMAGE`，但其他基础镜像仍可在全局 digest 门禁关闭时漂移，当前镜像还需 `--no-sandbox`。外部 Artifact 的声明大小现在限制为 `MAX_ARTIFACT_BYTES`，存储引用在写入前限制为数据库列宽，避免恶意远程结果触发整数溢出、截断或 poison-message 重试。后续需要通过网络层隔离、不可关闭的全量发布镜像完整性门禁和 staging 攻击测试收口。

同时增加了进程插件完整 manifest 篡改防护：当前 executable process plugin 只接受 `user_untrusted`，数据库中把 `trust_level` 改写为 `verified/platform` 会在启用和运行时双重拒绝；运行前会重算并校验 `key/revision/entrypoint/capabilities/permissions/network_scope/workspace_scope/secret_refs`，不一致即撤销 CapabilityRevision 并进入 connector unavailable/manual attention 路径。生产环境门禁同时覆盖规范化后的 `production` 与 `prod` 别名。新增 entrypoint、capabilities 和环境别名回归均已通过。未来签名信任根和独立 runner 上线前，不允许通过普通数据库字段获得执行权限。该修复属于 fail-closed 防护，不能替代签名信任根。

## 14. 用户进程插件 runner 的冻结实施契约

本轮没有提交伪隔离 runner。现有 `plugin_host.py` 只能作为 development/test 宿主；生产继续拒绝 `user_untrusted`。商用 runner 必须作为独立服务接收内部 NATS durable envelope，不改变外部 Run/Call/SSE 契约。请求至少绑定 `request_id、owner_id、run_id、call_id、plugin_id、revision、manifest_hash、capability_revision、input_ref、workspace_snapshot_ref、secret_lease_refs、deadline、reply_subject`；所有结果、进度和 Artifact 引用必须回显同一组绑定字段，`request_id` 作为 NATS `Msg-Id`，重复投递只读取结果 journal，不重新执行插件。

runner 每次调用启动短命 rootless sandbox：固定非 root UID、read-only rootfs、独立 PID/IPC/UTS/network namespace、`no-new-privileges`、seccomp/AppArmor、cgroup CPU/内存/PID/文件限制；cgroup v2 必须由宿主明确 delegated subtree 管理，不能依赖容器内临时 `cap_add` 自建控制器。工作区只允许 canonical allowlist 的 read-only bind mount，调用 scratch 单独可写；禁止挂载 Docker socket、平台 uploads/API Hub 数据和宿主凭据。网络默认 deny，非空 `network_scope` 在受控 egress proxy 与 DNS/IP 策略部署前必须拒绝。凭据只通过按 owner/run/call/manifest hash 绑定的一次性短 TTL secret lease 按需获取，值不得进入环境继承、日志、事件或模型上下文。Artifact 只能通过 broker 写入 owner/run/call 前缀并返回 checksum、size、mime 和 opaque object ref。runner journal 必须在 spawn 前后持久化状态，`spawned` 记录一旦存在，NATS 重投只能查询 journal 或对账，禁止再次执行同一 `call_id`。

manifest 字段 hash 不能替代插件包本身的完整性证明。runner 必须只接受不可变 bundle/artifact digest，并在启动前校验包内容；workspace 中可变的 executable 或依赖文件不能作为生产插件来源。runner staging 必须攻击验证 `/proc`、共享卷、symlink/`..`、内部 CIDR、非 allowlist 外联、fork/memory/CPU/file exhaustion、`setsid`/daemon 逃逸、凭据泄露、安装后替换 bundle、重复 NATS 投递、进程崩溃、取消和 worker 重启恢复；只有这些证据与发布/回滚演练完成后，才能解除生产 `user_untrusted` 的 fail-closed 门禁。

本轮还收紧了 callback 事件键的长度边界：`connector_id` 与 `provider_event_id` 即使各自达到协议上限，拼接后的 `command_id`/`idempotency_key` 也会在 255 字符数据库列内以确定性 SHA-256 短键落库；对应长标识回归已通过（callback 专项 `7 passed`）。
