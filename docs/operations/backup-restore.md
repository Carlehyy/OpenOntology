# 备份与恢复

生产备份至少覆盖：

- PostgreSQL；
- Neo4j；
- MinIO/对象存储；
- n8n workflow 定义及其受控凭据备份策略；
- API Hub 独立数据目录；
- 共享 uploads；
- WeKnora 独立 `weknora_postgres_data`、`weknora_redis_data`、`weknora_data`
  与 `weknora_docreader_tmp`；
- Kaneo 独立 `kaneo_postgres_data`（项目数据权威）与 `kaneo_data`
  （`/var/lib/kaneo` 运行时资源，不是附件持久化）；
- 启用 Hindsight 记忆 profile 时，其 `hindsight_postgres_data` 卷与 `.env` 中
  的 `HINDSIGHT_API_KEY`/`HINDSIGHT_SERVICE_TOKEN` 同恢复点保存
  （`hindsight_model_cache` 仅是模型缓存，可重新下载/预填，不属于恢复权威）；
- 服务器 `.env` 与部署所需非秘密配置；
- 当前镜像 digest/版本和 Compose 配置。

Redis 队列是否需要持久恢复取决于任务幂等与业务 RPO，必须在具体运行手册中
说明。WeKnora 的数据库、文件卷与 `.env` 中的
`WEKNORA_SYSTEM_AES_KEY`/`WEKNORA_SYSTEM_SIGNING_KEY` 必须作为同一恢复点
保存：

- MCP 端点 token 由管理员在原生页面管理；轮换后须同步平台 MCP 请求头，重新
  测试并登记受约束。平台不自动轮换或保存额外集成状态文件；
- 丢失 `WEKNORA_SYSTEM_AES_KEY` 或 signing key 且没有同恢复点备份时，已加密
  的模型/MCP 等凭据可能不可逆丢失。必须恢复同一恢复点的 key，或由授权管理员
  手工重新配置凭据；不能承诺 bootstrap 能恢复这些密文。
- `WEKNORA_JWT_SECRET` 丢失只影响登录会话，需要重新登录，不属于密文恢复。

Kaneo 的 `kaneo_postgres_data` 必须与服务器 `.env` 中的
`KANEO_DB_PASSWORD` 作为同一恢复点保存：PostgreSQL 角色密码保存在数据目录
内，旧卷配新密码会认证失败。`KANEO_AUTH_SECRET` 丢失只使既有登录会话失效；
`KANEO_ADMIN_PASSWORD` 只是部署初始密码，账号事实源在 Kaneo 数据库内。首版
没有 S3/SMTP，不存在需要一并恢复的附件对象存储。

服务器 `.env` 与 PostgreSQL/API Hub 数据必须作为同一恢复点管理。
`ENCRYPTION_KEY`（历史环境中可能由当时的 `SECRET_KEY` 派生）是模型、Connection、
n8n、MinIO、浏览器源、分享令牌、MCP 和 W3 密文的解密权威；只恢复数据而丢失
对应密钥，等价于不可恢复。备份副本必须在受控位置以最小权限保存，不得放入
Git、CI artifact、聊天或应用目录内的普通源码备份。

API Hub 使用 SQLite WAL。运行中只复制 `app.db` 不能证明得到一致备份；应先停止
API Hub 写入，再备份整个 `api_hub_data` volume（包括 WAL/SHM 和会话文件），或
使用 SQLite 官方一致性备份机制。API Hub 的接口导出功能不包含 W3 密码、settings
或完整登录会话，不能替代 volume 灾备。

## 恢复演练

备份成功日志不等于可恢复。至少定期在隔离环境验证：

1. 恢复数据；
2. 运行 Alembic 到目标版本；
3. 启动 API 和 worker，并验证 PostgreSQL、Redis、Neo4j、MinIO、n8n 与
   Chromium CDP readiness；
4. 校验对象附件、图关系、任务、n8n workflow 与 API Hub 配置；
5. 执行关键只读和写入旅程；
6. 记录 RPO、RTO 和失败项。

具体存储命令依赖生产供应商，补充前不得在文档中假设某一云平台。

## 破坏性功能退役

删除业务表的迁移不能把 schema downgrade 写成数据恢复。以 ADR-0003 的遗留
文档本体抽取退役为例，上线前必须取得同一时间点的 PostgreSQL、共享 uploads
和对象存储备份，并在删表前导出 `uploaded_files` 的 ID、所属本体、物理路径、
大小和 MIME 清单。0055 downgrade 只会重建空表，无法恢复行或文件引用。

数据库迁移不得顺带删除物理文件：uploads 和 bucket 可能被其他业务域共享，
仅凭旧表中的路径不能证明对象没有其他引用。待 canary 验收和保留期结束后，
依据升级前清单逐项核对，再通过独立、可审计且可恢复的运维变更清理；禁止对
uploads 根目录、整个 bucket、未展开变量或 glob 执行递归删除。
