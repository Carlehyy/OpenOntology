# 后端应用目录

`app/main.py` 是当前 FastAPI 装配入口。业务实现优先按稳定能力定位：

```text
app/
├── main.py            FastAPI HTTP composition root
├── config.py          兼容配置导入；canonical 配置在 shared/config.py
├── database.py        SQLAlchemy engine、Session 与 Base
├── deps.py            兼容依赖入口；canonical 依赖在 shared/deps.py
├── bootstrap/         FastAPI 健康检查、生命周期与启动 seed
├── platform/          平台概览
├── super_assistant/   超级助手、Skill 与 MCP
├── assistant_hub/     平台助手目录与统一委派契约（超级助手经此委派其他助手）
├── assistant_evaluation/  助手会话质量旁路评估（admin）
├── exploration/       业务探索
├── ontologies/        本体、映射、图、Agent 与 Sentinel
├── world_model/       世界模型（推演模型/推演服务/调用记录）
├── scenes/            三维场景（白模场景管理与建模）
├── events/            事件登记
├── tickets/           工单（使用反馈）
├── data_channel/      连接、数据集、流水线与数据管家
├── api_hub/           接口定义、发布、凭据与代理
├── community/         Plugin 社区 MCP 适配；Skill 社区当前无后端
├── model_configs/     模型配置
├── settings/          系统设置
├── auth/              身份、角色与菜单授权
├── inbox/             收件箱契约
├── shared/            迁移期共享基础能力
├── tasks/             后台任务体入口（Celery 已退役；定时/后台任务走 APScheduler + NATS JetStream → nats_executor）
├── engine/            预留的运行引擎 package；旧 post-harness 已退役
├── routers/           以兼容 facade 为主，仍有例外
├── models/            以兼容 facade/注册为主，仍有例外
├── schemas/           以兼容 facade 为主，仍有例外
└── services/          以兼容 facade 为主，仍有例外
```

不要根据目录名猜测 canonical。兼容例外、特殊模块身份和迁移顺序见
[AGENTS.md 兼容层例外台账](../../AGENTS.md)。HTTP、menu key、
Alembic revision、NATS subject 和 patch 路径都可能是兼容契约。

`main.py` 对部分 v1 域仍经 `app/routers/` facade 挂载（如 auth：`main.py` →
`routers/auth.py` → `auth/router.py` 两跳）；实现改动落在 canonical 包，
facade 只作转发。`settings/` 无顶层 router：HTTP 入口经 compat facade
（`routers/users.py`、`routers/domains.py`）转发至 `settings/users`、
`settings/domains`，`routers/settings.py` 聚合器仅挂 monitoring。

两条最复杂的核心链路另有目录入口：

- [数据通道](./data_channel/README.md)
- [本体业务域](./ontologies/README.md)
