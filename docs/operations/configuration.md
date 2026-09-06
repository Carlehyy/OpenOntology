# 配置与秘密管理

## 配置分区

- `.env.example`：本地开发与 Compose 的无秘密模板；
- `config/generated/local/.env`：本地配置中心生成，不进入 Git；
- `backend/.env`：旧本地兼容入口，不应作为新配置源；
- `deploy/production.dependencies.env`：部署时由 Actions 在 runner 上从
  Repository secrets/variables 物化生成，不进入 Git；
- `deploy/production.dependencies.example.env`：不含秘密的人工校验模板。

直接启动应用进程时，系统环境变量仍按框架规则具有最高优先级。本地完整模式由
配置中心生成统一环境文件；生产部署则先保留服务器已有 `.env`，再把部署时在
runner 上物化的 `deploy/production.dependencies.env` 合并进去，并以合并后的
`.env` 作为唯一 Compose 权威。`deploy/deploy-prod.sh` 的所有 Compose 调用都会清除宿主 shell
中同名的环境、端口、依赖、镜像及严格镜像校验变量，避免已验证配置被临时
`export` 静默覆盖。

生产清单只保留运行时实际消费的连接事实：PostgreSQL 以 `DATABASE_URL` 为
连接权威，且服务器最终 `.env` 统一使用 `postgresql://`；部署入口会兼容读取
旧清单中的 `postgres://` 与 `postgresql+psycopg2://`，但在校验和启动前规范化
为 `postgresql://`，不会把别名继续写入运行配置。MinIO 以
`MINIO_ENDPOINT`（S3 API 端口）为连接权威；历史
`POSTGRES_HOST`、`POSTGRES_PORT`、`MINIO_CONSOLE_URL` 字段部署时仅为旧清单
兼容而忽略，不再写入运行环境。n8n 调用超时由 `N8N_TIMEOUT_SECONDS` 控制，
生产模板和 Compose 默认均为 30 秒。

Redis 客户端连接以 `REDIS_URL` 为唯一清单权威。使用随 Compose 启动的 Redis
时填写 `redis://:<password>@redis:6379/0`，部署脚本从同一 URL 推导服务端
`requirepass`；内置密码必须至少 16 位，并只使用字母、数字、`.`、`_`、`~`、
`-`，无需 URL 转义。旧清单中精确的 `redis://redis:6379/0` 会在服务器生成态
安全升级并同时持久化服务密码和带密码 URL。外部 Redis（包括 `rediss://`）的
URL 保持原样；Compose 仍启动的内置 Redis 使用独立本地密码，但 backend 和
worker 不会连接它。

## 应用密钥边界

`SECRET_KEY` 与 `ENCRYPTION_KEY` 不能作为同一个可随意替换的“应用密码”处理：

- `SECRET_KEY` 用于登录 JWT 和短期 Pipeline 文件上传 JWT；
- `ENCRYPTION_KEY` 用于 PostgreSQL 中保存的模型、Connection、n8n、Agent、
  MinIO、浏览器源、分享令牌和 MCP 凭据，以及 API Hub SQLite 中的 W3 密码；
- 用户密码哈希、API Hub proxy key 和设备/分享 token hash 不依赖上述可逆密钥。

只有在维护者已经证明 PostgreSQL、API Hub、Neo4j、MinIO、uploads 等全部持久
存储均为空，并通过手工 `workflow_dispatch` 勾选 fresh-install 确认时，服务器
才由 `deploy/deploy-prod.sh` 分别生成随机 `SECRET_KEY` 和独立 Fernet
`ENCRYPTION_KEY`。普通 push 或未确认的手工部署遇到 `.env` 缺失会 fail closed，
避免“配置文件丢了但数据还在”时生成新的、无法解密旧数据的 authority。
生产部署当前只支持应用目录内权限为 `0600` 的普通 `.env` 文件；有效或失效的
软链接都会在写入前被拒绝，以免一次原子更新悄悄替换 secret mount。若使用外部
秘密管理器，应先由受控发布步骤把完整配置原子落成该普通文件，再启动部署。

历史版本允许 `ENCRYPTION_KEY` 为空，此时实际加密密钥是从
当时生效的 `SECRET_KEY` 派生出来的。部署脚本会区分“没有 `SECRET_KEY` 行”与
“存在 `SECRET_KEY=` 空行”：前者旧程序使用 Settings 默认值，后者会以空字符串
覆盖默认值，两者派生 key 不同。对这些状态或仍为仓库已知示例值的旧安装，部署
脚本会先把这个**原有效加密密钥**固化为显式
`ENCRYPTION_KEY`，再更换 JWT `SECRET_KEY`。该状态转换不更新 PostgreSQL、
API Hub SQLite 或任何业务行，存量密文字节保持不变。

新 backend/worker 使用转换后的配置启动时，旧登录会话和尚未消费的短期上传
令牌会失效，用户需要重新登录；永久
Dataset/File 分享链接仍保留。`FIRST_ADMIN_PASSWORD` 的示例值也会被替换为随机
的未来 seed，但已有管理员的数据库密码哈希不会改变。未知的自定义短
`SECRET_KEY` 不会被猜测或自动迁移，部署会保留原值并明确失败。

不得手工同时生成新的 `SECRET_KEY` 和 `ENCRYPTION_KEY`，不得先更换
`SECRET_KEY` 后再补 `ENCRYPTION_KEY`，也不得为通过校验而清空密文。由公开示例
值派生的历史加密密钥虽然经显式固化后能完整保留数据，但保密强度没有因此
提高；真正轮换 `ENCRYPTION_KEY` 必须作为独立维护变更，在 PostgreSQL 与 API
Hub SQLite 完整备份、停写、全量解密校验和可恢复迁移机制就绪后执行。

## 必需运行依赖

| 依赖 | 运行责任 | 未就绪行为 |
|---|---|---|
| PostgreSQL | 平台主数据库、Alembic、发布与审计事实 | 启动/readiness 失败；不切平台 SQLite |
| Redis + NATS executor | Redis 缓存与平台配置存储；NATS 承担 durable 入队、异步调度和后台执行 | readiness/入队失败；异步路径不自动改在 API 线程执行，显式同步 API 保持原契约 |
| NATS + pipeline_executor | 流水线调度任务（定时触发与手动异步触发）、UI 手动运行整条流水线、数据集导入解析/提交的派发与执行 | 未配置 `NATS_URL` 时派发方报错、手动异步触发与数据集导入返回 503；executor 不健康时部署失败 |
| Neo4j | 图查询、分析和可重建发布投影 | 图接口返回明确 503，发布/投影 fail closed；不切 NetworkX/SQL 图 |
| MinIO | 新文件与对象的权威对象存储 | 写入失败；不切本地对象目录 |
| n8n | 数据管家工作流执行 | 启动/readiness 失败，不按可选增强跳过 |
| Chromium CDP | 浏览器接管与数据管家浏览器会话 | `STEWARD_BROWSER_CDP_URL` 只接受 HTTP(S) 服务根地址；启动配置必需，不可达不杀死 API liveness，但 readiness 失败，不用 mock 冒充就绪 |

外部依赖必须在本地配置中心或生产只读校验中提供完整配置，并以真实服务探针
验证；API 与 pipeline executor 启动后还必须通过 `/health/ready`。
`/health/live` 只证明 API 进程存活，不能替代上述检查。

ChromaDB 已从依赖、配置模型和运行链路移除。旧 Chroma 配置不应继续复制到新
环境；关键词搜索使用 PostgreSQL，语义搜索和统一 semantic 模式返回 501。
LLM 不参与启动门禁：平台就绪后，管理员在“模型配置”页面按需添加提供商、
模型和凭据。

n8n 在非测试环境中只有一个配置权威：启动环境中的 `N8N_API_URL`、
`N8N_API_KEY` 与 `N8N_TIMEOUT_SECONDS`（本地由配置中心生成）。启动同步仅将
这组值镜像到历史数据库记录，运行时客户端只使用启动值；系统设置不再提供
工作流配置页或 `workflow-config` 接口，连通性由 `/health/ready` 实时探测。
修改后必须重启 API 与 worker，使所有进程同时切换到同一配置。只有
`ENVIRONMENT=test` 可注入并持久化隔离 n8n 配置，以支持确定性测试。
`N8N_API_URL` 的新模板统一填写 n8n 服务根地址（例如
`https://n8n.example.com`）；运行时会规范化为 `/api/v1`。历史清单中已经带
`/api/v1` 的地址继续兼容，不要求为本次升级改写。

n8n 工作流调用接口管理中的接口走平台内部代理
`POST /api-hub/internal/interfaces/{id}/invoke`（Bearer
`API_HUB_INTERNAL_PROXY_TOKEN`，未配置时回退历史名 `API_HUB_SYSTEM_MCP_TOKEN`；
参数按接口参数契约校验并钉定 `interface_revision`）。数据管家编排接口时把
该地址写进 n8n HTTP 节点，地址取自 `STEWARD_INTERNAL_PROXY_BASE_URL`：默认是
compose 内网地址 `http://backend:8000/api-hub/internal/interfaces`，仅供 n8n 与
后端同网部署时可用；n8n 为外部实例时必须在服务器 `.env` 中覆盖为平台公网
origin（例如 `https://<平台域名>/api-hub/internal/interfaces`，Nginx 已代理该
前缀）。修改后重启 backend 与 pipeline_executor。

非测试 API 的 lifespan 会先完成 PostgreSQL、Redis、Neo4j、MinIO、n8n 的
真实连接探测，再初始化 Neo4j 索引、修复非 ready 本体投影，最后才启动 API
Hub、Sentinel 和数据调度器。任一阻塞型依赖或投影修复失败都不会
留下后台 worker。Chromium CDP 同样会在此时探测，但只记录提示并允许 API
进程提供诊断；`/health/ready` 在 CDP 恢复前仍返回 503。

数据调度器只在 API 进程内做「派发」：流水线定时触发与手动异步触发、UI
手动运行整条流水线、数据集导入的解析与提交，都经 NATS JetStream 送达到
独立 `pipeline_executor` 进程执行，不再占用 API 进程
的 GIL、内存与数据库连接；手动 `sync=true` 保持 API 进程内同步执行。NATS
只负责送达，并发正确性由执行引擎的数据库原子租约兜底，数据集导入由
导入任务目录内的文件状态机兜底。executor 被打断
（部署、宕机）时，在途任务表现为执行中断：其数据库租约最长 6 小时过期，
API 进程内的对账器（默认每 5 分钟，
`PIPELINE_RUN_RECONCILE_INTERVAL_SECONDS`）随后把过期中断的任务与运行
记录收口为 failed。executor 并发度由 `PIPELINE_EXECUTOR_CONCURRENCY`
（默认 2）控制。

### 数据任务池：源端增量游标（水位）

数据任务可选声明「增量游标列」（`cursor_column`，必须是流水线发布契约
中的列；词法可比较：ISO8601 时间戳、零填充序号或自增 ID）。声明后每次
运行把上次成功水位经引擎运行参数注入源端——Python 脚本读取脚本上下文
变量 `OB_RUN_PARAMS`（含 `cursor_column` / `cursor_since` /
`full_refresh`），n8n 工作流读取 webhook body 的
`incremental.cursor_since`——由流水线自行按键过滤只产出新数据；平台在
运行成功时把当次产出的游标词法最大值回写为下次起点
（`last_cursor_value`，仅成功推进；空产出/失败/被对账器收口均不推进）。
首次运行或水位为空时按全量拉取并建立水位。漏数回填入口：任务池「全量
回填」（`POST /pipeline-tasks/{id}/trigger?full_refresh=true`）——当次
忽略水位全量拉取，成功后水位推进到当次最大值。修改/清空游标列会自动
归零水位（旧水位对新列不可比较）。游标列词法序的口径约定：时间一律
ISO8601 定长格式，数字 ID 需零填充或用字典序可比较的编码。
CDP 配置只填写服务根地址（例如 `http://browser:9222`）；平台会自行请求
`/json/version`。带该 discovery 路径、查询参数、fragment 或 URL 用户信息的
地址会在配置阶段被拒绝，避免配置校验通过后实际探测到重复路径。

明确例外只有：API Hub 自有 SQLite；`ENVIRONMENT=test` 的隔离 SQLite 与 n8n
配置注入；历史 `local://` 对象的只读读取/迁移。新平台数据不写入这些兼容路径。

平台只有一套 MinIO 配置，即部署环境 `MINIO_*`。早期开发模式曾把平台对象
写到“系统设置”里手动保存的 MinIO 端点，该手动配置（页面、`/api/v1/settings/
minio*` 接口、`minio_config` 表、外部 `/mcp/minio` 端点）已整体移除，回归期
对象的 legacy 读取适配器也一并退役。超级助手内置的 MinIO MCP 继续使用同一个
环境 MinIO，但所有工具被服务端锁定到 `MINIO_MCP_BUCKET`（默认
`assistant-workspace`）专用桶，无法接触平台数据桶；删除/移动类操作默认关闭，
需要时由部署方设置 `MINIO_MCP_ALLOW_DELETE=true` 放开。

## Python 脚本流水线执行网关（可选）

Python 脚本流水线（`definition.engine=python`）由 Jupyter Kernel Gateway
执行用户脚本。三个环境变量：`PYTHON_KERNEL_GATEWAY_URL`（服务地址）、
`PYTHON_KERNEL_GATEWAY_AUTH_TOKEN`（内网调用令牌）、
`PYTHON_SCRIPT_TIMEOUT_SECONDS`（单次执行上限，默认 120 秒）。该依赖是
**可选**的：不参与启动探测与 readiness；未配置或不可达时，脚本执行/保存/
试运行返回明确错误，其余平台能力不受影响。

Compose 部署固定使用随栈启动的 `python_kernel_gateway` 服务。该服务以独立
内核执行用户脚本，因此**不得**给它挂 `env_file` 或注入任何平台凭据环境变量
（`DATABASE_URL`、`SECRET_KEY`、MinIO/n8n 凭据等对用户脚本可读），也不暴露
宿主机端口，仅 backend 经 Compose 内网访问。内核环境继承
backend 镜像依赖（requests/httpx/pandas/pymysql 等），脚本可直接发起 HTTP
请求取数；新增第三方库须加入 backend 依赖并重建镜像。

## 插件社区 stdio MCP（可选，默认关闭）

stdio 形态的 MCP 会在后端容器内以子进程运行 `command`，等同开放容器内命令
执行，因此按部署开关管理：`SUPER_ASSISTANT_MCP_STDIO_ENABLED`（默认
`false`）与 `SUPER_ASSISTANT_MCP_STDIO_ALLOWED_COMMANDS`（逗号分隔的命令
白名单，按可执行文件名匹配）。backend 镜像自带 `uvx` 并已内置
node/npm/npx，覆盖 PyPI 与 npm 两大主流 MCP 分发形态；MCP SDK 启动子进程时
只传递 PATH/HOME 等安全环境变量子集，平台密钥不会继承给子进程。经
`deploy/production.dependencies.env` 或服务器 `.env` 配置后，随下一次部署
生效；关闭开关或清空白名单即可随时回退。

## 当前自动部署配置源

生产依赖清单已随开源前置变更迁出 Git：`verify-gates` 与 `deploy` 两个作业
都调用 `scripts/ci/materialize-production-dependencies.sh`，从「GitHub 部署
传输参数」一节列出的 `PROD_*` Repository secrets/variables 原子物化权限
`0600` 的 `deploy/production.dependencies.env`；后续校验、归档、上传与服务
器合并逻辑与 tracked 时代完全一致，服务器 `.env` 仍是唯一运行时权威。
`scripts/ci/test-deploy-guards.sh` 与
`scripts/ci/check-repository-hygiene.sh` 锁定两条契约事实：清单不得被跟踪、
两个作业都必须物化。清单在 Git 历史中的旧值清理作为独立运维变更另行执行。

## GitHub 部署传输参数

部署传输参数由工作流直接读取：

| GitHub Secret | 是否必填 | 工作流行为 |
|---|---:|---|
| `DEPLOY_HOST` | 是 | SSH/SCP 目标；空值立即失败 |
| `DEPLOY_PASSWORD` | 是 | 当前密码式 SSH 凭据；空值立即失败 |
| `DEPLOY_USER` | 否 | 为空时使用 `root` |
| `DEPLOY_APP_DIR` | 否 | 映射为远端 `APP_DIR`，默认 `/opt/openontology` |
| `DEPLOY_HEALTH_URL` | 否 | 映射为 `HEALTH_URL`；为空时按 `PUBLIC_PORT` 生成 |

`DEPLOY_HEALTH_URL` 未设置时，`PUBLIC_PORT=80` 使用
`http://${DEPLOY_HOST}/`，其他端口使用
`http://${DEPLOY_HOST}:${PUBLIC_PORT}/`。这里的 `${...}` 表示工作流运行时
取值，不是要求把字面量保存为 Secret。生产 `PROD_PUBLIC_PORT` 当前为
`8088`，本轮迁移不改变现有公网端口。
显式 `DEPLOY_HEALTH_URL` 必须是无空白、无原始单引号的 HTTP(S) URL；不合法值
会在发起 SSH 前失败。

生产依赖 `PROD_*` 配置由 verify 与 deploy 作业在 runner 上物化清单时读取：
Secrets 为 `PROD_DATABASE_URL`、`PROD_REDIS_URL`、`PROD_POSTGRES_PASSWORD`、
`PROD_NEO4J_URI`、`PROD_NEO4J_PASSWORD`、`PROD_NEO4J_AUTH`、
`PROD_MINIO_ENDPOINT`、`PROD_MINIO_ACCESS_KEY`、`PROD_MINIO_SECRET_KEY`、
`PROD_N8N_API_URL`、`PROD_N8N_API_KEY`；Variables 为 `PROD_POSTGRES_DB`、
`PROD_POSTGRES_USER`、`PROD_NEO4J_USER`、`PROD_PUBLIC_PORT`、
`PROD_MINIO_USE_SSL`。任一必需项缺失时物化脚本立即失败，不会生成部分清单，
`N8N_TIMEOUT_SECONDS` 未配置时默认 30 秒。

`DEPLOY_APP_DIR` 为空时使用 `/opt/openontology`。自定义值必须是规范化的
绝对路径，并位于某个顶层目录之下；根目录、顶层目录本身、`.`/`..` 段、重复
或末尾斜杠，以及空格、引号、控制字符和 shell 标点都会在任何 SSH 删除/解包
命令前被拒绝。远端最终路径还必须是真实目录而非软链接，避免源码替换跟随链接
写入另一个目录。

品牌由 OntologyBuild 更名为 OpenOntology 时，默认部署目录随之从
`/opt/ontologybuild` 改为 `/opt/openontology`。存量服务器上已存在的
`/opt/ontologybuild` 部署（含其持久 `.env`）不会自动迁移：这类环境必须在
GitHub Secret 中显式设置 `DEPLOY_APP_DIR=/opt/ontologybuild`，或一次性把
目录迁移到 `/opt/openontology` 后再解除该 Secret。

## 首个管理员配置

`.env.example` 中的 `FIRST_ADMIN_USER=admin` 和示例密码只用于本地模板。首次
安装在确认全部持久存储为空后，必须从 Actions 手工运行 workflow 并勾选
`bootstrap_production_env`；脚本随后保留默认用户名 `admin`，为
`FIRST_ADMIN_PASSWORD` 生成随机值，将 `.env` 设为 `0600`，且不会把值打印到
Actions 日志。普通 push 不具有创建新加密 authority 的权限，后续部署会保留
服务器已有 `.env`。

`FIRST_ADMIN_PASSWORD` 只在数据库没有任何 admin 时用于 seed。管理员一旦创建，
数据库中的密码哈希就是登录事实源；只编辑 `.env` 不会轮换现存账号密码。
初次读取、立即轮换和账号恢复步骤见
[部署手册](./deployment.md#首次登录与管理员密码恢复)。

镜像变量来自服务器最终合并后的 `.env`。其中
`STRICT_IMAGE_DIGESTS=true` 会要求全部镜像使用 `@sha256`，合法值为
`true/false`、`yes/no` 或 `1/0`。部署入口不接受宿主 shell 中的同名变量覆盖
`.env`；如需改变策略，必须先修改持久配置并重新执行完整校验。

## 运行监控（API 性能监控）

平台后端自带接口性能观测（系统设置 → 运行监控，仅 admin 可见）：纯 ASGI
请求计时中间件把每个 HTTP 请求按「分钟 × 方法 × 路由模板 × 状态类别」
聚合进 PostgreSQL（api_perf_minute_rollups），超过慢阈值的请求同时以单条
证据行落库（api_perf_slow_requests，含 request_id、用户名、来源 IP、
User-Agent 与调用链）。聚合由进程内内存缓冲每
API_PERF_FLUSH_INTERVAL_SECONDS 批量写入；进程崩溃最多丢失最后几秒聚合，
慢请求证据不丢。/health*、/api/health、SSE 流式、
WebSocket、OPTIONS 与监控查询接口自身不参与统计。

慢请求的调用链（breakdown 列 JSON 的 spans 数组）记录请求内部各步骤的
有序时间轴：每条 span 含层级（db/llm/http）、操作名、目标（表名/模型/
下游 URL，查询参数已脱敏）、相对请求开始的时间偏移、耗时、状态与明细。
db 层由 SQLAlchemy 引擎事件自动捕获，明细为单行化、截断到 400 字符的
SQL 语句文本；llm 层覆盖模型网关、超级助手与本体抽取三条 LLM 路径；
http 层覆盖 Python 执行引擎、n8n、MCP、网页搜索、数据通道 REST 连接与
API Hub 出站代理等下游调用。慢请求明细表中点击「调用链」可查看瀑布时间
轴与步骤明细；单条调用链超过 128KB 时按耗时保留最慢步骤并标记
spans_truncated，旧版本记录的慢请求无 spans 数组仍可正常展示。

- API_PERF_ENABLED（默认 true）：整体开关；出现问题时置 false 可完全
  关闭采集（请求路径零额外开销）；
- API_PERF_SLOW_THRESHOLD_MS（默认 1000）：慢接口判定阈值；
- API_PERF_FLUSH_INTERVAL_SECONDS（默认 30）：分钟聚合批量落库周期；
- API_PERF_SLOW_RETENTION_DAYS（默认 7）：慢请求明细（含调用链）保留天数；
- API_PERF_AGG_RETENTION_DAYS（默认 30）：分钟聚合保留天数（后台维护
  循环按日清理过期行）。

响应头 X-Request-ID 与日志记录中的 request_id 一一对应，可用于在应用日志
中按 id 还原单次请求的执行细节。

## 安全要求

- 不在 PR、Issue、日志或迭代文档中粘贴真实值；
- 物化生成的生产依赖清单只存在于 runner 与服务器上，不得复制到其他文件或回显；
- 新增或轮换凭据时应优先规划逐项 Secret，使其可独立轮换和撤销；
- 生产依赖变化必须同时更新部署验证和本说明；
- 内置 Redis 的 `REDIS_URL` 与 `requirepass` 必须由部署脚本一次性规范化，
  禁止在服务器 `.env` 中手工维护两份不一致的密码；
- 不得用测试 SQLite、API Hub SQLite 或历史 `local://` 兼容路径绕过必需依赖；
- 新配置源迁移已完成并经部署验证；清单在 Git 历史中的旧值清理作为独立运维
  变更执行，删除工作树文件不会清理 Git 历史。
