# 能力矩阵与事实边界

本表把产品表述和代码/测试事实放在一起。`已实现` 表示仓库中存在可追踪实现，
不等于所有部署环境都已通过生产验收；`受限` 表示需要外部依赖、额外安全门禁或
发布时验证；`未支持` 表示当前不应作为产品能力宣传。

OpenOntology 提供的是通用的对象、关系、规则、事件、版本和动作能力，不绑定
GRC/IRM 或其他单一行业。产品页面中的行业场景只是说明同一套引擎如何映射到不同
领域，不能反推平台的垂直定位或已有行业连接器。

| 能力 | 状态 | 可公开的谨慎表述 | 事实源 |
|---|---|---|---|
| 本体建模与版本治理 | 已实现 | 可以建模对象、关系、动作和规则，并通过 `Draft → Trial → Impact → Promote` 门控发布 | `backend/app/ontologies/`、`backend/tests/ontologies/`、`AGENTS.md` |
| 数据通道与成品审核 | 已实现/受限 | 支持版本化数据集、流水线、同步任务和成品审核；完整链路依赖 PostgreSQL、Redis、NATS、Neo4j、MinIO 与 n8n 等服务 | `backend/app/data_channel/`、`docker-compose.local.yml`、`docs/development/setup.md` |
| 语义探索 | 已实现/受限 | 可以把语义澄清绑定到本体和编辑中的草稿版本；模型提供商需在启动后配置 | `backend/app/exploration/`、`backend/app/model_configs/`、`docs/architecture/super-assistant-agent-connector-model.md` |
| 超级助手与本体助手 | 已实现/待发布验收 | 支持受治理的会话、输入/审批等待、调用记录和 Artifact；外部 Agent、真实浏览器和部署隔离需单独验收 | `backend/app/super_assistant/`、`backend/app/assistant_hub/`、`docs/architecture/` |
| Sentinel 与 CEP | 已实现/待真实数据验收 | 支持状态变化和事件模式评估，包括时间窗、前值、阶段序列、缺失分支和窗口聚合 | `backend/app/ontologies/sentinels/`、`scripts/data/run_sentinel_cep_e2e.py` |
| 事件登记 | 已实现但独立 | 支持事件登记和查询；当前不能宣称事件登记已自动驱动 Formal 或 Sentinel | `backend/app/events/`、根 `README.md` 的限制说明 |
| API Hub | 已实现/受外部服务约束 | 支持接口定义、代理、授权和调用记录；实时外部服务链路要按环境验收 | `backend/app/api_hub/`、`backend/scripts/api_hub_http_proxy_live_e2e.py` |
| Plugin / MCP 社区 | 已实现入口/隔离待验收 | 支持登记、manifest、启用和调用入口；rootless runner、网络策略和密钥 broker 仍是发布门禁 | `backend/app/community/`、`docs/architecture/super-assistant-capability-plugin-model.md` |
| 模型与系统治理 | 已实现 | 管理员可以在启动后配置模型提供商和系统设置；LLM 不是基础平台启动依赖 | `backend/app/model_configs/`、`backend/app/settings/`、`docs/development/setup.md` |
| 语义搜索 | 未支持 | 不能宣传为已提供能力；当前返回 `501 semantic_search_unsupported`，关键词搜索由 PostgreSQL 提供 | `README.md`、`docs/development/setup.md`、搜索实现与测试 |
| RegisteredEvent 自动接线 | 未验证 | 不能宣称已存在 `RegisteredEvent → Formal/Sentinel` 自动链路 | `README.md`、`backend/app/events/` 与对应测试 |
| 多租户、SLA、价格和合规认证 | 未声明 | 在商业审批和真实证据出现前，不写入产品承诺 | 当前仓库没有可作为正式承诺的权威文件 |

## 版本发布前的最小证据

- Markdown 链接、仓库卫生和部署变更分类通过；
- 受影响后端测试、配置中心测试、前端 unit/lint/build 和分类门禁通过；
- 目标环境的 PostgreSQL、Redis、NATS executor、Neo4j、MinIO、n8n 和 Chromium CDP 真实健康检查通过；
- 如果宣传 Sentinel/CEP、文件、API Hub、助手、插件或外部 Agent，必须附对应的真实环境验收；
- 如果宣传生产可用，必须附迁移、现存数据库升级、部署、回滚和清理证据。
