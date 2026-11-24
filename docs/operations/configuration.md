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

## 本体助手的外部 MCP 接入

在本体助手选择已发布本体后，选择框左侧的 MCP 入口提供标准 `mcpServers` 连接
配置模板，可接入任何支持 Streamable HTTP 与自定义请求头的 MCP 客户端
（Cursor、Claude、pi 等）。连接地址为
`/api/v2/formal/ontologies/{ontology_id}/mcp`；连接固定本体 ID，每次读取该本体
的当前发布版本。旧的 `/mcp` 与 `/api-hub/mcp` 继续返回 404，不能用作本体查询
地址。

外部客户端通过 `X-OpenOntology-Username`、`X-OpenOntology-Password` 请求头
填写平台账号密码。服务端逐请求校验密码、账号启用状态、本体助手菜单权限与
本体的助手授权边界；不签发专用凭据。改密后需要更新外部配置，账号停用或权限
收回后旧配置不再可用。远程接入使用 HTTPS；不要把密码放入 URL、对话、日志或
仓库中的项目配置。

以 pi（首批实际验证客户端）为例：配置保存在用户级 `~/.pi/agent/mcp.json`，
从弹窗复制配置后替换密码占位符，保留 `exposure: "direct"`；首次运行
`pi mcp list --json` 检查连接与工具清单。pi 会解释请求头值里的环境变量及
命令：字面量 `$` 写成 `$$`，值开头的 `!` 写成 `$!`，避免把密码当作变量或
命令执行（弹窗模板已对预填用户名自动转义）。网页不会收集或保存该密码。
本批接入验证覆盖可打印 ASCII 账号密码；HTTP 请求头不能直接传递含中文等
完整 Unicode 凭据。服务端不做 URL 解码，不能用百分号编码替代原密码。

部署时将平台公开 origin 写入现有 `CORS_ALLOWED_ORIGINS`，例如
`https://ontology.example.com`；多个 origin 用逗号分隔。MCP 使用其中的 hostname
校验 Host，并对带 Origin 的请求校验完整 origin。通配 `*` 只放行回环来源，
不代表允许任意公网 MCP 域名。Nginx 须保留原 Host 和上述认证请求头。

服务只开放 `ontology_schema`、`ontology_query`、`ontology_query_page`：先读取
按助手授权边界整理的对象、属性、关系与业务说明，再由外部 Agent 组织结构化
查询。平台不调用对话模型，不提供动作执行、决策推演或报告生成工具，也不导出
含动作、函数、哨兵的完整本体包。查询可能创建有期限的分页快照，但不修改业务
对象。

成功响应携带实际使用的 `ontologyId`、`releaseId`、`versionNumber` 和时间字段。
结构读取后若发布版本或授权范围变化，旧查询或游标会被拒绝；调用方应重新读取
`ontology_schema` 再组织查询，不应把不同版本的分页结果拼接。更新发布版本无需
重新复制连接配置。

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
| 集成数据集 dbx | 集成源登记、schema 发现与刷新；生产 metadata 只走固定版本 dbx，不回退 native | 生产部署自动启动仅内网可达的官方 digest dbx，或使用显式外部端点；缺失/不可达/认证失败/版本不符使部署 preflight 与生产 readiness fail closed，非生产不要求 dbx |
| WeKnora v0.9.0 | 原生知识库 Web 与超级助手托管只读 MCP 知识来源 | 服务或管理员初始化失败时阻断启动；未配置模型、知识库或 MCP 不影响基础服务启动 |
| Kaneo 2.49.0 | 单用户项目管理原生 Web（独立账号与独立 PostgreSQL） | 服务、管理员初始化或实例状态探针失败时启动/readiness fail-closed；平台不降级、不用 mock 冒充 |
| Hindsight 1.2.0（可选） | 超级助手长期记忆（profile `hindsight`，默认关闭） | 可选特性：不可达/未启用时记忆特性降级，聊天与全局 readiness 不受影响；不回退旧记忆实现 |
| Multica 默认服务（默认启用） | 超级助手的部署级默认 Multica 集成（profile `multica` 默认启动；官方 v0.11.0 三服务栈 digest 钉扎） | 默认启用的 bundled 栈：启动失败只降级该特性（五值默认集未配置时）；五值默认集配置完整后生产 readiness 门禁 `multica_default`，部分配置 fail-closed；每用户自配 multica 集成不受影响 |

### WeKnora 共享知识库（v0.9.0）

WeKnora 使用官方 app、UI、docreader 和独立 ParadeDB、Redis、文件卷。
图谱增强默认关闭，不运行额外 Neo4j，也不共用平台业务图数据。原生 Web
使用独立根路径及端口（HTTP 8089、HTTPS 8444）。正式 app 仅在 Compose 内网的
`http://weknora-app:8080` 提供 API/MCP，公网网关对精确 `/mcp` 与全部 `/mcp/`
子路径返回 404。

一键免登录（共享管理员会话，与 Kaneo 中继同构）：在超级助手聊天区域右键
菜单选择「领域知识」时，平台后端为已登录且有超级助手菜单权限的用户签发
一次性票据（HMAC 签名、90 秒有效、Redis 单次消费），新标签打开
`WEKNORA_WEB_URL/sso/entry?ticket=...`；
`weknora-gateway` 把该精确路径反代到 backend，backend 校验票据后以管理员凭据
服务端登录 WeKnora，并把登录响应经 WeKnora 原生 `#oidc_result=` 管道在其自身
origin 落盘（纯 Bearer token 模型，无 cookie）。全程用户无需知道 WeKnora 账号
密码；所有有权限的平台用户共享同一个 WeKnora 管理员身份。任何失败（票据无效/
重放、Redis 不可用、WeKnora 拒绝登录）都回落到 WeKnora 自带登录页；直接打开
原生地址手动登录仍是兜底。管理员凭据除
`weknora-bootstrap` 外只进入 backend 容器（compose 掩码锚点下的唯一例外；
pipeline executor 等运行时容器被掩码，托管工具调用只使用端点自身 token），
不进日志、接口响应与前端。

平台只使用 `WEKNORA_BASE_URL`、`WEKNORA_WEB_URL` 和
`WEKNORA_TIMEOUT_SECONDS` 做服务探活、页面跳转与端点目录访问（backend 另持
`WEKNORA_ADMIN_EMAIL`/`WEKNORA_ADMIN_PASSWORD` 供上述中继登录，以及
「助手配置 → MCP」中 WeKnora 端点目录/初始化/轮换的私网管理 API 调用）。
前端知识库入口与免登录跳转地址在 `WEKNORA_WEB_URL` 为主机回环地址（localhost/127.0.0.1
等）时，自动改用浏览器当前访问平台的主机名（协议与端口保留），内网与公网
部署未显式配置时同样跳到同机网关；非回环地址按配置原样跳转。标准 MCP 的
内部访问范围由
`SUPER_ASSISTANT_MCP_INTERNAL_BASE_URLS` 指定（Docker 固定
`http://weknora-app:8080/mcp`；源码模式由配置中心生成为
`http://127.0.0.1:18080/mcp`）。只允许精确同源、路径在前缀内的请求；其它 MCP
继续执行现有公网/HTTPS 校验。不要扩大为任意内网地址。平台托管的 WeKnora
连接固定 Streamable HTTP，地址是 `WEKNORA_BASE_URL` origin 下的
`/mcp/<endpoint_id>`（须落在上述内部前缀内）；端点 token 用平台
`ENCRYPTION_KEY` 加密保存在超级助手 MCP 连接行，不进入任何部署环境变量。

首次部署仅自动初始化账号：私网 `weknora-app-init` 临时允许注册，bootstrap
注册管理员后删除该容器；正式 app 启动时将指定邮箱提升为系统管理员，
`bootstrap --verify` 登录并持久化 `auth.registration_mode=invite_only` 后才开放
公网网关。此流程不创建模型、知识库或 MCP 端点，不调用大模型，不维护集成状态文件。
服务故障仍阻断启动；尚未配置知识能力属于正常首次使用状态。端点发现、默认
端点托管与接入由管理员在平台「助手配置 → MCP」的 WeKnora 区块完成（见下方
流程），不属于部署步骤。

`WEKNORA_ADMIN_EMAIL` 默认为 `admin@weknora.local`；生产管理员密码由部署脚本
首次生成并持久化于服务器 `.env`。由授权管理员在服务器受控终端查看，禁止发送到
应用页面、日志、Git 或工单。原生页面修改密码后，应在受控终端同步该账号的部署
密码，保证下一次账号安全验证仍可登录；不会自动重置已有账号密码。
DB、Redis、JWT、AES 与 signing 凭据独立生成并跨部署保持不变。AES 必须恰为
32 个 ASCII 字节，JWT/signing 至少 32 字符；不复用平台密钥。
官方 app/UI/docreader、ParadeDB、Redis 镜像均固定 `@sha256`。

管理员首次配置流程：

1. 在超级助手聊天区域右键菜单点击「领域知识」一键免登录进入 WeKnora（中继
   失败时回落 WeKnora 自带登录页，直接打开原生地址手动登录仍是兜底）。
2. 在“设置 → 模型”添加聊天（KnowledgeQA）和 Embedding 模型，在原生页面输入
   模型地址、密钥、名称及向量维度，并指定默认模型；无需 GitHub 模型变量或
   Secrets。两个模型是知识能力的硬前置：真实镜像允许在无模型时创建
   keyword-only 知识库（创建接口返回 201），但文档入库与索引仍要求
   Embedding，平台不宣称无模型知识可用，也不读取平台自己的模型配置。
3. 按需创建知识库、上传文档并等待解析、索引完成（全部原生能力照常可用）。
4. 管理员首次打开超级助手「助手配置 → MCP」：WeKnora 区块自动拉取端点目录
   （GET），未初始化时补一次幂等初始化（POST initialize）。三种结果：
   - WeKnora 已有 MCP 端点：平台只持久化“首次已检查目录”标记并在区块中
     列出目录，不创建、不修改任何原生端点；
   - 目录为空且恰好存在一个默认（或唯一启用）的 Embedding 与 KnowledgeQA
     模型：平台经私网管理 API 创建名为 `OpenOntology` 的共享知识库（已存在
     同名知识库且其绑定的 Embedding/聊天模型仍可用时直接复用）与同名
     MCP 端点（只勾选 8 个只读知识工具，显式绑定该知识库），端点 token
     加密写入平台托管 MCP 连接（`SuperAssistantMcpServer` 镜像行），随即
     自动完成工具发现验证、清单冻结与“受约束”登记；
   - 缺模型或默认模型不唯一：初始化失败并明确提示先在原生页面配置，不产生
     空壳知识库/端点，不置初始化闩，配置后可直接重试。
5. 接入已有原生端点（自建或其他来源）：平台不擅自修改原生端点的知识库范围
   与工具。端点工具必须全部落在只读白名单内，且必须保留
   `list_knowledge_bases`（平台核验实际知识库范围的必需工具，缺失同样拒绝
   托管并提示）；其余只读工具可按需子集。白名单为
   `list_knowledge_bases`、`search_knowledge`、`grep_chunks`、`list_documents`、
   `read_document`、`wiki_search`、`wiki_read_page`、`wiki_index`；`ask` 与
   文档新增、修改、删除工具一律不允许。接入需要输入该端点现有 token
   （创建时仅展示一次，需使用当时保存的完整值），或显式确认「轮换 Token」后接入——轮换会使
   旧 token 立即失效。多个已启用端点可同时接入使用，互不影响。
6. 目录刷新与每次工具调用都核验上游状态：端点在原生页被删除或停用、知识库
   范围变化、工具清单变化或 token 失效，都会使平台立即撤销该托管连接的
   “受约束”登记（fail closed）并提示重新验证；重新验证成功才恢复调用。
   调用前的核验只用端点自身 token 做工具发现并实际调用
   `list_knowledge_bases` 比对冻结范围（读取结果返回前再核验一次）；执行
   worker 不获得 WeKnora 管理员密码。
7. 断开连接只删除平台侧托管行并撤销其授权，不会删除 WeKnora 侧的端点、
   知识库或文档，也不会轮换 token。初始化闩持久保留：之后目录再为空时
   平台不会自动重建，需在区块中显式“创建默认端点”（recreate 语义）或去
   原生页自建后再接入。

固定 WeKnora v0.9.0 与其 ParadeDB v0.25.11 组合在关闭问题生成时，关键词查询
可能返回 `Unsupported query shape`；`grep_chunks` 同受影响。这是上游查询问题，
不代表知识库没有相关内容。原生 `search_knowledge` 的 `mode=semantic` 已验证可
在开启向量索引的知识库返回片段与引用；平台不会为此擅自修改原生问题生成设置。

服务健康不代表知识已可检索。真实验收需上传脱敏文档，验证检索命中、引用和
超级助手调用；模型或索引未配置时应显示实际错误，不能用探活结果冒充知识就绪，
也不得在未配置 Embedding/KnowledgeQA 模型时宣称知识可用。

### Kaneo 项目管理（2.49.0）

Kaneo 是随平台 Compose 同启的单用户项目管理看板：官方
`ghcr.io/usekaneo/kaneo:2.49.0` 固定 digest 镜像 + 独立 PostgreSQL（固定
`postgres:16-alpine` digest，无宿主端口），保留自身账号体系；平台不接 MCP、
不改导航 menu key，唯一平台入口是超级助手会话头部「知识库」入口左侧的
「项目管理」按钮。

一键免登录（共享管理员会话）：普通左键点击按钮时，平台后端为已登录且有
超级助手菜单权限的用户签发一次性票据（HMAC 签名、90 秒有效、Redis 单次
消费），新标签打开 `KANEO_WEB_URL/sso/entry?ticket=...`；`kaneo-gateway` 把
该路径反代到 backend，backend 校验票据后以管理员凭据服务端登录 Kaneo，并
把会话 cookie 以 Kaneo origin 的第一方响应下发，随后 302 进入工作台。全程
用户无需知道 Kaneo 账号密码；所有有权限的平台用户共享同一个 Kaneo 管理员
身份。任何失败（票据无效/重放、Redis 不可用、Kaneo 拒绝登录）都回落到
Kaneo 自带登录页；中键/修饰键/右键点击按钮仍直开原生地址作手动登录兜底。

服务拓扑与地址契约：

- `kaneo-app`：Compose 内网固定 `http://kaneo-app:5173`，即 `KANEO_BASE_URL`
  在容器部署中的取值；本地源码运行改为 loopback `http://127.0.0.1:18090`
  （`KANEO_APP_LOOPBACK_PORT`，供宿主后端探活）。`DISABLE_REGISTRATION`、
  `DISABLE_GUEST_ACCESS`、`DISABLE_EMAIL_OTP_SIGN_IN` 全程为 `true`。
- `kaneo-gateway`：唯一公开入口。HTTP `KANEO_HTTP_PORT`（默认 8090）只做 308
  跳转到规范 HTTPS origin；HTTPS `KANEO_HTTPS_PORT`（默认 8445）反代 app 并
  保留 WebSocket 升级，另把精确路径 `/sso/entry` 反代到 backend 的免登录中继
  （`/api/v2/super-assistant/kaneo/sso/consume`，经 Docker 内嵌 DNS 解析
  `backend:8000`）。证书复用平台自签证书卷，首次访问需手动信任。
- `KANEO_WEB_URL` 必须是无凭据、无路径/查询/fragment 的绝对 HTTPS origin：
  它同时作为 308 跳转目标与 app 的 `KANEO_CLIENT_URL`/`KANEO_API_URL`，换用
  非规范 origin（如直连 8090 或带路径前缀）会破坏原生登录。本地默认
  `https://localhost:8445`；生产未显式配置时由部署脚本从 `PUBLIC_TLS_IP` 与
  `KANEO_HTTPS_PORT` 推导。已有服务器配置缺少 `PUBLIC_TLS_IP` 时，从显式
  部署 `HEALTH_URL` 中校验出的公网 IP 初始化并持久化；已有值不覆盖。若健康
  地址使用域名、回环地址或无法确定公网 IP，须先配置 `PUBLIC_TLS_IP`。
- readiness：backend 与 pipeline_executor 强依赖 kaneo-app 健康，
  `/health/ready` 经 `/api/instance/status` 校验 `hasUsers`/`hasAdmin` 均为
  true；静态 `/api/health` 200 不算就绪。

账号与密钥：

- 管理员默认 `KANEO_ADMIN_USER=admin`、`KANEO_ADMIN_EMAIL=admin@kaneo.local`
  （显示名 admin）。首次部署在私网完成账号创建与登录验证后，公网网关才启动；
  空实例本身允许创建首个管理员，因此不存在 WeKnora 式「临时开放注册」的
  init 容器。重复部署或重复 bootstrap 不修改已存在账号的密码；原生页面改密
  后，须在受控终端同步服务器 `.env` 的 `KANEO_ADMIN_PASSWORD` 并重启 backend，
  否则一键免登录立即失效（回落到手动登录），下次部署的管理员验证也会失败；
  部署流程不会覆盖数据库中的密码。
- `KANEO_ADMIN_EMAIL`/`KANEO_ADMIN_PASSWORD` 除 `kaneo-bootstrap` 外只进入
  `backend` 容器，供免登录中继做服务端登录；`pipeline_executor`、`task_worker`
  等其余运行时容器仍由 Compose 掩码锚点屏蔽，`KANEO_DB_PASSWORD`、
  `KANEO_AUTH_SECRET` 对所有平台容器屏蔽。凭据不进日志、接口响应与前端。
- `KANEO_ADMIN_PASSWORD`、`KANEO_DB_PASSWORD`、`KANEO_AUTH_SECRET` 独立于
  平台密钥，不复用 `SECRET_KEY`/`ENCRYPTION_KEY`。生产首次部署由部署脚本
  随机生成并持久化到服务器 `.env`（校验下限：管理员密码 8–32 位且不含空白、
  DB 密码至少 12 位、AUTH_SECRET 至少 32 位），之后保持稳定；已知本地默认值
  会被替换。安全获取方式只有服务器受控终端读取 `.env`，禁止回显到页面、
  日志、Git、工单或聊天。
- 其余模板变量：`KANEO_DB_NAME`/`KANEO_DB_USER`（默认 `kaneo`）、
  `KANEO_TIMEOUT_SECONDS`（默认 15，3–60）、`KANEO_APP_IMAGE`/
  `KANEO_POSTGRES_IMAGE`（固定 digest，漂移会被生产 digest 门禁拒绝）。

存储边界：项目数据的权威存储是 `kaneo_postgres_data` 卷；`kaneo_data` 卷
（`/var/lib/kaneo`）只是运行时资源。Kaneo 附件上传官方仅支持 S3 存储，首版
未配置 S3/SMTP，因此附件上传与邮件找回密码不可用——不得把 `kaneo_data`
当作附件持久化，也不得以本地卷 workaround 冒充附件能力。

### Grafana 监控看板（13.3.1）

Grafana 是随平台 Compose 同启的监控看板工作台：官方
`grafana/grafana:13.3.1` 固定 multiarch digest 镜像（实测 linux/amd64），
生产不发布任何宿主端口，唯一浏览器入口是平台 frontend（nginx）的同源子
路径 `/grafana/`。空看板由使用者自行配置：平台不 provisioning 任何数据源
或面板（无 provisioning 挂载），使用者在 Grafana 原生界面（经平台「编排」
入口）完成配置。

服务拓扑与免登录机制：

- `grafana`：`grafana_internal` 私网成员（仅 grafana、grafana-bootstrap、
  backend、frontend），子路径模式（`GF_SERVER_SERVE_FROM_SUB_PATH=true`），
  auth-proxy 免登录（`GF_AUTH_PROXY_*`：可信网关注入 `X-WEBAUTH-USER`
  共享管理员；`GF_AUTH_DISABLE_LOGIN_FORM=true` 使绕过网关也没有密码登录
  入口，`GF_SECURITY_ALLOW_EMBEDDING=true` 允许平台预览弹窗 iframe 嵌入）。
- `frontend`（nginx）：`/grafana/` 对 HTTP 与 WebSocket 统一
  `auth_request`——子请求显式转发原始 `Cookie` 头到 backend 的
  `/api/v2/operations/dashboards/grafana-auth` 校验端点，204 且携带可信
  身份才放行并覆盖客户端身份头；否则 401（中文提示页）。无 cookie、伪造
  身份头、篡改/过期 cookie 的 HTTP/API/WS 全部拒绝；裸 `/grafana` 301 到
  `/grafana/`。
- 看板会话独立：平台登录态（localStorage JWT）与 Grafana 访问解耦，
  `POST /api/v2/operations/dashboards/grafana-session`（Bearer + 菜单授权）
  签发 8 小时 `oo_grafana_session` HttpOnly cookie（`Path=/grafana/`，
  SameSite=Lax，HTTPS 加 Secure），HMAC-SHA256 独立域前缀签名。平台退出、
  Bearer 过期或改密不撤销已签发的看板 cookie；到期后重新续签即可。有效
  cookie 下原生编辑器、`/grafana/api/*` 与 Grafana Live WebSocket 全部
  正常，全程不出现 Grafana 登录页。
- viewer 限制（单用户共享管理员）：任何持有看板 cookie 的平台用户在
  Grafana 内都是共享管理员。平台角色（viewer/custom）只约束平台卡片操作
  与会话签发，不构成 Grafana 内的只读边界；未建立独立只读账号。
- readiness：`/health/ready` 的 `grafana` 检查要求
  `/grafana/api/health` `database=ok` 且 basic `/grafana/api/user`
  `isGrafanaAdmin=true`；运行期 Grafana 故障使 `/health/ready` 返回 503
  并在 `checks.unavailable` 标注 `grafana`，其他服务继续运行，恢复后自动
  转绿，无需重启任何业务。

变量表（全部经 `.env`/环境变量，不用配置文件）：

| 变量 | 默认 | 说明 |
|---|---|---|
| `GRAFANA_IMAGE` | 固定 digest | 固定 `grafana/grafana:13.3.1@sha256:0615…c13cf`；漂移被生产 digest 门禁拒绝 |
| `GRAFANA_BASE_URL` | `http://grafana:3000` | 私网 API 地址；生产/本地 Compose 契约值 |
| `GRAFANA_ADMIN_USER` | `admin` | 共享管理员用户名（网关注入的 `X-Grafana-User`） |
| `GRAFANA_ADMIN_PASSWORD` | 部署脚本随机生成 | 8–32 位无空白；只进 grafana/backend/grafana-bootstrap |
| `GRAFANA_TIMEOUT_SECONDS` | `15`（3–60） | backend 对 Grafana HTTP API 的超时 |
| `GRAFANA_FOLDER_UID` / `GRAFANA_FOLDER_TITLE` | `openontology` / `OpenOntology 监控看板` | 平台创建的看板统一放入该文件夹 |
| `GRAFANA_SESSION_TTL_SECONDS` | `28800`（8 小时，60–86400） | 独立看板会话 cookie 有效期 |
| `GRAFANA_APP_LOOPBACK_PORT` | `13001` | 仅本地：grafana 的 127.0.0.1 回环端口 |
| `GRAFANA_PROXY_TARGET` | `http://grafana:3000` | 仅本地：vite 容器 `/grafana/` dev 代理上游 |

账号与密钥：

- `GRAFANA_ADMIN_PASSWORD` 仅进入 `grafana`（首次卷初始化创建管理员）、
  `backend`（内网 HTTP API：文件夹/建删看板/存在性对账）与
  `grafana-bootstrap`（健康+管理员+文件夹校验）；`pipeline_executor`、
  `task_worker` 等其余运行时容器由 Compose 掩码锚点合法置空。密码不进
  日志、接口响应与前端。
- `GF_SECURITY_ADMIN_PASSWORD` 只在首次卷初始化生效：原生界面改密后，
  运维须同步服务器 `.env` 的 `GRAFANA_ADMIN_PASSWORD` 并重启 backend 与
  grafana-bootstrap；环境变量无法重置已有卷的管理员密码。部署脚本不会
  回显或覆盖已持久化的密码。

本地开发差异：`docker-compose.local.yml` 同样默认启动 grafana 与
grafana-bootstrap（与生产同构）；本地例外是 grafana 仅在 `127.0.0.1:13001`
回环端口发布（供宿主机源码运行的 backend/Vite 使用），vite 容器经
`GRAFANA_PROXY_TARGET` 访问内网 grafana。宿主机 Vite 的 `/grafana/` 代理
（`frontend/vite.config.ts` 的 dev 插件）与 nginx `auth_request` 语义一致：
每个 HTTP/WS 请求先带原始 cookie 调 backend 校验端点，204 且身份有效才注入
`X-WEBAUTH-USER` 代理，禁止也不存在静态注入 admin 的旁路。

可直接复制的无秘密示例（`GRAFANA_ADMIN_PASSWORD` 一律从受控环境注入，
不在任何示例或文档中携带真实值）：

- 生产/本地 Compose（grafana 容器与 backend、grafana-bootstrap 同在
  `grafana_internal`，走容器 DNS，不发布宿主端口）：

  ```dotenv
  GRAFANA_IMAGE=grafana/grafana:13.3.1@sha256:0615ec4fbd4072004b7d86b46ded63013a74b7d6eb1119273dd89e8fa25c13cf
  GRAFANA_BASE_URL=http://grafana:3000
  GRAFANA_TIMEOUT_SECONDS=15
  GRAFANA_ADMIN_USER=admin
  GRAFANA_ADMIN_PASSWORD=<由 deploy-prod.sh 首次部署随机生成；本地 Compose 默认 localGrafanaAdmin2026!>
  GRAFANA_FOLDER_UID=openontology
  GRAFANA_FOLDER_TITLE=OpenOntology 监控看板
  GRAFANA_SESSION_TTL_SECONDS=28800
  ```

  首次/旧版服务器 `.env` 缺少上述键时，`deploy-prod.sh` 会按固定契约物化
  （镜像钉扎 digest、密码随机生成且不回显、其余按默认值；已持久化的合法值
  一律保留不改）。

- 宿主机源码运行（backend 与 Vite 不在 Compose 网内，必须用回环地址，
  不得把容器 DNS `grafana:3000` 写进宿主源码配置）：

  ```dotenv
  # config/generated/local/.env 或启动环境（backend）
  GRAFANA_BASE_URL=http://127.0.0.1:13001   # GRAFANA_APP_LOOPBACK_PORT
  GRAFANA_TIMEOUT_SECONDS=15
  GRAFANA_ADMIN_USER=admin
  GRAFANA_ADMIN_PASSWORD=<从受控本地环境注入；不写进示例文件>
  GRAFANA_SESSION_TTL_SECONDS=28800

  # Vite（frontend dev，GRAFANA_PROXY_TARGET 默认同回环地址）
  GRAFANA_PROXY_TARGET=http://127.0.0.1:13001
  ```

  Vite 校验端点复用既有 `VITE_API_PROXY_TARGET`（指向宿主 backend），不需要
  额外代理变量。管理员密码不进配置中心、不进任何示例 env 文件。

存储边界：Grafana 看板数据的权威存储是 `grafana_data` 卷（SQLite/数据源/
面板/用户）；平台侧 `operations_dashboards` 表只保存卡片元数据（名称/
描述/图标/UID/排序）。两者必须成对备份与恢复（见 backup-restore.md）。
平台「改名」只改卡片元数据，不读不写 Grafana dashboard 的标题/JSON；
用户随后在 Grafana 原生界面改标题或移动目录不影响平台 UID 对账。

### Hindsight 长期记忆（生产默认启用）

Hindsight 1.2.0 以独立 Compose profile `hindsight` 提供超级助手长期记忆，
含专用 pgvector 数据库与模型缓存卷，不共用平台 PostgreSQL。**生产开箱即用**：
`HINDSIGHT_ENABLED` 默认为 `true`，`deploy-prod.sh` 自动生成凭据并启动；栈的
拉取与启动始终后置到核心部署成功之后，任何失败只降级记忆特性，不影响聊天与
全局 readiness。本地不受该开关控制，仍用
`docker compose -f docker-compose.local.yml --profile hindsight up -d` 显式启用。
停用记忆栈就在服务器 `.env` 显式写 `HINDSIGHT_ENABLED=false`（脚本随即停掉
历史容器，不删卷）；历史模板默认 `false` 的存量服务器会在下次部署被一次性
迁移为启用，此后显式 `false` 一律尊重。API 仅 Compose 内网（本地另有 loopback
`18888`）；管理 UI（9999 控制面）本地经 loopback `19999` 访问，生产由
`HINDSIGHT_WEB_PORT`（默认 `19999`）发布到宿主并以官方访问密钥登录保护。

平台侧设置：`HINDSIGHT_BASE_URL`（容器内网固定 `http://hindsight:8888`）、
`HINDSIGHT_API_KEY`（backend → Hindsight Bearer，镜像内 ApiKeyTenantExtension
强校验）、`HINDSIGHT_TIMEOUT_SECONDS`（默认 5）、`HINDSIGHT_NAMESPACE`、
`HINDSIGHT_SERVICE_TOKEN`（Hindsight → 平台内部记忆 LLM 代理
`/api/v2/super-assistant/internal/memory/v1` 的 service token，与容器
`HINDSIGHT_API_LLM_API_KEY` 同值；不是真实模型 key）。
三把密钥只进 backend/pipeline_executor 与 Hindsight 服务环境，生产由
deploy-prod.sh 幂等生成，可选经 GitHub `PROD_HINDSIGHT_DB_PASSWORD`/
`PROD_HINDSIGHT_API_KEY`/`PROD_HINDSIGHT_SERVICE_TOKEN` secrets 物化；缺失不
阻断平台部署。注意 `PROD_HINDSIGHT_DB_PASSWORD` 只在专用 DB 卷首次初始化时
生效：DB 角色密码落库后再改该 secret 不会轮换存量库，反而会让 Hindsight 连
不上自己的数据库（仅记忆特性降级）；要么在首次启用前配置，要么与服务器
`.env` 现值保持一致。镜像与专用 DB 一律 `@sha256` 钉扎（`HINDSIGHT_IMAGE`、
`HINDSIGHT_POSTGRES_IMAGE`，可选经同名 `PROD_HINDSIGHT_*` repository
variables 覆盖）。

`HINDSIGHT_WEB_URL` 是管理员专用的 Hindsight 原生 Web 入口：完整的 http(s)
根地址，非密钥，服务器 `.env` 原样保存，仅 backend 显式映射并使用（其余
env_file 服务可能继承该值但不使用；不经 GitHub secrets 物化）。**生产默认
开箱即用**：`HINDSIGHT_ENABLED=true` 且该值为空时，`deploy-prod.sh` 自动按
`http://<PUBLIC_TLS_IP>:<HINDSIGHT_WEB_PORT>` 推导并写入 `.env`（与
`WEKNORA_WEB_URL` 同构），记忆面板不再提示手工配置；已有值或运维改写时一律
保留。本地即 loopback 管理页 `http://127.0.0.1:19999`（写 `localhost` 也按
原样保留，不做 WeKnora 式主机名改写）。URL 内不得携带 tenant 或 API key。

控制面自身的防护由 `HINDSIGHT_CP_ACCESS_KEY` 提供（官方登录机制，Hindsight
1.2.0）：未设置时控制面对所有可达者**完全无认证**，因此生产启用记忆时
`deploy-prod.sh` 强制生成该密钥（16 个十六进制字符，保存在服务器 `.env`，
登录一次换 24 小时会话 cookie）。控制台检索记忆的 BFF 调用由
`HINDSIGHT_CP_DATAPLANE_API_KEY`（与 backend 的 `HINDSIGHT_API_KEY` 同值）
通过数据面租户鉴权。

管理员面板的「打开 Hindsight」是一键免密的：前端先领一次性票据
（`POST /api/v2/super-assistant/memory/console/entry`，仅管理员），浏览器
经平台 origin 的中继（`/memory/console/sso`）校验票据后，由 backend 用上述
密钥在服务端登录控制面，并把会话 cookie 透过 302 交给浏览器——用户无需
知道或输入密钥（与 WeKnora/Kaneo 一键免登录同构）。会话 cookie 按主机存储
且忽略端口，因此该链路要求平台与控制面同主机（生产同为 `PUBLIC_TLS_IP`）；
平台改用域名访问时 entry 自动 503，入口回落为控制台自带密钥登录（中键/右键
直开同样如此）。Redis 不可用或控制面未就绪时中继 fail-closed，不影响记忆
写入/召回链路。密钥本身仍只保存在服务器 `.env`，平台页面不回显。

记忆 LLM 的模型选择是服务级的、每用户只有一个"当前模型"：Compose 三文件
把 Hindsight 的 `HINDSIGHT_API_LLM_MODEL` 固定为平台别名
`oo-memory-default`（Hindsight 1.2.0 禁止 bank 级 `llm_model` 覆盖，部署不
提供也不接受自定义别名变量）。平台记忆网关收到该别名的请求后，从该用户的
记忆设置中取「唯一一条 `enabled` 且 `model_config_id` 非空」的记录解析真实
模型（复用平台模型配置，不向 Hindsight 存任何模型密钥）：0 条或多条满足
条件时返回 503，管理员需把该用户的记忆设置收敛为恰好一条启用的模型。

embedding 固定为 ONNX `intfloat/multilingual-e5-small`（384 维，E5 非对称
前缀），模型不在镜像内：首启需 HuggingFace egress 下载。缓存走
`HF_HOME=/hindsight-model-cache`（`hindsight_model_cache` 卷；镜像以 uid 1000
运行，`hindsight-model-init` 负责卷属主）。离线（`HINDSIGHT_HF_OFFLINE=1`）
要求 Hindsight 运行时经 HuggingFace 缓存解析的每一个模型都在该卷内：不只
embedding，还包括默认 reranker（`hindsight-ai/hs-rerank-1`）——`HF_HOME` 已
指向该卷，镜像内预载副本不在离线解析路径上；除非实测确认 reranker 走镜像内
独立路径，否则必须与 embedding 一并入卷。可靠的预填方式是让同一钉扎镜像挂
同一 `hindsight_model_cache` 卷、在有空闲 egress 的环境完整首启一次，由服务
自身写入所需模型，再整卷复用；不要按猜测的目录布局手工拼装，也不要只
`snapshot_download` embedding 一个 repo 就宣称离线可用——离线行为必须在
staging 实测。rerank 中文效果未验证，属效果门禁。

### Multica 默认服务（默认启用，可显式关闭）

Multica 是超级助手的外部任务/智能体集成（每用户自配模式见「外部集成」
设置页）。平台提供**默认启用的 bundled 官方栈**，并可额外提供一个
**部署级默认集成**：五项 `MULTICA_DEFAULT_BASE_URL` /
`MULTICA_DEFAULT_WEB_URL` / `MULTICA_DEFAULT_WORKSPACE_ID` /
`MULTICA_DEFAULT_WORKSPACE_NAME` / `MULTICA_DEFAULT_PAT` 全部留空即未提供
部署默认（bundled 栈照常运行，超级助手按每用户自配工作）；五项**必须全部
填写**（all-or-nothing，部署脚本对部分填写 fail-closed），配置完整后生产
`/health/ready` 的 `multica_default` 项用 PAT 真实探测（`/api/me` + workspace
可见性）：未配置显示 `disabled`，部分配置或探测失败显示 `unavailable` 并
**使生产 readiness 返回 503**（平台默认 profile 由这组设置派生并按用户默认
自动选用，默认服务不可达是真实的配置失败，不藏在每用户特性降级里）；开发/
测试环境该项仅 advisory。启动探针（`dependency_probe`）在**任何环境**都把
`Multica default` 作为 advisory：bundled 栈在核心部署之后才启动，阻塞进程
启动会自我死锁；生产收敛由 readiness 门禁保证。探测错误只含静态描述，
绝不带 URL、PAT 或上游响应体。

同一用户的 Multica 工作区 ID 不得跨 API 地址复用，因为 AutoLoop 用它区分
任务历史。如果部署默认与该用户已有绑定（包括已删除配置的历史）冲突，该
用户不会切到冲突的默认配置。可用的自定义配置和已冻结的任务来源不受影响。

在超级助手「能力拓展」中，自定义 Multica 配置的「服务地址」用于 API 调用，
「工作台地址」填写浏览器可访问的 HTTP(S) origin，允许两者使用不同主机或端口。
工作台地址留空时不显示「打开 Multica」入口。升级时会用存量自定义配置的 API
origin 一次性回填原入口；如果该实例的 Web 地址另设，请在编辑配置时更新。

凭据与网络边界：

- `MULTICA_DEFAULT_PAT` 是部署级密钥。Compose 掩码锚点把它对所有
  `env_file` 服务置空，仅 `backend` 与 `pipeline_executor` 显式取回真值
  （AutoLoop 派发消费与内核执行链路在 executor 内调用 Multica）；`task_worker`、
  `migrate` 等保持空掩码。栈自身密钥 `MULTICA_JWT_SECRET` 与
  `MULTICA_POSTGRES_PASSWORD` 同样被掩码锚点对全部平台 `env_file` 进程
  置空（backend/executor 也不例外）。部署日志对三者 redact，任何页面/
  日志/工单都不得回显。
- Multica 内网地址允许清单是独立设置
  `SUPER_ASSISTANT_MULTICA_INTERNAL_BASE_URLS`（逗号分隔 origin/路径
  前缀），**刻意不与** `SUPER_ASSISTANT_MCP_INTERNAL_BASE_URLS` 共用：
  开启 Multica 内网信任不扩大 MCP 的内网信任面，反之亦然。
- 生产 `MULTICA_DEFAULT_WEB_URL`（设置时）必须是无凭据/路径/查询的绝对
  http(s) origin 且浏览器可达公网 host（后端启动校验拒绝 loopback/
  localhost）；`MULTICA_DEFAULT_BASE_URL` 允许内网 origin（bundled 部署
  即 compose 内网地址）。WEB_URL 的取值按默认集指向的 Multica 分两种
  契约：
  - **bundled 栈**（BASE_URL 为推导值或显式的 `http://multica:8080`）：
    WEB_URL 留空时推导为 multica-gateway 的公开 origin，即
    `PUBLIC_TLS_IP` + `MULTICA_HTTPS_PORT`（默认 8446）——与
    `WEKNORA_WEB_URL`/`KANEO_WEB_URL` 的推导同构，也是本部署对 bundled
    栈发布的公开 Web 入口。显式值一律保留（清单下发的值必须原样落到
    `.env`；自备反代的部署靠它保留自己的 origin）；与网关 origin 不一致
    时部署日志记一行 `warning: MULTICA_DEFAULT_WEB_URL differs ...`，
    提醒核对该入口是否可达（平台默认卡的链接指向它）。
  - **外部 Multica**（BASE_URL 指向别处）：平台无从推导其 Web 入口，
    显式 WEB_URL 原样保留；缺失时不代写（更不会套用 bundled 网关地址），
    由 all-or-nothing 校验以 4/5 拒绝。

bundled 官方栈（`MULTICA_ENABLED` 留空/true 均启用，默认；仅显式 `false`
关闭，关闭只停 bundled 容器、不删卷，外部 Multica 仍可配合五项默认值使用）：
compose 提供上游 multica-ai/multica **v0.11.0**（commit
`cb190810a659c1a61c4a86d9f6ecfc9df4b086d8`）官方自托管 Compose 的等价
三服务栈，契约按下述证据核验：

- 官方栈 = `pgvector/pgvector:pg17` + API 容器（`ghcr.io/multica-ai/
  multica-backend`，容器内固定监听 8080，`GET /healthz`=ready、
  `GET /health`=live，容器入口自动执行 `./migrate up` 再启动 server，
  附件上传持久化卷挂 `/app/data/uploads`）+ web 容器
  （`ghcr.io/multica-ai/multica-web`，容器内固定 3000，Next 服务端按
  `REMOTE_API_URL` 代理 API/WS，浏览器只需访问 web origin）。
- GHCR 只发布 `latest` tag，因此镜像引用是 **digest-only 钉扎**，digest
  已与 v0.11.0 release 镜像的 OCI `org.opencontainers.image.revision`
  （= `cb190810…`）/`version`（= `v0.11.0`）标签核验一致（多架构
  amd64+arm64 index）：backend
  `@sha256:099fd0d1eea8fc4cf03254c6dff86f7de206662ff71db8dc9a78e66726dd65a4`、
  web `@sha256:ba8fd0fda94a9ec141e15d77e591db8ee4b246720a722d6f9dffd185ee48863b`、
  postgres 复用平台既有 pgvector pg17 验证 digest。升级 = 重取新 release
  digest 后整体替换；部署脚本 digest 门禁拒绝浮动引用。
- 平台 API 服务名保留 `multica`（上游原名 `backend`），内网契约
  `http://multica:8080`；postgres、API 与 web 三个容器都只在
  `multica_internal` 私网，API 不挂 default 网——确要调用的 `backend`
  与 `pipeline_executor`（AutoLoop 派发消费/内核执行链路）按既有钉扎网络清单另行加入
  该专网（与 WeKnora/Kaneo/Hindsight 同构），default 网上的其余服务一律
  不可达 Multica。`multica_internal` 是普通 bridge、刻意不设
  `internal: true`：官方 backend 需要出站调用（配置 RESEND 时的验证
  邮件、Multica 智能体调用管理员配置的模型服务），标成 internal 只会
  破坏官方 web/agent 依赖而不增加真实隔离面（API 无宿主端口、web 默认
  只绑 loopback 已界定暴露面）。生产 web 另发布两个宿主端口：
  `MULTICA_WEB_PORT`（默认 18100）**默认只绑 loopback**
  （`MULTICA_WEB_BIND_ADDR`，默认 `127.0.0.1`），供 SSH 隧道
  （`ssh -L 18100:127.0.0.1:18100 <server>` 后开
  `http://localhost:18100`）或自备同宿主反代使用；公开入口走 bundled
  **multica-gateway**（nginx，`MULTICA_HTTPS_PORT`，默认 8446，与
  weknora/kaneo 网关同构地复用 `frontend_ssl_certs` 证书），把公开
  HTTPS origin（即 `MULTICA_DEFAULT_WEB_URL` 应配置的值）反代到
  multica-web:3000——Multica web 自带账号登录与 PAT 铸造界面，
  **不得明文公网暴露**，公开访问一律经该 TLS 网关（或自备 TLS 反代，
  反代在另一台机器时才用 `MULTICA_WEB_BIND_ADDR` 显式改绑具体网卡 IP，
  必须保持在 TLS 反代之后）。API 容器不发布任何宿主端口；本地 compose
  仅 loopback、不带网关（web `MULTICA_WEB_LOOPBACK_PORT` 18100、API
  `MULTICA_API_LOOPBACK_PORT` 18101），本地默认随 `.env` 的
  `COMPOSE_PROFILES=multica` 启动（见 development/setup.md）。容器健康
  检查用官方 `/healthz`；平台侧保留上文的真实 readiness 探测。
- 栈自身密钥 `MULTICA_JWT_SECRET`（Multica 会话签名）与
  `MULTICA_POSTGRES_PASSWORD` 与平台凭据无关：生产留空时由 deploy-prod.sh
  生成强随机值写入服务器 `.env`；本地 compose 有开发兜底。所有 multica
  变量在 compose 里都是默认值插值（无 `:?` 强制变量），profile 未激活时
  `docker compose config` 与核心 up 永不被阻断；deploy-prod.sh 的 compose
  调用固定 `COMPOSE_PROFILES=`（空），服务器 `.env` 里的同名值（如误拷
  .env.example）不会把 multica 偷渡进核心 up。
- 启动时机（与 Hindsight 不同，刻意在 readiness 门禁**之前**）：最终核心
  `up -d` 成功后立即以单独的有界 `--profile multica` 调用启动
  `multica multica-web multica-gateway` 并做有界 `/healthz` 健康等待，
  然后才进入 backend
  readiness 循环——五项默认值配置完整时 readiness 会门禁 `multica_default`，
  等就绪之后再启动会形成自我死锁。有界启动默认上界 300 秒
  （`MULTICA_START_TIMEOUT`，与 Hindsight 的隔离启动先例一致——首次
  部署须在该调用内拉满三个钉扎镜像，更短的上界会在慢链路首拉时超时）。
  启动失败只停掉本次起了一半的容器（卷保留，下次部署重试，已拉取的
  镜像层会被复用），不回滚已成功的核心 up；五值未配置时失败仅是
  特性级告警，五值已配置时随后 readiness 如实判定部署失败。
- 回滚边界：Multica 官方入口只在容器启动时执行 `./migrate up`，上游没有
  经核验的 downgrade 路径。bundled 栈一旦启用并正常使用（账号/workspace/
  PAT/任务数据已写入 `multica_pgdata`，附件已写入 `multica_uploads`），
  回滚或降级以恢复**同恢复点的卷备份**为准（见
  [备份与恢复](./backup-restore.md) 与 [回滚](./rollback.md)）；
  仅在卷仍为空（从未注册使用）时才可直接回退镜像 digest 重启。

**空卷初始化边界（平台绝不代写上游数据库）**：全新卷只会被官方入口
自动建表（`./migrate up`），账号/PAT/workspace 三者没有任何受支持的
自动播种——默认启用的 bundled 栈是"自动启动、手动初始化"，不是零配置
可用。管理员手动步骤：

1. 首启后打开 web 入口，用邮箱注册首个账号。Multica 没有密码登录：输入
   邮箱后填 6 位验证码，会话默认 30 天（上游 `AUTH_TOKEN_TTL`，滑动续期）。
   生产直接访问 multica-gateway
   （`https://<PUBLIC_TLS_IP>:<MULTICA_HTTPS_PORT>`，默认 8446；自签证书
   需在浏览器信任一次），或建 SSH 隧道走 `http://localhost:18100`，本地
   compose 直接 `http://localhost:18100`。验证码的获取方式二选一：
   - 默认：未配置 RESEND/SMTP 时打印在 API 容器日志
     （`docker compose --profile multica logs multica` 中的
     `[DEV] Verification code` 行），配置了邮件服务则发到邮箱。
   - 固定验证码：设置 `MULTICA_DEV_VERIFICATION_CODE`（恰好 6 位数字）
     后，任何账号邮箱 + 该验证码即可登录。**它等同于公开的登录口令**：
     能访问 web 入口的人只要知道一个账号邮箱就能进，只适合私有或测试
     实例；需要对外收紧时改回留空并配置邮件服务。上游只在 `APP_ENV`
     非 production 时认它，因此 `MULTICA_APP_ENV` 留空时部署自动落
     development，显式 production 与固定验证码并存则部署拒绝。该值只进
     multica 容器，部署日志脱敏。
   生产建议配置邮件后再开放注册并收紧 `MULTICA_ALLOW_SIGNUP`。
2. 在 Multica 内创建/选定 workspace，记下其 ID 与名称。
3. 用该账号铸造 PAT：Multica 原生界面/API（`POST /api/tokens`），或
   官方 CLI `multica login` 指向本部署（浏览器登录流自动 mint PAT）。
4. 回填默认值。bundled 部署只需给出 `MULTICA_DEFAULT_WORKSPACE_ID` /
   `MULTICA_DEFAULT_WORKSPACE_NAME` / `MULTICA_DEFAULT_PAT` 三项（直接写
   服务器 `.env`，或经受控 manifest 物化：vars
   `PROD_MULTICA_DEFAULT_WORKSPACE_ID` / `PROD_MULTICA_DEFAULT_WORKSPACE_NAME`
   与 secret `PROD_MULTICA_DEFAULT_PAT`）；
   `MULTICA_DEFAULT_BASE_URL` 推导为 `http://multica:8080`，
   `MULTICA_DEFAULT_WEB_URL` 推导为 multica-gateway origin（见上）。指向
   外部 Multica 时五项都要显式给出。回填后下一次部署起生产 readiness
   门禁 `multica_default`。

部署脚本启用时校验：三个镜像 digest、JWT/DB 密钥非空且非开发兜底、
五值 all-or-nothing、PAT ≥8 位且无空白、`MULTICA_WEB_PORT` 端口范围、
`MULTICA_WEB_BIND_ADDR` 为 IP 字面量、固定验证码恰好 6 位数字且不与
显式 `MULTICA_APP_ENV=production` 并存，以及两个 URL 的 origin 形态
（完整五值时）。

### 集成数据集 dbx（生产）

集成数据集的源登记与 schema 发现只使用固定版本 dbx；native PostgreSQL
能力只用于已登记资产的快照/读写，不能替代 metadata 发现。生产 Compose 提供
profile `integrated-dbx` 的官方镜像服务，镜像 digest 固定、无宿主发布端口，
只通过专用 bridge 网络供 backend 访问，并允许 dbx 出站连接 MySQL/PostgreSQL
源库。自动部署在服务器 `.env` 中无显式配置时生成独立 32-byte 密码、设置
`INTEGRATED_DBX_URL=http://dbx:4224` 和固定 `INTEGRATED_DBX_COMMIT`；显式外部
URL+password 原样保留，单缺任一或 commit 不符则 fail closed，不会回退内部服务。
生产启动前 preflight 及 `/api/health`/`/health/ready` 会认证并校验 dbx 版本；
非 production 不要求 dbx。不要将 dbx 密码写入 Git、日志或工单。

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

本体助手聊天回合也走同一 `PIPELINE_TASKS` JetStream 流，但使用独立
durable `ontology-assistant-run-executor` 和独立并发上限（默认 2），
不会占用流水线的 2 个默认槽位。该 subject 由 `pipeline_executor` 消费；
`TASK_INSTANCES_EXECUTOR=docker` 且未排除任务控制面的专用 `task_worker`
不订阅本体助手回合。聊天回合由 Web 请求先把 run 落库，再把
`run_id` 派发到 `ontology.assistant.run`；NATS executor 消费后调用
`run_service.execute_run`，DB 租约保证同一 run 最多一个执行者。Web 进程内的
APScheduler 每 45 秒对账一次：重派 queued run，仅把租约已过期（默认
`AGENT_RUN_LEASE_SECONDS=600`，心跳 `AGENT_RUN_HEARTBEAT_SECONDS=15`）的
running run 标记为 interrupted；未过期租约属于其它存活 worker，启动/重启
不会粗暴中断。派发失败时 run 保持 queued 等下一轮对账。Msg-Id 固定为
`ontology.assistant.run:<run_id>`，JetStream 10 分钟去重窗内重复补发会被
broker 合并；首条消息仍在工作队列时按未 ack 重投，queued 行本身是持久
事实源，不会因去重丢失。新增的
`AGENT_RUN_*` 限额登记在 `backend/app/ontologies/agent_runtime/limits.py`，
可按环境变量覆盖。

### AutoLoop 通知与日程事件

外部通知仍使用既有 ingest key 鉴权、来源限制、附件和渠道扇出。通知正文、
AutoLoop 接收事实、初始事件与待派发记录在同一数据库事务提交；
`(source_system, eventId)` 重放关联原通知和原接收事实。owner 只取原通知所用
key 的持久 `created_by` 且要求用户仍有效，保存 key 身份与来源限制；缺失归属
时保留 `awaiting_owner` 供排查，不从正文、来源名称或 PAT 推导他人身份。
可选 `threadId`（最多 512 字符）保存在接收事实中；重放保留原线程。历史绑定
按 owner、实际 key、线程、工作区和连接器地址校验，线程字符串本身不授予
Issue 权限。日程以持久任务 ID 作为来源线程。

全部存量日程目标到期后只生成 AutoLoop 事件。原计划槽位、Asia/Shanghai
时区、周期计算、一次性停用和执行历史继续保留；成功入站的 run 摘要为
“已生成AutoLoop事件，等待规则路由”。该摘要表示事件已持久接收，业务规则
和后续动作的结果应查对应 AutoLoop 事件。日程不再直接创建普通会话或激活
任务模板实例。

API 的 lifespan 在依赖就绪后启动日程扫描器（30 秒）和 AutoLoop 派发扫描器
（5 秒），每个调度任务仅运行一个实例，停止时等待在途扫描完成并回收线程。
派发通过现有 `PIPELINE_TASKS` stream 的
`super_assistant.autoloop.dispatch` subject，独立 executor 订阅 durable
`super-assistant-autoloop-dispatch`；原 subject/durable 继续保留。stream 保持
工作队列、7 天保留和 10 分钟 Msg-Id 去重窗，遇到不相容的存量策略会报错，
不静默覆盖。Web 派发循环在独立线程中运行并复用事件循环，生命周期结束时
回收线程和连接。

PostgreSQL 是接收与派发的事实源。Msg-Id 固定为
`super_assistant.autoloop.dispatch:<dispatch_id>:<version>`；发布确认超时后保留
原身份，由扫描器再次投递。executor 通过 CAS 领取默认 120 秒的 lease，过期
lease 由扫描器恢复；旧 token 不能完成新领取的工作。重复消息查持久结果。
路由领取后固定首条命中规则的版本和模板快照。命中规则按沙箱模板确定性生成
任务或追加评论；未命中时由模型输出 `create_task`、`append_task`、
`create_decision`、`ignore` 四种动作之一。校验失败、模型不可用或超时生成
可见的 `kernel_fallback` 决策。若运行时路由不可用，派发仍保留 `pending` 与
`error=awaiting_business_router`，追加 `routing.deferred` 并延迟 30 秒 NAK。
生产排查同时检查接收事件、路由事实、派发 state/error、lease/attempts、
逐步骤结果和 durable 的 pending/ack_pending。

AutoLoop 扫描器每 5 秒恢复派发和动作步骤，看门狗默认每 120 秒读取评论、
时间线和任务消息。`AUTOLOOP_MODEL_CONFIG_ID` 可指定精确启用的模型配置，
显式无效 ID 不回退默认模型；`AUTOLOOP_MODEL_TIMEOUT_SECONDS` 默认 30 秒，
范围 1–60。`AUTOLOOP_TEMPLATE_BUDGET_MS` 默认 1000，范围 1–10000，超限
明确失败，不截断模板结果。`AUTOLOOP_WATCHDOG_INTERVAL_SECONDS` 默认 120，
范围 10–3600；`AUTOLOOP_RECENT_FAILURE_HOURS` 默认 24，范围 1–168。
看门狗只关注 blocked、in_review 以及具有近期失败和同次失败回收证据的 todo；
远端未提供可归因的 task/cause 字段时，不能仅凭状态或相近时间推断失败回收。
历史被远端截断时保留已有事项，待完整证据后再判定。相同证据和模型配置复用
成功的持久研判；模型不可用时保留自由文本事项，后续扫描重新评估。模型配置
修订或超时设置变化会使研判缓存失效。

动作身份和步骤先提交再调用连接器。超时、发送中断或远端成功而本地提交失败
保留为 unknown；没有权威请求身份或明确未执行证明时不会重发。明确未发送或
HTTP 429 拒绝的步骤可复用原身份重试，最多尝试 3 次；已确认步骤不重复执行。
连接器的 owner、服务地址与工作区必须仍与接收动作时的作用域一致。
「已人工处理」仅原子关闭本地事项并消费证据水位，任何已认领、已发送、成功
或结果未知的相关步骤都会阻止关闭。watchdog 决定只写普通评论；助手或内核
决定生成唯一 `decision.answered` 事件，关联 Issue 评论作为独立步骤执行。

进程收到 HTTP 403 `DAILY_LIMIT_EXCEEDED` 后立即停止 AutoLoop 后续扫描及调用，
不自动重试；额度恢复需操作人员确认后重启相关 API/executor 进程。该停止标记
为进程内状态，运维须同时检查所有运行实例。发布失败先恢复依赖并让原派发
继续，不更换 dispatch 身份，不通过重复建通知规避去重，不直接修改 Issue 状态。
真实模型质量和实际 Multica 部署的评论/执行效果需在授权的隔离环境单独验收。
故障恢复执行方法见[测试手册](../development/testing.md#autoloop-事务入站与派发恢复)。

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

浏览器页面目标默认拒绝私网地址，`STEWARD_BROWSER_ALLOW_PRIVATE_NETWORKS`
默认 `false`。生产配置校验拒绝该值为 `true`，生产 Compose 的 backend 与
pipeline_executor 固定为 `false`；升级前依赖私网页面的部署必须改用受控
集成入口。隔离的本地 development/test 环境可显式启用。该限制针对页面目标，
不限制 backend 通过 `STEWARD_BROWSER_CDP_URL` 连接 Compose 内的 browser。
它仍不能替代浏览器网络出口策略：自动重定向、子资源和再次 DNS 解析需要在
隔离网络/受控代理上做攻击验收。

配置加载时会去除 `ENVIRONMENT` 两侧空白并转为小写，`prod` 统一为
`production`，以确保启动、权限、发布和浏览器入口使用相同的生产环境判定。

明确例外只有：API Hub 自有 SQLite；`ENVIRONMENT=test` 的隔离 SQLite 与 n8n
配置注入；历史 `local://` 对象的只读读取/迁移。新平台数据不写入这些兼容路径。

平台只有一套 MinIO 配置，即部署环境 `MINIO_*`。早期开发模式曾把平台对象
写到“系统设置”里手动保存的 MinIO 端点，该手动配置（页面、`/api/v1/settings/
minio*` 接口、`minio_config` 表、外部 `/mcp/minio` 端点）已整体移除，回归期
对象的 legacy 读取适配器也一并退役。超级助手内置的 MinIO MCP 继续使用同一个
环境 MinIO，但所有工具被服务端锁定到 `MINIO_MCP_BUCKET`（默认
`assistant-workspace`）专用桶，无法接触平台数据桶；删除/移动类操作默认关闭，
需要时由部署方设置 `MINIO_MCP_ALLOW_DELETE=true` 放开。

## 助手本体查询与动作

本体助手的模型请求默认在连续 30 秒没有响应时结束，并且只尝试一次，避免
网关重试叠加长时间等待。backend 和 pipeline_executor 均可通过
`AGENT_LLM_TIMEOUT_SECONDS` 调整无响应超时（1–600 秒）；单个模型配置的
`options.ontology_assistant_timeout_seconds` 可覆盖该值。流式响应已经开始后，
此值约束后续读取的无响应间隔，不是整个正常生成过程的总时长。超时后用户
可检查模型服务或选择其它可用模型重试。

助手只使用 `current_release_id` 指向的当前发布结构。知识图谱提供发现线索，
不能作为业务实例身份或跨本体关联键的授权依据；关联必须引用已发布关系或
已验证的显式键依据。发布、权限或参数变化后，需要重新准备动作并再次确认。

`explicit_key` 关联需要本人已确认的业务键声明；仅字段存在、字段同名或知识图谱
中的相似描述都不足以证明关联。助手通过 `ontology_register_join_assertion` 展示
两端本体、类型、属性、键类型与业务含义，用户确认后保存本人声明；后续结构片段
只返回当前用户相关的有限声明，查询时再次核验两端和键类型。已发布关系实例使用
`declared_relation`。这些声明不修改本体定义，也不代表组织级治理认证。

统一查询在 PostgreSQL 的独立 `REPEATABLE READ` 事务内执行，以下配置由
backend 和 pipeline_executor 同时读取：

| 变量 | 默认值 | 作用 |
|---|---:|---|
| `AGENT_QUERY_STATEMENT_TIMEOUT_MS` | 3000 | 单条 SQL 的执行时限 |
| `AGENT_QUERY_LOCK_TIMEOUT_MS` | 1000 | 等待数据库锁的时限 |
| `AGENT_QUERY_WORK_MEM_MIB` | 4 | 单个排序或哈希操作的内存预算 |
| `AGENT_QUERY_TEMP_FILE_LIMIT_MIB` | 32 | 查询临时文件上限 |

超时和内存预算使用 `SET LOCAL`，事务结束后不污染连接池。`temp_file_limit`
是 PostgreSQL 管理员参数：应用以普通数据库角色运行时，数据库管理员须先执行
`ALTER ROLE <app_role> SET temp_file_limit = '32MB'`，然后重建应用连接池。
自定义上限时应与 `AGENT_QUERY_TEMP_FILE_LIMIT_MIB` 一致或更严格。应用会检查
当前连接的有效上限；未配置、无限制或宽于所需预算时，查询返回
`503 query_budget_unavailable`，不会为可用性取消预算，也不需要给应用超级用户
权限。源行数、关联行数和结果大小上限仍由查询服务独立强制执行。

`ASSISTANT_ACTION_EXECUTION_ENABLED` 是助手业务动作写入总闸。直接启动生产
应用且未配置时默认关闭；生产 Compose 显式默认开启，以交付确认后的业务
动作执行能力。部署方可在服务器 `.env` 设置为 `false`，重建 backend 和
pipeline_executor 后停止新的批准和 worker 领取。已批准但未领取的动作保留在
`dispatch_queued`，重新开启后可继续领取；已经进入执行的外部请求不能通过
关闭开关撤回。用户二次确认与动作自身的 `requiresApproval` 组织审批始终保留，
开启总闸不会跳过它们。

动作确认、执行操作和外部尝试均持久化。`unknown` 表示外部执行结果待核实，
`compensation_required` 表示外部已生效而本地提交失败、需要人工处理。这两种
状态都保留相同载荷的执行保护，同一请求的新确认会被
`409 operation_unresolved` 拒绝，不能用刷新、重试或新会话重复执行。维护者
应结合操作审计、`reconcile_evidence` 和外部系统结果核实；当前没有自动解除
保护或自动补偿端点，不得仅凭本地日志成功就宣称业务成功。

超级助手的外部与自研 MCP 仅在管理员审核凭据隔离、工具清单与出网边界后，
通过已有 MCP 更新接口登记 `constrained=true` 才可调用。管理员可跨用户只更新
该登记位；连接、凭据、工具清单或自研发布代码变化会撤销登记，需重新审核。
内置 MinIO 和 API Hub 由平台执行边界约束，不能手工改变信任位。等待用户确认
期间发生停用、清单变更或授权撤销，执行时会重新检查并拒绝过时授权。

浏览器和 API Hub 的助手调用通道拒绝平台自身管理来源。保护主机包含回环地址、
Compose 服务名，以及从平台公开 API、文件公开应用和 API 地址派生的主机；
反向代理的其他别名通过 `ASSISTANT_BROWSER_PROTECTED_ORIGINS` 逗号分隔补充，
同一主机的不同端口和协议同样受保护。浏览器当前来源不可核实时停止操作；
API Hub 在变量展开后和每次 HTTP 重定向前检查目标，MCP 桥接须使用 MCP 自身
的授权和确认入口。用户直接使用接口代理的既有功能保持原有权限规则。

## Python 脚本流水线执行网关（可选）

Python 脚本流水线（`definition.engine=python`）由 Jupyter Kernel Gateway
执行用户脚本。三个环境变量：`PYTHON_KERNEL_GATEWAY_URL`（服务地址）、
`PYTHON_KERNEL_GATEWAY_AUTH_TOKEN`（内网调用令牌）、
`PYTHON_SCRIPT_TIMEOUT_SECONDS`（单次执行上限，默认 120 秒）。该依赖是
**可选**的：不参与启动探测与 readiness；未配置或不可达时，脚本执行/保存/
试运行返回明确错误，其余平台能力不受影响。

插件社区「开发 MCP」（自研 MCP，`transport=developed`）同样经该网关执行：
开发页解析/试跑/保存/发布校验沿用 `PYTHON_SCRIPT_TIMEOUT_SECONDS` 时限；
agent 会话内调用自研 MCP 工具使用独立的 `MCP_DEV_TOOL_TIMEOUT_SECONDS`
（默认 60 秒，5-1800 可调）——会话内工具调用对时延更敏感，故默认收紧。

Compose 部署固定使用随栈启动的 `python_kernel_gateway` 服务。该服务以独立
内核执行用户脚本，因此**不得**给它挂 `env_file` 或注入任何平台凭据环境变量
（`DATABASE_URL`、`SECRET_KEY`、MinIO/n8n 凭据等对用户脚本可读），也不暴露
宿主机端口，仅 backend 经 Compose 内网访问。内核环境继承
backend 镜像依赖（requests/httpx/pandas/pymysql 等），脚本可直接发起 HTTP
请求取数；新增第三方库须加入 backend 依赖并重建镜像。

## 插件社区 stdio MCP（可选，默认关闭）

生产环境的 MCP、Remote Agent 和 Multica 外部端点默认要求 HTTPS，由
`SUPER_ASSISTANT_EXTERNAL_HTTPS_REQUIRED`（默认 `true`）控制；development/test
环境仍允许本地 HTTP fixture。内网例外分两份独立清单：MCP 用
`SUPER_ASSISTANT_MCP_INTERNAL_BASE_URLS`，Multica 用
`SUPER_ASSISTANT_MULTICA_INTERNAL_BASE_URLS`（见「Multica 默认服务」一节），
两者互不扩大对方信任面。切换既有生产集成前，应先迁移端点并执行外部回调、
凭据和证书验收。

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
生产部署强制 `STRICT_IMAGE_DIGESTS=true`，所有镜像必须使用不可变
`@sha256` digest；`false`、`no`、`0` 或缺失值都会被拒绝。部署入口不接受宿主
shell 中的同名变量覆盖 `.env`。浏览器承载外部网页内容，不能使用浮动 tag。
browser Dockerfile 中的字体包也固定到 Debian 版本号；更新版本必须重新构建、
执行 CDP 健康检查并复核浏览器安全探针。

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
