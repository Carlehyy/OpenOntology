# 本地开发

## 前置环境

- Python 3.12.x；
- uv 0.11.14 或与锁文件兼容版本；
- Node.js 22；
- npm；
- PostgreSQL、Redis、NATS、Neo4j、MinIO 和 n8n，以及已明确配置
  地址的 Chromium CDP；
- WeKnora v0.9.0 共享知识库（官方 app/ui/docreader + 独立 ParadeDB/Redis）。本地源码运行可复用 `docker-compose.local.yml` 的栈；
  首次仅初始化账号，模型与知识库随后在原生 Web 配置。
- Kaneo 2.49.0 项目管理（官方固定镜像、独立单用户账号与独立 PostgreSQL）。
  本地源码运行同样复用 `docker-compose.local.yml` 的 Kaneo 服务，管理员账号
  由 Compose 首次启动在私网自动创建。

不存在可用于正常开发的“最小降级模式”。PostgreSQL、Redis、
NATS、Neo4j、MinIO 和 n8n 必须提供真实配置并通过连通性检查；Chromium CDP
地址必须配置，其服务连通检查是提示性的，不可达时 API 可启动用于诊断但深度
readiness 失败。地址只填写 HTTP(S) 服务根地址（例如 `http://browser:9222`），不要附加
`/json/version`、查询参数或 URL 用户信息。平台不会改用 SQLite、API 线程任务、
NetworkX/SQL 图或本地对象存储。
API Hub 自有 SQLite、测试环境 SQLite 和历史 `local://` 只读迁移兼容有各自
边界，不代表开发运行时可以省略依赖。

## 推荐：本地配置中心

```bash
./config/start.sh
```

Windows 使用 `config/start.bat`。配置中心生成
`config/generated/local/.env`，该文件不进入 Git。生成前必须通过 PostgreSQL、
Redis、NATS、Neo4j、MinIO 和 n8n 探针；Chromium CDP 地址同样必须配置，但其启动前
探针是提示性检查，暂时不可达不会阻止生成配置。后台任务 executor 在配置生成后
按下列命令启动，CDP 未恢复前深度 readiness 保持失败。

随后安装前端依赖，并在三个终端分别启动后端、后台任务 executor 和前端：

```bash
npm --prefix frontend ci

# 终端 1：后端（dev_server 会先执行 alembic upgrade head）
uv run --directory backend python -m app.dev_server

# 终端 2：后台任务 executor，持续刷新 30 秒心跳
uv run --directory backend python -m app.data_channel.pipeline_tasks.nats_executor

# 终端 3：前端
npm --prefix frontend run dev
```

注意：`dev_server` 带 uvicorn `--reload`，应用启动失败（如依赖探针拦截）
后 reloader 父进程会驻留并占用端口等待文件变更重试；重新启动前先
`pkill -f app.dev_server` 清理残留进程，否则会遇到 `Address already in use`。

Python 脚本流水线（可选能力）还需要一个 Jupyter Kernel Gateway 执行网关，
Windows 原生可跑、无需 Docker：

```bash
uv run --directory backend jupyter kernelgateway --KernelGatewayApp.port=8088
```

并在启动环境中设置 `PYTHON_KERNEL_GATEWAY_URL=http://localhost:8088`（令牌可
留空）。不启动网关不影响其余开发：仅脚本执行/保存会返回明确的未配置错误。
内核直接使用 backend 依赖环境，脚本可用 requests/pandas 等已装库。Compose
本地起栈（docker-compose.local.yml）时该网关由 `python_kernel_gateway` 服务
自动提供，无需手工启动。

手动 `sync=true` 仍在 API 进程内同步执行；其余后台任务都经 NATS JetStream
派发给独立 executor，不再占用 API 进程。
executor 被打断的执行由数据库租约兜底：租约最长 6 小时过期，API 进程内
的对账器（默认每 5 分钟）把过期中断的任务/运行记录收口为 failed。

宿主机源码运行时的 `NATS_URL` 由 `config/generated/local/.env` 提供（默认
`nats://127.0.0.1:4222`）；compose 栈内由编排注入 `nats://nats:4222`。唯一的
无 NATS 降级例外是超级助手反思任务（`SUPER_ASSISTANT_REFLECT_*`）：未配置
`NATS_URL` 时降级为 Web 进程内联执行，其余派发一律 fail-closed。

随后执行配置中心的“启动后复检”，确认后端深度 readiness、前端以及独立的
NATS executor 进程健康（30 秒心跳）。复检未通过时平台不算启动完成。

n8n 地址、API Key 和超时由配置中心生成的启动环境统一托管，连通性由
`/health/ready` 实时探测；修改 n8n 后需重启 API 与 executor。生产环境 n8n
不可达会在启动探针处 fail-closed；开发环境的 n8n 探针为提示性（与
Chromium CDP 同等待遇）：没有 n8n 的开发机可以正常启动 API 进行诊断，
深度 readiness 保持失败，工作流相关能力在使用点明确报错。测试代码只有在
`ENVIRONMENT=test` 下才可注入隔离配置。

API、executor 和前端都启动后，再由管理员登录“模型配置”页面，按需配置 LLM
提供商、模型和凭据。LLM 未配置不阻断基础平台启动；相关接口会明确报告未配置，
或在已声明的文本抽取场景使用可识别的确定性规则模式，不会伪装成 LLM 结果。

## 本地端口

- 前端 Vite dev server：默认 `5173`，`strictPort`（被占用会明确失败），可用
  `LOCAL_FRONTEND_PORT` 覆盖（见 `frontend/vite.config.ts`）；
- 后端 dev_server：默认 `127.0.0.1:8000`（监听地址与端口由配置中心生成的
  `.env` 决定，默认值见 `backend/app/shared/config.py`）；
- NATS：`4222`（本机一行启动命令见 [config/README.md](../../config/README.md)）；
- 可选 Jupyter Kernel Gateway：`8088`（见上文）；
- WeKnora 原生 Web：`8089`（HTTP）与 `8444`（本地自签 HTTPS），只暴露 UI
  网关；app 仅在 loopback 18080 可达；docreader/ParadeDB/Redis 无宿主端口。
- Kaneo 原生 Web：`8090`（HTTP，仅 308 跳转 HTTPS）与 `8445`（本地自签
  HTTPS）；app 另在 loopback `18090` 暴露给源码后端探活；Kaneo 独立
  PostgreSQL 无宿主端口。

## WeKnora 本地初始化

完整本地栈使用 `docker compose -f docker-compose.local.yml up --build`。
WeKnora 私网 init 注册管理员后，正式 app 提升该账号为系统管理员；verify 持久化
关闭注册，成功后开放原生 Web。无需模型密钥、状态文件或独立 Neo4j。

只为源码后端启动 WeKnora（不启动平台 Compose 的 8000/5432）时：

```bash
docker compose -f docker-compose.local.yml up -d --build weknora-gateway
# 确认账号验证容器以 0 退出，网关已健康
docker compose -f docker-compose.local.yml logs weknora-verify
docker compose -f docker-compose.local.yml ps -a weknora-verify weknora-gateway
docker compose -f docker-compose.local.yml stop weknora-app-init
```

配置中心只生成服务地址 `WEKNORA_BASE_URL=http://127.0.0.1:18080`、
`WEKNORA_WEB_URL=http://127.0.0.1:8089`、超时和内部 MCP 前缀
`SUPER_ASSISTANT_MCP_INTERNAL_BASE_URLS=http://127.0.0.1:18080/mcp`。
模型、共享知识库与 MCP 端点配置按
[WeKnora 操作流程](../operations/configuration.md#weknora-共享知识库v090) 在原生页面完成，
随后在超级助手现有 MCP 管理中登记一次地址与 token，测试并登记受约束。

本地 Compose 管理员默认邮箱为 `admin@weknora.local`，密码采用仅用于本地的
`localWeKnoraAdmin2026@`，可在根 `.env` 显式覆盖。生产密码由部署脚本生成并稳定
保存，不能使用本地默认值。配置中心探针只验证服务与注册保护，不代表检索已就绪。
重置知识库或删除 `weknora_*` 数据卷会丢失文件、索引和凭据，须单独确认；普通
重新部署不会清空这些数据。

## Kaneo 本地初始化

Kaneo 使用官方 `ghcr.io/usekaneo/kaneo:2.49.0` 固定 digest 镜像，以独立单用户
账号与独立 PostgreSQL 运行，平台不接 MCP/账号同步，也不改导航 menu key；
唯一入口是超级助手会话头部「知识库」（悬停「领域知识」）入口左侧的
「项目管理」按钮。普通左键走一键免登录：backend 签发一次性票据，
`kaneo-gateway` 的 `/sso/entry` 中继在 Kaneo origin 服务端登录并种会话
cookie，直接进入工作台；中键/修饰键/右键直开原生地址作手动登录兜底。

完整本地栈中 Kaneo 与平台同启且为强依赖：`kaneo-db` 健康后启动 `kaneo-app`，
`kaneo-bootstrap` 一次性容器在私网创建管理员并登录验证，成功后 `kaneo-gateway`
才开放公网入口；backend 与 executor 均依赖 kaneo-app 健康及 bootstrap 成功
完成，Kaneo 未就绪时平台启动/深度 readiness fail-closed。本地默认管理员为
`admin / admin@kaneo.local / localKaneoAdmin2026!`（用户名/邮箱/密码，仅用于
本地，可在根 `.env` 用 `KANEO_*` 变量覆盖；backend 容器读取同一邮箱/密码做
免登录中继）。重复执行 bootstrap 或重新 `up` 不会修改已存在账号的密码；在
原生页面改密后，须在受控终端同步根 `.env` 的 `KANEO_ADMIN_PASSWORD` 并重启
backend，否则一键免登录立即失效，下次部署的管理员验证也会失败并关闭入口。

只为源码后端启动 Kaneo（不启动平台 8000/5173）时：

```bash
docker compose -f docker-compose.local.yml up -d --build kaneo-db kaneo-app kaneo-bootstrap
# 确认 bootstrap 容器以 0 退出、app 健康，再开放网关
docker compose -f docker-compose.local.yml logs kaneo-bootstrap
docker compose -f docker-compose.local.yml ps -a kaneo-bootstrap kaneo-app
docker compose -f docker-compose.local.yml up -d kaneo-gateway
```

源码模式的探活地址是 `KANEO_BASE_URL=http://127.0.0.1:18090`（Compose 把 app
发布到 loopback 18090；栈内固定 `http://kaneo-app:5173`）。配置中心探针请求
`/api/health` 与 `/api/instance/status`，要求 `status=ok` 且 `hasUsers`、
`hasAdmin` 均为 true，仅 health 200 不会通过；重定向会被拒绝，因此只做 308
跳转的 8090 端口不能填入内部地址。原生页面固定为 HTTPS
（`KANEO_WEB_URL`，本地默认 `https://localhost:8445`）：前端严格保留该规范
地址。需要从其他机器访问时，先将它设置为实际可达的 HTTPS origin，并重启
Kaneo，使页面 API、认证来源和平台入口一致；不会按当前访问主机改写。

本地或隔离 staging 环境验收时核对以下结果：

- 首次访问 `https://localhost:8445` 须手动信任自签证书（复用平台本地证书卷）；
- HTTP `http://localhost:8090/` 返回 308 且 Location 精确指向规范 HTTPS origin；
- 登录平台后在超级助手页点击「项目管理」：新标签直接进入 Kaneo 工作台
  （免登录中继生效，全程无账号密码），`/sso/entry` 无票据访问 302 回根路径；
- 手动用本地默认管理员在 HTTPS 页面登录同样成功，页面无注册/访客/邮件验证入口；
- 停掉 `kaneo-app` 后平台 `/health/ready` 失败，恢复后自动回到就绪。

首版未接 S3/SMTP：附件上传（官方仅支持 S3 存储）与邮件找回密码不可用；
`kaneo_data` 卷（`/var/lib/kaneo`）只是运行时资源，不是附件持久化，项目数据
的权威存储是 `kaneo_postgres_data`。

## Hindsight 长期记忆（可选）

Hindsight 默认不启动。启用本地记忆栈（含专用 pgvector DB 与模型缓存卷）：

```bash
docker compose -f docker-compose.local.yml --profile hindsight up -d
curl http://127.0.0.1:18888/health/ready   # API + DB 就绪探针
```

首次启动需 HuggingFace egress 下载 embedding 模型
（`intfloat/multilingual-e5-small`，缓存在 `hindsight_model_cache` 卷）；无
egress 环境先在有网机器预下载同一 repo 并填入该卷，再在 `.env` 设
`HINDSIGHT_HF_OFFLINE=1`（方法见
[operations/configuration.md](../operations/configuration.md#hindsight-长期记忆可选默认关闭)）。
API 仅 loopback `18888`，管理 UI 仅 loopback `19999`，都不对外。服务不可用时
仅记忆特性降级，聊天不受影响；配置中心 `Hindsight` 节为可选测试，不阻塞生成本地配置。

可选的 `HINDSIGHT_WEB_URL` 是管理员专用的原生 Web 入口：本地填
`http://127.0.0.1:19999`（写 `localhost` 也按原样保留，不做改写），不为此发布
额外端口。URL 内不得携带 tenant 或 API key；生产在 `HINDSIGHT_ENABLED=true`
且留空时由 `deploy-prod.sh` 从 `PUBLIC_TLS_IP` + `HINDSIGHT_WEB_PORT` 自动
推导默认入口，控制面由自动生成的 `HINDSIGHT_CP_ACCESS_KEY` 登录保护（详见
[operations/configuration.md](../operations/configuration.md#hindsight-长期记忆可选默认关闭)）。

## 最小验证

改动后先跑受影响范围，再执行 [AGENTS.md](../../AGENTS.md) 第 5 节完整门禁：

- 后端：`uv run --directory backend pytest -q tests/<domain>`（测试套件按
  AGENTS.md 第 5 节方式离线运行，无需真实依赖服务）；
- 前端：`npm --prefix frontend run test:unit`（无 DOM、无网络的纯逻辑快检）。

## 搜索契约

- `GET /api/v2/ontologies/{ontology_id}/search/keyword` 使用 PostgreSQL；
- `POST /api/v2/ontologies/{ontology_id}/search` 的 `mode=keyword` 使用
  PostgreSQL；
- 语义搜索端点和 `mode=semantic` 返回
  `501 semantic_search_unsupported`；
- 不需要也不应配置 ChromaDB。

## 测试环境例外

`ENVIRONMENT=test` 可以使用隔离 SQLite、mock 服务、临时目录和数据库 n8n
配置注入，以保证测试确定性。测试例外不得进入正常启动配置，也不能作为真实
依赖验收证据。生产配置和部署见
[配置说明](../operations/configuration.md)与
[部署说明](../operations/deployment.md)。
