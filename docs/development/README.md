# 开发文档

```text
development/
├── README.md
├── setup.md                    本地环境与启动
├── testing.md                  测试分层和强制门禁
└── ontology-network-goal.md    本体网络端到端改造的 Goal 需求与验收基线
```

命令必须与 `pyproject.toml`、`uv.lock`、`package.json` 和 GitHub Actions
一致。新命令只有在本地或 CI 实际执行通过后才能写入本文档。

开始开发前先读仓库根目录 [AGENTS.md](../../AGENTS.md)，再读
[本地开发](./setup.md) 和 [测试指南](./testing.md)。

[本体网络 Goal 需求与验收基线](./ontology-network-goal.md) 记录本次已确认的
跨本体查询、数据资产读写、结构投影及端到端交付边界；后续 Goal 执行先读全文，
按验收编号提交证据。本文不代表对应功能已经实现或验收通过。
