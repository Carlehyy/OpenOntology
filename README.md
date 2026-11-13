# OpenOntology

OpenOntology 是一个“本体即服务（Ontology-as-a-Service）”平台：把企业数据
接入、清洗并映射为对象、关系、动作与规则，再由 Sentinel Engine 监听状态
变化、评估条件并执行受治理的动作。

![平台能力总览](./docs/images/platform-capabilities.png)

## 平台能力

- 本体管理与治理：对象、关系、动作与规则建模，版本按
  Draft → Trial → Impact → Promote 门控发布；
- 数据通道与数据管家：版本化数据集、流水线发布与成品审核；
- 业务探索、超级助手与本体助手：对话式业务建模与受治理的本体问答；
- 事件登记：独立的事件登记与查询能力；
- 工单反馈：全角色可提交的使用反馈工单（附件 + 管理员四态处理流水线）；
- API Hub：接口代理与对外发布（Plugin 社区已接通，Skill 社区维护中）；
- Sentinel：监听状态变化、评估条件并执行受治理动作；支持事件模式（CEP）——时间窗对齐（changed_within/prev）、阶段序列与缺失分支、窗口聚合；
- 模型配置与系统治理：LLM 不是启动依赖，启动后由管理员按需接入提供商。

## 快速开始

### 完整本地栈（推荐）

```bash
git clone --branch nano-ontoprompt https://github.com/Carlehyy/OpenOntology.git
cd OpenOntology
cp .env.example .env
# 填写并验证外部 n8n 地址与凭据后再启动
docker compose -f docker-compose.local.yml up --build
```

启动后访问 `http://localhost:5173`，本地示例账号 `admin / admin123`（仅用于
本地开发）。后端存活检查位于 `http://localhost:8000/health/live`；完整平台
可用性还必须通过 `/health/ready` 检查。

WeKnora 原生 Web 入口是 `http://localhost:8089`（HTTPS `https://localhost:8444`）。
它使用独立数据库、缓存和文件卷。本地 `up` 自动初始化管理员并关闭注册后开放
原生 Web；模型、知识库和 MCP 端点由管理员在原生页面配置，再在超级助手现有
MCP 管理中登记一次连接。模型密钥不是部署前置。细节见
[WeKnora 本地初始化](./docs/development/setup.md#weknora-本地初始化)。

Kaneo 项目管理（官方 2.49.0，独立单用户账号）随同一套 Compose 启动：超级助手
会话头部「知识库」入口左侧的「项目管理」新标签打开原生 Web，本地
`http://localhost:8090` 仅 308 跳转 `https://localhost:8445`（自签证书，首访
需信任）。它使用独立 PostgreSQL，是平台 fail-closed 强依赖；公开注册、访客
与邮件验证登录全程关闭。首版未接 S3/SMTP，附件上传与邮件找回密码不可用。
细节见 [Kaneo 本地初始化](./docs/development/setup.md#kaneo-本地初始化)。

超级助手长期记忆是可选服务：本地 `docker compose --profile hindsight up -d`
启用（loopback `http://127.0.0.1:18888`），不启用或故障时仅记忆特性降级，
聊天不受影响。细节见
[Hindsight 长期记忆](./docs/development/setup.md#hindsight-长期记忆生产默认启用本地手动开启)。

三个编排文件分工如下：`docker-compose.local.yml` 是单机一键完整栈（即上文
流程，`up --build` 会自动执行 Alembic 迁移并按 `.env` 播种首个管理员）；
`docker-compose.e2e.yml` 仅为真实后端 E2E 提供端口错开的基础设施，不含应用
服务；`docker-compose.prod.yml` 是生产编排入口，按
[部署说明](./docs/operations/deployment.md) 与 GitHub Actions 工作流执行，
须由 `deploy/deploy-prod.sh` 完成迁移、`.env` 密钥与镜像门禁——裸
`docker compose up` 无法启动，且生产模式会拒绝 `admin123` 这类弱初始密码
（首次部署密码由 bootstrap 随机生成）。不要用本地编排推断生产依赖已经验证。

### 源码开发

前置要求：Python 3.12、[uv](https://docs.astral.sh/uv/)、Node.js 22、npm，以及
已经启动的 PostgreSQL、Redis、NATS、Neo4j、MinIO 和 n8n。Chromium CDP 地址必须
配置；它暂时不可达时仍可启动 API 用于诊断，但深度 readiness 保持失败。先运行
本地配置中心并通过所有阻塞型外部依赖连通性检查：

```bash
./config/start.sh
```

Windows 使用 `config/start.bat`。配置通过后，安装依赖，并在三个终端分别启动后端、后台任务 executor 和前端：

```bash
uv sync --directory backend --frozen --group dev
npm --prefix frontend ci

# 终端 1：后端（dev_server 会先执行 alembic upgrade head）
uv run --directory backend python -m app.dev_server

# 终端 2：后台任务 executor，30 秒心跳供“启动后复检”使用
uv run --directory backend python -m app.data_channel.pipeline_tasks.nats_executor

# 终端 3：前端
npm --prefix frontend run dev
```

配置中心生成的 `config/generated/local/.env` 不进入 Git。启动后从“模型配置”
页面按需配置 LLM。完整依赖、端口和例外边界见
[开发环境](./docs/development/setup.md) 与
[config/README.md](./config/README.md)。

## 核心运行机制

数据不是一条可以跳过状态门的直线：

```text
数据生产（版本化数据集 → 流水线发布 → 成品审核）
  → 定义发布（Draft → Trial → Impact → Promote）
  → 数据刷新（Approved Version → Mapping → Formal → 查询投影 → Sentinel → Action）
```

平台采用 fail-closed 依赖契约：PostgreSQL、Redis、NATS 与
流水线 executor、Neo4j、MinIO、n8n、WeKnora 共享知识库和 Kaneo 项目管理
必须真实就绪，
后端不会在依赖失败时静默切换到 SQLite、内存图或本地对象存储。语义搜索当前明确返回 `501 semantic_search_unsupported`，关键词
搜索由 PostgreSQL 提供。事件登记是独立业务能力，当前没有可核验的
RegisteredEvent → Formal/Sentinel 自动接线。状态门与发布契约的实现以
`backend/app/data_channel/`、`backend/app/ontologies/` 及对应测试为准。

## 技术栈

- 后端：FastAPI、Python 3.12、SQLAlchemy、Alembic、NATS 任务链；
- 前端：React、TypeScript、Vite、Tailwind CSS；
- 数据与中间件：PostgreSQL、Redis、Neo4j、MinIO；WeKnora 自带独立
  ParadeDB/Redis（图谱增强默认关闭，不运行独立 Neo4j）；Kaneo 项目管理
  自带独立 PostgreSQL；
- 工作流与浏览器：n8n、Chromium CDP；Python 脚本流水线执行：Jupyter Kernel Gateway（可选）；
- 交付：Docker Compose、GitHub Actions。

## 开发指南

开始开发前请先阅读 [AGENTS.md](./AGENTS.md)（仓库级强制准则，含分支与 PR
规则）。所有文档入口和事实源见 [docs/README.md](./docs/README.md)。

### 日常开发流程

1. 本地基于 `nano-ontoprompt` 分支创建新的 worktree 分支，并行开发若干功能，
   每个功能在独立 worktree 中进行；
2. 功能开发完成后，先合入本地 `nano-ontoprompt` 分支，有冲突就解决冲突；
3. 推送至 GitHub 远程 `nano-ontoprompt` 分支，触发 GitHub Actions 验证并
   自动部署到目标云服务器；
4. Actions 自动部署成功后，通过云服务器访问环境，查看最新功能效果；
5. 每次推送后，还需在另一台电脑手动拉取最新代码：先在 `config/` 配置中心
   填写配置信息并通过连通性检查，再按上文「源码开发」分别启动后端与前端。

### 三分钟定位

| 想了解 | 从这里开始 |
|---|---|
| 本地如何启动 | [开发环境](./docs/development/setup.md) |
| 改动后必须跑哪些测试 | [AGENTS.md](./AGENTS.md) 与 [测试指南](./docs/development/testing.md) |
| 配置和秘密放在哪里 | [配置说明](./docs/operations/configuration.md) |
| GitHub Actions 如何部署、如何回滚 | [部署](./docs/operations/deployment.md) 与 [回滚](./docs/operations/rollback.md) |
| 功能的前端、后端和测试在哪里 | 代码即文档：从 `frontend/src/config/navigation.ts` 的导航项出发，对照 [AGENTS.md](./AGENTS.md) 第 1 节的业务域表定位后端包 |
| 最近迭代了什么 | Git 提交历史（`git log` 或 GitHub 提交页） |

### 仓库结构

```text
OpenOntology/
├── backend/                 FastAPI、领域模块、Alembic 和后端测试
├── frontend/                React 应用、纯逻辑单元测试与 Playwright 旅程
├── config/                  独立的本地配置中心
├── docs/
│   ├── development/         本地环境搭建与测试门禁
│   └── operations/          配置、部署、回滚、备份和排障
├── scripts/                 CI 与受控数据脚本
├── deploy/                  生产部署脚本与依赖清单（含受控临时例外的真实凭据）
├── agentloop/               阿里云 AgentLoop 观测接入（探针镜像与 compose 覆盖层）
├── docker/                  容器初始化与运行资源
├── test_data/               受版本控制、已分类的测试 fixture
├── uploads/                 运行期上传与过程产物输出（不入 Git）
├── .github/workflows/       PR 验证与自动部署
├── .env.example             本地/容器环境变量模板
├── AGENTS.md                强制开发和交付准则
├── docker-compose.local.yml  推荐的本地核心完整栈
└── docker-compose.prod.yml   生产编排
```

后端已处于从传统横向目录向业务域迁移的中间状态。新同事应按
[AGENTS.md](./AGENTS.md) 第 1 节的业务域表和兼容层例外台账找到当前权威实现，
不要仅凭目录名称猜测。前端平台概览已迁入 `src/features/overview/`，其他业务
仍以 `pages/`、`api/`、`components/` 等现有路径为主；不得把目标结构误写成
全仓已经完成的事实。

### 标准验证

```bash
git diff --check
node scripts/ci/check-markdown-links.mjs
bash scripts/ci/check-repository-hygiene.sh

uv sync --directory backend --frozen --group dev
uv run --directory backend pytest -q --disable-warnings --ignore tests/v2/perf

uv sync --directory config --frozen --group dev
uv run --directory config pytest -q

npm --prefix frontend ci
npm --prefix frontend run test:unit
npm --prefix frontend run check:feature-boundaries
npm --prefix frontend run test:e2e:classification
npm --prefix frontend run lint
npm --prefix frontend run build
npm --prefix frontend run test:e2e:mocked
```

完整强制门禁矩阵以 [AGENTS.md](./AGENTS.md) 第 5 节为准。

真实后端、浏览器接管、对象存储、n8n、LLM 和生产部署变更还需要隔离环境
验收；不能用离线 mock 结果替代。详细矩阵见
[测试指南](./docs/development/testing.md)。

## 部署与安全

推送到 `nano-ontoprompt` 分支会触发验证与自动部署，流程与回滚见
[部署说明](./docs/operations/deployment.md) 和
[回滚](./docs/operations/rollback.md)。

生产第三方依赖清单 `deploy/production.dependencies.env` 不进入 Git：每次部署
由 GitHub Actions 在 runner 上从 Repository secrets/variables（`PROD_*`）经
`scripts/ci/materialize-production-dependencies.sh` 物化生成，再合并进服务器
`.env` 随 `deploy/deploy-prod.sh` 消费，任何人不得修改、复制或回显其中的真实
值。凭据与卫生红线见 [AGENTS.md](./AGENTS.md) 第 6 节，配置分区与密钥边界见
[配置与秘密管理](./docs/operations/configuration.md)。
