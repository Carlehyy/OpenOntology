# 超级助手实现蓝图（提案）

状态：讨论稿。本文把已确定的架构边界映射到现有 Python 包和后台运行机制，作为后续开发拆分依据，不是立即执行的重构清单。

## 1. 推荐包结构

新代码继续位于 `backend/app/super_assistant/`，按职责拆分，不为每个页面新建业务域：

```text
super_assistant/
├── kernel/
│   ├── coordinator.py       Run 激活、续行和终态决策
│   ├── state_machine.py     纯状态转换和不变量
│   ├── commands.py          用户、控制、恢复和外部回调命令
│   ├── inbox.py             next_turn / next_step 和关联输入
│   └── leases.py            Worker 租约与 fencing
├── execution/
│   ├── models.py            Run / Turn / Step / Call / Attempt 逻辑模型
│   ├── attempts.py          重试与未知结果处理
│   └── budgets.py           时间、次数、token 和 Artifact 预算
├── events/
│   ├── store.py             Event append / stream / replay
│   ├── outbox.py            事务 Outbox 与发布
│   └── projections.py       旧 Conversation / ToolRun 等投影
├── capabilities/
│   ├── descriptors.py       Capability、Tool、Agent 描述
│   ├── registry.py          作用域目录和 revision 快照
│   ├── policy.py            权限、审批和数据范围
│   └── runner.py            schema、超时、取消、并行和结果顺序
├── plugins/
│   ├── manager.py           安装、启停、drain 和卸载
│   ├── process_host.py      用户进程插件宿主
│   └── protocol.py          initialize / list / call / progress
├── connectors/
│   ├── agents/base.py      AgentConnector Protocol
│   ├── agents/assistant_hub.py
│   ├── agents/rap_v1.py
│   ├── agents/a2a.py        后续适配器
│   ├── tools/builtin.py
│   ├── tools/mcp.py
│   └── tools/remote.py
├── context/
│   ├── planner.py           Context Pack 预算和快照
│   ├── sources.py           Context Source Protocol
│   ├── memory.py            Memory provider facade
│   ├── knowledge.py         Palace / 图谱 provider facade
│   └── compaction.py        可追溯压缩
├── artifacts/
│   ├── service.py           元数据、MinIO、checksum
│   └── validation.py        类型、大小和完整性检查
└── compatibility/
    ├── runtime_facade.py    旧 stream_chat 接口适配
    ├── legacy_projection.py
    └── rap_projection.py
```

`assistant_hub` 仍是平台助手目录与业务适配的 canonical package。`connectors/agents/assistant_hub.py` 只依赖 Hub 契约，不直接导入 ontology 或 exploration 域。

## 2. 最小 Protocol

初期用 Python `Protocol` 和不可变 dataclass 表达边界，避免先创建复杂继承树。

```python
class EventStore(Protocol):
    def append(self, command: AppendCommand) -> list[ExecutionEvent]: ...
    def stream(self, run_id: str, after_seq: int = -1) -> Iterator[ExecutionEvent]: ...

class ContextSource(Protocol):
    def search(self, request: ContextQuery) -> list[ContextCandidate]: ...
    def expand(self, source_id: str, revision: str) -> ContextEvidence: ...

class CapabilityRunner(Protocol):
    def start(self, intent: CallIntent, cancel: CancellationToken) -> CallHandle: ...

class AgentConnector(Protocol):
    def descriptor(self) -> AgentDescriptor: ...
    def start(self, request: AgentRequest) -> AgentHandle: ...
    def poll(self, handle: AgentHandle) -> list[AgentEvent]: ...
    def cancel(self, handle: AgentHandle) -> CancelOutcome: ...

class ExecutionKernel(Protocol):
    def activate(self, command: RunCommand) -> ActivationResult: ...
    def resume(self, run_id: str, reason: WakeReason) -> ActivationResult: ...
    def cancel(self, run_id: str, reason: str) -> CancelRequestResult: ...
```

这些是边界语义，不是最终公开 API。调用结果必须携带事件关联、Call/Attempt 身份和是否可以安全重试的事实。

## 3. Runtime 拆分顺序

当前 `runtime.py` 先由 `compatibility/runtime_facade.py` 包住，按下列顺序提取：

1. 工具目录和 schema 组装；
2. Context Planner 和 prompt snapshot；
3. CapabilityRunner 和并行结果归并；
4. Approval/Policy；
5. Model Request Step；
6. Agent Call；
7. Event Store、Outbox 和 durable worker；
8. 删除旧分派分支。

每一步保留旧测试 seam，避免同时改变 import 路径、HTTP 协议和执行语义。

## 4. 不应插件化的部分

以下能力属于平台内核或安全组合根，不能由用户插件替换：

- Event Store 和事件 schema 校验；
- Run 状态机、租约、幂等和 Outbox；
- 当前用户身份、菜单/RBAC 和审批裁决；
- 凭据解密和 secret scope；
- Context Pack 脱敏与出站策略；
- Artifact 权限和完整性校验；
- 数据库事务和投影 cursor。

用户插件只能提供工具、资源、Skill 或 Agent Connector 实现，并经宿主调用。

## 5. 与 Rust Harness 的映射

| Harness 思想 | OpenOntology 实现 |
|---|---|
| append-only Session | PostgreSQL ExecutionEvent + Run projection |
| Turn / Step | Run activation、Model Request Step 和 Call |
| Inbox | PostgreSQL InboxItem + NATS wakeup |
| Tool Registry | CapabilityRegistry + Policy + Runner |
| Service Registry | ContextSource、Artifact、Model provider 的受限组合 |
| request reconstruction | ContextSnapshot + request.header 校验 |
| process plugin | 受控用户插件宿主，不直接授予 shell 信任 |
| subagent loop | AgentConnector Call，不另建第二套 Kernel |
| crash repair | 合法事件尾部闭合 + outcome_unknown 对账 |

借鉴的是不变量和接缝，不复制本地 JSONL、tokio 内存锁或任意动态挂载的安全假设。

## 6. 开发边界

一个开发任务必须只改变一个层级或一种迁移动作：原样移动、导入路径调整、状态机实现、投影、前端事件消费和协议 Adapter 分开验证。新增能力不能直接在兼容 router 或巨型 runtime 分支内实现。
