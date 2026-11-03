# 本地开发

## 前置环境

- Python 3.12.x；
- uv 0.11.14 或与锁文件兼容版本；
- Node.js 22；
- npm；
- PostgreSQL、Redis、NATS、Neo4j、MinIO 和 n8n，以及已明确配置
  地址的 Chromium CDP；
- WeKnora v0.9.0 共享知识库（官方 app/ui/docreader + 独立 ParadeDB/Redis/
  APOC Neo4j）。本地源码运行可复用 `docker-compose.local.yml` 的 WeKnora 栈；
  首次使用需按本文初始化流程写入 `weknora_state`。

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
  网关；app/docreader/MCP/ParadeDB/Redis/Neo4j 均无宿主端口。

## WeKnora 本地初始化

完整本地平台使用 `docker compose -f docker-compose.local.yml up --build` 时，
WeKnora 会自动完成：仅私网、无宿主端口的 `weknora-app-init`（临时允许注册）
启动 → `weknora-bootstrap` 完成管理员/模型/KB/REST 初始化 → 正式
`weknora-app` 以 `DISABLE_REGISTRATION=true` 启动并提升已注册账号为系统管理员
→ `weknora-verify` 登录管理员、通过官方设置 API 持久化 `invite_only`、发现
MCP 工具并刷新 state。只有 verify 成功，`weknora-gateway` 才会在 backend 健康
后发布 8089/8444；任一失败网关保持未启动。

源码后端只启动 WeKnora（不启动平台 Compose 的 8000/5432 等服务）时，使用：

```bash
mkdir -m 700 -p runtime/weknora
export WEKNORA_STATE_UID="$(id -u)" WEKNORA_STATE_GID="$(id -g)"
docker compose -f docker-compose.local.yml up -d \
  weknora-db weknora-redis weknora-neo4j weknora-docreader \
  weknora-app-init weknora-bootstrap weknora-app weknora-verify \
  weknora-ui weknora-ssl-init
# 等 weknora-verify 成功；bootstrap 的 state 以 0600 写入宿主机目录
docker compose -f docker-compose.local.yml logs -f weknora-verify
docker compose -f docker-compose.local.yml up -d --no-deps weknora-gateway
# 可选：初始化完成后停止仅私网 init
docker compose -f docker-compose.local.yml stop weknora-app-init
```

源码 backend 的 WeKnora 变量由配置中心生成：
`WEKNORA_BASE_URL=http://127.0.0.1:18080`（loopback 直连 app，MCP 不经过公网
UI 网关）、`WEKNORA_STATE_ORIGIN=http://weknora-app:8080`（state 的部署身份，
严格校验，不要改成 loopback）、`WEKNORA_STATE_PATH=./runtime/weknora/integration.json`、
`WEKNORA_WEB_URL=http://127.0.0.1:8089`。state 由容器以宿主机 UID/GID 写入
`0600`；不要把权限放宽到 0644，也不要在 root 写完后让普通用户读取。普通
完整 `up` 未导出 UID/GID 时默认 root 写，仅供容器内 backend 使用；源码模式
必须先 `mkdir -m 700` 并导出非 root UID/GID。

首次登录 WeKnora 原生 Web 的邮箱/密码只从受控终端的 `.env`
（`WEKNORA_ADMIN_EMAIL` / `WEKNORA_ADMIN_PASSWORD`）读取，不要打印真实值、
不要发到前端页面或工单。配置中心的 WeKnora 测试只覆盖 app 直连 HTTP 与
verified state 元数据/工具白名单，不等于真实聊天、检索或图谱深度验收。

本地仍必须提供 `WEKNORA_CHAT_*` 与 `WEKNORA_EMBEDDING_*`（可在 `.env` 或
shell 环境），没有假默认模型。`WEKNORA_ADMIN_PASSWORD` 本地有仅用于开发的
默认值，生产始终由部署脚本生成。重置本地知识库属于破坏性手工操作，文档不会
自动执行：删除 `weknora_*` 卷会永久丢弃本地知识库文件、图谱、Redis
任务/缓存状态和 state（含 MCP 凭据），只有在确认这些数据不再需要时才可手动
删除并重新 `up`。已有成功退出的 bootstrap 容器可用
`docker compose ... up --force-recreate weknora-bootstrap` 重新执行。

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
