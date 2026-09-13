# 超级助手迁移与验收方案（开发基线 v1.0）

状态：开发基线 v1.0 的迁移与验收说明。本文定义从当前实现迁移到 `kernel.v1` 的阶段、门禁、回滚和真实环境验证。

## 1. 迁移总原则

所有架构稿以 [开发基线 v1.0](./super-assistant-development-baseline.md) 为合同入口；变更必须同时更新受影响的不变量、迁移/回滚方案与验收用例。

- 每个 Run 创建时固定执行版本和唯一写入方；
- 旧 Run 按旧路径收尾，新 Run 才能进入新 Kernel；
- shadow 阶段旧 Runtime 权威，事件只做对比；
- canary 阶段新 Kernel 权威，旧表由投影提供查询；
- 任一阶段发现事件、投影或副作用不一致，停止扩大范围并回到上一版本；
- 不用双写同一个 Run 的状态来“保持兼容”；通过版本隔离保持兼容。

## 2. 阶段与退出条件

### 阶段 0：契约冻结

产出：顶层、执行、能力、Agent、上下文、数据和蓝图文档；状态机纯函数；协议和权限边界清单。

退出条件：所有对象语义没有重复定义；已确认的 HTTP、SSE、RAP、MCP、菜单和数据库契约有负责人；业务澄清委派必须绑定目标本体和编辑中的草稿版本，缺失条件进入 `waiting_input` 的策略已冻结。

契约冻结还必须完成以下五项接缝设计：

- Run 状态机补齐排队取消/过期、等待失效、暂停 Inbox、取消超时和 stuck-run 触发器；
- 将业务澄清现有适配器和领域服务中的最近本体回退、无绑定会话、版本静默漂移和软提示路径列为明确拆除项；
- 给旧 HTTP/SSE 端点建立按 `execution_version` 的语义映射，尤其是单生成 409、会话级 cancel、tool decision、SSE 事件集和 600 秒死流回收；
- 定义 `agent.needs_input`、审批决定的远端回送能力，以及 RAP minor 的幂等字段和不支持能力的诚实声明；
- 将委派恢复和运行中唯一约束从 Conversation/Agent 作用域收敛到 Run/Call 作用域，并给出历史行迁移策略。
- 冻结 Call `status/outcome` 双轴、Turn/Step close reason、事件注册表 v1、`seq` 串行化、问题 TTL/Run deadline 和 reconciliation metadata；
- 明确 `run.expiry_requested`、`run.cancel_timeout`、`run.recovery_requested`、`inbox.expired` 的 payload、redaction、幂等和回放规则；
- 将 `source_ref` 删除传播、插件运行时能力扩展拒绝、旧 facade 退役条件和委派绑定测试列入阶段产物；
- 明确 `binding_mode=delegated` 只约束超级助手委派；直接 UI 的空会话和 current release 兼容语义不在本次隐式收紧范围内。

### 阶段 1：事件 shadow

旧 Runtime 继续完成请求，同时追加 Run/Turn/Step/Call 事件。比较事件投影和现有 Message/ToolRun/Delegation 结果，不让 shadow 事件改变用户行为。

退出条件：正常、工具失败、审批、取消、SSE 断开和进程重启样本可对齐；差异能定位到具体事件。

回滚：关闭 shadow 写入或停止发布器，不影响旧 Runtime。

### 阶段 2：Kernel canary

为新建 Run 启用新 Kernel，旧会话和旧 Run 继续使用旧版本。先覆盖只读能力和可验证 Artifact，再接入写操作与外部 Agent。

退出条件：事件重放、请求重建、租约接管、重复 Outbox、取消竞态通过；旧 API 的投影结果与现有契约一致。

回滚：停止创建新版本 Run，让进行中的 canary Run 按明确的暂停/收尾策略处理；不把未完成 Run 静默转回旧 Kernel。

### 阶段 3：Capability 迁移

依次接入内置工具、Skill、MCP、文件/Palace、Multica、Assistant Hub 和 RAP v1。每种能力先只读，再写入，再外部异步。

退出条件：manifest、权限、审批、超时、取消、未知结果和 revision 固定测试通过；本体助手最近使用回退的旧测试改为澄清/不建子会话，委派探索路径覆盖 `binding_mode=delegated` 的本体、draft+editing 版本和写权限校验，直接 UI 兼容路径保持独立。

### 阶段 4：Context 迁移

将 Memory、Palace、会话、附件和任务状态包装为 ContextSource。先以 shadow Context Pack 记录选择和预算，再让新 Kernel 使用它。

退出条件：来源引用、删除传播、冲突、压缩、出站脱敏和请求重建通过；图谱抽取的 `source_version + recipe_revision + extraction_id` bump 规则、内容定位和 Neo4j provenance 引用可验证；反思由新 Run/Turn 终态事件触发，旧 reflection subject 的幂等与投影保持一致。

### 阶段 5：长任务与 Agent

接入 NATS wakeup、Worker 租约、RAP direct/pull 和外部 Agent 事件。浏览器断开后 Run 继续，SSE 通过序号重连。

退出条件：外部任务接受、进度、审批、取消、未知结果、迟到回调和结构化 Artifact 的真实 staging 验收通过；Connector 的 `query_status`、Call reconciliation、无查询能力的人工升级和 Run 终态不重开均有证据。

### 阶段 6：前端与旧路径退役

增加任务状态、事件时间线、等待输入、审批、取消、Artifact 和失败重试；旧页面通过兼容投影继续工作。

退出条件：新旧页面关键路径、刷新、深链、权限、下载和真实结果验证通过，才删除旧分派逻辑；主页面和悬浮 widget 在 SSE 断开、widget 关闭、Run 继续执行和完成回看时的可见性行为有 E2E 证据。

### 2.1 旧端点与执行版本映射（必须冻结）

| 旧入口 | 旧语义 | 新 Kernel / `execution_version` 处理 |
|---|---|---|
| `POST /conversations/{id}/chat` | 以 Conversation 级“唯一 streaming 消息”闸门返回 409 | 旧版本保持原语义；新版本必须创建带幂等键的 Run，旧版本保持 409；`kernel.v1` 入口返回 202 创建独立 Run，不能让投影行误触发旧闸门 |
| `POST /conversations/{id}/cancel` | 取消该会话唯一 streaming 消息 | 旧版本只取消旧 Message；新版本必须按 Run/Call/Approval 关联取消，不能按“最近任务”猜测 |
| `POST /tool-runs/{id}/decision` | 以 `tool_run_id` 提交决定 | 新版本必须能解析到 `Approval`、Run、Call 和参数/权限快照；映射失败不得消费其他 Run 的审批 |
| 旧 SSE 事件流 | 固定事件集合，缺少 Run 内序号和重连游标 | 旧版本保持兼容；新版本使用版本化事件集合、`event_id`、Run 内 `seq` 和 `after_seq`，不得在同一流中无声明混用两套语义 |
| 600 秒死流回收与启动恢复 | 将遗留 streaming 行收为 error | 必须按执行版本识别旧投影；长任务新投影不得被旧回收器误判，需有 version-aware 迁移验收 |

该表是 legacy 与 kernel.v1 的迁移合同；新增端点、状态码、响应结构、幂等字段和前端策略以开发基线 v1.0 为准。

旧路由的状态码和响应保持不变：Conversation GET=200、POST=201、PATCH=200、DELETE=204，`chat` 为 SSE 且同会话 streaming 返回 409，`cancel` 为 202 `{cancelled}`；tool decision 为 200，缺失 404、已处理 409；MCP/Skill/Memory/Palace/Multica/Remote Agent 的既有 CRUD 状态码、公开 remote task 的 401/404/409/429/204 和 Palace sync token 门禁均保持。kernel.v1 新路由只返回开发基线中的 202/200/404/409/410/422。

迁移执行前先运行 `cd backend && uv run python scripts/super_assistant_migration_report.py`（可加 `--owner-id`），默认只读并输出 `kernel.v1.legacy-disposition.v1` 报告。负责人确认后，使用显式 `--apply --migration-id <id>` 执行逐行幂等回填；回填只生成带原始 source、owner 和 migration id 的 legacy Run/Call 或 immutable、`user_untrusted` CapabilityRevision，不创建外部派发，不复制 secret。Memory/Palace 及缺少 owner、Conversation 或 assistant key 的 Delegation 保持只读并列出 reason，禁止静默重绑。需要撤销时先执行 `--rollback --migration-id <id>` 预览，再加 `--apply` 精确删除该批次生成的 kernel facts；旧表始终不被修改。



### 2.2 现有表和历史数据处置

不得重命名或删除既有 Alembic 表。新增 `execution_runs/turns/steps/calls/attempts/events/inbox_items/approvals/context_snapshots/artifacts/capability_revisions/execution_dispatch_outbox/projection_cursors` 表，或在等价表中增加同名逻辑字段；所有新表使用 UUID、UTC timestamptz、owner 外键和开发基线中的唯一索引。现有 0032–0107 revision 链保持单 head、可升级和可回滚。

- `super_assistant_messages`、`tool_runs`、`super_assistant_delegations` 保持旧列和索引；新 Run 通过 projection 关联 `run_id/execution_version`，不写 `streaming`；delegation 历史逐行回填 legacy Run/Call 后，才替换 Conversation 级 running 唯一索引，无法映射行只读。
- `super_assistant_memories` 的旧 `source` 字符串和 `memory_profiles.auto_accept_enabled` 保留；旧行标记 `legacy`、risk `unknown`，继续可召回但不回溯伪造 `source_ref`。新写入必须 `source_ref+risk`，旧 profile 的 True 只影响 explicit low-risk。
- `palace_files/builds` 的 `content_hash` 映射为 legacy `source_version`；新图谱抽取增加 `recipe_revision/extraction_id/locator`，Neo4j provenance 缺失的历史事实不自动升级为 current。
- `mcp_servers`、`skills` 的 manifest/版本字段通过 immutable `CapabilityRevision` 投影，不把旧配置直接当作受信任能力；既有 Multica、Remote Agent、Palace sync 和公开 remote task API 保持路径和状态码。
- `execution_dispatch_outbox` 使用 `SA_EXECUTION_V1`、`sa.execution.dlq`；行字段含 `command_id/run_id/subject/payload_ref/attempt_count/claim_token/next_attempt_at/error_ref`。DLQ 只允许带原 command_id 的人工重放，重放仍受幂等检查和权限检查约束。

## 3. 回滚原则

回滚分为：

- **流量回滚**：新 Run 停止创建，旧 Run 不改变；
- **Worker 回滚**：停止新 Worker，保留事件和未完成 Run，等待人工处理；
- **协议回滚**：外部 Agent Adapter 降级到已验证能力集合，不伪造流式或取消；
- **数据库回滚**：只回滚尚未成为权威的 schema/投影变更，不能删除已有执行事件；
- **插件回滚**：停用新 revision；若旧 Run 的 revision 被明确撤销，未完成外部调用按未知结果进入人工处理，绝不重定向到新端点或新凭据。未撤销的旧 revision 仍按其固定能力校验执行。

不使用 `git reset`、删除事件表或直接改终态作为业务回滚手段。

## 4. 自动化测试矩阵

### 纯逻辑测试

- 状态转换、终态不可逆、等待和取消优先级；
- Call/Attempt 副作用分类；
- 幂等键、事件 seq、schema version；
- Inbox 关联和多个 Run 路由；
- Context Pack 排序、预算、冲突和引用；
- Descriptor 前置条件和澄清问题；
- Plugin manifest、revision 和权限。

### 后端集成测试

使用隔离 PostgreSQL 验证：事件、状态、Outbox、租约、投影和 Alembic 升级。使用真实 NATS JetStream 验证至少一次派发、重复消费和 worker 接管。

使用真实 MinIO 验证 Artifact 分片、checksum、删除和下载内容；使用 Neo4j 验证图谱来源、版本、删除传播和重复导入幂等。

### 故障注入

在以下窗口终止 Worker：

- 写入 Call intent 前；
- 外部发送前后；
- 收到远端接受后；
- 收到一部分 Artifact 后；
- 状态提交后、Outbox 派发前；
- SSE 已断开但 Run 仍在执行时。

验证不能重复非幂等写入，未知结果会进入对账路径，已完成 Call 不会重新执行。

### 真实协议验收

在隔离 staging 中验证：

- MCP stdio、SSE、streamable HTTP；
- 已有 RAP v1 direct 和 pull；
- 一个支持流式、取消或 Artifact 的外部 Agent；
- 一个只支持最终文本的旧 Agent；
- 重复、延迟、乱序和过期回调；
- 凭据、出站范围和审批撤销。

### 前端 E2E

- 新建 Run、多个 Run 并行、等待输入和正确恢复；
- SSE 断开、刷新、`after_seq` 重连；
- 审批卡片实际阻断动作；
- 取消后的真实状态和迟到结果；
- Artifact 下载内容和 checksum；
- 插件安装、启停、卸载和错误展示；
- 本体助手和业务澄清的前置条件询问；
- 业务澄清没有目标本体或编辑中草稿版本时不会创建子会话；
- admin/editor/viewer 菜单权限不越界。
- `run.cancel_timeout` 的超时、迟到远端确认和 Call 对账端到端一致；版本生命周期事件能唤醒绑定 Run；结构化 `source_ref` 删除传播和插件运行时自扩拒绝均有测试。

## 5. 质量门禁

每个阶段至少执行：

```text
git diff --check
node scripts/ci/check-markdown-links.mjs
bash scripts/ci/check-repository-hygiene.sh
backend affected pytest
frontend affected unit / E2E
real staging checks for external side effects
```

进入新 Kernel 前增加：

- OpenAPI 与既有路由 diff；
- Alembic 单 head、全新数据库升级和现存数据库升级；
- 事件回放一致性报告；
- Worker 重启和租约接管报告；
- 外部写入重复执行为零的证据；
- SSE 重连和 Artifact 内容校验。

另外必须提供：RAP direct 重试无重复证明、needs_input/审批回送测试、旧端点按执行版本映射测试、投影 streaming 行与死流回收器的版本隔离测试、委派唯一索引替换和历史行处置报告、stuck-run 检测与 `outcome_unknown` 对账收敛报告。

阶段 0 的退出审查必须逐项签收 `SA-CONTRACT-STATE/EVENT/SEQ/IDEMPOTENCY/LEASE/INBOX/API/SSE/LEGACY/CONNECTOR/CALLBACK/PLUGIN/RECONCILE/BINDING/CONTEXT/MEMORY/ARTIFACT/STORAGE/NATS/E2E` 用例和证据；任何用例失败都阻止进入下一阶段。

## 6. 上线硬门禁

生产只允许 `platform`、`verified` 插件；`user_untrusted` 只能在独立 uid/container、network deny、workspace read-only 和 secret allowlist 均具备时启用。kernel.v1 固定运行默认值为：Run deadline 24h、active Run 并发 4、Step 模型超时 120s、Call 最多 3 次、lease TTL 30s/heartbeat 10s、stuck detector 5m、cancel grace 30s、reconciliation 5s→5m/12 次、outbox 最多 10 次、事件与 SSE replay 24h、Artifact 默认保留 30d（用户删除立即失效）。

NATS poison message、DLQ、backpressure、最大重试和积压告警必须在 staging 演练；新增 `SA_EXECUTION_V1` 只追加 stream/subject，不改现有 `PIPELINE_TASKS` 和旧 durable。kernel.v1 长任务禁止无 NATS 时 inline fallback；Palace legacy fallback 只在 legacy 路径退役前保留并单独验收。

staging 的第一步使用 `cd backend && uv run python scripts/super_assistant_kernel_live_e2e.py`。
该脚本只读检查 PostgreSQL、`SA_EXECUTION_V1` 的 Run/Call/Reconcile subjects、MinIO
Artifact bucket 和 Neo4j 连通性，并在 `.artifacts/super-assistant-kernel-live-e2e.json`
生成机器可读证据；缺失依赖时返回非零。`--allow-missing` 只用于开发环境收集缺失项，
不能作为发布验收结果。依赖探针通过后，才执行本计划中的真实长任务、Connector、回调、
撤销、重启和 DLQ 场景。

## 7. 可观测指标

初始目标值固定为：Run 创建到首次可见进度 ≤2s（正常依赖可用时）、Worker 恢复 ≤60s、Artifact 校验完成 ≤30s（1GB 以内）、SSE replay 24h。持续记录 Run 首进度、成功/失败/未知比例、恢复耗时、租约接管、重复回调/Outbox、Context Pack 裁剪、取消确认和 Artifact 完整性失败；超过目标只产生告警，不改变状态机语义。

## 8. post-v1 deferred

以下能力明确延期，不阻塞 kernel.v1：A2A 和 ACP 适配器、超级助手被外部系统调用、多租户隔离、插件市场、跨用户共享记忆、分布式工作流引擎、“断开即停”用户偏好，以及把内置 subagent 迁移为独立外部 Connector。任何延期能力都不得在本轮实现中引入第二套执行事实或状态机。
