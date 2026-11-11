# 回滚

当前部署脚本尚未实现自动回滚，因此每次生产变更必须在迭代记录中提供具体
回滚方案，不能只写“重新部署上一版”。

## 应用回滚

1. 停止继续发布并保存失败运行日志；
2. 确认数据库迁移是否已经执行；
3. 选择最后一个已验证 commit/镜像；
4. 恢复与该版本匹配的 Compose 和环境契约；
5. 启动后检查 API 深度 readiness、PostgreSQL、Redis、Neo4j、
   MinIO、n8n、Chromium CDP、Kaneo 公网入口、前端资源和关键业务旅程；
6. 记录回滚原因和遗留数据处理。

## 数据库约束

- 不默认执行 Alembic downgrade；
- 标准部署会在 Alembic 前停止 backend；迁移失败时保持停止
  停止，必须先确认数据库实际 revision 和目标版本的 schema 兼容性，再决定
  恢复或继续升级，不能直接把旧写入进程拉起；
- destructive migration 必须采用 expand/migrate/contract 或双读写方案；
- 只有经过副本验证且明确安全时才允许 downgrade；
- 无法安全执行 schema downgrade 时，应回滚应用到兼容新 schema 的版本。

回滚不能通过重新启用平台 SQLite、API 线程任务、NetworkX/SQL 图、本地对象
存储或已移除的向量服务来“恢复可用”。目标版本必须满足同一必需依赖契约；
如上一版本依赖不同外部系统，应把依赖迁移作为单独、经验证的回滚步骤。

Kaneo 是平台 fail-closed 强依赖：回滚目标是带 Kaneo 的版本时，kaneo-app 未
就绪则 readiness 失败；回滚到接入 Kaneo 之前的版本则不受影响。Kaneo 不参与
平台 Alembic 迁移，回滚平台版本不要求回滚或清空 `kaneo_*` 数据卷；部署失败
时 Kaneo 公网网关保持关闭，完成回滚并确认私网 bootstrap 验证通过后再随
Compose 重新开放。

Hindsight 记忆 profile 是可选特性：回滚包含它的新版本时，把服务器 `.env`
的 `HINDSIGHT_ENABLED` 置回 false 并重跑部署即可停用（卷保留）；回滚目标
版本不含该 profile 时无需任何额外步骤，平台其余回滚流程不变。

## 目录治理期间

每个迁移 PR 必须保留旧 import/facade 或提供可逆提交。目录移动和删除兼容层
不得在同一批完成。
