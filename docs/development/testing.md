# 测试与验收

测试采用“业务域 × 测试层级”组织。迁移期仍有部分旧目录，新增测试应遵循
目标层级。

## 后端

```bash
cd backend
uv sync --frozen --group dev
uv run pytest -q --disable-warnings --ignore tests/v2/perf
uv run pytest -q --disable-warnings tests/v2/perf
```

性能测试当前是信息性门禁；涉及性能路径时必须解释阈值和结果。新增或删除测试
后，需要全量执行一次 `uv run pytest -q --disable-warnings --ignore tests/v2/perf
--store-durations --clean-durations`，提交更新后的 `.test_durations`，保证 CI
分片时长表覆盖全部收集项。

### 本体查询与动作的 PostgreSQL 验收

SQLite 合同测试不能证明 PostgreSQL 的 JSON 表达式、事务快照、锁与并发唯一
约束可用。以下脚本只在显式开启的隔离数据库上运行，各自创建随机命名的临时
schema，并在退出时清理自己的数据；不得指向生产数据库：

```bash
cd backend
export ENVIRONMENT=test
export OPENONTOLOGY_ONTOLOGY_ASSISTANT_E2E=1
export DATABASE_URL=postgresql://ontoprompt:ontoprompt@127.0.0.1:25432/ontoprompt
uv run python scripts/ontology_assistant_postgres_e2e.py \
  --report ../.artifacts/ontology-assistant/query-postgres.json
uv run python scripts/ontology_action_confirmation_postgres_e2e.py \
  --report ../.artifacts/ontology-assistant/action-postgres.json
```

查询验收覆盖发布结构、同本体和跨本体关联、派生字段、稳定分页、实际事务隔离
与普通数据库角色的资源预算，并记录 SQL 数、结果大小与耗时。动作验收覆盖
并发确认、单次领取和外部结果不确定时的持久保护。PR 和自动部署 CI 都执行
这两项，并将脱敏报告上传到 `ontology-assistant-postgres-<run_id>` artifact。
数据库迁移、前端真实后端旅程和外部连接器验收仍需分别执行，不能用这些脚本
替代完整门禁。

## 本地配置中心

```bash
cd config
uv sync --frozen --group dev
uv run pytest -q
```

## 前端

```bash
cd frontend
npm ci
npm run test:unit
npm run check:feature-boundaries
npm run check:component-convergence
npm run check:color-tokens
npm run test:e2e:classification
npm run check:e2e-assertions
npm run lint
npm run build
npm run test:e2e:mocked
```

`test:unit` 使用 Node.js 22 内置 test runner 和原生 TypeScript strip-types，
覆盖无 DOM、无网络依赖的业务纯逻辑；新增纯函数必须优先放入
`frontend/src/test/unit/<domain>/`。浏览器测试采用显式 allowlist：

- `test:e2e:mocked`：完全离线 spec；配置会把后端地址指向不可达端口；
- `test:e2e:stack`：需要隔离 OpenOntology 后端的 spec；
- `test:e2e:external`：需要显式开关及真实 LLM/外部服务的 spec；
- `test:e2e:classification`：保证全部 spec 恰好属于一组。

Playwright 默认不会复用已有 Vite 服务，避免多个 worktree 共用 5173 时把旧
bundle 当成当前源码。并行运行时请为每个 worktree 指定独立端口，例如
`PLAYWRIGHT_PORT=5200 npm run test:e2e:mocked`；只有确认服务确实属于当前
worktree 时，才显式设置 `PLAYWRIGHT_REUSE_SERVER=1`。

各组收录数量以 `frontend/playwright.*.config.ts` 的 testMatch 为准，文档不
维护会随迭代腐烂的计数。

新增测试必须先分类。不能通过文件名含 `real`、grep 排除或“默认 skip”来假装
完成分类。`stack` 和 `external` 只在隔离环境执行；external 所需开关与秘密
由对应 spec 和运行手册明确提供。截图、trace、video 和 HTML 报告写入
`.artifacts/playwright/` 或 CI artifact。

### 像素尺寸断言规范

对 boundingBox 的宽高/坐标做「尺寸/位置不变」类断言时，禁止 `toBe/toEqual`
精确相等：dvh/rem 推导的分数像素在两次布局间存在浮点舍入差（CI Linux 实测
~2e-5px，先例 6596ffa7 的 590.390625 vs 590.3906478），精确相等会间歇性
误报。宽高统一使用 `src/test/e2e/support/geometry.ts` 的
`expectSameBoundingBox`，坐标用 `toBeCloseTo`（亚像素容差）；
`npm run check:e2e-assertions` 在本地与 CI 强制拦截违例写法。

### 隔离 E2E 栈（本地真实后端）

`stack` 套件的本地执行方式：`docker-compose.e2e.yml` 提供五个错端口的基础
服务（db/redis/nats/neo4j/minio，端口避开本机常用的 5432/6379/9000），
后端在宿主机以 `dev_server` 起在 8000，前端由 Playwright 的 webServer 自行
拉起并经 Vite 代理指向 8000。以下五服务栈不包含 n8n 与 Chromium CDP；开发
环境允许 n8n 暂不可达时启动 API，但 `/health/ready` 仍会返回失败。完整就绪
验收必须另外配置隔离的真实 n8n 和 CDP，不能把进程存活当成依赖就绪：

```bash
docker compose -f docker-compose.e2e.yml up -d --wait
cd backend && env \
  ENVIRONMENT=development \
  SECRET_KEY=e2e-secret-key-0123456789abcdef0123456789abcdef \
  FIRST_ADMIN_USER=admin FIRST_ADMIN_PASSWORD=admin123 \
  DATABASE_URL=postgresql://ontoprompt:ontoprompt@127.0.0.1:15432/ontoprompt \
  REDIS_URL=redis://:ontopromptredis123@127.0.0.1:16379/0 \
  NATS_URL=nats://127.0.0.1:14222 \
  NEO4J_URI=bolt://127.0.0.1:17687 NEO4J_USER=neo4j NEO4J_PASSWORD=ontoprompt123 \
  MINIO_ENDPOINT=127.0.0.1:19000 MINIO_ACCESS_KEY=minioadmin MINIO_SECRET_KEY=minioadmin \
  N8N_API_URL=http://127.0.0.1:5678 N8N_API_KEY=e2e-dummy-n8n-key \
  uv run python -m app.dev_server
cd ../frontend && npx playwright test --config=playwright.stack.config.ts src/test/e2e/<受影响spec>
```

用完清理：`pkill -f app.dev_server` 终止后端，
`docker compose -f docker-compose.e2e.yml down -v` 连卷销毁数据。首次在新库
上启动时 `dev_server` 会自动执行 `alembic upgrade head` 并 seed 管理员
（admin / 上述 FIRST_ADMIN_PASSWORD）。

当前没有引入 Vitest/React Testing Library。需要 DOM 的组件行为继续进入
Playwright；这不是跳过纯逻辑单元测试的理由。

早期 `frontend/scripts/` 手工场景已在零消费者审计后删除。不要恢复截图驱动
或直接修改持久数据的一次性脚本；正式浏览器场景统一新增到
`frontend/src/test/e2e/`。

### 双源集成数据集：隔离环境与容量验收

双源容量验收使用独立的 [Compose](../../docker-compose.integrated-e2e.yml) 与
[环境脚本](../../scripts/e2e/integrated_stack.py)，不复用其他 worktree 的数据库、
端口、容器或个人配置。环境脚本只使用本地镜像，显式禁用 Compose dotenv；
镜像缺失直接失败，不会隐式拉取或降级成 SQLite/mock。所有发布端口仅绑定
`127.0.0.1`，默认项目为 `oo-integrated-20261024`：

| 服务 | 端口 | smoke 内存上限 |
|---|---|---|
| 元数据库 PostgreSQL | 45432 | 512MiB |
| 源 PostgreSQL | 45433 | 384MiB |
| 源 MySQL | 43306 | 512MiB |
| Redis / NATS | 46379 / 44222、48222 | 各 96MiB |
| Neo4j HTTP / Bolt | 47474 / 47687 | 1280MiB |
| MinIO API / Console | 49000 / 49001 | 256MiB |

API/Web 约定使用 48004/45173；n8n/CDP 若另行启用使用 45678/49222。
Compose 不包含 API、Web、n8n 或 CDP；基础服务健康不等于完整 readiness。
`/health/ready` 缺依赖或返回 503 时必须如实记录，不能以 liveness 代替。

先取得协调者对端口、资源预算及测试时隙的批准。完整后端/前端门禁由协调者
串行安排，不与容量试验并发。缺失镜像、依赖或固定版 dbx 的下载/安装必须
单独获准；不得停止他人容器、扩大 Docker VM、全局 prune 或复用生产连接。

```bash
# 在仓库根目录运行；脚本仅依赖 Python 标准库和本地 Docker CLI。
python3 scripts/e2e/integrated_stack.py preflight db source-pg
python3 scripts/e2e/integrated_stack.py up db source-pg
python3 scripts/e2e/integrated_stack.py capacity
python3 scripts/e2e/integrated_stack.py seed --source postgres --rows 500
python3 scripts/e2e/integrated_stack.py oracle --source postgres --rows 500
python3 scripts/e2e/integrated_stack.py seed --source postgres --rows 10000 --extend
python3 scripts/e2e/integrated_stack.py oracle --source postgres --rows 10000

# 只有确认资源余量且本地镜像存在时，才逐步启动其余服务。
python3 scripts/e2e/integrated_stack.py up redis nats minio neo4j
python3 scripts/e2e/integrated_stack.py up mysql
python3 scripts/e2e/integrated_stack.py seed --source mysql --rows 500
python3 scripts/e2e/integrated_stack.py oracle --source mysql --rows 500
python3 scripts/e2e/integrated_stack.py status

# 结束后只清理 ledger 中、本项目标签匹配的自有资源；销毁数据需显式开关。
python3 scripts/e2e/integrated_stack.py down --destroy-data
```

资源 ledger、源 oracle、SHA256、容量测量均位于 `.artifacts/integrated-e2e/`，
不得提交到 Git。环境脚本不启动宿主服务；另外启动的 API、Web、浏览器与测试
runner 必须记录 job ID、PID、命令、端口并精确清理。不得用宽泛的 `pkill`。
脚本会拒绝接管未记账的同名项目容器，启动前复查端口，失败启动也记录部分
成功创建的容器，便于按所有权清理。

造数固定为 **47 列**：稳定 bigint PK、超过 JavaScript 安全整数范围的关联键、
整数金额、布尔、时间文本、来源标记、含 Unicode/NULL 的备注、40 个 payload
字段。每批有界，不落全量 CSV；oracle 对全表 47 列逐值验证并记录 ordered
SHA256、精确 count/distinct、主键范围及金额总和。seed/source-oracle 成功仅证明
源数据正确，报告明确 `full_e2e_pass=false`，不能作为页面或映射通过证据。

最终两个规模必须分别执行 **2300000×47** 与 **10000000×47**，MySQL 和
PostgreSQL 双源、全部 47 字段完整映射；500/10000 行只用于 smoke/容量校准，
不减少最终行数或映射属性数。每个规模必须保留以下真实末端证据：

1. 浏览器页面创建/选择双源集成资产，输入 SPL 文本，精确统计/跨源 join 与
   源库 oracle 一致；覆盖 NULL、fan-out、bigint/decimal、排序分页，区分预算
   拒绝、partial 与分页，并如实标记跨源非原子快照。
2. 页面 PK CRUD 的持久结果通过源库核对；覆盖权限拒绝、冲突、幂等重试、
   事务回滚、断连、schema 漂移和恢复，不以 toast/HTTP 200 代替最终结果。
3. 完整源游标耗尽后才发布不可变版本，验证行数/主键/47 列 checksum/manifest；
   再走 draft → trial/impact → promote 正门，不直写对象或版本运行状态。
4. SQL Entity/ObjectInstance/property facts 和 mapping 的已应用源版本一致，
   Neo4j `projection_status=ready` 且节点/边计数、稳定 ID、末行属性一致。
5. Sentinel 全量 scanned/命中差分无截断，事件与动作持久结果一致；真实助手
   聚合 count 对齐全量且 `partial=false`。缺 LLM 配置或真实 dbx 均单列未通过，
   不伪造消息/调用或将 native connector 当作 dbx 验收。

容量判定取 **Host 实际可用空间与 Docker VM 逻辑空间中较小者**；稀疏虚拟磁盘
的较大 `df` 不能代表 Host 可分配容量。先以小规模正式映射测
`pg_total_relation_size`、facts/对象与 Neo4j 实际增量，连同 WAL、索引、版本、
trial 和重试暂存估算最终空间，保留停止水位。47 属性加 presence 可产生每对象
48 条 facts，不能仅按源 CSV 大小估算；内存须记录 RSS/容器峰值、OOM 与耗时，
串行执行，超预算停止本次自有任务并报告，不以缩样本冒充最终通过。

统一查询的本体 binding 除 `data` 菜单外还须具备 `agent`
菜单；数据集/集成源 binding 仍按 `data.structured` 检查，不能以数据菜单绕过
原本体助手权限。游标每次使用都重新检查授权与发布范围。

PostgreSQL 助手精确检索/聚合须检查完整授权发布范围的声明类型，拒绝非空非法
数值、非整数的 integer 值与非法 boolean，不能将它们转成 NULL 后忽略并标为
`exact=true`。非法值检查与结果读取必须来自同一 SQL statement 快照；预算或
类型校验失败应明确拒绝，不返回部分统计。隐藏属性不参与检查，避免形成权限
侧信道。SQLite 单测和 SQL 编译断言不能替代真实 PostgreSQL 专项。

`runtime-copy` 子命令只将 allowlist 中的源码复制到 artifact 目录，排除实际
运行 dotenv、生成配置、symlink 与秘密目录，并记录源码 SHA256；不安装依赖、
不启动服务。`--tests` 加入仓库测试及其已跟踪 fixture、配置静态页面和部署合同
依赖。只有已跟踪的公开模板 `.env.example`、`config/defaults.env.example` 与
`deploy/production.dependencies.example.env` 可以保留；实际 `.env` 或
`deploy/production.dependencies.env` 始终排除。manifest 分开记录
`dotenv_copied=false` 与 `public_env_templates_copied`，前者不表示公开模板缺失。
未跟踪文件仅接受代码后缀，不复制任意本地数据。复制后重新核对源 hash，检测
到并发变化则报错，不把混合快照当作有效证据。必须给每次运行不同的 `--name`；
脚本拒绝覆盖既有快照。

```bash
python3 scripts/e2e/integrated_stack.py runtime-copy --name verifier-01 --tests
# 在快照 backend 目录执行；使用原工作树 venv 的绝对可执行路径，不重新 sync。
# 通过工具的 workdir 进入快照，不把 app/config symlink 回原树。
# env -i PATH="$PATH" HOME="$HOME" PYTHONPATH="$PWD" /reviewed/venv/bin/python -m pytest tests/...
```

PostgreSQL 迁移/facts 专项可使用
[probe](../../scripts/e2e/verify_integrated_postgres.py)：在上述副本根目录运行，
用工作树现有 venv 的绝对 Python 路径，`--rows` 仅允许 500 或 10000，
不会直接启动最终大规模测试。

```bash
/reviewed/venv/bin/python scripts/e2e/verify_integrated_postgres.py \
  --rows 10000 --facts --report .artifacts/postgres-migration-facts.json
# 默认 rollback-target=base；旧历史迁移失败必须保留证据，不修改/忽略旧失败。
# 若另需独立验证本功能迁移，可显式指定本功能之前的最后一个 revision：
/reviewed/venv/bin/python scripts/e2e/verify_integrated_postgres.py \
  --rows 10000 --facts --rollback-target 0133_mcp_constrained_flag \
  --report .artifacts/postgres-feature-roundtrip-facts.json
```

专项创建 UUID 数据库，仅在核对精确库名与 ownership comment 后删除；
`full_e2e_pass=false`。0133 专项通过不代表全历史回滚通过。fact 容量通过
production bounded batch API 写入 47 属性+presence，并量测 heap/index/总量、
WAL LSN 增量与按批采样 WAL 目录峰值；release/version lineage 为显式合成 ID，
**不是**正式 mapping/promote。线性外推同时报告双源大小及是否超出 Host 余量，
还须加上湖、trial、Entity、Neo4j 与 WAL/索引构建暂存，超出即停止扩容运行。

真实 native CRUD 与 query/quota 专项分别位于
[native PG test](../../backend/tests/v2/perf/test_integrated_native_pg.py) 和
[query tests](../../backend/tests/v2/perf/test_unified_query/test_pg_executor.py)、
[quota test](../../backend/tests/v2/perf/test_unified_query/test_pg_quota.py)。
[JSON null 专项](../../backend/tests/v2/perf/test_integrated_json_pg.py) 检查真实 PostgreSQL
SQL NULL / JSON literal null、非空 JSON 列、精确 JSON 数字在 native → 正式 sync
封版 → COPY staging 后的 COUNT/IS NULL 与旧 token CAS；
[版本并发/游标专项](../../backend/tests/v2/perf/test_lake_version_concurrency_pg.py)
检查双 session 锁定的历史读取和 named cursor/字节批次边界。
这些都是 service-level 证据，不绕过 dbx 阻塞给 HTTP/浏览器链路计通过。
JSON 源列在传输中声明 `encoding=json_text`，值为未经 parse/re-encode 的原始
JSON 文本字符串；SQL NULL 为 `null`，JSON literal null 为字符串 `"null"`。
类型化 JSON 写入也使用原始文本，不能把普通 JSON 对象当作传输 wrapper。
源端时间写入会按反射出的 PostgreSQL precision / MySQL fsp 检查可表示性；
非零的第七位及以后小数、超出列精度的小数和非零小数时区偏移都会拒绝，
不能依赖 Python 或数据库静默截断/舍入。SPL 的 timestamp/time 比较参数与字面量
同样拒绝这些不可精确表示的时间值；超过六位但仅增加尾零仍可接受。
真实 PG 专项另行核对拒绝后的源行和 token。源端 INSERT/UPDATE/DELETE 只有在驱动
确认单行受影响、且写后原主键存在性符合操作时才记为 applied；触发器抑制写入、
搬移主键或 DELETE 后重插均回滚并记为 `409 SOURCE_WRITE_NOT_APPLIED`。
普通 no-op UPDATE 按 matched-row 语义仍算单行成功；失败回执的幂等重放不再写源。
本体映射到 string 时保留原始文本；raw JSON 映射到 object/array 或
number/integer/float 暂不支持保真语义，必须在 Trial 和 live 持久化前拒绝，
不能有损解析后发布。普通 DECIMAL 源值在查询/湖中保留精确文本，但旧有
Formal number 转换仍使用浮点表示非整数小数，不能宣称任意 DECIMAL 全链路无损；
需要原文精度的属性应声明 string，大整数的整数转换不经过 float。
使用 `INTEGRATED_NATIVE_PG_E2E=1` / `INTEGRATED_QUERY_E2E=1` 显式 opt-in；
后者必须提供 `INTEGRATED_QUERY_PLATFORM_DSN`、`INTEGRATED_QUERY_POSTGRES_DSN`
（以及运行 `[mysql]` case 时的 `INTEGRATED_QUERY_MYSQL_DSN`），并验证为上表
隔离端口及 fixture 库。`-k 'not mysql'` 仍会运行真正 PostgreSQL COPY staging、
独立事务 join、精度/NULL/Unicode、预算回滚、稳定分页与 SSI quota，
但不能冒充 MySQL 跨方言覆盖。

[HTTP runner](../../backend/scripts/integrated_datasets_e2e.py) 与
[真实浏览器 suite](../../frontend/src/test/e2e/integrated_datasets_real.spec.ts)
只有在 genuine pinned dbx、双源资产、API/Web 和浏览器具备时才能运行。
`POST /api/v2/integrated-datasets/{asset}/sync` 返回 202 与 `run_id`，
必须轮询 `/sync-runs/{run_id}` 到 `sealed && complete && version_id` 才验证
不可变版本；queued/loading/failed 都不是完成。浏览器 suite 只用共享
`stack-credentials`，不内嵌账号密码；其 9 次以内 SPL 避免在单次 smoke 中主动
超出 10 个 active snapshot quota。缺 dbx transport 时应得到 `DBX_NOT_CONFIGURED`，
不得从 native reflection 回填其可用状态。
当前同步仅为手动、单次 repeatable-read 的完整扫描，不提供 CDC、水位或源发现。
starting/loading 为单一 CAS owner 的私有状态：owner 在 `snapshot_json` 写入
`integrated-owner-lease-v1` 租约（token/actor/expires_at），执行线程每 20s 续约一次；
`row_count`/`__row_seq` 不是源端续传游标，任何失效 RR 快照都必须新 run 从头全量扫描。

JetStream 重复投递/ACK 丢失时：`queued` 仍走 CAS；`sealed`/`failed` 只返回 durable
receipt（已在 failed 且带恢复审计时标记 idempotent）；`starting`/`loading` 若 owner
租约仍存活，worker 抛出的重投异常会按剩余租约 NAK 延迟重投，绝不接管活 worker；
只有租约过期才由重复投递用 CAS 转 `failed`、仅删除经 `lake_iv_<uuid(run.id)>`
双重校验的私有表，并在同一事务写入 `v2_integrated_sync_recoveries` 审计
（run/原 owner/证明方式/操作者/表名/是否删除），重复调用返回同一 durable receipt。
无租约或无法证明 owner 已死时保持原状并报告 unverifiable，不猜 cursor、不盲删。
操作者入口（不新增路由/生命周期依赖）：

```bash
cd backend
python -m app.data_channel.sync_tasks.integrated_sync recover \
  --run-id <uuid> --operator ops:oncall --reason "worker SIGKILL proven" \
  [--worker-proof '{"kind":"worker_terminated","reference":"pid:...","observed_at":"...","owner_token":"..."}']
python -m app.data_channel.sync_tasks.integrated_sync sweep --operator ops:oncall
```

`sweep` 只回收已过期租约，`recover` 在活租约下必须提供与当前 owner_token 匹配的
operator 证据（审计 ownership_method=operator_evidence）。任何回收后的重试都必须
新 run_id、新私有表、新 RR 快照并重新完成 EOF/count/schema/checksum 校验。
[集合同步故障注入测试](../../backend/tests/v2/datasets/test_integrated_sync.py)
用真实子进程 SIGKILL（只 kill 自己 Popen 的 PID，tmp SQLite，不启停容器/端口）覆盖
“杀 worker→残留 loading/私有表→重复投递回收→幂等→新 run 全量重试”以及
“活租约拒绝/NAK→operator 证据才可回收”。该测试证明的是恢复契约，不替代
t6 的真实双源/规模验收。

链接类型的业务属性名 `exists` 为链接存在性事实保留，canonical 定义校验与
Trial/Promote 校验必须拒绝，不能通过忽略该属性掩盖事实冲突。对于同一关系类型、
相同端点数据集对的多个映射，当前冻结模型没有逐边 producing-mapping 血缘；
即便边的实际配对互不相交，只要候选映射的冻结版本不同，Promote 也必须以
HTTP 409 `link_lineage_ambiguous` 安全拒绝，不能任意选版本或宣称完整多映射支持。

运行 API/Vite 前须确保配置根位于该无个人配置的副本，而不只是 `env -i`：后端
和 Vite 的默认加载器仍会主动读取工作树 dotenv。业务源码变化后必须生成新的
经过校验的副本，不能验收旧 bundle。完整门禁仍针对当前工作树执行；若使用
快照跑完整门禁，协调者必须核对 manifest 与最终 diff、回收 `.test_durations`
等要求的产物，并标明实际执行目录与配置隔离方式。

## 文档与仓库结构

```bash
node scripts/ci/check-markdown-links.mjs --self-test
node scripts/ci/check-markdown-links.mjs
bash scripts/ci/check-repository-hygiene.sh
git diff --check
```

链接检查同时验证活跃文档能从 `docs/README.md` 到达，以及需求、ADR、迭代
记录是否进入各自索引；仓库卫生检查验证主要目录 README、唯一锁文件、过程
产物和个人路径边界。前端 feature boundary 还会检查生产模块从 `main.tsx`
可达、没有循环依赖，并只允许 ADR 登记的兼容入口不可达。

## 迁移专项证据

目录或导入路径变化必须额外保存：

- 变更前后测试收集数量；
- OpenAPI/路由/RBAC diff；
- Alembic 单 head、新库升级和现存库副本升级；
- Celery task registry；
- Docker Compose config 与生产镜像 build；
- 受影响导航的真实浏览器旅程；
- staging 健康检查与回滚演练。

完整强制矩阵见仓库根目录 [AGENTS.md](../../AGENTS.md)。

## 必需依赖验收

正常启动的 PostgreSQL、Redis/Celery worker、Neo4j、MinIO 和 n8n 必须在隔离
真实环境验收；Chromium CDP 也必须用真实服务验证完整 readiness，但其连通失败
不应终止 API 进程。`ENVIRONMENT=test` 下的 SQLite、mock broker、临时对象目录
或假的 CDP/n8n 响应只证明确定性契约，不证明真实服务 ready。

必须覆盖失败关闭行为：数据库不切 SQLite、入队失败不切 API 线程、图失败不切
NetworkX/SQL、对象写入失败不切本地目录。关键词搜索应验证 PostgreSQL 后端；
语义及统一 semantic 模式应验证 `501 semantic_search_unsupported`。LLM 独立于
启动门禁，在模型配置页完成配置后再运行对应 external E2E。

## 变更类型矩阵

| 变更类型 | 强制门禁 | 额外证据 |
|---|---|---|
| 仅文档 | Markdown 自测/链接检查、仓库卫生、`git diff --check` | 无源码测试 |
| 后端功能/重构 | 受影响 pytest、后端完整回归 | API/RBAC/异步专项 |
| 配置中心 | 配置中心完整回归 | 后端本地配置集成 |
| 前端功能 | unit、feature boundary、分类、lint、build、mocked E2E | 受影响 stack 旅程 |
| 路由/导航/RBAC | 后端完整回归、lint/build、mocked | 多角色真实浏览器验证 |
| Alembic/ORM | 后端完整回归、单 head、新库升级 | 现存库副本升级 |
| Compose/Actions/部署脚本 | infra 测试、前端门禁、生产镜像 | staging 与回滚 |
| 目录迁移 | 所有相关完整门禁 | 收集数、路由、任务注册前后对比 |
| 外部服务协议 | mocked 回归仍执行 | 对应 external/live 验收 |

真实链路脚本按变更选择：

- API Hub：`backend/scripts/api_hub_http_proxy_live_e2e.py`；
- 数据管家浏览器：`backend/scripts/steward_companion_e2e.py`；
- n8n/LLM：`backend/scripts/steward_live_e2e.py`、
  `steward_api_hub_live_e2e.py`；
- Chromium CDP：`backend/scripts/steward_companion_e2e.py`；
- 探索真实 LLM：`backend/scripts/exploration_live_e2e.py`；
- 文件网关/MinIO：`backend/scripts/steward_file_asset_live_e2e.py`；
- Sentinel：`scripts/data/run_sentinel_real_data_e2e.py`；
- Sentinel CEP（事件日志/时间算子/序列缺失/窗口聚合）：`scripts/data/run_sentinel_cep_e2e.py`（须在隔离 staging 运行，直写实例在 production 环境被拒绝）；
- MinIO：`backend/tests/object_storage/test_minio_live.py`。
