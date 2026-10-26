# 排障入口

## 启动失败

- 检查 Python 3.12、Node 22 和锁文件是否匹配；
- 检查 `config/generated/local/.env` 是否由当前配置中心生成；
- 逐项检查 PostgreSQL、Redis、NATS、Neo4j、MinIO 和 n8n；这些阻塞型
  依赖在正常本地与生产启动时都必须 fail closed；
- 检查 Chromium CDP 地址和服务；地址配置必需，服务不可达不终止 API，但深度
  readiness 必须 fail closed；
- 运行 `/health/live` 与 `/health/ready`；不要只看前端页面；
- 不要用平台 SQLite、API 线程任务、NetworkX/SQL 图或本地对象目录绕过故障。

API Hub 自有 SQLite、测试环境 SQLite 和历史 `local://` 只读迁移路径有独立
用途；看到它们不代表主平台存在降级模式。LLM 未配置时，先确认基础平台已
ready，再由管理员在模型配置页添加提供商。

## 部署失败

- verify 失败：先修测试/迁移/构建，不允许跳过 deploy 依赖；
- manifest 生成失败：检查 `production` Environment 的逐项配置；
- executor 失败：检查 NATS 连接与 pipeline_executor 容器心跳；
- 图接口 503：检查 Neo4j 连接与本体投影状态，不应转查 SQL/NetworkX 结果；
- 文件写入失败：检查 MinIO，不要改用本地路径写新对象；
- 语义搜索 501：这是已移除语义搜索的预期契约；使用 PostgreSQL 关键词搜索；
- n8n/CDP 失败：检查外部端点、凭据、网络策略和真实 readiness；
- 静态资源失败：检查 Nginx、Vite base、懒加载 chunk 和内容类型；
- 迁移失败：保留日志与数据库副本，禁止直接 stamp/改 Alembic 历史。

出现 `SECRET_KEY is missing or still uses an example credential` 时，不要删除
`.env`、数据库或 Compose volume，也不要只随机覆盖 `SECRET_KEY`。当前版本会对
缺失键、显式空值和仓库已知示例值自动执行一次无损密钥解耦，并按旧 dotenv
语义区分缺失与空值；若仍看到这条错误，先确认
远端实际执行的 `deploy/deploy-prod.sh` 已是本版本，并确认源码上传阶段没有
失败。若错误变为 `SECRET_KEY must contain at least 32 characters`，说明服务器
使用的是未知自定义短密钥：保持原 `.env` 和所有数据不动，由维护者在受控终端
确认旧运行实例的实际加密边界，不允许猜测或清空存量凭据。

成功日志中的 `pinned the existing SECRET_KEY-derived encryption key` 表示只把
旧有效 Fernet key 显式写入 `.env`，并没有更新任何业务密文。随后用户需要重新
登录，尚未消费的短期 Pipeline 上传令牌需要重新生成；持久分享链接与已有管理
员密码应保持不变。日志或工单中不得粘贴 `.env` 的值。

出现 `production .env is missing; refusing to generate new encryption authority`
时，首先按“存量环境配置丢失”处理并恢复原 `.env`。只有已证明 PostgreSQL、
API Hub、Neo4j、MinIO、uploads 等全部持久存储为空的首次安装，才手工 Dispatch
并勾选 `bootstrap_production_env`；不得对有数据的环境使用这个确认项。

出现 `production .env must be a regular server-side file` 时，部署检测到了有效或
失效软链接并在任何密钥迁移前停止。不要删除链接后勾选 bootstrap；先确认链接
原本指向的秘密管理器/挂载是否可恢复，再按部署手册物化完整的普通 `0600`
`.env`。出现 `non-empty and has no .git directory` 时同样属于保护性失败：脚本
不会删除目录内的 `.env` 或其他状态，应改用 Actions 归档部署路径或先审计目录。

## 集成数据集登记与结构漂移

- 在「数据资产湖 → 集成数据集 → 登记集成数据集」内配置 MySQL/PostgreSQL
  数据源、测试连通、发现源表并核验字段，无须进入数据流水线。测试与发现不
  保存连接；确认登记时才将加密连接、数据集与字段配置一并保存。
- 连通测试通过只证明平台能访问源库；源表发现、字段核验和结构刷新还要求
  固定版本 dbx 可用。dbx 失败时不切换到其他元数据通道。容器与平台访问源库
  的地址不同时，在高级设置中成对填写 dbx 访问源主机与端口。
- 字段标识、显示名和纳入范围属于平台逻辑契约；源列名、类型、可空性和主键
  为只读事实。本流程不执行源表 DDL，源主键必须纳入。查询与受控写入使用
  逻辑字段标识，已同步版本保留自己的字段映射，不随实时配置改变。
- `SOURCE_SCHEMA_DRIFT` 表示实际物理结构与登记时不一致。确认源库变更后
  执行结构刷新；刷新保留有效字段配置。若返回 `FIELD_CONFIG_SOURCE_MISSING`，
  已映射的源列已不存在：先恢复或纠正源库结构，或按新的源表契约重新登记。
  不要通过修改物理指纹或直接覆盖历史版本来消除错误；本流程不提供连接或
  已登记字段映射的编辑入口。
- 登记响应丢失不代表事务失败。先检查资产列表和已有源，再决定是否重试，
  避免将网络错误误认为「什么都没有创建」。

## 目录移动后失败

优先检查：

- Python import 和 monkeypatch target；
- `Path(__file__).parents[n]`；
- fixture 相对路径；
- Vite/TypeScript alias；
- Playwright `testDir`；
- Compose context、volume、env_file；
- Actions `working-directory` 与 cache path；
- README、部署脚本和配置中心中写死的目录。
