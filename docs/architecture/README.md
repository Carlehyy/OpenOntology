# 超级助手商用升级架构基线

本目录是超级助手商用升级的开发基线。阅读入口是 [开发基线 v1.0](./super-assistant-development-baseline.md)，其余文档分别解释顶层边界、执行状态机、能力插件、Agent 协作、上下文记忆、数据事件、实现拆分和迁移验收。所有实现任务都必须引用基线中的状态、事件、API、权限和验收合同。

## 阅读顺序

1. [开发基线 v1.0](./super-assistant-development-baseline.md)：唯一合同入口、冻结选择和交付门禁。
2. [顶层架构](./super-assistant-top-level.md)：目标、边界、分层和不变量。
3. [执行模型](./super-assistant-execution-model.md)：Run、Turn、Step、Call、Attempt、等待、取消和恢复。
4. [能力与插件模型](./super-assistant-capability-plugin-model.md)：Capability、Tool、Agent、Skill、Plugin 和 Connector。
5. [Agent 协作模型](./super-assistant-agent-connector-model.md)：内部助手、RAP、A2A、Agent Client Protocol 和 MCP。
6. [上下文、记忆与知识模型](./super-assistant-context-memory-model.md)：Context Pack、私人知识、记忆、图谱、压缩和 Artifact。
7. [数据与事件模型](./super-assistant-data-event-model.md)：逻辑实体、事件封套、Outbox、幂等和兼容投影。
8. [实现蓝图](./super-assistant-implementation-blueprint.md)：包边界、Protocol 和 Runtime 拆分顺序。
9. [迁移与验收方案](./super-assistant-migration-validation-plan.md)：阶段、回滚、故障注入和真实环境门禁。
10. [审查交接包](./super-assistant-review-packet.md)：原始需求、关键决策和审查资料。

## 使用规则

- 基线中的对象、状态、事件、端点、默认配置和权限只有一份定义；专题文档只能展开说明，不能另建词汇。
- 旧 API、SSE、NATS subject、数据库表和前端路径属于兼容契约；新 Kernel 使用 `execution_version=kernel.v1`，不得无版本混流。
- 业务探索的超级助手委派必须使用 `binding_mode=delegated` 并绑定本体及 `draft + editing` 版本；直接 UI 的空会话和 current release 语义保持现状。
- A2A、ACP、超级助手被外部调用、多租户和插件市场列为 post-v1 deferred，不阻塞本轮开发，也不在实现中偷偷预埋第二套运行时。
- 任何必须改变基线的实现发现，都要先更新基线、迁移/回滚方案和验收用例，再修改源码。
