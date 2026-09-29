# OpenOntology 本地配置中心

这个小工具只负责本地源码运行配置，不修改 GitHub Actions、生产部署清单或服务器环境变量。

> **状态说明**：配置中心的界面和部分探针代码仍保留旧版 Celery worker 文案与检查，
> 这不是当前后台任务运行契约。它可以用于生成配置，但不能用旧 worker PONG 作为
> 平台启动完成的证据；支持的后台执行入口是 NATS JetStream + `nats_executor`。
> 配置中心代码迁移需要独立的源码与测试变更，本页不把未完成迁移描述成已完成。

## 启动

- Windows 11：双击 `start.bat`
- Ubuntu 或 macOS：在终端执行 `./start.sh`

首次启动会由 `uv` 创建独立 Python 环境。网页默认只监听 `127.0.0.1:8888`，
不会主动暴露到局域网。需要临时改端口时，可在启动前设置
`OPENONTOLOGY_CONFIG_PORT`。
启动器会打开一个带随机本地访问令牌的地址；令牌只保存在当前页面内存中，每次重启都会变化。
如果浏览器没有自动打开，请复制启动窗口显示的完整地址，不要只输入端口地址。

## 配置边界

生成配置前必须确认平台与端口、安全与目录，并通过 PostgreSQL、Redis、NATS、
Neo4j、MinIO 和 n8n 的真实连通性测试。Chromium CDP 地址同样是必填启动配置，但其
生成前连通测试只用于提示：暂时不可达仍可生成配置并启动 API 诊断，深度
readiness 会保持失败。生成后启动 API、NATS executor 和前端，再由真实运行检查
确认后端深度 readiness、executor 消费心跳和前端可用。配置中心旧版 worker 检查
不作为证据；配置中心不会提供 SQLite、API 线程任务、内存图或本地对象存储降级。

CDP 地址只填写 HTTP(S) 服务根地址，例如 `http://127.0.0.1:9222`。配置中心和
后端会自行请求 `/json/version`，因此不要把 discovery 路径、查询参数、fragment
或 URL 用户信息写入该字段。

ChromaDB 已从平台及配置模型中移除。关键词搜索由 PostgreSQL 提供；语义搜索
端点和统一搜索的 semantic 模式明确返回 `501 semantic_search_unsupported`。
LLM 不属于启动门禁：先启动完整平台，再由管理员在“模型配置”页面按需添加
提供商、模型和凭据；API Key 留空时不会创建本地托管的默认模型记录。

配置写入：

```text
config/generated/local/.env
```

该文件是本地唯一配置来源并被 Git 忽略。后端、API Hub、Alembic 和前端 Vite 会读取它；系统环境变量仍具有最高优先级，因此 Docker 和生产部署保持原有行为。

生成操作采用同目录临时文件加原子替换。已有配置会先备份为 `.env.bak`。在 macOS 和 Ubuntu 上，配置文件权限会限制为当前用户读写。

## 本机默认值

配置中心已经内置常用的非敏感默认值：PostgreSQL 主机
`127.0.0.1:5432`、账号 `postgres`、数据库 `openontology`（源码运行口径；
`docker-compose.local.yml` 完整栈使用独立的 `ontoprompt` 库），Redis
`localhost:6379`，NATS `127.0.0.1:4222`（本机默认无认证，需以 `-js` 启用
JetStream），Neo4j `neo4j://localhost:7687`，MinIO
`localhost:9000`、账号 `admin`，以及默认的 CDP、n8n、平台端口和项目相对目录。

本机启动 NATS 可执行：

```bash
docker run -d --name nats -p 4222:4222 -v nats_data:/data nats:alpine -js --store_dir /data -m 8222
```

密码和 API Key 不会提交到 Git。需要在首次打开页面时自动加载固定凭据，可复制
`config/defaults.env.example` 为 `config/generated/local/defaults.env`，只在本机填写。
配置中心仅在正式的 `config/generated/local/.env` 尚未生成时读取该文件；读取后所有
敏感值仍会在浏览器中遮蔽。`config/generated/` 已整体被 Git 忽略。

## Windows 系统代理

配置中心和正式后端会强制直连 `127.0.0.1`、`localhost`、`::1` 等回环地址，
避免 Python 从 Windows 注册表继承公司代理后误转发本机 CDP 或 n8n
请求。远程模型及远程依赖仍沿用系统代理和 CA 配置，不需要全局关闭代理。

## 启动完整平台

全部必需探针通过并生成配置后，请分别打开三个终端，按网页给出的顺序启动：

1. 后端
2. NATS executor
3. 前端

后端启动命令会读取配置的监听地址和端口。Vite 使用严格端口，如果端口被其他程序占用会明确失败，不会悄悄切换。
流水线调度任务（定时触发与手动异步触发）由独立 executor 进程经 NATS 执行；本地源码运行时另开一个终端启动：

```bash
cd backend && uv run python -m app.data_channel.pipeline_tasks.nats_executor
```

平台可用后，再进入“模型配置”页面配置需要使用的 LLM。

## 安全说明

- 配置中心不把密钥写入浏览器存储。
- 不要分享启动窗口显示的完整访问地址，其中包含本次运行的临时访问令牌。
- 已保存的密钥不会再次回传到网页；输入框留空会保留原值。
- `SECRET_KEY` 和 `ENCRYPTION_KEY` 用于登录令牌与数据库密文，已有数据后不要随意更换。
- `FIRST_ADMIN_PASSWORD` 只在数据库中还没有管理员时生效。
- 不要把 `config/generated/` 下的文件发送给他人或提交到版本库。
