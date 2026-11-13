# GitHub Actions 自动部署

## 触发边界

当前生产部署由 `.github/workflows/deploy-nano-ontoprompt.yml` 管理：

- push 到 `nano-ontoprompt`；
- 手工 `workflow_dispatch`。只有已证明所有持久存储为空的首次安装，才勾选
  `bootstrap_production_env`；普通发布保持未勾选。

目录治理和功能开发必须通过功能分支与 PR，不得直接向该分支试错。

## 当前流程

改动先经 `changes` 作业按家族分类（`scripts/ci/classify-deploy-changes.sh`）。
这是粗粒度、不确定则全量的分流，不是按文件增量跑单测。手工触发、空
diff、未知路径、rename 源路径，以及改到分类器或本工作流时始终全量。文档
与配置中心只跑卫生门禁；纯测试源码跑对应测试但不上线；时长表跑后端回归
但不上线。Compose、`deploy-prod.sh` 和物化脚本与 `.env.example` 一样会跑
后端生产配置回归并部署。只改后端产品代码时仍构建前端 `dist` 并部署，但
跳过离线 Playwright；只改前端产品代码时跳过后端六分片。文档/卫生门禁始终
运行：

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
   上传。生产构建同时烧入 HTTPS 入口（`VITE_HTTPS_UPGRADE_HOST/PORT`，
   取自 `PROD_PUBLIC_TLS_IP` / `PROD_PUBLIC_HTTPS_PORT` 变量，缺省
   64.83.14.139:8443）：SPA 在同主机普通 HTTP 页面启动时自动跳转到该
   入口，解锁麦克风等浏览器安全上下文能力；localhost、局域网 IP 与
   E2E 环境不受影响；直调 HTTP API 的机器客户端不经过 SPA，行为不变；
6. 并行验证生产镜像可构建；前端镜像为预构建形态（`Dockerfile.prod` 直接
   打包上一步的 `dist`），服务器端不再执行 npm/vite 构建——本地手动构建
   前端镜像前需先在 `frontend/` 下执行 `npm ci && npm run build`。前端
   Nginx 以 `frontend/nginx/gzip.conf` 在 http 级启用响应压缩（JSON 接口
   与大静态资源 gzip 直传，SSE 与二进制下载不压缩）；
7. 将本次作业扫描到的 SSH host key 写入 `known_hosts` 并强制
   校验，同时在任何远端命令前校验部署目录；
8. 在 runner 上从 `PROD_*` Repository secrets/variables 物化
   `deploy/production.dependencies.env` 作为受控部署输入；
9. 从精确 `GITHUB_SHA` 生成 `backend/app` 生产源文件 manifest，并与
   verifier 一起打入现有部署包；`EXPECTED_RUNTIME_COMMIT` 独立传入远端，
   不由 manifest 自证；
10. 通过受测试的运行时白名单生成部署包（含 manifest、CI 构建的
    `frontend/dist`）并先上传；远端替换源码时始终原地保留
    服务器 `.env`，不把秘密复制到固定 `/tmp` 文件；
11. 服务器部署入口再次校验目录；旧安装如仍使用示例运行密钥，先执行不改密文
    的密钥解耦，再启动 WeKnora 私网依赖与一次性 init app，完成账号
    bootstrap 后删除 init、启动关闭注册的正式 app 并执行
    `bootstrap --verify`；Kaneo 同样先停用公网网关、在私网启动 kaneo-db 与
    kaneo-app，由 `kaneo-bootstrap` 一次性容器创建管理员并执行
    登录验证；两个原生服务都验证通过后，才做平台依赖探测、停止旧 backend、
    数据库迁移和 Compose 启动；
12. 检查 API 深度 readiness、pipeline executor、PostgreSQL、
    Redis、Neo4j、MinIO、n8n、Chromium CDP、WeKnora 公网网关/注册关闭/
    公开 `/mcp/` 404、Kaneo 公网入口（HTTP 308 精确跳转规范 HTTPS origin、
    HTTPS 网关健康）和前端静态资源；readiness 通过后
    只读比对 active backend 容器 `/app/app` 文件字节、image ID 和
    `StartedAt`，manifest 缺失、路径额外/缺失、字节不同、commit 不符或
    verifier 失败都会使部署非零并沿用既有 stop helper 停止新 runtime；
13. 无论成功失败，清理 runner 上的上传压缩包、物化清单和本次 manifest。

PR 到 `nano-ontoprompt` 时，独立的 `.github/workflows/ci.yml` 会并行执行
文档/仓库卫生、后端、配置中心和前端门禁，但不会执行部署；PR 工作流目前
仍是全量验证，不受上述家族分流影响。

生产编排可能按需叠加观测层：`deploy/deploy-prod.sh` 检测到 `ARMS_LICENSE_KEY`
时自动合并 `agentloop/compose.agentloop.yml`（阿里云 AgentLoop 探针后端），
详见 [agentloop/README.md](../../agentloop/README.md)。

生产 Nginx 对 `/api/`、`/api-hub/` 和 `/proxy/` 使用各自的后端代理契约。
已退役的通用 `/mcp` 与全部 `/mcp/` 子路径（含已移除的外部 MinIO MCP
端点）必须直接返回 404，不能落入 SPA 的 `index.html`。
`scripts/ci/test-deploy-guards.sh` 固定这条生产路由边界。

WeKnora v0.9.0 的公开入口与平台 Nginx 独立：它只暴露原生 Web 网关
`WEKNORA_HTTP_PORT`/`WEKNORA_HTTPS_PORT`（默认 8089/8444），HTTPS 复用
`frontend_ssl_certs`；正式 app、docreader、MCP、ParadeDB 和 Redis 都没有宿主端口。部署脚本先以仅内网、profile 为
`weknora-bootstrap` 的 `weknora-app-init` 完成首次账号注册，删除后
才启动 `DISABLE_REGISTRATION=true` 的正式 app；`bootstrap --verify` 会通过
官方管理员设置 API 持久化 `invite_only`，因此数据库运行时设置不能覆盖环境
关闭开关。公网精确 `/mcp` 与 `/mcp/` 都固定 404；平台通过内网 MCP 使用工具。
公开入口检查除 `/healthz`、`invite_only` 与 MCP 404 外，还要求免登录中继
`/sso/entry` 无票据访问必须 302 回根路径（证明 weknora-gateway → backend
反代与中继路由接通）。`backend` 容器额外持有
`WEKNORA_ADMIN_EMAIL`/`WEKNORA_ADMIN_PASSWORD` 供「领域知识」一键免登录
中继做服务端登录，其余运行时容器仍被掩码。全部
`WEKNORA_*_IMAGE` 已有 `@sha256` 默认值；部署仍在 digest 校验处 fail closed，
防止 tag 漂移。模型与知识库不属于部署输入；管理员登录原生 Web 配置后，
通过超级助手现有 MCP 管理手工接入，详见
[配置流程](./configuration.md#weknora-共享知识库v090)。

Kaneo 项目管理（官方 2.49.0 固定 digest，独立单用户账号与独立 PostgreSQL）
随同一 Compose 启动，部署顺序与 WeKnora 同一原则：先 `compose stop
kaneo-gateway` 关闭公开面，在私网启动 `kaneo-db`/`kaneo-app`，一次性
`kaneo-bootstrap` 容器创建管理员（默认 `admin / admin@kaneo.local`）并执行
登录 `--verify`；验证失败或后续部署失败都会重新关闭 Kaneo 网关，不留半初始
化的公开入口。Kaneo 全程关闭注册（空实例也只允许创建首个管理员），没有
WeKnora 式开放注册的 init 容器；重复部署不修改已存在账号的密码。首次部署
时 `.env` 缺失或仍为本地默认值的 `KANEO_ADMIN_PASSWORD`、`KANEO_DB_PASSWORD`、
`KANEO_AUTH_SECRET` 由部署脚本随机生成并持久化，之后保持稳定，不会回显到
Actions 日志。`KANEO_WEB_URL` 未显式配置时从 `PUBLIC_TLS_IP` 与
`KANEO_HTTPS_PORT` 推导为规范 HTTPS origin。旧服务器 `.env` 缺少
`PUBLIC_TLS_IP` 时，部署脚本从显式传入的 `HEALTH_URL` 提取并校验公网 IP，
持久化后供证书与 Kaneo 共用；已有值保持不变。健康地址使用域名或回环地址、
无法确定公网 IP 时，须先在服务器 `.env` 配置 `PUBLIC_TLS_IP`，否则部署失败。
公开入口检查要求 HTTP 8090 精确 308 跳转该 origin、HTTPS 8445 网关健康，
且免登录中继 `/sso/entry` 无票据访问必须 302 回根路径（证明 kaneo-gateway →
backend 反代与中继路由接通）。`backend` 容器持有
`KANEO_ADMIN_EMAIL`/`KANEO_ADMIN_PASSWORD` 供一键免登录中继做服务端登录，
其余运行时容器仍被掩码。密码获取只能在服务器受控终端读取 `.env`，边界同
[首个管理员密码](#首次登录与管理员密码恢复)。服务与地址契约详见
[配置说明](./configuration.md#kaneo-项目管理2490)。

Hindsight 1.2.0 长期记忆是可选 profile，默认不部署：服务器 `.env` 写
`HINDSIGHT_ENABLED=true` 后，`deploy-prod.sh` 幂等生成
`HINDSIGHT_DB_PASSWORD`/`HINDSIGHT_API_KEY`/`HINDSIGHT_SERVICE_TOKEN`（可选经
`PROD_HINDSIGHT_DB_PASSWORD`/`PROD_HINDSIGHT_API_KEY`/`PROD_HINDSIGHT_SERVICE_TOKEN`
secrets 与 `PROD_HINDSIGHT_IMAGE`/`PROD_HINDSIGHT_POSTGRES_IMAGE` variables
物化，全部可缺省，缺省时由服务器端生成/钉扎），并校验镜像 `@sha256` 钉扎。
核心 compose 调用一律不携带 hindsight profile：可选栈只在核心部署成功之后
（readiness、源码 attestation 与 `deployment succeeded` 已判定，backend 已
就绪，Hindsight 的 LLM 回调目标存在）才由单独的有时间上界的
`--profile hindsight up -d hindsight` 启动（`HINDSIGHT_START_TIMEOUT`，默认
300 秒；本地已有钉扎镜像时不访问 registry，缺镜像时随该调用自拉）。镜像
拉取、缓存卷属主 init、专用 DB 健康、应用启动或超时的任何失败都只降级记忆
特性：停掉本次起了一半的可选容器（`hindsight`/`hindsight-db`/
`hindsight-model-init`，卷保留，下次部署自动重试），绝不停止核心服务或改写
部署结论；启动成功但未在观察窗内健康（首启可能在下载 embedding 模型）则
保持运行并仅记录告警。未启用时自动停掉历史容器（保留卷）。它不参与平台
readiness 与部署成败判定，部署日志仅记录功能健康状态。详见
[配置流程](./configuration.md#hindsight-长期记忆生产默认启用)。

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

0141 迁移完成后、整套新服务启动前，部署会执行
`backend/scripts/maintenance/retire_palace_storage.py --execute`，只清理旧
记忆宫殿专属状态：`<uploads>/super-assistant-palace`、无 `OntologyEntity`
标签的 `PalaceEntity` 节点/索引、`ob:palace:*` Redis 键和四个精确 NATS
subject/durable；各自源缺失时 no-op。脚本不会猜测历史自定义 palace 根目录；
若旧部署使用自定义根，必须由运维在停止旧写入后显式传入 `--palace-root` 与
`--allow-custom-root`，不得把宽泛目录交给自动清理。

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
`.git` 的非空目录会保留全部内容并失败，不再递归删除。该模式还会用
`python3` 从 clean checked-out HEAD 生成 runtime source manifest；缺少
`python3` 时明确失败，不会隐式跳过核验。
部署目录自身也不能是软链接，远端源码替换和脚本都会在清理任何文件前拒绝。

部署包由 `scripts/ci/create-deployment-archive.sh` 构建，只包含生产 Compose、
前后端构建/运行输入、CI 构建的 `frontend/dist` 静态产物、Alembic、容器
初始化资源、部署入口、runtime verifier、exact-HEAD manifest 和受控维护脚本。
`docs/`、测试、fixture、前端 E2E 源码及过程产物不会上传；白名单由
`scripts/ci/test-deploy-guards.sh` 在 PR 和部署验证阶段共同锁定。部署包含有受控
依赖清单，因此 runner/远端临时归档及远端解包后的清单均限制为 `0600`；一旦
远端替换脚本启动，Bash trap 会在校验、清理或解包失败时删除该次唯一命名的
临时包。若任务恰好在上传完成后、远端脚本启动前被外部强制终止，受限为 `0600`
的唯一命名包可能暂留 `/tmp`，应按 run id 核对后清理。

手工 archive 流程必须在构造部署包前显式生成 manifest，并在远端独立传入同一
commit；不得复用任意工作区字节：

```bash
runtime_commit="$(git rev-parse HEAD)"
python3 deploy/runtime_source_attestation.py generate \
  --repo . --commit "$runtime_commit" \
  --output deploy/runtime-source-manifest.json
bash scripts/ci/create-deployment-archive.sh /tmp/openontology.tar.gz
# 远端解包后：
EXPECTED_RUNTIME_COMMIT="$runtime_commit" bash deploy/deploy-prod.sh
```

该核验只声明 active backend 运行容器的 `/app/app` 文件字节与 image identity
绑定（manifest 路径白名单为 `backend/app`），不声称已加载 Python 内存字节；
不新增公开 version 接口，也不改变 Compose 启动/重启顺序。

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

Kaneo 原生页面的管理员凭据同样只在受控终端读取同一 `.env`（把上例 awk 的键
换成 `KANEO_ADMIN_USER`、`KANEO_ADMIN_EMAIL`、`KANEO_ADMIN_PASSWORD`；默认
账号 `admin` / `admin@kaneo.local`）——日常使用不需要它：超级助手头部
「项目管理」按钮已提供一键免登录（见配置说明）。首版无 SMTP，没有邮件找回
通道，服务器 `.env` 是初始密码的唯一恢复来源；密码一旦在原生页面修改，账号
事实源即转为 Kaneo 自身数据库。改密后须在受控终端同步服务器 `.env` 的
`KANEO_ADMIN_PASSWORD`，否则一键免登录立即失效（回落手动登录），下次部署也
会因管理员验证失败而关闭入口；部署不会自动重置数据库密码。

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

## 任务实例 worker（task_worker）部署

任务实例（`backend/app/task_instances/`，menu key `task_instances`）的 agent
节点由 `task_worker` 服务以 Docker 容器执行（Claude Agent SDK + 只读 rootfs +
bind-mount 工作区 + 模型凭据容器环境注入，详见域内 `container_runtime.py` 头注释）。

- **服务形态**：与 `pipeline_executor` 同镜像、同一 nats_executor 入口；
  差异仅在三处环境变量与挂载（见 `docker-compose.prod.yml` 的 `task_worker`）：
  - `TASK_INSTANCES_EXECUTOR=docker`（容器执行模式；本地/测试默认 `fake`
    确定性假执行器）；
  - `/var/run/docker.sock` 挂载（唯一需要 Docker 权限的进程）；
  - `pipeline_executor` 侧设 `TASK_INSTANCES_CONTROL_EXCLUDE=1` 让出
    `task_instances.control` 消费归属，避免同一 durable 在无 sock 进程间
    负载均衡。
- **镜像构建**（部署机或 CI 预构建，产物 `openontology-task-agent:local`）：

  ```bash
  docker build -t openontology-task-agent:local docker/task-agent
  ```

  未构建该镜像时 task_worker 仍可启动，但 agent 节点派发将以
  `docker run 失败` 收口为节点失败（fail-closed，不静默降级）。
- **本地栈**：`docker compose -f docker-compose.local.yml --profile task-agent up`
  启用（`pipeline_executor` 需同时置 `TASK_INSTANCES_CONTROL_EXCLUDE=1`）；
  不启用 profile 时本地以假执行器承接，无需 Docker。
- **模型要求**：agent 节点仅认 Anthropic 协议的模型配置（provider=anthropic
  或 api_base 含 anthropic 兼容端点）；密钥只经容器环境注入，不落库、不进
  事件流。
- **隔离边界**：执行容器 `--read-only` + 非 root + `/workspace` 唯一可写根；
  egress 默认 bridge 网络（`TASK_AGENT_NETWORK` 可覆盖）。容器级网络隔离弱于
  完整方案，生产启用前应在隔离 staging 按 AGENTS.md §5 完成验收：
  激活 → 容器执行 → 契约校验 → 审批 → 收货全链路 + 插话实测，证据入
  `.artifacts/`。

## 集成 CDC 执行器（integrated_cdc_executor，opt-in）

集成数据集的源端 CDC（`backend/app/data_channel/integrated/`，控制面
`cdc_control.py` + 运行时 `cdc_executor.py`）由独立的常驻执行器承接：
每个受管连接一个 Debezium Server 子进程（Python 监督者管理），事件经
HTTP sink 发往**同容器** 127.0.0.1 临时端口的接收器，standard-webhooks
HMAC 密钥每次运行期/每次子进程启动随机生成，只写入 0600 临时配置文件
（tmpfs），不进环境变量、不进公开 API。执行器不发布任何端口、不挂载
docker.sock、以非 root（uid 10001）只读 rootfs 运行。

- **启用**（覆盖层只新增服务与专用卷，不改任何既有服务/密钥）：

  ```bash
  docker compose -f docker-compose.prod.yml -f docker-compose.cdc.yml \
    up -d --build integrated_cdc_executor
  ```

- **镜像**：`backend/Dockerfile.cdc`。Python 运行时同 backend 镜像；Debezium
  Server 发行版与其 Java 21 运行时整体取自固定摘要官方镜像
  `quay.io/debezium/server:3.7.1.Final@sha256:8c4e6c7efc392f9adfd283ede2e75cd1a3594ef1126b014248054a5e718bfbd5`
  （升级 = 换摘要重测，不跟踪浮动 tag）。复用已构建 backend 镜像的快速路径
  见 Dockerfile 头注释（`CDC_PREBUILT_BACKEND=1`）。
- **状态卷**：`integrated_cdc_state` 挂到 `/var/lib/openontology-cdc`，按
  `连接/generation` 分目录存 Debezium 偏移与模式历史。订阅增删、凭据轮换
  （暂停等待连接重测）同代续用偏移；物理端点变更被控制面 409 阻塞，直到
  旧源观测到停止 + 复制槽清理确认，随后显式换代重锚。执行器重启
  （SIGTERM/宕机）从不清理活跃源的槽，重启即续流。
- **覆盖语义（无回填）**：`snapshot.mode=no_data`。新订阅先进入子进程
  include 集，待该子进程被接收器确认过事件/心跳且健康探测 UP 后才锚定
  激活前沿；锚定前的区间是显式未覆盖期，绝不回填。WAL/binlog 缺口显式
  报 `gap`，绝不自动补快照：包括续流前自有 PG 槽缺失/失效，以及任一方言
  下“已声称覆盖但本地偏移文件缺失/为空”（状态卷丢失）——此时绝不以
  `no_data` 从当前位点静默续接。模式漂移按
  **连接级** fail-closed：整源暂停（首版不做单表隔离）。事件事实达到容量
  上限（100 万条）时接收器把源置为暂停（`EVENT_CAPACITY_EXCEEDED`），
  执行器保持该暂停、不自动续流，也没有预留额度或自动恢复。超限裁剪只裁到
  上限本身，不产生余量；恢复需要到期清理后事实总量不超过“上限减去新事件
  所需事实数”，此时管理员对连接做显式重测，控制面解除暂停后从保留的偏移
  续流。重测本身不删除事实，容量不足时仍会再次暂停；若 7 天内的本地事件量
  已达到上限，需先做容量规划或另行运维处理。
- **源端前置条件**：MySQL `binlog_format=ROW`、`binlog_row_image=FULL`、
  `REPLICATION SLAVE/CLIENT`，以及 `RELOAD`（或动态权限 `FLUSH_TABLES`）
  ——`no_data` 模式启动时的 schema 快照用 `FLUSH TABLES ... WITH READ LOCK`
  取读锁，Debezium Server 3.7.1 的表级回退同样执行 `FLUSH TABLES`，所以仅
  授予 `LOCK TABLES` 不够；`SHOW DATABASES` 按官方要求建议授予（本平台按
  显式库表寻址，缺失不致命）。缺 `RELOAD/FLUSH_TABLES` 时预检直接报
  `SNAPSHOT_LOCK_PRIVILEGE_MISSING`，不会拉起子进程去做“健康 UP 但内部无限
  重试”的空转。PostgreSQL `wal_level=logical`、订阅表
  `REPLICA IDENTITY FULL`，并建议设置 `max_slot_wal_keep_size` 等槽保留
  上限，防止休眠源在等待清理期间推高 WAL。
- **状态口径（低流量源）**：`active` 需要新鲜窗口（15 秒）内的真实流证据。
  MySQL 的 Debezium 心跳只在处理 binlog 事件时产生，源端完全空闲时没有
  新证据，状态会如实回落为 `starting`（健康为 false），有新数据即恢复；
  执行器不向业务库写入任何东西来制造心跳。回落只影响健康口径，不影响
  已激活订阅：后续变更照常接收入库，偏移与激活前沿不变。
- **资源与健康**：`CDC_MAX_SOURCES`（默认 2，与覆盖层默认 3g 内存上限
  匹配；调大须同步上调 `CDC_EXECUTOR_MEM_LIMIT`）限制并发子进程，每个子进程
  JVM 堆由 `CDC_CHILD_JAVA_OPTS`（默认 256m）约束；服务级
  `mem_limit/cpus/pids_limit` 见覆盖层（可用 `CDC_EXECUTOR_MEM_LIMIT`/
  `CDC_EXECUTOR_CPUS` 收紧）。健康检查基于“最近一拍对账完成”心跳文件：
  元数据库不可达时如实不健康。多副本/滚动重叠由每源 PostgreSQL 咨询锁
  租约互斥，同一源不会出现两个子进程争用同一复制槽。
- **运行日志边界**：子进程输出只分类为稳定错误码后记录，不转发原文；
  连接配置仅运行期瞬时解密，主机/账号/SQL/源值不进任何日志。

## 集成副本执行器（integrated_replica_executor）

集成数据集的副本表（`backend/app/data_channel/integrated/` 的 `replica_*`
模块，控制面 `replica_control.py` + 运行时 `replica_executor.py`）由独立的
常驻执行器承接：为每个受管连接按捕获代（capture cohort）拉起 Debezium
Server 子进程做 initial 快照并续流，事件经 TransportMetadata SMT 封装
（保留提交偏移）后由同容器回环接收端写入平台库的副本表。执行器不发布任何
端口、不挂载 docker.sock，生产以非 root（uid 10001）只读 rootfs 运行。

- **启动（标准栈自动拉起）**：服务定义在标准 `docker-compose.prod.yml`，
  无 profile/opt-in——常规 `docker compose -f docker-compose.prod.yml
  up -d` 与 deploy-prod.sh 的核心启动序列都会构建并启动它（首启会拉取
  固定摘要的 Debezium 基础镜像）。它不在 deploy-prod.sh 的停机/重启核心
  序列（frontend/pipeline_executor/backend）内：迁移窗口内执行器持续运行，
  元数据库短暂不可达时按执行器自身重试口径恢复，健康检查可能如实抖动。
  执行器运行期解密受管连接配置，需要与 backend 相同的
  `SECRET_KEY`/`ENCRYPTION_KEY`（经服务器 `.env` 插值注入，缺失时应用侧
  fail-closed），不引入任何新凭据变量。
- **与 CDC 执行器的关系**：两者共用 `backend/Dockerfile.cdc` 镜像但容器、
  状态根（`/var/lib/openontology-replicas` 与 `/var/lib/openontology-cdc`）、
  卷完全独立，可同时运行；`replica-smt.jar` 对 CDC 子进程只是 classpath 上
  未被引用的 jar，不改变 CDC/Sentinel 行为。`docker-compose.cdc.yml` 覆盖层
  只定义 `integrated_cdc_executor`，任何覆盖层都不得重定义
  `integrated_replica_executor`（Compose 对同名 service 静默合并，
  `test-deploy-guards.sh` 拒绝重复定义）。
- **镜像**：`backend/Dockerfile.cdc`。SMT（TransportMetadata + ReadOffsets）
  在镜像构建的 debezium 阶段用固定摘要 Debezium 镜像自带的完整 JDK 工具链
  对其 `/debezium/lib` 离线编译，构建即随跑 SelfTest 自校验（失败即构建
  失败），产物放 `/opt/debezium/lib/replica-smt.jar`（执行器 classpath 的
  `lib/*` 通配直接可见）；SMT 源码随部署归档上服务器
  （`backend/integrated_runtime`）。
- **状态卷与同宿主扩容（非跨主机 HA）**：`integrated_replica_state` 挂到
  `/var/lib/openontology-replicas`，按连接/代保存 Debezium 偏移与模式
  历史。同一宿主机可运行多个 executor 实例并共享该卷（service 未设
  `container_name`，可 `docker compose ... up -d --scale
  integrated_replica_executor=N`）；每个 capture 由 PostgreSQL 咨询锁
  （`SourceLease`）单写者围栏，同一 capture 只有一个实例拉起子进程。不支持
  跨主机共享卷或跨主机接管；宿主机迁移需先停全部实例再整体迁移卷。
- **容量预算（诚实口径，禁止假宣称）**：`REPLICA_MAX_CAPTURES` 是**每个
  executor 实例**的并发捕获上限，默认 2 仅匹配单实例 `mem_limit=3g`/
  `cpus=2.0`（`REPLICA_EXECUTOR_MEM_LIMIT`/`REPLICA_EXECUTOR_CPUS` 可收紧）
  的保守预算，**不满足 200 源容量**。单实例粗预算约为
  `REPLICA_MAX_CAPTURES × 256m` JVM 堆 + Python/接收端开销；默认 2 对应 3g。
  容量占满时新注册/新表的 cohort 保持可观测的 `queued` 状态，不会挤掉
  已 active 的捕获，也不会自动接入全部资产；必须通过增加同一宿主机上的
  executor 实例并同步提升实例资源/并发推进。禁止把默认 2 直接放大到 200
  导致 OOM。任何 200 源/30M 行级/P95 或更大规模的容量承诺，都必须在目标
  资源与隔离环境（staging/CI）用可复核证据验收；证据保存到 `.artifacts/`
  或 CI artifact，不写入长期文档。容量只按已验收的分档宣称，不按源数量
  宣称。
- **覆盖证明与源端 WAL 副作用（固定版本协议）**：副本屏障按固定
  Debezium Server 3.7.1.Final + PostgreSQL 14+ 的 pgoutput logical
  messages 实现。初始快照完成和恢复续流前，执行器在源库发出受管
  prefix/token 的 nontransactional `pg_logical_emit_message(false)` 作为
  切换 cut；运行期约每 10 秒发一次轻量覆盖确认消息（可再收敛，不按批
  发送），用于在保持源端健康的同时避免副本长时间旧标。该消息只产生源端
  WAL，不写业务表；因此副本链路不得再被描述为源端“全只读/零写”——受管
  WAL 逻辑消息是明确副作用，必须纳入源端 WAL 容量与监控。预检要求
  PostgreSQL >=14、当前用户对 `pg_logical_emit_message` 有 EXECUTE，且
  源不是 standby/read_only；PG<14 直接阻塞为
  `REPLICA_PG_VERSION_UNSUPPORTED`，不提供 fallback。MySQL 侧只读执行
  `SHOW BINARY LOG STATUS`（8.4+；旧版 `SHOW MASTER STATUS`）读取
  GTID/filepos，并以已完成的 heartbeat offset 作为覆盖证明，不写源表。
- **分档扩容验收条件**：
  - 档 1（默认每实例 2 并发捕获）：在目标资源隔离环境用真实源库完成注册 →
    快照 → 变更续流 → 执行器重启续流 → 偏移保留的全链路验收（AGENTS.md §5
    的真实环境 E2E 口径）；通过后才按该默认分档运行。
  - 档 2（每实例 >2 并发捕获或上调内存档，含任何 200 源/30M 行级/P95
    目标）：在目标资源隔离环境按目标并发实测快照吞吐、内存峰值与源库
    WAL/binlog 保留增长，留存可复核容量证据后才允许在生产 `.env` 覆盖
    `REPLICA_MAX_CAPTURES`/`REPLICA_EXECUTOR_MEM_LIMIT` 并增加 executor
    实例；未通过验收的分档不得进入生产配置或对外承诺。
- **持久偏移丢失＝blocked/rebuild**：偏移卷丢失/为空而平台库仍声称副本已
  覆盖时，执行器绝不以当前位点静默重开快照或续流——对应连接被阻塞，只能
  由管理员显式 rebuild（重建捕获代、全量重锚）。因此恢复必须整恢复点还原
  （见 [backup-restore.md](backup-restore.md)）：平台库（副本表 + 控制面）
  与 `integrated_replica_state` 卷必须同一恢复点一致，只还原其一即落入
  上述阻塞态，等价于一次全量 rebuild。

## 部署前检查

超级助手用户进程插件的生产验收需要独立 Linux staging，不能用 macOS Docker
或单元测试代替 namespace/cgroup 证据。环境至少应提供 cgroup v2 delegated
subtree、非 root user namespace、seccomp/AppArmor、受控 egress/DNS 策略，
以及与生产隔离的 PostgreSQL、NATS JetStream、MinIO、Neo4j、n8n 和 Chromium
CDP。插件包须以不可变 digest 交付；secret/artifact broker 使用仅限 staging
的短期授权，证据写入 CI artifact 或 `.artifacts/`，不得写入 Git。缺少任一
隔离、broker 或恢复条件时，保持生产 `user_untrusted` 插件 disabled，并记录
未执行的攻击、崩溃、重复投递、取消、升级和回滚验收，不得按商用通过处理。

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
- 上一版本镜像、Compose 配置和回滚负责人已确认；
- Kaneo：`PUBLIC_TLS_IP` 已配置，`KANEO_WEB_URL` 使用该 IP 和
  `KANEO_HTTPS_PORT`（未显式设置时自动推导）；本版生产编排不支持不同域名或
  外部 TLS 代理的地址。`8090`/`8445`（或自定义端口）已放行且无端口冲突。

LLM 不属于部署启动门禁。基础平台发布成功后，再由管理员在模型配置页添加并
测试所需提供商；不得为了通过平台 readiness 预置或回显真实模型凭据。

发布前必须在隔离 staging 完成 Kaneo 强依赖接入的下列验收
并保留证据（证据入 CI artifact，不进 Git），不得以探活 200 代替：

1. 首次部署在 `.env` 缺失时生成 `KANEO_ADMIN_PASSWORD`/`KANEO_DB_PASSWORD`/
   `KANEO_AUTH_SECRET`，重复部署后这三项与已部署实例保持一致（只核对存在与
   指纹，不回显值）；
2. 私网初始化失败演练：kaneo-app 未就绪或 bootstrap 失败时部署失败，且
   kaneo-gateway 保持关闭，公网无法访问半初始化实例；
3. 公网入口契约：HTTP 入口返回 308 且 Location 精确等于
   `${KANEO_WEB_URL}/`；HTTPS 入口经网关健康并可完成原生登录；
4. 登录后确认无公开注册、访客与邮件验证登录入口；附件上传与邮件找回密码
   按预期不可用（首版无 S3/SMTP）；
5. 平台超级助手「项目管理」入口与 Kaneo 的页面 API、认证来源共同使用
   `KANEO_WEB_URL` 规范 origin；远程访问须配置真实地址，不改写回环主机名；
6. 停止 kaneo-app 后平台 `/health/ready` 失败，恢复后自动回到就绪。

配置清单见 [配置说明](./configuration.md)，回滚见 [回滚说明](./rollback.md)。
