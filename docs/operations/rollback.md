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

Multica 默认栈不参与平台 Alembic 迁移，官方入口只在容器启动时执行
`./migrate up`（上游没有经核验的 downgrade 路径）。已验证的回滚边界：
栈一旦启用并正常使用（账号/workspace/PAT/任务数据已写入
`multica_pgdata`，附件已写入 `multica_uploads`），回滚或降级没有
schema 层面的安全路径——以恢复同恢复点的卷备份为准（`multica_pgdata` +
`multica_uploads`，与服务器 `.env` 同恢复点，见
[备份与恢复](./backup-restore.md)）；仅在卷仍为空（从未注册使用）时，
才可直接把镜像 digest 回退到上一钉扎值重启。显式停用 bundled 栈
（`MULTICA_ENABLED=false`）只停容器不删卷，回滚平台版本不要求清空
multica 数据卷；如需取消部署默认绑定，清空五项并重跑部署，readiness
不再门禁 `multica_default`。这不会撤销历史任务的 PAT；需要撤销时应在
Multica 原生管理界面吊销凭据。

平台数据库迁移 `0154_multica_profiles` 与上述上游卷独立。旧版本只能表示
每用户一条配置；存在平台默认、多 profile、多 revision 或仍绑定来源的
未决调用时，降级会拒绝而非丢弃数据。已正常使用新配置后，回退到迁移前版本
应恢复升级前的平台 PostgreSQL 与对应加密密钥，并核对上游任务和 PAT 状态。

Grafana 是平台 fail-closed 强依赖（随平台 Compose 启动，无独立公网入口）：
回滚目标是本接入版本时，部署会先确认 Grafana 在本次部署前是否已运行
（`compose ps --status running -q grafana`；查询失败即部署非零退出，不把
查询失败当作无容器），随后启动并执行 grafana-bootstrap 校验。启动/健康等待
失败且此前未运行时，仅停止 Grafana 服务；此前已在运行则保留现场、不主动
停止。grafana-bootstrap/`--verify` 失败保持原有收尾语义：停止 Grafana 服务。
所有失败收尾都不停止其他业务服务。完成回滚并确认 bootstrap `--verify` 通过后
再随 Compose 重新开放；回滚到接入 Grafana 之前的版本则不受影响。
Grafana 使用自身 SQLite（`grafana_data` 卷），不参与平台 Alembic
迁移；回滚平台版本不要求回滚或清空 `grafana_data` 卷。若回滚跨越
`operations_dashboards` 表的引入（迁移 `0150`），schema downgrade 只回退
表结构、不恢复数据——必须按 [备份与恢复](./backup-restore.md) 的成对恢复
点（平台 PostgreSQL + `grafana_data` 卷 + 受控 `.env`）恢复，否则会出现
「已失效」卡片或 Grafana 侧孤儿看板。

## 目录治理期间

每个迁移 PR 必须保留旧 import/facade 或提供可逆提交。目录移动和删除兼容层
不得在同一批完成。
