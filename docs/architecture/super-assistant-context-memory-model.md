# 超级助手上下文、记忆与知识模型（提案）

状态：讨论稿

本文细化顶层架构中的 Context Planner、Memory、Knowledge Graph 和 Artifact 边界，目标是让超级助手“更懂用户”而不把所有历史数据塞入每次模型请求。

## 1. 四个上下文平面

超级助手的上下文不是一张无限增长的消息列表，而是四个有不同生命周期和权威级别的平面：

| 平面 | 内容 | 生命周期 | 默认是否进入模型 |
|---|---|---|---|
| Identity | 用户明确的身份、稳定偏好和安全约束 | 长期 | 只注入与当前目标有关的部分 |
| Knowledge | 用户资料、文件、图谱实体和来源证据 | 长期，可修订 | 先注入索引和证据摘要 |
| Episode | 会话摘要、已确认决定、任务经验和反思候选 | 中长期 | 按当前目标召回 |
| Working | 当前 Run、计划、未决问题、Call 和 Artifact | 当前任务 | 当前 Run 需要的部分 |

能力目录和 Agent Descriptor 是第五类运行时输入，属于 Capability Context；它只描述可用能力和约束，不把插件说明全文或远端会话历史常驻进模型。

## 2. Context Source

每个来源以统一的 Context Source 接口提供候选，不要求 Memory、Neo4j、PostgreSQL 和对象存储使用同一种实现。

```text
source_id
source_type
owner_scope
content_ref
summary
authority
confidence
sensitivity
citation
source_version
valid_from / valid_to
ttl
token_cost
permissions
```

`content_ref` 可以指向数据库正文、对象存储文件、图谱事实或历史事件。候选本身应尽量短，模型需要原文时再通过受控能力展开。

来源提供事实，不提供系统指令。文件、网页、远端 Agent 消息和插件返回内容中的指令都按不可信数据处理。

## 3. Context Pack 生成流程

```text
user input / external event
        ↓
identify task and required context
        ↓
query Context Sources in parallel
        ↓
permission and sensitivity filter
        ↓
deduplicate and detect conflicts
        ↓
rank by relevance, authority, freshness and cost
        ↓
assemble bounded Context Pack
        ↓
write context.snapshot
        ↓
reconstruct model request
```

Context Planner 必须记录：

- 使用过的来源 ID 和版本；
- 被过滤的高敏感来源及原因摘要；
- 选择和淘汰的 token 预算；
- 冲突或低置信度标记；
- 引用和展开入口；
- 生成该 Pack 的查询和策略版本。

Context Pack 是一次模型请求的输入快照，不是永久修改 Memory。下一次请求需要重新计划，除非 Working Context 明确延续。

## 4. 推荐的请求结构

```text
stable system rules
current run goal and acceptance criteria
confirmed decisions and unresolved questions
relevant knowledge index with citations
relevant episode summaries
working plan and completed evidence
capability summaries required for this step
recent conversation messages
ordered capability results
```

常驻 system prompt 不应包含全部记忆、所有 Skill 全文、全部 MCP schema 或完整图谱。模型需要详细资料时，使用带权限检查和来源引用的展开能力。

## 5. 来源权威与冲突

不设置跨领域的固定“某种来源永远优先”。实际判断遵循：

1. 用户本轮明确表达优先于旧偏好；
2. 事实判断结合来源、时间、版本和领域验证；
3. 原始资料通常比抽取摘要更适合核验；
4. 已确认的用户偏好优先于模型推断；
5. 互相冲突时显示冲突和来源，不静默覆盖。

每个回答都不必展示全部来源，但当回答依赖私人知识、图谱事实、外部 Agent 结果或不确定推断时，应保留可展开的引用。

## 6. 私人知识与图谱

### 6.1 导入

用户文件导入后，文件本身是原始来源，抽取任务产生带 `source_file_id`、内容范围和抽取版本的实体、关系和属性。重复导入使用文件 hash 或版本号去重，不能因为一次抽取失败删除上一版有效知识。

抽取版本由两部分组成：源资料的 `source_version`（文件 hash、文档版本或同步游标）和抽取配方的不可变 `recipe_revision`。抽取 prompt、模型身份或配置快照、工具集合、分块器参数、归一化 schema、抽取代码或安全过滤规则任一变化，都必须生成新的 `recipe_revision`；普通重试复用原配方，不产生新知识版本。每次抽取保存 `extraction_id`、输入范围、配方 revision 和输出 hash。

### 6.2 使用

图谱检索返回：

```text
entity / relation
source file and location
extraction version
confidence
validity
```

图谱事实是可修订的推导，不自动比原文更可信。回答需要高确定性时，先返回图谱摘要，再允许读取原始来源核验。

### 6.3 删除和版本

删除或撤销原文件时，关联事实进入失效、待重算或删除状态。历史事件可以保留无正文的审计信息，但检索器不能继续把已撤销事实作为当前知识返回。

图谱合并、实体更名和关系修订必须有来源和版本，不能只更新当前 Neo4j 节点而失去可追溯性。

新生成的图谱事实必须带结构化 `source_ref`，至少包含 `source_version`、内容定位（chunk/页码/偏移等）、`recipe_revision` 和 `extraction_id`。Neo4j 可以继续承载实体和关系投影，但来源记录以可删除、可校验的 provenance 引用保存；检索时必须过滤已撤销的来源。旧的 `file_ids/source_files` 仅作为兼容展示字段，不能代替新抽取的版本和定位信息。

## 7. 长期记忆

记忆分为三种来源：

- `explicit`：用户明确要求记住或明确提供的长期事实；
- `derived`：从文件、任务或对话中推导出的候选；
- `reflection`：反思流程提出的偏好、经验或技能候选。

建议的写入流程：

```text
memory candidate
→ normalize to one fact
→ attach source and confidence
→ conflict / duplicate check
→ risk policy
→ auto-accept or user approval
→ versioned write
→ update index
```

低风险、可撤销的偏好可以按用户设置自动接受；敏感身份、权限、财务、健康和第三方信息默认进入确认。具体默认值需要结合平台设置设计，不能由模型自行决定。

`derived` 和 `reflection` 记忆的来源必须使用结构化 `source_ref`（例如消息、Run、文件及其版本或 Artifact），不能只写自由字符串。来源删除、撤销或不可访问时，记忆索引和 Context Pack 必须按该引用传播失效；没有可验证来源的候选只能保留为待确认候选，不能晋升为当前事实。

记忆修改采用新版本或 supersedes 关系，不覆盖原始来源。用户纠正事实后，旧事实应失效并保留纠正来源。用户删除记忆时，后续召回和 Context Pack 都必须排除它。

## 8. Working Context 与任务记忆

Working Context 保存当前 Run 的：

- 目标和验收标准；
- 用户已经确认的选择；
- 计划和已完成证据；
- 未决问题；
- Call 状态和外部任务 ID；
- Artifact 引用；
- 当前预算和截止时间。

这些内容必须由 Run/Event 状态保存，不能只依赖模型摘要。摘要可以帮助模型阅读，但不能替代结构化任务状态。

## 9. Compaction

压缩不是简单删除旧消息，而是一次可追溯的 Context 变换：

```text
selected messages / events
→ summary with source references
→ verify unresolved decisions and active calls
→ context.snapshot(compaction_version)
→ use summary + required recent messages
```

压缩必须保留：

- 用户明确决定；
- 当前目标和验收标准；
- 未决问题；
- 未完成或未知结果的 Call；
- Artifact 和来源引用；
- 权限和版本约束。

压缩后的摘要不能声称包含被删正文的全部细节。需要核验时必须回到事件、来源或 Artifact。

## 10. 外部 Agent 的数据边界

外部 Agent 默认只收到本次 Invocation 的最小 Context Pack：

- 当前任务和必要背景；
- 用户明确允许的资料引用；
- 已确认的本体、版本或工作区范围；
- 输出格式和验收要求。

它不能自动读取全部 Conversation、Memory、Knowledge Graph、插件凭据或其他 Agent 的会话。发送前执行敏感信息过滤和出站审批；返回的消息、Artifact 和建议按不可信数据处理。

## 11. Artifact 与上下文

Artifact 是可独立验证和引用的上下文来源：

```text
artifact_id
run_id / call_id
kind
name
mime_type
storage_ref
size
checksum
status
source
created_at
```

Artifact 的“传输完整”与“业务正确”分开记录。校验和通过不代表报告、代码或模型结果满足任务目标。下一次模型请求默认只注入 Artifact 摘要和引用，需要全文时再展开。

新执行内核产生的持久化 Artifact 默认由 Artifact 服务写入 MinIO 或等价对象存储；现有会话附件和 Palace 的 `SessionWorkspace` 本地文件在迁移期保持原存储，不因本设计自动迁移。两类存储都必须通过统一的 `storage_ref` 和权限/校验接口暴露，避免把“复用 MinIO”误解为立即搬迁存量文件。

## 12. Context Planner 验收

必须覆盖：

- 相关上下文优先于全量历史；
- 超出预算时按可解释规则裁剪；
- 当前用户纠正覆盖旧偏好；
- 已删除文件和记忆不再召回；
- 图谱事实可追溯到文件和抽取版本；
- 图谱抽取配方变化会生成新的 `recipe_revision`，来源定位和输出 hash 可校验；
- 冲突不会静默消失；
- 没有结构化 `source_ref` 的 `derived/reflection` 记忆不会晋升，来源删除会传播到索引和 Context Pack；
- 外部 Agent 只能收到批准范围内的内容；
- 压缩后 Run 状态、未决问题和未知 Call 保留；
- 请求快照能重建来源、版本、权限和模型可见内容；
- 重启或重复 Context 查询不会生成重复记忆或重复图谱事实。

具体检索算法、向量索引、默认 token 预算和审批默认值在本层之后单独设计。
