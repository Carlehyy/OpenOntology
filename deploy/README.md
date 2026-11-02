# 生产部署

```text
deploy/
├── README.md
├── deploy-prod.sh                      服务器生产部署入口
├── runtime_source_attestation.py       exact-HEAD manifest 生成与运行容器只读校验
├── runtime-source-manifest.json        CI/手工从精确 HEAD 生成，随 archive 打包（不入库）
├── production.dependencies.env         部署时由 Actions 从 Repository secrets 物化（不入库）
└── production.dependencies.example.env 无秘密的人工校验模板
```

推送到 `nano-ontoprompt` 后，GitHub Actions 在验证阶段以
`DEPLOY_VALIDATE_ONLY=1` 运行 `deploy-prod.sh` 做依赖配置门禁，再通过 SSH
在服务器上执行同一脚本完成部署。清单与 `.env` 的合并规则、回滚步骤和
`PROD_*` secrets/variables 清单见 [配置说明](../docs/operations/configuration.md) 与
[部署说明](../docs/operations/deployment.md)。

部署包必须在构造前从精确 Git HEAD 生成
`deploy/runtime-source-manifest.json`；archive 流程还须独立传入
`EXPECTED_RUNTIME_COMMIT`。直接 Git 模式会在服务器用 `python3` 从 clean HEAD
生成同一 manifest，缺少 `python3` 时明确失败，不跳过核验。readiness 成功后，
`deploy-prod.sh` 只读校验 active backend 容器内 `/app/app` 文件字节与镜像 ID；
该证明范围是运行容器文件字节和 image identity，不声称已加载 Python 内存字节。

物化生成的 `production.dependencies.env` 含真实生产凭据：不得修改、复制或
回显其中的值，红线见 [AGENTS.md](../AGENTS.md) 第 6 节。
