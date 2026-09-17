# GitHub Actions

- `ci.yml`：PR 到 `nano-ontoprompt` 的文档/仓库卫生、后端、配置中心和前端
  验证，不部署；
- `deploy-nano-ontoprompt.yml`：push 到自动部署分支后按改动家族验证，再进入
  部署。分类器是 fail-closed 的路径家族（见
  `scripts/ci/classify-deploy-changes.sh`），不是增量单测；未知路径仍全量。
  SSH 传输参数与生产依赖 `PROD_*` 均来自 Repository secrets/variables，清单
  在 runner 上物化。

工作流必须使用与源码相同的 Python/Node 版本和锁文件。文档与仓库卫生门禁
始终运行；后端六分片、前端单测/E2E/feature 边界只在分类器打开对应开关时
执行。未知路径、空 diff、手工触发，以及改到分类器或部署工作流本身时仍全量。
部署机密不得写入日志；生产依赖清单不进入 Git，由 workflow 在 runner 上从
Repository secrets/variables 物化，`test-deploy-guards.sh` 与
`check-repository-hygiene.sh` 锁定该契约。runner 无论成功失败都要清理上传
压缩包与物化清单。

`DEPLOY_APP_DIR` 在任何 SSH 命令使用前必须调用
`scripts/ci/validate-deploy-app-dir.sh`；默认 `/opt/openontology` 可用，空值
之外的危险/未规范化路径必须直接终止部署。
