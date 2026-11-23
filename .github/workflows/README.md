# GitHub Actions

- `ci.yml`：PR 到 `nano-ontoprompt` 的文档/仓库卫生、后端、配置中心和前端
  验证，不部署；
- `deploy-nano-ontoprompt.yml`：push 到自动部署分支后按改动家族验证，再进入
  部署。分类器是 fail-closed 的路径家族（见
  `scripts/ci/classify-deploy-changes.sh`），不是增量单测；未知路径仍全量。
  SSH 传输参数与生产依赖 `PROD_*` 均来自 Repository secrets/variables，清单
  在 runner 上物化。

工作流必须使用与源码相同的 Python/Node 版本和锁文件。文档与仓库卫生门禁
始终运行。前端 mocked E2E 按文件分成两个单 worker 作业，证据按分片上传。
生产镜像验收在 `frontend/dist` 上传后即可与各回归作业并行。部署作业等待所有
适用的门禁通过，再进入独立的生产队列。连续推送的验证可并行，部署在取得锁后
确认分支最新提交。上一笔推送未经确认时，当前推送执行全量验证。

部署机密不得写入日志；生产依赖清单不进入 Git，由 workflow 在 runner 上从
Repository secrets/variables 物化，`test-deploy-guards.sh` 与
`check-repository-hygiene.sh` 锁定该契约。runner 无论成功失败都要清理上传
压缩包与物化清单。

`DEPLOY_APP_DIR` 在任何 SSH 命令使用前必须调用
`scripts/ci/validate-deploy-app-dir.sh`；默认 `/opt/openontology` 可用，空值
之外的危险/未规范化路径必须直接终止部署。
