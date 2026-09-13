# OpenOntology 文档

仓库只维护少数长期有效的文档，功能与行为说明以代码和测试为准
（见 [AGENTS.md](../AGENTS.md) 第 7 节）。

```text
docs/
├── development/      本地环境搭建与测试门禁
├── operations/       配置、部署、回滚、备份与排障
└── architecture/     处于讨论阶段的架构设计提案
```

## 开发

- [开发目录](./development/README.md)
- [本地开发](./development/setup.md)：启动完整本地栈与源码开发；
- [测试指南](./development/testing.md)：测试分层和强制门禁。

## 架构设计

- [超级助手架构设计目录（提案）](./architecture/README.md)：按顶层到细节的完整阅读顺序和讨论规则。
- [超级助手顶层架构设计（提案）](./architecture/super-assistant-top-level.md)：目标、边界、分层和演进原则。
- [超级助手执行模型（提案）](./architecture/super-assistant-execution-model.md)：Run、Turn、Step、Event、Inbox 和恢复语义。
- [超级助手能力与插件模型（提案）](./architecture/super-assistant-capability-plugin-model.md)：能力目录、插件生命周期、隔离和调用策略。
- [超级助手 Agent 协作模型（提案）](./architecture/super-assistant-agent-connector-model.md)：内部助手、RAP、A2A、Agent Client Protocol 和 MCP 的边界。
- [超级助手上下文、记忆与知识模型（提案）](./architecture/super-assistant-context-memory-model.md)：Context Pack、私人知识、记忆、图谱、压缩和 Artifact。
- [超级助手数据与事件模型（提案）](./architecture/super-assistant-data-event-model.md)：逻辑实体、事件封套、Outbox、幂等和兼容投影。
- [超级助手实现蓝图（提案）](./architecture/super-assistant-implementation-blueprint.md)：包边界、Protocol 和 Runtime 拆分顺序。
- [超级助手迁移与验收方案（提案）](./architecture/super-assistant-migration-validation-plan.md)：阶段、回滚、故障注入和真实环境门禁。

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
不能用推测填空。
