# 超级助手商用升级开发基线 v1.0

状态：开发基线，已冻结，可直接拆分为实现任务、迁移任务和验收任务。

本文是本目录的合同入口。它把顶层架构、执行内核、能力插件、Agent 协作、上下文记忆、数据事件和迁移方案收敛为一套实现约束。其余专题文档负责解释设计原因和实现细节；如果出现重复定义，以本文和项目级 `AGENTS.md` 为准。本文不表示代码已经实现，代码完成度以源码、测试、Alembic 和部署探针为准。

## 1. 目标、范围和固定产品选择

超级助手是一个面向单用户的任务执行内核。它在多个 Conversation 中创建多个独立 Run，按需选择私人知识、记忆、平台能力、插件和外部 Agent，持续执行并交付有证据的文本或结构化 Artifact。浏览器连接只是观察和交互通道，不是任务生命周期。

本版本固定以下选择：

| 主题 | v1.0 决定 |
|---|---|
| 部署 | 单用户、单部署、有界并发；保留 `owner_id` 和资源 scope，避免未来无法演进 |
| 并行 | 一个 Conversation 可有多个 Run；每个 Run 拥有独立 Inbox、租约、预算和终态；前端默认一个前台输入框，其他 Run 以任务卡显示 |
| 长任务 | SSE、页面关闭或浏览器断开不取消 Run；完成、失败、等待和结果通过任务时间线与 Inbox 可回看；没有“断开即停”默认行为 |
| 外部 Agent | 首发实现 Assistant Hub、现有 RAP v1 direct/pull、MCP；A2A 只保留 Connector 边界，不作为首发硬依赖 |
| 插件 | 用户可以安装、启用、停用和卸载自建插件；可执行插件必须由独立进程宿主运行，并遵守显式网络、工作区和凭据 scope；未经健康检查的 revision 不可启用 |
| 信任 | manifest 是声明，平台策略和审批是授权事实；插件不能覆盖平台能力、扩大运行时能力目录或自行决定审批 |
| 委派绑定 | `binding_mode` 固定为 `none | delegated | direct_ui | legacy`；超级助手委派业务探索时强制 `binding_mode=delegated`，绑定 `ontology_id + draft_version_id + editing`、用户归属和写权限；缺失时提问，不创建无绑定子会话。直接 UI 的空会话和 current release 语义保留 |
| 记忆 | 原始资料是事实源；图谱是可追溯推导；用户明确要求记住的 low-risk `explicit` 记忆可自动接纳；`derived`/`reflection` 即使 low-risk 也保持 pending，需确认后成为 current fact；敏感信息和指令类记忆永不自动接纳 |
| 问题过期 | `ContextRequirement.expiry_policy` 只允许 `reask_once`、`fail_branch`、`fail_run`。可选信息默认 `reask_once`，必需绑定默认 `fail_branch`；只有明确的 Run 安全前置条件才使用 `fail_run`。问题 TTL 与 Run deadline 取更早者，过期回答不能复活 Run。`reask_once` 到期先关闭原 Inbox 并最多创建一个新问题；再次到期转 `fail_branch`；`fail_branch` 只结束当前分支并保留其他可推进 Step；`fail_run` 直接按 `failed` 关闭 Run；Run deadline 始终优先，统一进入 `expired`。 |
| 未知结果 | 用户看到“结果待确认”；Kernel reconciliation service 负责查询。默认首次 5 秒、指数退避倍数 2、上限 5 分钟、最多 12 次或到 Run deadline；无 `query_status` 能力或达到上限则 `manual_attention=true`，不得伪造失败或重发写操作 |
| 取消 | 用户取消使本地 Run 进入 `cancelled`；Run deadline 使本地 Run 进入 `expired`；未确认的外部 Call 独立进入 `reconciling`，迟到结果只更新 Call/Artifact，不重开 Run |
| 文档 | 本目录在开发和迁移完成前是长期开发基线；行为以代码和测试落地，基线变更必须同时更新状态机、事件注册表、API 契约和验收矩阵 |

## 2. 模块边界和依赖

实现继续位于现有后端单体和 NATS executor，不为分层增加微服务。业务域边界遵守项目级 `AGENTS.md`：超级助手只通过 `assistant_hub` 契约调用平台助手，不直接 import 本体或业务探索编排器；`exploration` 依赖 `ontologies`，反向依赖被架构测试禁止。

```text
HTTP/SSE + frontend
        ↓
super_assistant application service
        ↓
Execution Kernel ── Context Orchestrator ── Memory/Palace/Graph/Artifact
        ↓                         └────── Capability Registry / Policy
Run/Event Store ── Dispatch Outbox ── NATS JetStream ── nats_executor
        ↓                                      ↓
compatibility projections                    Agent/MCP/Plugin connectors
```

职责固定为：

- `application` 接收用户命令、校验 Conversation/Run 归属并返回查询或 SSE；
- `kernel` 驱动 Turn/Step、预算、等待、取消、恢复和完成判定；
- `registry` 固定 CapabilityRevision、schema、授权和副作用分类；
- `connector` 只做协议转换、前置条件和结果映射，不能改变 Kernel 状态机；
- `context` 选择带来源的 Context Pack，不把整份事件日志塞进模型；
- `store` 在 PostgreSQL 中原子写状态、事件、Inbox 和 dispatch outbox；
- `projection` 只从新事件生成旧 Conversation/Message/ToolRun/Delegation 读模型；
- `reconciler` 是 Kernel 共享服务，按 Call 级租约调用 `query_status`。
- `DomainBindingResolver` 是 Kernel 依赖的唯一领域端口：`resolve(owner_id, requirements, user_selection) -> BindingSnapshot | NeedsInput`，并提供 `validate(snapshot, mode=delegated)`；Kernel 不直接 import ontology/exploration。

## 3. 规范化状态和关闭原因

### 3.1 Run

Run 状态为 `queued | active | waiting_input | waiting_approval | waiting_external | waiting_retry | paused | cancel_requested | cancelling | cancelled | expired | completed | failed`。终态只有 `cancelled`、`expired`、`completed`、`failed`；终态不可逆。

`cancel_requested` 是用户或 deadline 的意图，`cancelling` 是协作取消和本地收尾中间态。`cancel_reason=user|parent` 最终是 `cancelled`，`cancel_reason=deadline` 最终是 `expired`；同一 Run 行锁下首个成功提交的控制命令获胜，后续冲突命令不改变终态。`cancelling` 超过 `cancel_deadline` 时必须写一次 `run.cancel_timeout` 并收敛本地 Run，未决 Call 进入 `reconciling`。

### 3.2 Call 双轴

Call `status` 为 `offered | dispatched | running | waiting_external | cancel_requested | reconciling | closed`；Call `outcome` 为 `not_sent | accepted | remote_running | completed | failed | cancelled_confirmed | outcome_unknown`。合法组合：

| status | outcome |
|---|---|
| `offered` | `not_sent` |
| `dispatched` | `not_sent` 或 `outcome_unknown` |
| `running`、`waiting_external` | `accepted`、`remote_running` 或 `outcome_unknown` |
| `cancel_requested`、`reconciling` | `accepted`、`remote_running` 或 `outcome_unknown` |
| `closed` | `not_sent`、`completed`、`failed` 或 `cancelled_confirmed` |

`cancel_requested` 只写 status；`remote_running` 是外部观测，不是本地可调度状态；只有证明未发送时才能写 `not_sent`。`outcome_unknown` 不得自动转换为 `failed`。

### 3.3 Turn、Step、Inbox

Turn 和 Step 都是短激活边界，均只能 `open | closed`。Turn close reason：`waiting_input | waiting_approval | waiting_external | waiting_retry | paused | completed | cancel_requested | cancelled | expired | failed | stuck_recovery | interrupted | yielded`。Step close reason：`decision_complete | waiting_input | waiting_approval | waiting_external | waiting_retry | paused | cancelled | failed | interrupted`。`yielded` 只在同一事务已登记下一次激活时使用。

Inbox item 必须绑定 `run_id`，必要时绑定 `call_id`、`approval_id` 或 `question_id`。回答以原 `question_id` 路由；不按“最近任务”猜测。Inbox 的 `accepted_at` 是 TTL 判断时间，晚于 `question_expires_at` 的回答产生 `inbox.expired`，不能复活 Run。

## 4. 逻辑数据合同

第一阶段可以将实体合并为少量表，但不能丢失这些身份、唯一键和引用：

| 实体 | 必备字段/约束 |
|---|---|
| `ExecutionRun` | `id, owner_id, conversation_id, parent_run_id, execution_version, status, wait_reason, goal, acceptance_ref, policy_snapshot_ref, budget_snapshot_ref, deadline, version, lease_epoch, idempotency_key, binding_mode, binding_snapshot_ref, ontology_id, draft_version_id, permission_snapshot_ref, join_policy, required_child_ids, created_at, updated_at, finished_at`；创建幂等键为 `(owner_id,conversation_id,idempotency_key)` 唯一 |
| `ExecutionTurn` | `id, run_id, turn_no, trigger_ref, status, close_reason, opened_at, closed_at`；`(run_id,turn_no)` 唯一 |
| `ExecutionStep` | `id, turn_id, step_no, request_snapshot_ref, status, close_reason, opened_at, closed_at`；`(turn_id,step_no)` 唯一 |
| `ExecutionCall` | `id, run_id, turn_id, step_id, call_index, capability_key, capability_revision, target_ref, input_snapshot_ref, side_effect_class, authorization_snapshot_ref, idempotency_key, status, outcome, remote_task_ref, next_reconcile_at, reconcile_attempt_count, remote_observed_state_ref, manual_attention, lease_epoch, lease_owner, lease_expires_at`；Call 幂等键为 `(run_id,capability_revision,idempotency_key)` 唯一 |
| `ExecutionAttempt` | `id, call_id, attempt_no, transport_request_ref, started_at, finished_at, timeout, provider_status, result_ref, error_ref, safe_to_retry, token_usage_ref, cost_ref`；`(call_id,attempt_no)` 唯一 |
| `ExecutionEvent` | `event_id, run_id, seq, event_type, schema_version, occurred_at, actor, causation_id, correlation_id, idempotency_key, payload_ref/payload, redaction`；`(run_id,seq)` 和 `event_id` 唯一 |
| `InboxItem` | `id, run_id, kind, priority, status, call_id, approval_id, question_id, payload_ref, source, expires_at, expiry_policy, accepted_at, claim_token, claim_expires_at, idempotency_key, consumed_at`；状态 `pending|claimed|consumed|expired|rejected`，kind `user_input|question_answer|external_event|approval_decision|control|resume`，优先级 `control>approval>external>user`；同一来源幂等键唯一 |
| `Approval` | `id, owner_id, run_id, call_id, plugin_revision, target_summary, parameter_summary, scope_summary, capability_revision, parameter_hash, status, expires_at, decided_at, decided_by`；status `pending|approved|denied|expired|revoked`，TTL 与 Run deadline 取更早；冲突或迟到决定返回 409 并保留原决定 |
| `ContextSnapshot` | `id, run_id, turn_id, attempt_id, pack_hash, source_refs, budget, policy_revision, redaction_revision` |
| `Artifact` | `id, owner_id, run_id, call_id, revision, kind, mime_type, size, checksum, storage_ref, status, integrity_status, business_status, visibility, retention_until, provenance_ref`；单 Artifact 默认最大 1 GiB；status `declared|uploading|complete|integrity_failed|business_failed|deleted`；visibility `owner|run|call` |
| `CapabilityRevision` | `key, revision, source, manifest_hash, trust_level, permissions, input_schema, output_schema, side_effect_class, supports_stream, supports_cancel, supports_approval, supports_artifact, supports_query_status, workspace_scope, network_scope, secret_refs, enabled` |
| `ProjectionCursor` | `projection_name, run_id/partition, last_seq, status, error_ref, updated_at`；每个投影分区唯一 |

`binding_mode=delegated` 的 BindingSnapshot 必须含 ontology_id、draft_version_id、lifecycle=editing、owner_id 和 write permission hash；`direct_ui` 可为空或绑定 current release，`legacy` 只用于历史投影。

所有时间使用 UTC；PostgreSQL 使用 uuid/timestamptz，SQLite 测试使用字符串化 UUID 适配同一逻辑约束；凭据只保存受控引用；大正文使用 MinIO 引用和 checksum；Neo4j 只保存可重建的图谱投影。Run/Call lease 使用 epoch、owner、expires_at 三元组，过期写入被 fencing 拒绝；Inbox claim TTL 固定 60 秒。kernel.v1 API 字段统一 snake_case，旧 API casing 保持原样。

状态枚举、Call 双轴组合、事件类型、脱敏模式和 payload 大小由 Kernel command/event service 在所有写入口统一校验；数据库字段保留可查询事实和幂等约束，但不依赖数据库 CHECK 代替服务层状态机。恢复器和人工运维入口也必须复用同一校验器，直接 SQL 写入不属于受支持的执行路径。

## 5. 事件、事务和派发

事件类型注册表 v1：

```text
run.created, run.status_changed, run.expiry_requested, run.cancel_requested, run.child_bound, run.child_joined,
run.cancel_timeout, run.pause_requested, run.recovery_requested,
turn.started, turn.closed, step.started, step.closed,
context.snapshot, request.header, assistant.delta, assistant.message,
call.intent, call.progress, call.outcome_changed,
attempt.started, attempt.result,
inbox.appended, inbox.claimed, inbox.expired, inbox.consumed,
approval.requested, approval.decided, approval.expired, approval.revoked,
artifact.declared, artifact.chunked, artifact.completed,
projection.applied, projection.failed, source.tombstoned
```

SSE 重连首帧使用 synthetic `run.snapshot`（不占 Run seq），随后从游标 seq+1 推送事件；它不写入 Event Store。

每个事件封套必须有 `event_id, run_id, seq, event_type, schema_version, occurred_at, actor, causation_id, correlation_id, command_id, payload, redaction`。`actor.kind` 为 `user | worker | reconciler | connector | plugin | system`；`redaction.mode` 为 `none | reference | redacted`，reference 必须带 `content_ref` 和 checksum；单事件 payload 上限 64 KiB，超出内容必须转 Artifact。`seq` 在同一 Run 的数据库串行化边界内分配，不能由进程内计数器生成；事务回滚不消耗可见序号。新事件类型或 close reason 必须提升 schema version 并更新投影和 UI 映射。

控制事件统一使用 `reason`、`actor`、`command_id` 和 `idempotency_key`：`run.cancel_requested` 另含 `cancel_reason=user|parent|deadline`；`run.pause_requested` 另含 `pause_reason`；`run.expiry_requested` 另含 `deadline` 和 `unresolved_call_ids`；`run.cancel_timeout` 另含 `cancel_deadline`、`unresolved_call_ids`、`run_terminal_status`；`run.recovery_requested` 另含 `lease_epoch`、`diagnostic_ref`。首个在 Run 行锁下成功提交的控制命令裁决终态，后续相同命令幂等返回，冲突命令返回 409。

关键 payload 固定为：

| 事件 | 必需字段 |
|---|---|
| `run.status_changed` | `from, to, reason, actor, version` |
| `call.intent` | `call_id, capability_key, capability_revision, input_snapshot_ref, side_effect_class, idempotency_key` |
| `attempt.started` | `attempt_id, provider_status, request_ref, started_at` |
| `attempt.result` | `attempt_id, provider_status, result_ref, error_ref, safe_to_retry, token_usage_ref, cost_ref` |
| `call.outcome_changed` | `status, outcome, evidence_ref, connector_id, provider_event_id` |
| `inbox.appended` | `inbox_id, kind, target_ref, expiry_policy`；外部来源另含 `provider_event_id` |
| `inbox.claimed` | `inbox_id, claim_token, claim_expires_at, actor` |
| `inbox.expired` | `inbox_id, kind, target_ref, question_id, question_expires_at, expiry_policy, accepted_at` |
| `approval.requested` | `approval_id, run_id, call_id, scope_snapshot_ref, expires_at` |
| `approval.decided` | `approval_id, decision, actor, decided_at, authorization_hash`；`decision` 为 `approved|denied`；过期/撤销只使用独立生命周期事件 |
| `context.snapshot` / `request.header` | `snapshot_id, pack_hash, model, prompt_ref, capability_snapshot_ref` |
| `assistant.delta` | `attempt_id, delta_seq, content_ref`；SSE 可合并发送不超过 4 KiB 的 inline delta，但正文事件仍只保存引用和 checksum |
| `artifact.declared` | `artifact_id, kind, mime_type, size, checksum, storage_ref, visibility` |
| `artifact.chunked` | `artifact_id, chunk_index, offset, checksum` |
| `artifact.completed` | `artifact_id, checksum, integrity_status, business_status` |
| `run.child_bound` / `run.child_joined` | `parent_run_id, child_run_id, join_policy, required` |
| `approval.expired` / `approval.revoked` | `approval_id, reason, actor, occurred_at` |
| `source.tombstoned` | `source_ref, reason, tombstone_at` | 追加事实，不修改历史事件；索引和 Context Pack 排除 |
| `projection.applied` / `projection.failed` | `projection, cursor, error_ref` |

同一幂等 scope 重试且 payload_hash 相同则返回首次结果；同键不同 payload 返回 409 `idempotency_conflict`。parent_run_id 必须同 owner/Conversation、子 Run 不得重复绑定；join_policy 和 required_child_ids 在 `run.child_bound` 事务锁下固定。

所有 mutation event 必须携带 `causation_id`、`command_id` 和命令幂等键；外部回调事件必须带 `connector_id` 与稳定的 `provider_event_id`，唯一键为 `(connector_id,provider_event_id)`，重复时校验 payload_hash，hash 不同则拒绝。未知 schema 或 payload 拒绝自动解释。

任何改变执行事实的命令都按以下顺序在一个 PostgreSQL 事务中完成：

```text
idempotency check → lease/version fencing → state update → event append
→ dispatch outbox append → commit → publish/execute
```

固定使用专用 `execution_dispatch_outbox` 表和 JetStream stream `SA_EXECUTION_V1`。subjects 为 `sa.execution.run.<owner>`、`sa.execution.call.<owner>`、`sa.execution.reconcile`；durable consumers 为 `sa-kernel-v1`、`sa-call-v1`、`sa-reconciler-v1`。Run 唤醒与外部 Call 派发均先写入同一 Outbox，再由对应 durable consumer 执行。Outbox 状态为 `pending | claimed | published | dead`，claim lease 60 秒，最多 10 次指数退避，失败进入 DLQ 并告警；唯一键为 `command_id`。正文只放 ID、revision 和引用。NATS 至少一次投递；发布器和消费端都必须幂等。`SA_EXECUTION_V1` 是新增 stream，不改现有 `PIPELINE_TASKS`、subject union、durable、ack_wait 和 max_deliver；由同一 `nats_executor` handler registry 扩展 `sa-kernel-v1`/`sa-call-v1`/`sa-reconciler-v1`，不启动第二套后台执行体系。反思、Palace、pipeline 和 ontology published 的既有 subject/payload 继续按原合同运行。kernel.v1 长任务不得使用进程内 inline fallback；NATS 暂不可用时 API 仍返回 202 queued，由 Outbox 重试并告警。ExecutionEvent、Inbox 和 SSE replay 至少保留 24 小时，清理任务不得删除窗口内事件；窗口外事件先归档再删除，过早游标返回 410。

## 6. Kernel 循环和恢复

每次激活执行：领取可消费 Inbox → 读取 Run/策略/权限快照 → 选择 Context Pack → 创建 Step 和 request header → 解析 CapabilityRevision → 校验参数和副作用 → 需要时请求 Approval → 持久化 Call intent → 派发 Attempt → 合并结果 → 验证目标和 Artifact → 关闭 Step/Turn 或登记下一次激活。

模型请求只包含经过预算选择的消息、来源、任务状态和能力 schema。上下文选择按 `required > relevant > optional` 分层，Context Pack 默认 hard cap 32k tokens，预算为 system 4k、working 8k、knowledge 12k、episode 4k、recent 4k；超出部分按 relevance → 同域 authority → freshness → cost → source_id 排序裁剪。压缩必须保存来源和 recipe revision。结果按 `call_index` 重建模型视图，事件按到达顺序保留事实。

Worker 持有短租约和 `lease_epoch` fencing，默认 lease TTL 30 秒、heartbeat 10 秒；Run 默认 deadline 24 小时、active Run 并发上限 4、单 Step 模型超时 120 秒、Call 最多 3 次 Attempt、stuck detector 5 分钟。等待输入、审批或外部结果时释放执行租约，只保留唤醒责任。启动扫描未完成 Run：合法崩溃尾部追加 `interrupted`/`outcome_unknown` 并恢复；seq 断裂、非法引用、矛盾终态拒绝自动恢复并写 `run.recovery_requested`。

Reconciliation service 只处理 `reconciling` Call，使用 Call 级租约调用 Connector `query_status(handle)`。默认退避 5 秒起步、倍数 2、上限 5 分钟、最多 12 次；结果映射为 `remote_running`、`completed`、`failed` 或 `cancelled_confirmed`。Connector 无查询能力、达到上限、到达 Run deadline 或凭据失效时设置 `manual_attention=true`，创建关联 Inbox/通知；人工操作只能确认外部事实，不能无证据重发非幂等写。Run 查询显示“结果待确认”，提供 `confirm_completed`、`confirm_failed`、`confirm_remote_running` 三种受控处置，均要求 evidence_ref、追加事件且不得重开 Run。

父子 Run 固定 `join_policy=all|any`，默认 `all`；父取消向所有非终态子 Run 级联发送幂等 cancel，子 Run 终态不自动完成父 Run，父 Run 必须显式归并所有必需子结果；可选子 Run 失败不使父 Run 失败，必需子 Run 失败使父 Run 失败。

## 7. HTTP、SSE 和交互合同

现有端点保留原路径和响应契约：`/api/v2/super-assistant/conversations`、`/conversations/{id}/chat`、`/conversations/{id}/cancel`、`/tool-runs/{id}/decision`、Conversation 文件、Palace、Memory、Skill、MCP、Multica、Remote Agent 管理端点。旧请求固定 `execution_version=legacy`，旧 Conversation 级 409 和 600 秒死流回收只作用于 legacy 投影。旧 SSE 事件集合不与新流混用。

新 Kernel 端点使用同一前缀。kernel.v1 请求和响应全部使用 snake_case：创建请求至少包含 `goal`、`idempotency_key`（HTTP `Idempotency-Key` 必须同时提供且相等，不等返回 422）、可选 `deadline`、`parent_run_id`、`join_policy`、`context_refs`、`binding`；创建响应固定包含 `run_id`、`execution_version`、`stream_url`、`request_id`。Run 查询包含 `status`、`wait_reason`、`version`、`current_inbox`、Call/Artifact 摘要和 `binding_snapshot`。Inputs 请求包含 `kind`、`content`（UTF-8，最大 256 KiB）或 `content_ref` 二选一、`question_id`（回答时必需）、`idempotency_key`；回答过期返回 409，关联不存在返回 404，schema 不符返回 422；审批请求包含 `decision`、`idempotency_key`。

新 Kernel 端点使用同一前缀：

| 方法 | 路径 | 语义 |
|---|---|---|
| POST | `/api/v2/super-assistant/conversations/{conversation_id}/runs` | 创建 Run；必需 `goal` 和 `Idempotency-Key`，返回 `202 {run_id,execution_version,stream_url,request_id}` |
| GET | `/api/v2/super-assistant/runs/{run_id}` | 返回 Run、当前等待项、Call、Artifact 摘要和绑定信息 |
| POST | `/api/v2/super-assistant/runs/{run_id}/cancel` | `202 {command_id,status}`；写入取消意图 |
| POST | `/api/v2/super-assistant/runs/{run_id}/pause` / `resume` | `202 {command_id,status}`；写入控制 Inbox |
| GET | `/api/v2/super-assistant/runs/{run_id}/events?after_seq=` | SSE；支持 `Last-Event-ID`，事件按 seq 连续发送 |
| POST | `/api/v2/super-assistant/runs/{run_id}/inputs` | 提交关联问题回答或追加输入；`content` 或 `content_ref` 二选一，回答时必需 `question_id` 和 `Idempotency-Key` |
| POST | `/api/v2/super-assistant/runs/{run_id}/approvals/{approval_id}/decision` | `202 {command_id,status}`；校验 Run、Call、scope 和版本 |
| GET | `/api/v2/super-assistant/runs/{run_id}/artifacts/{artifact_id}` | 读取有 checksum 的结构化或文件 Artifact |

kernel.v1 新端点的 mutation 要求 owner check、`Idempotency-Key`、`If-Match: run.version`（创建除外；缺失返回 428，版本不匹配返回 409）；legacy 端点沿用各自现有鉴权和幂等语义。跨 owner 返回 404，版本冲突返回 409。SSE 只返回当前用户有权限的脱敏事件。

Widget close/unmount 只中止 SSE subscription，并保存 `run_id:last_seq`；Run 继续执行。重新打开先 GET Run，再用 `Last-Event-ID` 重连；只有停止按钮才 POST cancel。Run 完成、等待和人工处理通过任务卡和现有应用 Inbox 投影展示。

SSE `id` 使用 `run_id:seq`；每个事件有 `event`, `data`, `retry`，heartbeat 使用注释不占 seq。`Last-Event-ID` 优先于 `after_seq`；其格式必须是当前 `run_id:non_negative_seq`，格式错误返回 400，跨 Run 返回 404；游标早于 24 小时保留窗口返回 410 并要求重新获取 Run 快照。kernel.v1 每 15 秒发送 `: ping`，不占 seq，`retry: 5000` 固定；重连首帧为当前 Run snapshot 事件。断线重连不得重复产生业务副作用。事件中可以发送 `run.status_changed`、`call.progress`、`inbox.appended`、`approval.requested`、`artifact.completed` 和 `assistant.delta`；远端不支持的能力不发送伪造事件。

## 8. Agent、插件和委派

`AgentDescriptor` 必须声明 `agent_id, key, revision, transport, capabilities, context_requirements, binding_requirements, session_policy, supports_stream, supports_cancel, supports_push, supports_query_status, supports_artifact`。`session_policy` 为 `new_only | resumable | stateless`。`supports_query_status` 表示连接器实现了 `query_status`；`supports_push` 表示可接收远端事件，均需握手或健康检查证据。

首发 RAP 固定版本 1。direct 请求仍兼容现有 `{message,session_ref}` 和 `{status,content,session_ref,note}`；pull 任务仍兼容 `pending→claimed→done|expired`。RAP v1 无幂等、needs_input、approval、cancel、query_status 或 Artifact 字段时，Connector 必须声明 `false/unsupported`，不得自动重试非幂等写，不得伪造流式、取消或结构化 Artifact。新增字段只允许 additive 或版本化 minor。

远端 `agent.*` 仅是线缆/适配器输入，持久化事件使用 canonical 事件：accepted → `call.outcome_changed`，progress → `call.progress`，needs_input → `inbox.appended + run.status_changed`，approval_required → `approval.requested`，completed/failed → `attempt.result + call.outcome_changed`，cancel_requested → status `cancel_requested`，结果未知 → status `reconciling`、outcome `outcome_unknown`。同一 Call 的 provider event id 去重，事件顺序由 Call 状态不变量校验。

Multica 的 `list_agents/list_tasks` 作为 read_only Capability；`create_task` 作为 `external_async`，必须绑定 workspace scope、保存外部 task/issue id、使用 Call 幂等键并支持 `query_status`/`cancel`。Connector 返回 opaque remote ref 后保持 `waiting_external`，kernel scheduler 周期查询；终态结果写入 `external.result` Artifact 并唤醒 Run，查询不到活动任务时保留 issue 状态，无法确认时进入 `outcome_unknown`。kernel.v1 通过 `MulticaToolConnector` 复用既有服务的 workspace/凭据校验，所有调用仍经 Call/Attempt/Artifact/Outbox；当前仍不承诺 provider 流式传输，实时进度通过状态事件和周期对账提供。

业务探索委派流程固定：解析 descriptor → 发现缺失前置条件 → 以结构化问题询问 → 用户选定本体 → 校验用户拥有可写的 `draft + editing` 版本 → 在同一事务创建带 `binding_mode=delegated` 的子 Run/Call → 后续恢复只读绑定快照。版本失效、权限变化或版本漂移只能重新询问或终止，不能静默改绑。直接探索 UI 的空会话/current release 路径单独保留，不得被委派约束误伤。

## 9. 上下文、知识和记忆

`ContextSource` 统一提供 `source_id, source_type, source_version, locator, content_ref, summary, permissions, validity, sensitivity, freshness, authority, retrieval_cost, token_cost`；每个事实带结构化 `source_ref`：`{kind,id,revision,locator,recipe_revision,extraction_id}`。文件 hash、同步 cursor、分块算法、模型、prompt、schema、过滤器或代码改变时提升 `recipe_revision` 或 `source_version`，重试复用相同 recipe。

原始文件/消息是事实材料，图谱是带 provenance 的推导，记忆是可复用候选或确认事实，Run 状态是任务事实。来源删除、撤销或不可访问时执行 tombstone → index exclusion → Context Pack exclusion；删除后的正文不进入新请求。记忆更新使用新版本或 `supersedes`，保留纠正来源，不覆盖历史。explicit low-risk 记忆默认自动接纳（用户可关闭），derived/reflection 即使 low-risk 也必须确认；medium/high-risk 和指令类永不自动接纳，并保存 confidence 和 source_ref；无来源候选保持 pending。

## 10. 插件信任和资源策略

插件信任级别固定为 `platform | verified | user_untrusted`。用户安装入口强制写入 `user_untrusted`；`platform/verified` 仅保留给未来平台签名或管理员受控流程，不能由用户请求体自选。用户插件可以安装但默认 disabled，必须经过用户显式启用和部署环境能力检查后才能调用。可执行插件宿主使用独立 uid/container；network 默认 deny、workspace 默认 read-only、secret 使用 allowlist；默认资源上限 CPU 1 core、内存 512 MB、wall time 10 分钟。manifest SHA-256 revision immutable，namespace 冲突安装失败。

安装和启用分离。安装流程为 inspect → manifest/schema 校验 → 依赖解析 → 用户启用 → immutable revision；首次调用前由独立宿主完成 healthcheck 和身份/协议校验，失败保持 disabled。用户进程插件通过宿主获得输入引用、凭据引用、取消令牌、deadline、网络/工作区 scope 和 Artifact 回传接口，不能访问 Event Store、租约、授权器或任意数据库；宿主从最小环境启动进程，只有 `manifest.secret_refs` 明确列出的凭据才允许注入，禁止继承其他插件或 Worker 的 `PLUGIN_*` 环境变量；生产环境仍需由部署层提供 uid/container、资源和网络隔离。

现有 Skill 仅作为文本/行为说明按需加载，不因扩展名变成可执行插件；Skill 与 user_untrusted process plugin 不互转。插件安装、启用、卸载和在途 drain 都产生审计事件。卸载先阻止新 Call、撤销凭据、取消/对账在途 Call、停止进程；外部结果未确认时保持 `outcome_unknown`。插件返回的数据不能注册新 Capability、扩大权限或改变 Run 策略。MCP stdio 继续使用现有 enable/allowed-command 门控，不因插件 manifest 放宽；MCP 既有 `require_confirmation=true` 保持兼容；read_only 且仅 owner scope 的能力可在用户明确关闭确认后免确认，所有 `idempotent_write`、`non_idempotent_write` 和 `external_async` 必须审批。

## 11.1 兼容投影的具体规则

新 Run 的增量内容只写 kernel event/ContextSnapshot；不得写旧 `Message.streaming`。投影在 Run 结束或生成安全检查点后，才以 `Message.complete` 写入旧读模型，并以 `run_id` 元数据和 `call_id` 关联 ToolRun；同一 Conversation 的多个 Run 按 Run 创建时间、Turn 序和事件 seq 排列。旧 `GET /messages` 只能看到投影完成行，旧 600 秒 reaper 只扫描 legacy `streaming` 行。新事件的 delta 重复按 `(attempt_id,delta_seq)` 去重。

事件正文、外部回包和凭据引用按 `redaction.mode` 处理：凭据永不写事件；正文只写 content_ref+checksum；用户投影只发脱敏摘要；来源删除追加 `source.tombstoned` 事实并更新 ContextSnapshot、索引和投影；原事件封套不可改写，正文按保留策略物理清除后保留 hash/redaction，expand 返回 410/redacted，不恢复正文。

## 11.2 兼容映射和外部回调安全

旧记录只通过 projection 映射到 kernel.v1，不能反向猜测或改写其他 Run：

| 旧记录 | kernel.v1 映射 | 规则 |
|---|---|---|
| `Message.streaming` | legacy Message 继续由 600 秒 reaper 处理 | 不触发新 Run；新 Run 的事件不进入旧 reaper |
| `Message.complete` | 仅当 projection 有确定的 Run/Call 关联时映射 `Run completed` + `Call closed/completed`；否则只保留 legacy baseline snapshot | 映射只读，保留旧正文和 `message_end` 投影 |
| `Message.cancelled` | `Run cancelled` | 只由 legacy cancel 或 legacy projection 产生 |
| `Message.error` | `Run failed` | 不把旧 error 推断为外部 Call failed |
| `Delegation.running` | `Call running/remote_running` | 新恢复按 Run/Call 引用，不按 Conversation 最近记录 |
| `Delegation.answered` | `Call closed/completed` | 结果写入 `attempt.result` |
| `Delegation.failed` | `Call closed/failed` | 保留原错误引用 |
| `Delegation.timeout` | `Call reconciling/outcome_unknown` | 不自动重发；无查询能力则 manual attention |
| `RemoteTask.pending` | `Call dispatched/not_sent` | 任务 ID 作为 remote_task_ref |
| `RemoteTask.claimed` | `Call running/accepted` | claim 必须校验 agent 与 owner |
| `RemoteTask.done(answered)` | `Call closed/completed` | 回传 event id 幂等 |
| `RemoteTask.done(failed)` | `Call closed/failed` | 错误结构化保存 |
| `RemoteTask.expired` | 未发送则 `Call closed/failed`；已发送则 `reconciling/unknown` | 依据发送证据裁决 |

现有 `Assistant Hub` 的 `answered|failed|cancelled` 只映射为 AgentResult 的 completed/failed/cancelled；前置条件缺失由 Connector 在 `start` 前生成结构化 `needs_input`，不把等待状态塞进旧 `TurnResult.content`。

kernel.v1 外部 direct HTTPS 必须 TLS、token 使用 `secret_ref`；kernel.v1 RAP callback 入口为
`POST /api/v2/super-assistant/runs/{run_id}/calls/{call_id}/callback`，必须携带
`X-Callback-Timestamp` 和 `X-Callback-Signature`。请求体包含
`connector_id`、`request_id`、`provider_event_id`、`payload_hash`、`event_type` 和
`payload`；签名覆盖 `timestamp + run_id + request_id + call_id + provider_event_id + payload_hash`，
允许时钟偏差 5 分钟。`provider_event_id` 去重并拒绝重放，结果回调由同一
reconciler 原子推进 Call/Attempt/Artifact 状态。所有 kernel.v1 回调同时校验 owner、agent、
Call 和 Run 绑定，跨绑定返回 404；回调自述不能提升权限。legacy RAP v1 的公开
`/tasks/{task_id}/result` 保持现有 Bearer 与响应合同，不被新 HMAC 要求收紧；新字段只能以 additive header/版本启用。

## 12. 迁移、回滚和交付顺序

每个 Run 创建时固定 `execution_version=kernel.v1` 和唯一写入责任方，按以下顺序实施：

1. **契约与纯函数**：状态机、事件注册表、幂等、TTL/deadline、source_ref 和 connector mapping。
2. **shadow**：旧 Runtime 权威，新事件只比较，不改变用户行为。
3. **canary**：新 Run 使用 Kernel，旧 Run 按旧路径收尾；先只读，再写，再外部异步。
4. **context/capability**：Context Pack、Memory、Palace、MCP、插件、Hub、RAP 逐类接入。
5. **long task**：NATS wakeup、租约、对账、取消超时和浏览器断开恢复。
6. **projection/UI**：新任务时间线、审批、Artifact、重连和多 Run；旧接口稳定后退役兼容写路径。

迁移旧 `SuperAssistantDelegation` 时，为每行生成 `ExecutionRun(execution_version=legacy)` 和关联 Call，回填原 owner、Conversation、assistant、status、conversation_ref；`timeout/interrupted` 按未知结果或已终态规则映射。无法映射的历史行保持只读并记录处置报告，禁止静默重绑。确认历史回填后，将运行中唯一索引从 Conversation/assistant 作用域替换为 `(run_id,assistant_key)`，并用 Alembic upgrade/downgrade 验证。

回滚只停止创建新版本 Run 或停止新 Worker，保留事件和未完成 Run；不能删除事件、直接改终态或把新 Run 静默转回旧 Kernel。外部写入按未知结果对账，插件按 revision 停用。

## 13. 必须通过的验收（SA-CONTRACT 用例）

开发完成必须提供以下证据：

- `SA-CONTRACT-STATE`：状态机纯函数覆盖所有状态、终态不可逆、取消/deadline 竞争、问题 TTL、暂停 Inbox 和 stuck-run；
- `SA-CONTRACT-EVENT`、`SA-CONTRACT-SEQ`、`SA-CONTRACT-IDEMPOTENCY`、`SA-CONTRACT-LEASE`：同一 Run `seq` 无间隙无重复，重复命令/回调/Outbox 只产生一个逻辑结果，旧租约写入被 fencing 拒绝；
- `SA-CONTRACT-API`、`SA-CONTRACT-SSE`、`SA-CONTRACT-LEGACY`：新旧 API、OpenAPI、SSE、旧 409 和死流回收按 `execution_version` 隔离；
- `SA-CONTRACT-INBOX`、`SA-CONTRACT-RECONNECT`：多 Run 输入按关联 ID 路由；SSE 断线、刷新和 `after_seq` 重连不丢不重；
- `SA-CONTRACT-CONNECTOR`、`SA-CONTRACT-CALLBACK`、`SA-CONTRACT-PLUGIN`：外部 Agent/MCP/plugin 的流式进度、审批、取消、Artifact、未知结果、迟到回调和无能力诚实降级；
- `SA-CONTRACT-RECONCILE`：`query_status` 对账达到确认、人工升级和不重开 Run 的完整链路；
- `SA-CONTRACT-BINDING`：委派只创建绑定了本体、draft+editing 版本和写权限的探索会话；直接 UI 空会话/current release 回归通过；
- `SA-CONTRACT-CONTEXT`、`SA-CONTRACT-MEMORY`、`SA-CONTRACT-ARTIFACT`：图谱 recipe/source version/provenance、来源删除传播、记忆敏感类别和插件运行时自扩拒绝；
- `SA-CONTRACT-STORAGE`、`SA-CONTRACT-NATS`、`SA-CONTRACT-E2E`：PostgreSQL、NATS、MinIO、Neo4j、Alembic、真实 staging 外部副作用和前端下载结果验收通过；
- 执行项目级门禁：`git diff --check`、Markdown 链接检查、仓库卫生检查、受影响后端/前端测试、OpenAPI diff、单 Alembic head、全新与现存数据库升级。

测试用例统一使用 `SA-CONTRACT-*` 命名，并按 setup → action → assert → evidence 落盘到 `.artifacts/sa-contract/<case>/`：

| 用例 | setup/action | assert/evidence |
|---|---|---|
| `STATE` | 构造每个状态并并发提交 cancel/deadline/answer | 只有首个合法命令赢；保存事件序列和状态转移报告 |
| `EVENT/SEQ/IDEMPOTENCY` | 并发 append、事务回滚、重复 command/callback/outbox | `(run_id,seq)` 连续且唯一，重复仅一份逻辑事实；保存数据库断言与回放报告 |
| `INBOX/SSE/LEGACY` | 多 Run 回答、断线、旧 `/chat` 和 600s reaper | 关联 ID 路由、游标重放、旧事件不混流；保存 HTTP/SSE trace |
| `CONNECTOR/CALLBACK/RECONCILE` | 重复/乱序/迟到回调、无 query_status、远端取消 | 进入正确双轴、人工升级且 Run 不重开；保存 provider event 和 Call timeline |
| `BINDING/CONTEXT/MEMORY` | 无本体、draft 失效、来源 tombstone、derived 记忆 | 委派只产生绑定快照，删除后不可召回，derived 保持 pending；保存 SQL/检索报告 |
| `PLUGIN/ARTIFACT/STORAGE/NATS/E2E` | 宿主崩溃、chunk 损坏、重复投递、真实下载 | 权限/隔离/checksum/幂等/最终文件内容均正确；保存 staging artifact 与校验摘要 |

源码完成度以这些用例和项目级门禁为准。本基线没有未决设计项。实现中若发现必须改变公开契约或上述不变量，先提交基线变更并更新受影响的迁移、回滚和测试证据，再开始编码。
