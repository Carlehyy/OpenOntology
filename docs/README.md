# OpenOntology 文档

本目录按受众和可信度分层。产品叙事、客户运维、开发/Agent 约束和内部架构材料
不能混用；功能与行为说明必须回到代码、可执行配置或测试（见
[AGENTS.md](../AGENTS.md) 第 7 节）。

```text
docs/
├── product/          产品能力、事实边界与合成演示案例
├── development/      本地环境搭建与测试门禁
├── operations/       配置、部署、回滚、备份与排障
└── architecture/     内部设计基线、迁移方案与审查材料（非产品承诺）
```

## 按受众查找

| 受众 | 入口 | 内容边界 |
|---|---|---|
| 产品、销售和客户沟通 | [产品文档](./product/README.md) 与根 `README.md` | 只写可追溯能力和明确标注的合成案例 |
| 部署和运维 | [运维目录](./operations/README.md) | 配置、部署、监控、备份、回滚和排障 |
| 开发者和 Agent | [开发目录](./development/README.md)、根 `AGENTS.md`、根 `DESIGN.md` | 代码边界、测试门禁、设计系统和兼容契约 |
| 内部架构评审 | [架构目录](./architecture/README.md) | 提案、迁移和审查记录；不能直接当作已交付能力 |

## 开发

- [开发目录](./development/README.md)
- [本地开发](./development/setup.md)：启动完整本地栈与源码开发；
- [测试指南](./development/testing.md)：测试分层和强制门禁。

## 产品

- [产品文档目录](./product/README.md)
- [平台架构](./product/architecture.md)：组件职责、主数据边界和运行依赖。
- [能力矩阵](./product/capability-matrix.md)：当前实现、依赖、限制和推荐表述。
- [跨领域合成案例](./product/synthetic-cases.md)：用多个领域说明通用本体能力，全部为构造数据。

## 内部架构设计

- [超级助手架构设计目录（内部基线）](./architecture/README.md)：按顶层到细节的阅读顺序和讨论规则。
- [超级助手顶层架构设计（提案）](./architecture/super-assistant-top-level.md)：目标、边界、分层和演进原则。
- [超级助手执行模型（提案）](./architecture/super-assistant-execution-model.md)：Run、Turn、Step、Event、Inbox 和恢复语义。
- [超级助手能力与插件模型（提案）](./architecture/super-assistant-capability-plugin-model.md)：能力目录、插件生命周期、隔离和调用策略。
- [超级助手 Agent 协作模型（提案）](./architecture/super-assistant-agent-connector-model.md)：内部助手、RAP、A2A、Agent Client Protocol 和 MCP 的边界。
- [超级助手上下文、记忆与知识模型（提案）](./architecture/super-assistant-context-memory-model.md)：Context Pack、私人知识、记忆、图谱、压缩和 Artifact。
- [超级助手数据与事件模型（提案）](./architecture/super-assistant-data-event-model.md)：逻辑实体、事件封套、Outbox、幂等和兼容投影。
- [超级助手实现蓝图（提案）](./architecture/super-assistant-implementation-blueprint.md)：包边界、Protocol 和 Runtime 拆分顺序。
- [超级助手迁移与验收方案（提案）](./architecture/super-assistant-migration-validation-plan.md)：阶段、回滚、故障注入和真实环境门禁。
- [超级助手架构审查交接包（内部）](./architecture/super-assistant-review-packet.md)：原始需求、关键决策、相关资料路径和审查记录。

## 运维

- [运维目录](./operations/README.md)
- [配置与秘密](./operations/configuration.md)
- [自动部署](./operations/deployment.md)
- [回滚](./operations/rollback.md)
- [备份与恢复](./operations/backup-restore.md)
- [排障](./operations/troubleshooting.md)

## 当前事实源

| 事实 | 权威路径 |
|---|---|
| 项目目标与启动方式 | `README.md` |
| 导航与 menu key | `frontend/src/config/navigation.ts` |
| React 路由 | `frontend/src/App.tsx` |
| 后端路由装配与生命周期 | `backend/app/main.py` |
| 服务端 menu key / RBAC | `backend/app/auth/permissions.py` |
| 数据库历史 | `backend/alembic/versions/` |
| Python 版本与后端依赖 | `backend/pyproject.toml`、`backend/uv.lock` |
| 前端命令与依赖 | `frontend/package.json`、`frontend/package-lock.json` |
| 浏览器测试分组 | `frontend/playwright.*.config.ts` |
| 核心状态门与发布契约 | `backend/app/data_channel/`、`backend/app/ontologies/` 及对应测试 |
| 推荐本地核心完整栈 | `docker-compose.local.yml` |
| 生产编排 | `docker-compose.prod.yml` |
| 自动部署 | `.github/workflows/deploy-nano-ontoprompt.yml` |
| 服务器部署行为 | `deploy/deploy-prod.sh` |
| 本地配置中心 | `config/README.md` |

文档描述必须来自源码、可执行配置或测试。若这些事实互相矛盾，先修正事实，
不能用推测填空。架构目标、阶段计划和审查记录必须保留“未实现/待验收”状态，
不能因为被索引就升级为产品承诺。
