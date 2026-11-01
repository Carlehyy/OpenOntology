# 仓库脚本

```text
scripts/
├── README.md
├── ci/              CI 与仓库契约检查
├── data/            数据导入、fixture 和真实链路脚本
└── e2e/             隔离验收环境脚本（compose 编排、fixture seed 与 oracle，
                    只按容器标签操作自有项目，见 docs/development/testing.md）
```

服务器生产部署入口与生产依赖清单位于 [`deploy/`](../deploy/README.md)。

脚本必须可从仓库根目录定位资源，不得包含个人绝对路径。会修改数据、调用
外部服务或产生费用的脚本必须在文件头和对应 README 标明，并把运行证据写入
`.artifacts/`。

后端专用迁移、维护、演示和真实链路验收入口位于
[`backend/scripts/`](../backend/scripts/README.md)。`scripts/data/` 只保留跨
前后端 fixture 数据链路；两处都必须逐项登记依赖、副作用和清理方式，零代码
引用不能替代人工入口台账。
