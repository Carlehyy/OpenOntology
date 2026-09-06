# 生产部署

```text
deploy/
├── README.md
├── deploy-prod.sh                      服务器生产部署入口
├── production.dependencies.env         部署时由 Actions 从 Repository secrets 物化（不入库）
└── production.dependencies.example.env 无秘密的人工校验模板
```

推送到 `nano-ontoprompt` 后，GitHub Actions 在验证阶段以
`DEPLOY_VALIDATE_ONLY=1` 运行 `deploy-prod.sh` 做依赖配置门禁，再通过 SSH
在服务器上执行同一脚本完成部署。清单与 `.env` 的合并规则、回滚步骤和
`PROD_*` secrets/variables 清单见 [配置说明](../docs/operations/configuration.md) 与
[部署说明](../docs/operations/deployment.md)。

物化生成的 `production.dependencies.env` 含真实生产凭据：不得修改、复制或
回显其中的值，红线见 [AGENTS.md](../AGENTS.md) 第 6 节。
