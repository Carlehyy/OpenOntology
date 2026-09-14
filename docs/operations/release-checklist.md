# 发布交付清单

本清单用于合并到 `nano-ontoprompt` 前和生产发布窗口内的最终核对。发布人应
在 PR 或 CI run 中保留可追溯的 commit、workflow URL 和必要 artifact；不要把
生产 secret、数据库内容或含敏感信息的日志复制到 PR。

## 合并前

- [ ] 变更所属业务域、受影响入口和兼容契约已检查；HTTP、SSE、WebSocket、
  NATS、数据库表与环境变量契约没有无意变化。
- [ ] `git diff --check`、Markdown 链接检查和仓库卫生检查通过。
- [ ] 受影响的后端/前端测试通过；前端改动至少完成 unit、分类/边界门禁、
  lint 和 production build。需要浏览器或外部依赖时，使用 staging 的真实
  E2E，并保留结果 artifact。
- [ ] 若新增或删除后端测试，已重录 `backend/.test_durations`；否则说明不适用。
- [ ] 若涉及 Alembic，已验证新库升级、现存库副本升级和单一 head，并记录
  发布前后的 revision。

## 发布前

- [ ] 目标 commit、上一版已验证 commit/镜像、Compose 配置和回滚负责人已确认。
- [ ] 生产依赖清单和服务器 `.env` 已通过部署前检查；`.env` 是可恢复的
  `0600` 普通文件。首次安装已单独证明持久数据为空并显式确认 bootstrap。
- [ ] 数据库备份完成，最近一次恢复演练仍有效；涉及 contract migration 时，
  已记录停机前运行的 backend/frontend 版本。
- [ ] 迁移窗口已安排：先停止 API/worker 写入者，再执行 migration，最后启动
  与 schema 兼容的版本。

## 发布后

- [ ] API 深度 readiness、PostgreSQL、Redis、NATS、pipeline executor、Neo4j、
  MinIO、n8n、Chromium CDP 和前端静态资源均通过检查。
- [ ] 关键用户旅程和本次变更的行为已验证；SSE 变更需确认跨 chunk、LF/CRLF
  帧均能正确消费，并记录真实验收结果。
- [ ] CI/deploy workflow、迁移 revision、健康检查结果和异常日志已归档；
  临时部署包、物化依赖清单和本地 watcher 已清理。

## 失败与回滚记录

记录以下最小信息后，按[回滚说明](./rollback.md)执行：

```text
发布 commit：
上一版已验证 commit/镜像：
数据库 revision（停机前 / 当前）：
迁移是否到达 head：
失败阶段与时间：
回滚负责人：
恢复的备份/验证证据：
```

迁移失败且 revision 未变化时，才可考虑重启发布前确实运行的旧服务。revision
已变化、无法读取，或新服务已针对新 schema 启动失败时，保持停机并恢复兼容的
数据库与文件备份，禁止直接把旧应用接回部分升级的 schema。
