# 本体业务域

本目录承载本体定义、版本、映射、Formal Runtime、Sentinel 和 Action。第一次
修改前应先阅读本目录及 `backend/tests/ontologies/` 中的状态门与发布契约测试；
这里同时存在 canonical Formal 实现和受控的 v1 兼容读模型，不能仅凭文件名
批量移动或删除。

```text
ontologies/
├── access.py              本体访问与权限守卫
├── release_context.py     当前 release scope 与写入围栏
├── runtime_fence.py       Mapping/Action/Sentinel/Release 共享的本体变更锁
├── projects/             本体项目与访问入口
├── formal_modeling/      Object/Link/Function/Action 定义、实例、Fact 与执行
├── versions/             快照契约、Draft、Trial、Impact、Release 与 Rollback
├── mappings/             数据集到 Object/Link 的映射、对账与查询投影
├── sentinels/            定义、CDC、评估、扫描、动态策略与触发记录
├── graph/                Neo4j 查询、分析与 NL-to-Cypher
├── agent_runtime/        本体 Agent 编排、工具、边界和报告
├── mcp/                  账号密码认证的单本体只读 MCP 接入与查询适配
├── decision_simulation/  决策模拟
├── extraction/           供现存模型调用复用的 LLM gateway；无抽取 API 或图/向量 bridge
├── files/                供业务探索/数据管家复用的通用文档转换器；无本体文件 API
├── export|audit/         导出与审计
└── entities|relations|logic|actions|inference|attribute_schemas/
                          迁移期 v1 兼容 API/模型或专项能力
```

## 关键边界

- 发布链固定为 Draft → Trial/Impact → Promote；发布后的 release 快照不可原地
  改写，回滚创建新的 activation 语义而不是覆盖历史。
- `mappings/` 按身份元数据、关系处理、实体对账、投影和候选发现拆分；
  `mapping_service.py` 保留 release scope、数据读取、apply/build 事务编排和
  Sentinel barrier；共享锁的 canonical 实现在 `runtime_fence.py`，旧路径仅
  显式重导出并继续尊重调用时 monkeypatch。
- `formal_modeling/action_engine.py` 是 Action 公共入口；参数/定义校验、运行值与
  契约、执行记录、独立持久化/通知 effect 和阶段上下文分别位于同目录模块。
  共享同一有序虚拟状态与事务的核心 effect 仍保持整体，避免拆出错误的跨事务
  handler。
- `sentinels/` 只在精确 release fence 内运行；项目门禁、查询、定义 CRUD 和
  运行态操作不在 router。Mapping、Action 和发布使用同一锁/CDC 屏障约束。
- `versions/snapshot_contract.py` 是无数据库/Mapping/Sentinel/Action 依赖的
  快照归一化、hash、编号和 model projection 叶子契约；
  `evolution_service.py` 保留历史对象重导出。
- `versions/router.py`、`mappings/router.py` 和 `formal_modeling/router.py`
  是 HTTP/兼容适配入口；release gate/activation、Mapping 工作流和 Formal
  业务规则位于同域 service。
- `entities/relations/logic/actions` 中仍有迁移兼容表和接口；生产写入限制、Formal
  投影和删除条件必须以源码守卫与测试为依据，不能把它们误认成第二套 canonical
  运行时。
- 旧文档 → 本体链路的 files/execute/v2 extraction API 与专用表已按 ADR-0003
  退役。不得把保留的通用文档转换器或 LLM gateway 重新解释为可公开调用的
  本体抽取流程；已无运行时引用的 legacy 图/向量 bridge 也不再保留。

包含函数内延迟 import 的完整生产依赖图只保留一个被精确锁定的运行期环：
`sentinels.cdc ↔ sentinels.engine ↔ sentinels.dynamic_service`。四条允许边分别
对应持久事件消费、调度事件入队/排空、release overlay reconcile 和启用
activation outbox，事务所有权跨越三者。静态 top-level import 图的只读审计
无多节点环；包含函数内 import 的架构守卫则精确拒绝除上述四条边外的任何新增
环。Mapping、Action、Release 和 legacy facade 均不在该运行期环中。改变这
四条边必须先设计 Outbox handler 注册和事务迁移，不能用隐藏动态 import
假装断环。领域行为以本目录实现和对应测试为准。
