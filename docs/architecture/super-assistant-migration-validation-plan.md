# 超级助手迁移与验收方案（提案）

状态：讨论稿。本文定义从当前实现迁移到新执行内核的阶段、门禁、回滚和真实环境验证，暂不确定具体版本号和上线日期。

## 1. 迁移总原则

本文与其他架构稿当前都是“讨论稿”。进入开发前必须冻结一份设计基线，标明评审人、日期、适用执行版本和待决事项；冻结后变更必须同时更新受影响的不变量与验收用例。

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

退出条件：manifest、权限、审批、超时、取消、未知结果和 revision 固定测试通过。

### 阶段 4：Context 迁移

将 Memory、Palace、会话、附件和任务状态包装为 ContextSource。先以 shadow Context Pack 记录选择和预算，再让新 Kernel 使用它。

退出条件：来源引用、删除传播、冲突、压缩、出站脱敏和请求重建通过。

### 阶段 5：长任务与 Agent

接入 NATS wakeup、Worker 租约、RAP direct/pull 和外部 Agent 事件。浏览器断开后 Run 继续，SSE 通过序号重连。

退出条件：外部任务接受、进度、审批、取消、未知结果、迟到回调和结构化 Artifact 的真实 staging 验收通过。

### 阶段 6：前端与旧路径退役

增加任务状态、事件时间线、等待输入、审批、取消、Artifact 和失败重试；旧页面通过兼容投影继续工作。

退出条件：新旧页面关键路径、刷新、深链、权限、下载和真实结果验证通过，才删除旧分派逻辑。

## 3. 回滚原则

回滚分为：

- **流量回滚**：新 Run 停止创建，旧 Run 不改变；
- **Worker 回滚**：停止新 Worker，保留事件和未完成 Run，等待人工处理；
- **协议回滚**：外部 Agent Adapter 降级到已验证能力集合，不伪造流式或取消；
- **数据库回滚**：只回滚尚未成为权威的 schema/投影变更，不能删除已有执行事件；
- **插件回滚**：停用新 revision，旧 Run 继续使用其固定 revision，未完成外部调用按未知结果处理。

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

## 6. 上线硬门禁

在没有部署级隔离前，生产只允许审核过的第一方或已验证的受控插件；用户任意可执行进程不能仅凭 manifest 上线。还必须给出任务最长时长、并发、恢复时间、SSE 首次进度、Artifact 可用时间和外部 Agent 超时的目标值。目标值由单用户压力和故障演练测量后冻结。

事件、请求快照、Artifact、外部返回和插件日志的保留、脱敏、删除和导出策略必须在上线前冻结。NATS poison message、死信、backpressure、最大重试和积压告警必须有演练结果。

## 7. 可观测指标

初始不硬编码商业 SLO，但必须测量：

- Run 创建到首次可见进度的延迟；
- 模型请求、工具和 Agent 的成功/失败/未知结果比例；
- Run 恢复耗时和租约接管次数；
- 重复回调、重复 Outbox 和投影失败数量；
- Context Pack token、来源覆盖和被裁剪原因；
- 外部 Agent 取消确认率；
- Artifact 完整性失败和用户重试次数。

这些指标用于确定单用户部署的默认并发、预算、超时和后续扩容需求，不在架构阶段凭空设定数值。
