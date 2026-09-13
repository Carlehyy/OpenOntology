# 超级助手数据与事件模型（提案）

状态：讨论稿。本文把执行模型落到逻辑实体、事件封套、Outbox、幂等和兼容投影；表名和字段类型仍需在实现阶段根据现有 Alembic 约束确认。

## 1. 数据责任

PostgreSQL 是执行状态、事件、Inbox、审批和索引元数据的权威存储。NATS 只负责至少一次派发和唤醒；MinIO 保存大型 Artifact；Neo4j 保存带来源的图谱投影。任何一个外部系统都不能反向成为 Run 状态的唯一事实源。

单用户部署仍保留 `owner_id`、scope 和资源边界。这样可以避免把今天的实现固化成无法演进的全局单例，也能防止用户插件跨会话读写。

## 2. 逻辑实体

| 实体 | 关键关系 | 责任 |
|---|---|---|
| ExecutionRun | 属于 Conversation，可有 parent Run | 目标、状态、版本、租约和预算 |
| ExecutionTurn | 属于 Run，按序排列 | 一次激活区间和输入来源 |
| ExecutionStep | 属于 Turn，保存请求视图 | 一次模型决策和 Call 归并边界 |
| ExecutionCall | 属于 Step，拥有稳定 call id | 逻辑能力调用和副作用分类 |
| ExecutionAttempt | 属于 Call，按次递增 | 一次实际发送、响应和未知结果 |
| ExecutionEvent | 属于 Run，按 seq 排列 | 不可变事实和回放依据 |
| InboxItem | 属于 Run，关联 Call/Approval | 用户、外部系统和控制输入 |
| Approval | 关联 Call 或插件安装 | 申请、决定、过期和撤销 |
| ContextSnapshot | 关联 Turn/Attempt | 模型可见上下文来源和策略版本 |
| Artifact | 关联 Run/Call | 产物元数据、校验、状态和引用 |
| CapabilityRevision | 安装或平台能力的不可变版本 | schema、权限和运行约束 |
| ProjectionCursor | 关联旧表或视图 | 兼容投影消费位置和错误 |

这些是逻辑边界，不要求每个实体立即独立建表。第一阶段可以把事件与最小 Run 状态放入少量表，但不能丢失上述身份关系。

## 3. Run 最小字段语义

```text
id
owner_id
conversation_id
parent_run_id
execution_version
status
wait_reason
current_turn_no
version
lease_epoch
idempotency_key
goal / acceptance_ref
policy_snapshot_ref
deadline
created_at / updated_at / finished_at
```

`version` 用于乐观并发控制，`lease_epoch` 用于 fencing。`execution_version` 在创建时固定，决定哪个 Kernel 和投影负责该 Run；恢复时不得静默切换版本。

## 4. Call 与 Attempt

Call 保存：

```text
call_id
run_id / turn_id / step_id
capability_key / capability_revision
target_ref
input_snapshot_ref
side_effect_class
idempotency_key
authorization_snapshot_ref
status
remote_task_ref
outcome
```

`target_ref` 必须能关联父 Run、目标 Agent/能力 revision 和不透明的远端会话引用；委派恢复不得只按 Conversation 和 Agent 取“最近一条”。同一目标 Agent 可以被同一 Conversation 的多个 Run 同时调用，唯一性和运行中约束必须至少收敛到 Run/Call 作用域；现有按“会话 + 助手”限制一条 running 的索引需要在迁移方案中替换，历史行不能被静默重绑。

Attempt 保存：

```text
attempt_no
transport_request_ref
started_at / finished_at
timeout
provider_status
result_ref
error_ref
```

`outcome` 至少区分 `not_sent`、`accepted`、`completed`、`failed`、`cancel_requested`、`cancelled_confirmed`、`outcome_unknown`。超时不等于失败，网络错误不等于未发送。

契约冻结时必须明确两条轴：`status` 表示 Call 当前是否可继续调度（例如 `offered`、`dispatched`、`running`、`waiting_external`、`cancel_requested`）；`outcome` 表示外部动作已知的结果或未知结果（例如 `not_sent`、`accepted`、`completed`、`failed`、`cancelled_confirmed`、`outcome_unknown`）。两者不能各自扩展出第二套状态机；同名值的转换规则、终态条件和 UI 展示值必须由一份权威枚举表冻结。

## 5. Event Envelope

所有事件使用同一封套：

```json
{
  "event_id": "stable id",
  "run_id": "run id",
  "seq": 42,
  "event_type": "agent.progress",
  "schema_version": 1,
  "occurred_at": "timestamp",
  "actor": {"kind": "worker", "id": "worker id"},
  "causation_id": "command or event id",
  "correlation_id": "run or call id",
  "idempotency_key": "optional stable key",
  "payload": {},
  "redaction": {"mode": "reference", "content_ref": "optional"}
}
```

`seq` 只在 Run 内单调递增；`event_id` 全局唯一；`schema_version` 不随代码版本隐式改变。Payload 未知版本必须拒绝自动解释或进入显式兼容处理。

`seq` 的分配必须在同一 Run 的串行化边界内完成（Run 行锁、等价的 advisory lock 或其他可证明的原子机制），不能使用 Worker 进程内计数器。并行 Worker、重试和事务回滚都必须保证无间隙、无重复、不可回写；具体锁策略在字段/事务契约中冻结。

## 6. Payload 分类

### 生命周期

```text
run.created
run.status_changed
turn.started
turn.closed
step.started
step.closed
```

### 请求与上下文

```text
context.snapshot
request.header
assistant.delta
assistant.message
```

`request.header` 保存模型、system prompt、可见能力 schema 和 Context Snapshot 的版本或加密引用。不要把凭据、隐藏推理或已删除正文写入不可控的明文事件。

### 能力调用

```text
call.intent
attempt.started
call.progress
attempt.result
call.outcome_changed
```

### 用户与控制

```text
inbox.appended
inbox.claimed
approval.requested
approval.decided
run.cancel_requested
run.pause_requested
```

### 产物和投影

```text
artifact.declared
artifact.chunked
artifact.completed
projection.applied
projection.failed
```

## 7. 事务与 Outbox

现有面向用户通知的 Inbox/Outbox 记录不能直接充当执行派发 Outbox：两者的消费语义、保留时间和重试责任不同。新执行模型使用独立的 dispatch outbox 逻辑（是否落独立表和 JetStream stream 在实现阶段确认），但必须和 Run 状态、事件在同一 PostgreSQL 事务中提交。现有 reflection、Palace 和 pipeline subject 保持不变。

实现时应优先复用仓库 Sentinel CDC Outbox 已验证的同事务插入、唯一 `dedupe_key`、claim token CAS 和退避模式；这是一种实现先例，不改变执行 Outbox 与用户通知 Inbox/Outbox 的职责分离。

改变执行事实的命令必须在一个数据库事务内完成：

```text
idempotency check
→ lease / version fencing
→ state update
→ event append
→ outbox append
→ commit
```

Dispatch Outbox 行携带 `command_id`、`run_id`、目标 subject、尝试次数和下一次投递时间；消息正文只放 ID 和版本引用，不放 prompt、凭据或大 Artifact。发布成功不能删除事实事件；可以标记 Outbox 已发送。发布失败由发布器重试，消费端按 `event_id` 或业务幂等键去重。

Inbox 的 `claimed` 不等于 `consumed`：领取必须有租约、过期时间和 fencing；只有关联结果事件或控制命令的事务提交后才标记消费。Worker 在 claim 后崩溃时，其他 Worker 可以在租约到期后重领，不能因先删除队列项而形成 at-most-once 丢失窗口。

跨 PostgreSQL、外部 Agent、MCP、Neo4j 和 MinIO 不做分布式事务。先记录本地意图，再执行外部动作；外部动作的结果通过 Call/Artifact 事件回填。

## 8. 兼容投影

现有 Conversation、Message、ToolRun、Delegation 和 RemoteTask 表在迁移期继续服务旧页面和旧 API：

```text
new events → projection worker → legacy read models
```

旧 API 发起的写操作必须转换为 Kernel command，不得直接更新事件权威。投影重复执行必须幂等；投影失败保留错误和 cursor，不能跳过未知事件。

历史旧消息可以导入为 `legacy.baseline` 快照，但不能声称旧记录拥有当时不存在的 request header 或 Context Snapshot。

## 9. 保留与删除

正文、请求快照、外部返回和凭据引用的保留策略分开定义。用户删除私人知识或记忆后：

- 检索索引和当前投影立即排除；
- 对象正文按删除策略清理；
- 无正文的执行元数据可按审计策略保留；
- 不能再声称可以重建已删除正文。

## 10. 数据模型验收

- 同一 Run 的 seq 无间隙、无重复、不可回写；
- 同一幂等键重复提交只产生一个逻辑 Call；
- 旧租约不能覆盖新版本；
- Outbox 重复投递不重复执行不可幂等写入；
- Call 和 Attempt 的未知结果可查询、可对账；
- `outcome_unknown` 有明确的 reconciler、查询节奏、升级人工条件和用户可见状态；
- 投影崩溃后从 cursor 继续，不跳过事件；
- 删除后的来源不再进入新的 Context Pack；
- 事件 schema 升级可拒绝不兼容 payload；
- 同一 Run 的执行版本和唯一写入方保持不变。
