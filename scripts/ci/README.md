# CI 脚本

- `materialize-production-dependencies.sh`：部署配置源工具，在 verify 与
  deploy 作业中从逐项 Repository secrets/variables 原子生成权限为 `0600`
  的生产依赖 manifest（该 manifest 不再被 Git 跟踪），也用于人工校验。生成
  结果只保留运行时消费的 PostgreSQL URL/数据库字段、
  Redis URL 和 MinIO S3 API 端点，不再生成重复的 PostgreSQL host/port 或
  MinIO console URL；`N8N_TIMEOUT_SECONDS` 默认 30 秒。
- `check-markdown-links.mjs`：零第三方依赖检查仓库 Markdown 链接、GitHub
  锚点、路径大小写、文档入口可达性和索引/模板契约。归档文档的问题默认
  为 warning；发布前可用 `--strict-archive` 提升为 error。
- `check-repository-hygiene.sh`：检查目录 README、被忽略但仍跟踪且存在的
  文件、禁止入库的个人文件、本机绝对路径、依赖锁文件唯一性，并调用
  lean-tree 与前端边界守卫。
- `check-lean-tree.mjs`：检查当前拟提交树中的过程产物、非包零字节文件和
  非空精确重复文件；删除状态不会被误报为仍存在。
- `validate-deploy-app-dir.sh`：在任何远端 shell 拼接或服务器目录操作前，
  拒绝根目录、非绝对路径、未规范化路径和 shell/控制字符。
- `create-deployment-archive.sh`：只打包生产构建/运行输入和受控维护入口，
  排除测试、文档、fixture、前端测试源码与过程报告。
- `test-deploy-guards.sh`：校验部署目录正反例，并通过
  `DEPLOY_VALIDATE_ONLY=1` 验证镜像摘要严格模式从最终 `.env` 读取、显式
  进程环境优先，同时验证当前配置来源契约和生产包白名单。

CI 脚本不得依赖个人环境或回显秘密。新增脚本必须同时有自动测试，并在
`.github/workflows/` 中引用同一个入口，避免本地与 CI 复制实现。

本地运行：

```bash
node scripts/ci/check-markdown-links.mjs --self-test
node scripts/ci/check-markdown-links.mjs
node scripts/ci/check-lean-tree.mjs --self-test
node scripts/ci/check-lean-tree.mjs
bash scripts/ci/test-deploy-guards.sh
bash scripts/ci/check-repository-hygiene.sh
```
