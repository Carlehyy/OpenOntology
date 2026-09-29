# 后端操作与真实链路脚本

本目录只保存需要操作者显式执行的后端工具，不是应用启动入口，也不会被普通
pytest 自动执行。默认从 `backend/` 目录运行：

```bash
uv sync --frozen --group dev
uv run python scripts/<script>.py --help
```

真实外部服务、当前数据库或对象存储参与时，只能使用隔离的 staging/canary
环境。命令、退出码和脱敏报告写入仓库根目录 `.artifacts/manual-e2e/`；不得把
API key、连接串、临时数据库或报告提交到 Git。任何新增、移动或删除的脚本都
必须同步更新本页。

表中临时 SQLite 只服务于隔离脚本或 API Hub，不是平台运行时降级。正常平台
验收仍要求 PostgreSQL、Redis、NATS executor、Neo4j、MinIO、n8n 和 Chromium
CDP 全部就绪。LLM 在平台启动后按脚本需要配置；ChromaDB 不再是任何脚本的
运行依赖。

## 真实链路验收

| 入口 | 运行方式与依赖 | 数据/外部影响与清理 |
|---|---|---|
| `api_hub_http_proxy_live_e2e.py` | `uv run python scripts/api_hub_http_proxy_live_e2e.py`；需要访问公开 AI HOT API | 使用临时 API Hub SQLite 和本机随机端口，只读取外部公开 API；临时目录自动删除 |
| `exploration_live_e2e.py` | 受保护环境提供 `DEEPSEEK_API_KEY`、`DEEPSEEK_BASE_URL`、`DEEPSEEK_MODEL` 后运行 `uv run python scripts/exploration_live_e2e.py` | 调用真实 LLM、会产生费用；使用临时 SQLite/上传目录并在退出前扫描密钥落盘，目录自动删除 |
| `runtime_secret_split_postgres_e2e.py` | `OPENONTOLOGY_RUNTIME_SECRET_E2E=1 ENVIRONMENT=test DATABASE_URL=<loopback-*_ci-or-*_e2e-url> uv run python scripts/runtime_secret_split_postgres_e2e.py --report ../.artifacts/runtime-secret-migration/report.json`；CI 在真实 PostgreSQL migration 后执行 | 只写 synthetic 密文；显式 E2E 哨兵、test 环境、无 query/fragment 的 loopback URL 与 `_ci`/`_e2e` 库名四重约束；PostgreSQL fixture 始终事务回滚，API Hub SQLite 与临时部署目录自动删除；0600 报告只含固定状态和计数，不含连接串、密钥、密文或哈希 |
| `steward_companion_e2e.py` | `uv run python scripts/steward_companion_e2e.py`；需要 Node.js | 仅启动 loopback CDP/WebSocket fixture 与真实 companion 进程，不访问外部站点、不写业务库 |
| `steward_context_live_e2e.py` | `uv run python scripts/steward_context_live_e2e.py`；API key 由 TTY `getpass` 读取，可通过参数选择 DeepSeek model/token 预算 | 调用真实 DeepSeek、会产生费用；使用临时 SQLite/目录，退出前验证密钥未落盘并自动删除 |
| `steward_live_e2e.py` | `uv run python scripts/steward_live_e2e.py --n8n-api-url <url> --llm-api-base <url>`；`N8N_API_KEY`、`LLM_API_KEY` 从环境或 TTY 读取 | 调用真实 n8n、LLM 和数据源，创建唯一远程 workflow；`finally` 删除 workflow 和本地临时目录。仅排障时使用 `--keep-temp`，之后人工清理 |
| `steward_proxy_live_e2e.py` | `uv run python scripts/steward_proxy_live_e2e.py --n8n-api-url <url>`；需要 `N8N_API_KEY`、Node/npm、`npx localtunnel` 和公开 AI HOT API | 创建临时 API Hub 状态、tunnel 和唯一 n8n workflow；`finally` 删除远程 workflow、停止进程并删除临时目录 |
| `steward_api_hub_live_e2e.py` | `uv run python scripts/steward_api_hub_live_e2e.py --n8n-api-url <url> --llm-api-base <url>`；需要 `N8N_API_KEY`、`LLM_API_KEY`、Node/npm 和 `npx localtunnel` | 调用真实 LLM/n8n，创建远程 workflow 与 credential；`finally` 删除两者、停止 tunnel/API 并删除临时目录 |
| `steward_file_asset_live_e2e.py` | 在隔离后端容器内运行 `uv run python scripts/steward_file_asset_live_e2e.py --public-root <temporary-origin>`；依赖当前 `WorkflowConfig`、n8n、MinIO/对象存储和可公开访问的临时 origin | 写当前隔离数据库、对象存储和远程 n8n；脚本要求远程 workflow、本地数据库行、对象与匿名分享全部清理成功，否则返回非零 |

这些脚本即使有自动清理，也不能直接在生产环境试跑。进程被强制终止时，
`finally` 可能来不及完成；执行前记录唯一资源前缀，失败后按 JSON 报告人工核对
n8n workflow/credential、对象存储和数据库残留。

## 确定性演示

| 入口 | 运行方式 | 数据与清理 |
|---|---|---|
| `demos/integrity_demo.py` | `uv run python -m scripts.demos.integrity_demo` | 启动时重建 `/tmp/integrity_demo.db`，验证悬空引用清理；不连接配置中的业务数据库，运行后可删除该临时文件 |
| `demos/sentinel_demo.py` | `uv run python -m scripts.demos.sentinel_demo` | 启动时重建 `/tmp/sentinel_demo.db`，验证手动/变化/扫描三种入口；运行后可删除该临时文件 |
| `demos/sentinel_edge_demo.py` | `uv run python -m scripts.demos.sentinel_edge_demo` | 启动时重建 `/tmp/sentinel_edge_demo.db`，验证进入/持续/离开/重新进入；运行后可删除该临时文件 |

演示脚本用于人工理解和故障定位；发布门禁仍以 `backend/tests/` 中的正式断言为
准。不得把固定 `/tmp` 数据库当作可复用 fixture。

## 维护工具

| 入口 | 默认行为 | 允许写入的条件 |
|---|---|---|
| `maintenance/cleanup_orphan_data.py` | `uv run python scripts/maintenance/cleanup_orphan_data.py` 只列出 `routeC-*` 测试流水线和孤儿数据集 | `--execute` 会删除当前配置数据库中的匹配记录；先备份并在 staging 核对 dry-run、数据库和对象存储 |
| `maintenance/reset_demo_data.py` | `uv run python scripts/maintenance/reset_demo_data.py --keep <ontology-id-prefix>` 只预览保留链路与删除范围 | `--execute` 会批量删除其他本体、流水线、数据集、连接和全部模型配置；禁止在生产环境执行 |
| `maintenance/reset_admin_password.py` | 无 dry-run；`uv run python scripts/maintenance/reset_admin_password.py --user <name> --password <new-password>` 立即更新当前数据库 | 仅用于受控的紧急恢复窗口；参数可能出现在 shell 历史/进程列表，只能使用不持久化明文命令的单用户管理终端，并保留脱敏审计 |
| `maintenance/verify_migration.py` | `uv run python scripts/maintenance/verify_migration.py --v1-db <sqlite> --pg-url <postgres-url>` 对比六张表的行数 | 只读两个数据库；连接串可能含秘密，必须使用临时凭据并只保存脱敏结果 |

## 迁移与受控初始化

| 入口 | 运行方式与依赖 | 写入与回滚 |
|---|---|---|
| `migrations/migrate_v1_to_v2.py` | 先停止 API 和全部 worker，再执行 `uv run python scripts/migrations/migrate_v1_to_v2.py --v1-db <sqlite> --pg-url <postgres-url> --dry-run`；正式模式要求当前 Neo4j 配置真实可用 | 每个本体在同一 PostgreSQL 事务中写项目、Entity、Relation 与 `projecting` 围栏，提交后调用平台统一全量投影；成功才转为 `ready`。投影失败立即停止并返回非零，已提交项目保持非 ready，可在修复 Neo4j 后幂等重跑，或按迁移前备份恢复 |
| `seed_deepseek_models.py` | `uv run python scripts/seed_deepseek_models.py <api-key>` 幂等创建/更新两个 DeepSeek 模型配置 | 写当前数据库；legacy CLI 只能从 argv 接收 key，可能暴露在历史/进程列表。优先使用受控 UI/API，只能在隔离管理终端执行本入口 |

`migrations/migrate_v1_to_v2.py` 仍被
[`tests/v2/migrations/test_migration.py`](../tests/v2/migrations/test_migration.py)
直接验证，不能作为“零引用脚本”删除。生产迁移还必须遵循
[`docs/operations/backup-restore.md`](../../docs/operations/backup-restore.md)和
[`docs/operations/rollback.md`](../../docs/operations/rollback.md)。

正式迁移期间不得让旧版 API/worker 与脚本并发写图：旧版本不了解新版投影围栏，
可能在 PostgreSQL 提交与 Neo4j 重建之间制造不可对账数据。脚本会校验每条 Relation
的 `source`/`target` 都能映射到同一本体内的 Entity ID；无法解析的端点在 SQL
提交前显式失败。迁移报告中的 PostgreSQL 计数代表已经耐久提交的权威数据，
Neo4j 计数仅在统一全量投影成功后增加，不能把非 ready 项目当作迁移成功。

## 已删除的陈旧入口

`demos/seed_financial_risk.py` 含历史个人绝对路径，
`../../scripts/data/seed_graph.py` 则在 import 阶段使用固定本地账号直接写运行中
服务，且没有 dry-run/清理边界；两者均无源码、测试、配置或文档消费者，已
移除。其他脚本即使没有
代码消费者，也因具备明确的人工运维或真实验收契约而保留在本页，不能仅凭
静态零引用批量删除。
