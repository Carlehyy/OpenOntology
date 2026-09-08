# 本地开发

## 前置环境

- Python 3.12.x；
- uv 0.11.14 或与锁文件兼容版本；
- Node.js 22；
- npm；
- PostgreSQL、Redis、NATS、Neo4j、MinIO 和 n8n，以及已明确配置
  地址的 Chromium CDP。

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
探针是提示性检查，暂时不可达不会阻止生成配置。Celery worker 在配置生成后
按下列命令启动，CDP 未恢复前深度 readiness 保持失败。

随后分别启动：

```bash
# dev_server 先执行 alembic upgrade head；迁移失败时 API 不会启动
uv run --directory backend python -m app.dev_server
uv run --directory backend celery -A app.tasks.celery_app:celery_app worker --loglevel=info
# 流水线 executor：消费 NATS 派发的流水线调度/手动触发、UI 手动运行与数据集导入任务
uv run --directory backend python -m app.data_channel.pipeline_tasks.nats_executor
npm --prefix frontend ci
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

流水线定时触发与手动异步触发、UI 手动运行整条流水线、数据集导入的解析与
提交，都经 NATS JetStream 派发给独立 executor 进程
执行，不再占用 API 进程；手动 `sync=true` 仍在 API 进程内同步执行。
executor 被打断的执行由数据库租约兜底：租约最长 6 小时过期，API 进程内
的对账器（默认每 5 分钟）把过期中断的任务/运行记录收口为 failed。

宿主机源码运行时的 `NATS_URL` 由 `config/generated/local/.env` 提供（默认
`nats://127.0.0.1:4222`）；compose 栈内由编排注入 `nats://nats:4222`。唯一的
无 NATS 降级例外是超级助手反思任务（`SUPER_ASSISTANT_REFLECT_*`）：未配置
`NATS_URL` 时降级为 Web 进程内联执行，其余派发一律 fail-closed。

随后执行配置中心的“启动后复检”，确认后端深度 readiness、前端以及至少一个
Celery worker PONG。复检未通过时平台不算启动完成。

n8n 地址、API Key 和超时由配置中心生成的启动环境统一托管，连通性由
`/health/ready` 实时探测；修改 n8n 后需重启 API 与 worker。生产环境 n8n
不可达会在启动探针处 fail-closed；开发环境的 n8n 探针为提示性（与
Chromium CDP 同等待遇）：没有 n8n 的开发机可以正常启动 API 进行诊断，
深度 readiness 保持失败，工作流相关能力在使用点明确报错。测试代码只有在
`ENVIRONMENT=test` 下才可注入隔离配置。

API、worker 和前端都启动后，再由管理员登录“模型配置”页面，按需配置 LLM
提供商、模型和凭据。LLM 未配置不阻断基础平台启动；相关接口会明确报告未配置，
或在已声明的文本抽取场景使用可识别的确定性规则模式，不会伪装成 LLM 结果。

## 本地端口

- 前端 Vite dev server：默认 `5173`，`strictPort`（被占用会明确失败），可用
  `LOCAL_FRONTEND_PORT` 覆盖（见 `frontend/vite.config.ts`）；
- 后端 dev_server：默认 `127.0.0.1:8000`（监听地址与端口由配置中心生成的
  `.env` 决定，默认值见 `backend/app/shared/config.py`）；
- NATS：`4222`（本机一行启动命令见 [config/README.md](../../config/README.md)）；
- 可选 Jupyter Kernel Gateway：`8088`（见上文）。

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
