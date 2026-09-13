# 超级助手架构设计目录（提案）

这些文档是从顶层架构逐层细化的讨论稿。实现、现有行为和外部契约仍以源码、测试、Alembic 历史和项目级 `AGENTS.md` 为准。

## 阅读顺序

1. [顶层架构](./super-assistant-top-level.md)：目标、边界、分层和不变量。
2. [执行模型](./super-assistant-execution-model.md)：Run、Turn、Step、Call、Attempt、Event、Inbox 和恢复。
3. [能力与插件模型](./super-assistant-capability-plugin-model.md)：Capability、Tool、Agent、Skill、Plugin 和 Connector。
4. [Agent 协作模型](./super-assistant-agent-connector-model.md)：内部助手、RAP、A2A、Agent Client Protocol 和 MCP。
5. [上下文、记忆与知识模型](./super-assistant-context-memory-model.md)：Context Pack、私人知识、记忆、图谱、压缩和 Artifact。
6. [数据与事件模型](./super-assistant-data-event-model.md)：逻辑实体、事件封套、Outbox、幂等和兼容投影。
7. [实现蓝图](./super-assistant-implementation-blueprint.md)：包边界、Protocol 和 Runtime 拆分顺序。
8. [迁移与验收方案](./super-assistant-migration-validation-plan.md)：阶段、回滚、故障注入和真实环境门禁。

## 讨论规则

- 下层设计不能改变上层对象语义；如需改变，先回写顶层文档。
- 讨论稿不直接代表开发任务，不以文档代替契约测试。
- “支持流式/取消/恢复”只能在 Connector 的真实能力协商和测试通过后声明。
- 业务澄清委派必须绑定目标本体和编辑中的草稿版本；缺失时先询问用户，不创建无绑定委派会话。
