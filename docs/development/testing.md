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

性能测试当前是信息性门禁；涉及性能路径时必须解释阈值和结果。

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
npm run test:e2e:classification
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

各组收录数量以 `frontend/playwright.*.config.ts` 的 testMatch 为准，文档不
维护会随迭代腐烂的计数。

新增测试必须先分类。不能通过文件名含 `real`、grep 排除或“默认 skip”来假装
完成分类。`stack` 和 `external` 只在隔离环境执行；external 所需开关与秘密
由对应 spec 和运行手册明确提供。截图、trace、video 和 HTML 报告写入
`.artifacts/playwright/` 或 CI artifact。

当前没有引入 Vitest/React Testing Library。需要 DOM 的组件行为继续进入
Playwright；这不是跳过纯逻辑单元测试的理由。

早期 `frontend/scripts/` 手工场景已在零消费者审计后删除。不要恢复截图驱动
或直接修改持久数据的一次性脚本；正式浏览器场景统一新增到
`frontend/src/test/e2e/`。

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
