# 数据与真实链路脚本

这里登记不能由普通单元测试替代的手工数据脚本。命令均从仓库根目录执行；
Python 依赖取自 `backend/pyproject.toml`。共享输入见
[test_data/README.md](../../test_data/README.md)。

## 入口台账

| 入口 | 运行方式与前置条件 | 写入范围与清理 |
|---|---|---|
| `import_snomed.py` | `cd backend && uv run python ../scripts/data/import_snomed.py --dry-run`；可用 `--csv`、`--ontology` 覆盖默认值，API 由 `API_BASE` 指定，账号由 `ONTOLOGY_USERNAME`、`ONTOLOGY_PASSWORD` 指定 | `--dry-run` 仍会登录并读取目标本体；去掉该参数后按 `canonical_id` 去重并创建实体。脚本没有自动清理，需按本次新增实体的 ID 回收 |
| `link_entities.py` / `link_entities.sh` | 本地先运行 `cd backend && uv run python ../scripts/data/link_entities.py --dry-run`；已有 backend 容器时可运行 `bash scripts/data/link_entities.sh --dry-run`。支持 `--ontology`、`--csv`、`--min-auto`、`--min-review`、`--assign-custom`；未给 `--apply` 时默认 dry-run | `--apply` 会直接修改当前数据库，包括合并实体、重写引用及删除孤立实体/关系，且没有自动回滚。执行前备份数据库并审阅 dry-run；Docker 包装脚本还会复制脚本和 CSV 到容器 `/tmp` |
| `run_supply_chain_pipeline.py` | `cd backend && uv run python ../scripts/data/run_supply_chain_pipeline.py`；要求本机 `http://localhost:8000` 的完整后端（含 python_kernel_gateway）、脚本内演示账号及 `test_data/供应链/` 的 8 个 fixture | 上传 Dataset（仅作原始数据展示），按结构化源创建 4 条 python 引擎 Pipeline（脚本内嵌演示数据，平台不再经 connector 读取上传文件）并运行生成 Curated 数据，创建或复用"供应链知识本体"后写入实体、规则、Action 和 Mapping。没有自动清理，必须记录输出 ID 后逐项删除，或直接销毁隔离环境 |
| `run_full_supply_chain.py` | `cd backend && uv run python ../scripts/data/run_full_supply_chain.py`；除完整后端（含 python_kernel_gateway）和 8 个 fixture 外，还要求带"结构化提取"标签的模型配置（供 LLM 映射建议）与 Neo4j；本体项目按手工模式创建，不再依赖 Prompt、旧文档抽取接口或 canvas/route A/B/C 流水线 | **仅限可丢弃环境。** 启动即删除名称含"供应链"的既有 Pipeline 和本体，随后上传数据、创建并运行 python 引擎 Pipeline（脚本内嵌演示数据）、调用可能计费的模型（映射建议）、创建映射并写 Neo4j；脚本不做收尾清理 |
| `run_sentinel_real_data_e2e.py` | `cd backend && uv run python ../scripts/data/run_sentinel_real_data_e2e.py --base-url http://127.0.0.1:8000 --username USER --password PASSWORD`；要求真实供应商工作簿及 PostgreSQL、Redis、NATS executor、Neo4j、MinIO、n8n、Chromium CDP 完整真实栈 | 创建本体版本、Dataset 版本、Mapping、Sentinel、Action 及图查询投影并验证级联；只关闭 HTTP client，不删除业务数据。仅在隔离的 staging/canary 运行，完成后销毁环境。当前 `--password` 会进入进程参数，不能传生产凭据 |

## 执行规则

1. 先确认目标 URL、数据库和容器不是共享或生产环境；`run_full_supply_chain.py`
   不满足此条件时禁止执行。
2. 先运行脚本提供的 dry-run；没有 dry-run 的三个真实链路脚本只能在可整体销毁
   的隔离环境运行。
3. 外部模型调用前确认供应商、模型和费用上限。
4. 输出、日志和验证证据写入 `.artifacts/manual-e2e/`，不得提交到 Git。
5. 在 PR 描述中记录命令、环境、结果、未执行项与清理方式。
