# GitHub Actions 自动部署

## 触发边界

当前生产部署由 `.github/workflows/deploy-nano-ontoprompt.yml` 管理：

- push 到 `nano-ontoprompt`；
- 手工 `workflow_dispatch`。只有已证明所有持久存储为空的首次安装，才勾选
  `bootstrap_production_env`；普通发布保持未勾选。

目录治理和功能开发必须通过功能分支与 PR，不得直接向该分支试错。

## 当前流程

改动先经 `changes` 作业分类：仅含 `docs/` 与根目录 Markdown 的推送不进入
部署归档，因此跳过代码验证与部署（文档/卫生门禁照常运行）；其余推送执行
完整流程，手动触发（workflow_dispatch）始终全量：

1. 使用 Python 3.12 和各自 `uv.lock` 并行执行后端（pytest-xdist 多进程 +
   pytest-split 按已记录时长均衡分片，分片数以该 workflow 的 matrix 定义
   为准，时长数据见 `backend/.test_durations`；测试环境使用最低 bcrypt
   轮次）与配置中心回归；
2. 运行 Alembic 新库升级与单 head 检查；
3. 使用从 Repository secrets/variables 物化的生产依赖清单验证
   PostgreSQL、Redis、Neo4j、MinIO、n8n 和 Chromium CDP 配置；
4. 执行文档链接、目录索引与仓库卫生守卫；
5. 执行前端单元测试、feature boundary、E2E 分类、lint、生产构建和离线
   Playwright 回归；通过门禁的 `frontend/dist` 作为本次部署的静态产物
   上传；
6. 并行验证生产镜像可构建；前端镜像为预构建形态（`Dockerfile.prod` 直接
   打包上一步的 `dist`），服务器端不再执行 npm/vite 构建——本地手动构建
   前端镜像前需先在 `frontend/` 下执行 `npm ci && npm run build`；
7. 将本次作业扫描到的 SSH host key 写入 `known_hosts` 并强制
   校验，同时在任何远端命令前校验部署目录；
8. 在 runner 上从 `PROD_*` Repository secrets/variables 物化
   `deploy/production.dependencies.env` 作为受控部署输入；
9. 通过受测试的运行时白名单生成部署包（含 CI 构建的 `frontend/dist`）并
   先上传；远端替换源码时始终原地保留
   服务器 `.env`，不把秘密复制到固定 `/tmp` 文件；
10. 服务器部署入口再次校验目录；旧安装如仍使用示例运行密钥，先执行不改密文
   的密钥解耦，再完成依赖探测、停止旧 backend、数据库迁移和
   Compose 启动；
11. 检查 API 深度 readiness、pipeline executor、PostgreSQL、
   Redis、Neo4j、MinIO、n8n、Chromium CDP 和前端静态资源；
12. 无论成功失败，清理 runner 上的上传压缩包与物化清单。

PR 到 `nano-ontoprompt` 时，独立的 `.github/workflows/ci.yml` 会并行执行
文档/仓库卫生、后端、配置中心和前端门禁，但不会执行部署。

生产编排可能按需叠加观测层：`deploy/deploy-prod.sh` 检测到 `ARMS_LICENSE_KEY`
时自动合并 `agentloop/compose.agentloop.yml`（阿里云 AgentLoop 探针后端），
详见 [agentloop/README.md](../../agentloop/README.md)。

生产 Nginx 对 `/api/`、`/api-hub/` 和 `/proxy/` 使用各自的后端代理契约。
已退役的通用 `/mcp` 与全部 `/mcp/` 子路径（含已移除的外部 MinIO MCP
端点）必须直接返回 404，不能落入 SPA 的 `index.html`。
`scripts/ci/test-deploy-guards.sh` 固定这条生产路由边界。

## 数据库迁移停机边界

生产迁移是一次有意的短暂停机。依赖连接预检通过后，`deploy/deploy-prod.sh`
会先对 `backend` 执行带 30 秒宽限期的停止，再检查 Alembic
单 head 并升级。这样旧版本进程不会在新投影状态迁移期间继续读写数据库。
前端可能仍能提供静态文件，但 API 在迁移和新版本就绪前不可用。

如果迁移图校验或 `alembic upgrade head` 失败，部署会立即失败，并保持 API 与
worker 停止；这是保护数据一致性的预期行为。此时先保留日志、判断迁移是否已
部分执行并按回滚方案处理，不能直接重启不理解新 schema 的旧进程。任何人工或
staging Alembic 升级也必须遵循同一顺序：先停止所有 API/worker 写入者，确认
没有遗留任务执行，再校验 head 和升级，最后才启动与该 schema 兼容的版本。

## 历史运行密钥的无损解耦

旧版本曾允许服务器 `.env` 缺少 `SECRET_KEY`/`ENCRYPTION_KEY`，或继续使用
`.env.example` 的示例 `SECRET_KEY`。在 `ENCRYPTION_KEY` 为空时，存量业务密文
实际上使用旧 `SECRET_KEY` 的派生 Fernet key。只随机替换 `SECRET_KEY` 会让
PostgreSQL 与 API Hub SQLite 中的配置仍存在却无法解密。

当前部署只对缺失键、显式空值和两个仓库已知示例值执行一次幂等转换。缺失键
沿用旧 Settings 默认值，显式空值则按旧 dotenv 覆盖语义从空字符串派生，不能
把二者当成同一状态：

1. 计算旧程序已经在使用的 Fernet key；
2. 在 `.env` 中把它显式固化为 `ENCRYPTION_KEY`；
3. 同一次原子文件替换生成新的随机 `SECRET_KEY`；
4. 将示例 `FIRST_ADMIN_PASSWORD` 改成随机未来 seed，不修改已有用户行；
5. 后续部署原样保留这些值。

该过程不连接或更新 PostgreSQL、Neo4j、MinIO、n8n、Redis、API Hub SQLite，
也不会重写任何密文。旧容器仍使用其启动时环境；新 backend/worker 切换后，旧
登录 JWT 和短期 Pipeline 上传 JWT 才会失效。因此发布窗口应避开在途文件上传，
并通知用户重新登录；持久分享链接和已有管理员密码不变。若服务器使用未知的
自定义短密钥，部署会失败且不猜测派生方式，必须先由维护者审计实际生效配置。

这一步只保证数据完整和部署可恢复，不等于完成历史密文强度升级。由公开示例
派生的 `ENCRYPTION_KEY` 后续仍应通过独立的停写、备份、双密钥或离线迁移进行
轮换；PostgreSQL 与 API Hub SQLite 不能被假设为一个事务，本次部署禁止顺带
执行全量重加密。

## 首次登录与管理员密码恢复

首次部署且服务器尚无 `.env` 时，部署脚本从 `.env.example` 创建持久配置：
初始用户名默认为 `admin`，`SECRET_KEY`、独立 `ENCRYPTION_KEY` 和
`FIRST_ADMIN_PASSWORD` 会在服务器上随机生成，`.env` 权限设为 `0600`，值不会
出现在 Actions 日志。上传新版本时，工作流在替换源码期间始终跳过现有
`.env`，因此正常重部署不会重新生成或从临时目录恢复这些值。

这个 bootstrap 不是“看到文件不存在就自动执行”。先停止并盘点目标环境，证明
PostgreSQL、API Hub `api_hub_data`、Neo4j、MinIO、uploads 和其他平台持久数据
均为空，再在 GitHub Actions 中手工运行 `Deploy nano-ontoprompt`，勾选
`bootstrap_production_env`。push 触发和未勾选的手工运行固定传入 0；如果
`.env` 缺失，部署会在生成任何新 key 前失败。已有数据但 `.env` 丢失时必须恢复
原 `.env`，绝不能通过勾选该选项“修复”部署。

`.env` 必须是应用目录内的普通服务器文件。部署会拒绝任何有效或失效软链接，
并且会在远端删除任何旧源码前完成该检查，不会删除位于应用目录内的链接目标，
也不会把 secret mount 替换成普通文件；外部秘密管理器需要先以受控、原子方式
物化完整的 `0600` 文件。直接运行 `deploy/deploy-prod.sh` 的 Git 模式也只会接管
不存在或空目录，并在任何 fetch/checkout/reset 前拒绝 `.env` 软链接；遇到没有
`.git` 的非空目录会保留全部内容并失败，不再递归删除。
部署目录自身也不能是软链接，远端源码替换和脚本都会在清理任何文件前拒绝。

部署包由 `scripts/ci/create-deployment-archive.sh` 构建，只包含生产 Compose、
前后端构建/运行输入、CI 构建的 `frontend/dist` 静态产物、Alembic、容器
初始化资源、部署入口和受控维护脚本。
`docs/`、测试、fixture、前端 E2E 源码及过程产物不会上传；白名单由
`scripts/ci/test-deploy-guards.sh` 在 PR 和部署验证阶段共同锁定。部署包含有受控
依赖清单，因此 runner/远端临时归档及远端解包后的清单均限制为 `0600`；一旦
远端替换脚本启动，Bash trap 会在校验、清理或解包失败时删除该次唯一命名的
临时包。若任务恰好在上传完成后、远端脚本启动前被外部强制终止，受限为 `0600`
的唯一命名包可能暂留 `/tmp`，应按 run id 核对后清理。

首次登录只能在受控的服务器交互终端读取该值。先确认文件权限，再读取所需的
两项；若自定义了 `DEPLOY_APP_DIR`，请替换下列路径。不要把输出复制到聊天、
Issue、PR、工单或 CI 日志：

```bash
cd /opt/openontology
sudo stat -c '%a %U:%G %n' .env
sudo awk -F= '
  $1 == "FIRST_ADMIN_USER" || $1 == "FIRST_ADMIN_PASSWORD" {
    print $1 "=" substr($0, index($0, "=") + 1)
  }
' .env
```

用该随机密码完成首次登录后，应立即在同一个受控终端重置数据库中的密码。
下面通过静默输入避免新密码进入 shell history；用户名如已自定义，替换
`admin`：

```bash
cd /opt/openontology
read -rsp 'New admin password: ' OPENONTOLOGY_NEW_ADMIN_PASSWORD
printf '\n'
sudo docker compose -f docker-compose.prod.yml exec -T \
  -e PYTHONPATH=/app backend \
  python - --user admin --password "${OPENONTOLOGY_NEW_ADMIN_PASSWORD}" \
  < backend/scripts/maintenance/reset_admin_password.py
unset OPENONTOLOGY_NEW_ADMIN_PASSWORD
```

生产 backend 镜像按 `.dockerignore` 不携带人工维护脚本；上述命令从服务器源码
目录把受版本控制的脚本送入正在运行的容器标准输入，并显式使用容器内
`/app` 代码。

该维护脚本目前通过命令参数接收密码，运行期间同机特权用户可能观察进程参数；
因此只能在访问受控的服务器上交互执行，不得封装到 Actions 或把命令输出保存
为 artifact。若初始随机值丢失，无需读取或修改 `.env` 来“同步密码”，直接用
同一维护命令重置目标账号即可。`FIRST_ADMIN_PASSWORD` 只是数据库中没有任何
admin 时的 seed 输入，编辑它不会修改已经存在的管理员密码。

## 当前已知缺口

- 发布前 verify 与 deploy 仍在同一个 workflow，避免绕过发布门禁；
- Actions 构建的镜像没有作为不可变产物推送，backend/browser 镜像仍由
  服务器再次构建（前端静态产物已由 CI 构建并随部署包直传）；
- 服务器部署会替换应用目录；
- 健康检查失败尚无自动镜像回滚；
- SSH 仍使用密码方式；
- SSH host key 在每次临时 runner 上通过 `ssh-keyscan` 建立信任并在后续命令
  强制校验，但尚未用受保护 Secret 固定预期 fingerprint，仍属于 TOFU；
- Playwright `stack`/`external` 真实栈验收尚未成为自动发布门禁。

这些缺口在目录移动前必须逐步关闭。生产部署最终应只消费 CI 构建并签名/标记
的不可变镜像。

## Contract migration 的额外失败恢复边界

上一节的停机顺序适用于全部生产迁移。会删除或收紧旧应用仍在使用的 schema
的 Alembic 迁移属于 contract migration；此类部署还必须记录升级前正在运行的
backend 和 frontend，且不得在旧 API 仍可访问数据库时
删表。

失败恢复同时以迁移是否到达 head、数据库 revision 是否变化为界：

- 迁移失败、未到达 head，且数据库 revision 与停机前记录完全相同时，才只
  重启本次部署前确实在运行的旧服务；
- revision 无法读取或已经变化（即使还没到 head）时，拒绝自动重启旧服务，
  避免旧二进制连接部分升级的 schema；
- 迁移已成功，但新服务启动或健康检查失败时，禁止把旧应用接到新
  schema。部署脚本会停止任何已部分启动的新版 runtime；必须保持停机，
  恢复部署前同一时间点的数据库与文件备份并部署旧镜像，或执行经批准的
  前向修复。

脚本自动恢复不可代替备份与恢复演练。对删表迁移，Alembic downgrade 只恢复
空 schema，不恢复已删行或物理文件。

## 部署前检查

- `PROD_*` Repository secrets/variables 齐全，物化出的生产依赖清单通过只读
  配置校验；
- 服务器 `.env` 是可恢复的普通 `0600` 文件，而不是软链接；
- 非首次安装确认服务器原 `.env` 可恢复；只有已证明全部持久存储为空的首次安装
  才手工勾选 `bootstrap_production_env`；
- `DATABASE_URL` 的持久运行值为 `postgresql://`；旧清单里的 `postgres://` 或
  `postgresql+psycopg2://` 只由部署入口在写入 `.env` 时一次性规范化；
- 若 `REDIS_URL` 指向 Compose 服务 `redis:6379`，确认它使用未转义的 16 位以上
  密码；部署会从该 URL 同步 Redis `requirepass`。精确旧地址
  `redis://redis:6379/0` 会自动迁移，外部 Redis URL 不会被改写；
- 既有 `DEPLOY_HOST`、`DEPLOY_USER`、`DEPLOY_PASSWORD`、
  `DEPLOY_APP_DIR`、`DEPLOY_HEALTH_URL` Repository Secrets 可用；
- `DEPLOY_APP_DIR` 通过 `bash scripts/ci/validate-deploy-app-dir.sh
  "${DEPLOY_APP_DIR:-/opt/openontology}"`；
- 如启用 `STRICT_IMAGE_DIGESTS`，最终服务器 `.env` 的全部镜像引用均已固定
  到不可变 `@sha256`；
- 服务器 shell 中没有被当作临时部署配置的同名依赖、端口或镜像变量；部署入口
  会隔离这些变量，配置变更应落到持久 `.env`/受控清单后重新校验；
- 数据库备份完成且恢复演练有效；
- Alembic 新库与现存库副本升级通过；
- staging 与关键真实 E2E 通过；
- 必需依赖全部真实就绪，且失败演练没有切换 SQLite、API 线程任务、内存/SQL
  图或本地对象存储；
- PostgreSQL 关键词搜索通过，语义及统一 semantic 模式返回预期 501；
- 上一版本镜像、Compose 配置和回滚负责人已确认。

LLM 不属于部署启动门禁。基础平台发布成功后，再由管理员在模型配置页添加并
测试所需提供商；不得为了通过平台 readiness 预置或回显真实模型凭据。

配置清单见 [配置说明](./configuration.md)，回滚见 [回滚说明](./rollback.md)。
