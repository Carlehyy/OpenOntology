# 任务实例（Task Instances）设计方案 v1.0 —— 评审版

| 项 | 内容 |
|---|---|
| 日期 | 2026-10-11 |
| 状态 | **已交付上线**（M1-M4 全量完成 + 栈级真实 E2E + P1-P4 弹窗产品化），线上 http://64.83.14.139:8088 |
| 作者 | 需求方（平台维护者）× ZCode 协同 |
| 实施分支 | 已合入 nano-ontoprompt（原分支 tmp/super-assistant-taskinst-20261010） |
| 评审方式 | 建议逐节审阅，重点攻击 §15 风险清单与 §2 决策记录 |

**评审指引（给审查同事的导览）**：本方案已与需求方多轮对齐（愿景 → 选型 → 形态 → 本文档）。最值得挑战的六处：
① §4.4 契约子集是否够用/过度；② §6.3 Claude Code 只认 Anthropic 协议的模型约束是否可接受；③ §6.5 插话到"运行中"节点依赖 Claude Agent SDK 能力，属未验证假设（M2 首周 spike）；④ §6.6 Docker 安全模型的残余风险；⑤ §5 状态机与竞态防护的完备性；⑥ §14 工期现实性。其余章节为事实性内容，可抽查证据索引（附录 B）。

---

## 1. 背景与目标

### 1.1 要解决的问题（需求方原话提炼，已逐条确认）

1. **固定流程自动化**：流程固定、步骤明确的任务应能自动执行，不需要人逐步驱动；
2. **关键节点人工审批**：人只在关口出现，批准或打回；
3. **随时查看最新进展**：进度真相 = 状态机 + 事件，随时可查；
4. **中途插话**：能把内容发送给**当前正在执行的节点**，当下生效（不是排队到下一节点）；
5. **DAG + 多 Agent + 明确契约验收**：借鉴 Homerail 的轨道与分层验收；
6. **人也是生产者**：既支持 Homerail 型（发起/审批/监督/插话），也支持 qwenpaw 型（人作为节点接单干活）；
7. **条件分流**：按上游产出内容路由（例：代码变更涉及前端 → 人工把关；分类为缺陷修复 → agent 直接接力）；
8. **驳回-修复环一等原语**：打回上游重做并携带驳回理由；
9. **Worker 形态**：参考 Homerail——Claude Code（SDK）+ Docker 容器作为 agent 执行体。

### 1.2 定位（锁定）

**任务实例是一个独立且强大的运行时功能域**。超级助手本期零参与；后续版本超级助手仅作为上层调用方（触发/查看/介入）接入。kernel.v1（`backend/app/super_assistant/kernel/`）与 LangGraph 均不参与本功能。

### 1.3 成功标准

- **短期**：1~2 个参考 Homerail 移植的真实流程（pr-review 仲裁评审 / release-notes / topic-outline）在平台上完成「发起 → 离开 → 关口审批 → 收货」闭环，全程不盯屏；
- **中期**：超级助手接入为客户端，"对话发起任务、任务自动跑、关口回来找人"成为平台标志性体验；
- **长期**：任务轨道成为平台自动化底座，各业务域固定流程可长入。

### 1.4 非目标（v1 明确不做）

DAG 画布编辑器（YAML 是唯一作者面，画布仅只读投影）、foreach/while 循环节点、join n_of_m、定时/事件触发器（手动激活 only）、scorecard/eval 事后评分、超级助手接入、入站模型网关适配器（放开非 Anthropic 模型驱动 Claude Code）、多租户 SaaS 化。

---

## 2. 关键决策记录（每条含被否替代方案）

| # | 决策 | 理由 | 被否替代 |
|---|---|---|---|
| D1 | 独立新域 `backend/app/task_instances/`，不依赖任何现有业务域 | 需求方明确定位为独立运行时功能 | 挂 super_assistant 域（被否：定位错误）；复用 kernel.v1（被否：需求方明确搁置） |
| D2 | 引擎 = 事件溯源 + 事务内单反应器推进（qwenpaw 范式）+ Homerail 网关语义 | qwenpaw 已用黄金轨迹与 50+ 次竞态测试验证此范式的正确性密度；同栈可平移 | 引入 LangGraph（被否：新重依赖违反仓库依赖政策；其 checkpoint 与 PG 状态机形成双真相源；契约/进度/插话层横竖自建） |
| D3 | 流程语言 = 自有 YAML WorkflowSpec（借鉴 Homerail v1 信封/节点/端口/契约结构） | 需求方指定 YAML 作者面；Homerail 已验证「YAML 事实源 + UI 投影」分工 | 表单编辑器（被否：图拓扑编辑体验差，需求方否决）；JSON（可读/可注释性差） |
| D4 | 契约 = 有界 JSON Schema 子集，pydantic 自研校验器，零新依赖 | 平台无 jsonschema 依赖；Homerail 证明有界子集足够表达交接契约 | 引入 jsonschema 库（被否：依赖政策，且子集可控性更好） |
| D5 | 契约可选、逐端口收紧 | 化解"契约过严卡死探索 / 过松假质量"两难（Homerail InputPort/OutputPort 的 contract 为可选字段） | 全端口强制契约（被否：作者面成本陡增） |
| D6 | 契约违规 → 有界纠正环（默认 2 次，携契约细节重派同节点）→ 耗尽失败 | Homerail 已验证语义 | 直接失败（被否：LLM 产出一次不合规很常见） |
| D7 | 驳回-修复 = 一等原语（approval rejected / human reject / 下游 reject → 上游新建 attempt 携 rejection 上下文；每边上限默认 3） | 需求方明确要求；qwenpaw 已验证（含熔断与 initiator 豁免思想） | Homerail 式用拒绝端口手工回接边（被否：非一等公民，语义易错） |
| D8 | Worker = compose 独立服务 `task_worker`，Claude Agent SDK 驱动无头 Claude Code，每 agent 节点一个 Docker 容器 | 需求方指定；pipeline_executor（同镜像不同入口）是现成部署先例 | 进程内直接调 LLM（被否：执行体强度不足）；外部 qwenpaw 实例（被否：需求方明确不接受） |
| D9 | 模型注入 = 容器 spawn 时从 model_configs 解析 → 环境变量（ANTHROPIC_BASE_URL/AUTH_TOKEN/MODEL），密钥不落库不进模板 | 平台 llm_gateway 为出站桥接、无入站网关（已核实）；平台密钥治理要求 | 建 Anthropic 协议入站网关（被否：v1 范围外，见非目标） |
| D10 | 模板版本化 = canonical_hash 幂等 revision；实例钉死创建时 revision 快照 | Homerail compile→hash→revision 三件套 + qwenpaw 快照教训 | 无版本化（被否：qwenpaw 需求文档自认的缺陷） |
| D11 | 进度消费 = SSE（instance.snapshot 首帧 + 事件流）+ 轮询兜底 | kernel.v1 与定时任务页的成熟范式；事件权威、投影独立 | WebSocket（被否：平台 SSE 先例充分）；LLM 定期摘要（被否：两边的源码调研均证伪） |
| D12 | 人工待办/审批通知投递平台 inbox（publish_event） | inbox 已有 task kind、role/user 定向、审批通知先例 | 新建通知系统（被否：重复建设） |

---

## 3. 概念模型与术语

| 概念 | 定义 | 关键属性 |
|---|---|---|
| **Template（模板）** | 一个声明式流程的命名容器 | name、description、latest_revision |
| **Revision（版本）** | 模板的一次不可变快照，YAML 编译产物 | revision_no、spec、canonical_hash（内容不变不建新版） |
| **Instance（任务实例）** | 模板某 revision 的一次激活执行（= 需求方口中的"任务实例"） | goal、inputs、spec 快照、状态、计数器 |
| **NodeRun（节点运行）** | 实例中某节点的一次执行尝试（attempt 递增承载纠正与打回） | node_id、attempt_no、inputs/output、inputs_hash、租约 |
| **Contract（契约）** | 命名的有界 JSON Schema 子集，挂接在端口上 | 校验于 handoff 时 |
| **Approval（审批）** | 关口节点的等待-决策记录 | proposal 快照 + hash、过期、决定（含驳回理由） |
| **Steering（插话）** | 发送给运行中节点的中途消息 | queued→delivered→acknowledged |
| **Artifact（产物）** | 节点交付的可校验文件 | sha256、MinIO storage_uri |
| **Event（事件）** | append-only 事实源，一切状态的来源 | (instance, seq) gap-free 唯一 |

**与参考系统的概念对照**（详细对比见附录 A）：Homerail 的 Run/节点状态机/端口契约/审批门/await_command → 本设计的 Instance/NodeRun/Contract/Approval/（插话在 v1 是节点内注入而非跨轮等待原语）；qwenpaw 的 FlowRule/Task/WorkUnit/事件/驳回修复 → 本设计的 Template+Revision/Instance/NodeRun/Event/Rework。

---

## 4. 流程语言（YAML WorkflowSpec）

### 4.1 信封结构

```yaml
api_version: openontology.task/v1     # 唯一合法值；缺省即拒绝（不设 legacy 双轨）
kind: Workflow
metadata:
  name: <str 5~64>                    # 模板名（同名校验）
  description: <str 可选>
contracts:                            # 命名契约表，≤128 个
  <ContractName>: <ContractSchema>    # 见 4.4
nodes:                                # 1~200 个
  <node_id>: <NodeSpec>               # 见 4.2
edges:                                # 1~1000 条
  - from: <node>.<port>               # 端口引用语法
    to: <node>[.in]                   # 目标节点（或其命名入口）
    rework: false                     # true = 打回边（携带驳回理由，计入每边驳回上限）
policies:                             # 全局预算，防失控
  max_parallelism: 8                  # 并发 running 节点上限
  corrections_per_node: 2             # 契约违规纠正上限
  rework_per_edge: 3                  # 每条打回边最大重做次数
  event_budget: 10000                 # 单实例事件总数上限
```

### 4.2 节点类型（6 种，判别字段 `kind`）

| kind | 必填字段 | 可选字段 | 语义 |
|---|---|---|---|
| `agent` | `system`（提示词模板） | `model`（节点级模型覆盖，见 §6.4）、`outputs`（命名端口→契约/必交产物）、`timeout_minutes`（默认 30） | 派发给 Docker 容器内 Claude Code 执行 |
| `human` | `role`（角色说明） | `deliverable`（期望产出描述，展示给人）、`outputs` | 生成人工待办，人提交 done（产出）或 reject（打回其上游） |
| `approval` | `approvers`（角色/用户列表，v1 取平台角色） | `expires_hours`（默认 72，过期自动 rejected）、`proposal_from`（默认直接上游） | 关口：approved/rejected 双端口 |
| `condition` | `on`（上游引用）、`branches`（`when` 谓词 + `to` 目标列表） | `default`（无匹配时的目标，未配置且无匹配 = 校验错误） | 确定性路由：对上游产出字段做 equals/in/matches 判定 |
| `join` | `mode: all\|any` | — | 汇合策略；n_of_m 留 v2 |
| `terminal` | `outcome: success\|failure` | — | 终点 |

`agent`/`human` 的提示词与待办中可引用上游产出：`{{nodes.<id>.output.<field>}}` 模板变量，编译期校验引用存在性。

### 4.3 校验规则（`POST /templates/validate`，保存与激活前强制）

结构：api_version/kind 合法；节点数/边数/契约数在界内；节点 id 唯一且合法标识符。
图论：边端点必须引用存在的节点与端口；**Kahn 拓扑 + 环检测（报出环路径）**；至少一个 terminal 可达；condition 分支目标存在。
契约：端口 `contract` 必须引用已命名契约；同名契约 schema 合法（递归深度/属性数受限）；打回边目标不能形成"仅含打回边"的环（打回边不参与拓扑序，但受 rework_per_edge 上限约束，编译期单独检测打回环）。
语义：approval 的 approvers 非空；human 有 role；condition 有 default 或分支穷举；必交产物声明的字段在契约 required 中。

### 4.4 契约子集（有界 JSON Schema）

允许关键字（白名单制，越界即校验错误）：`type`（object/array/string/number/integer/boolean）、`enum`、`const`、`required`、`properties`、`additionalProperties`、`items`、`minItems/maxItems`、`minLength/maxLength`、`minimum/maximum`、`pattern`。界限：嵌套深度 ≤8、总属性 ≤64、enum 项 ≤64、pattern 长度 ≤256（防 ReDoS，仅 anchored 简单量词）。实现：pydantic v2 构建解释器（~200 行），**零新依赖**。

### 4.5 版本化语义

- 保存模板 = 提交 YAML → 编译 → 归一化 canonical JSON → sha256 = canonical_hash；`(template_id, canonical_hash)` 唯一——**内容不变不建新 revision**；
- revision 不可变（只增不改）；「回滚」= 将旧 revision 的 spec 重新保存（内容相同则天然幂等命中既有 revision）；
- **实例激活时深拷贝 revision spec 为实例快照**——此后模板任何演进不影响已存在实例；
- 新实例默认绑定模板 latest revision，激活时可显式指定 revision。

### 4.6 示例（需求方给的条件分流场景）

```yaml
api_version: openontology.task/v1
kind: Workflow
metadata: { name: 代码变更评审 }
contracts:
  ChangeSummary:
    type: object
    required: [category, summary]
    properties:
      category: { enum: [frontend, backend, defect_fix] }
      summary:  { type: string, minLength: 1 }
      files:    { type: array, items: { type: string } }
nodes:
  analyze:
    kind: agent
    system: 分析本次代码变更，按契约产出结构化摘要
    outputs: { done: { contract: ChangeSummary } }
  route:
    kind: condition
    on: analyze.done
    branches:
      - { when: { field: category, equals: frontend },   to: [human_review] }
      - { when: { field: category, equals: defect_fix }, to: [auto_fix] }
    default: [human_review]
  human_review:
    kind: human
    role: 前端变更评审（可修改产出再交活）
  auto_fix:
    kind: agent
    system: 按上游摘要执行缺陷修复并产出补丁说明
  gate:
    kind: approval
    approvers: [admin]
  end_ok: { kind: terminal, outcome: success }
edges:
  - { from: human_review.done, to: gate.in }
  - { from: auto_fix.done, to: gate.in }
  - { from: gate.approved, to: end_ok.in }
  - { from: gate.rejected, to: auto_fix, rework: true }
```

---

## 5. 执行引擎（`backend/app/task_instances/`）

### 5.1 状态机

**实例存储态**：`active | cancelling | completed | failed | cancelled`。
派生投影 `needs_attention`（供看板/通知）：存在 `waiting_approval` 或 `waiting_human` 节点。

**NodeRun 状态**：`pending →(join 满足) dispatched →(worker 认领) running → completed | failed`；
分支：`dispatched/waiting_human`（human 节点直接进入）、`waiting_approval`（approval 节点）、`voided`（被取消/被驳回作废）、`skipped`（条件分支未选中路径的下游不可达传播）、`cancelled`。
**attempt_no 语义**：契约纠正与打回重做均递增 attempt，inputs 附 `correction`/`rejection` 上下文（qwenpaw 修复单范式：修复提示明确"只能改自己的产出"）。

### 5.2 事件溯源与推进规则（核心正确性设计，移植 qwenpaw 已验证范式）

- 一切状态变更 = 事务内「append event(s) + 物化 node_run/instance 行」，**事件表 append-only，(instance_id, seq) 唯一且 gap-free**；
- **单反应器规则 R**：`node.completed` 事件追加后，同一事务内评估该节点所有出边 → condition 谓词判定 → join 条件检查（all：每条入边均有"最新有效产出"；any：任一即触发；**被驳回过的产出不计入有效产出**）→ 满足则幂等创建下游 NodeRun（`(instance, node, inputs_hash)` 已存在则跳过）；
- **PG 行锁替代 qwenpaw 的 SQLite BEGIN IMMEDIATE**：反应器入口 `SELECT ... FOR UPDATE` 锁实例行串行化推进；
- **status guard 事务内重读**（`populate_existing`）：创建每个下游 NodeRun 前、追加实例终态事件前各重读一次实例状态，非 `active` 静默放弃副作用——防"取消请求与推进竞态"（qwenpaw T11 集成测试验证过的场景）；
- **执行事实与传输分离**（平台既有不变量）：先落库提交，事务外再发 NATS 派发消息；NATS 只负责送达，丢消息由对账兜底。

### 5.3 契约验收（handoff 时硬校验，Manager 侧）

worker 提交产出时依次校验：① 产出 JSON 可解析；② 端口契约（4.4 子集）校验，违规 → `contract.violated` 事件 → **有界纠正环**（重派同节点 attempt+1，提示词附违规细节与契约原文，上限 corrections_per_node）→ 耗尽 → 节点 failed；③ 必交产物存在且 sha256 一致（产物先经 MinIO 落盘，见 §7/§11）。

### 5.4 驳回-修复环（一等原语）

触发源：approval rejected（携理由）、human 节点 reject（打回其直接上游）、API 手动打回。
动作（单事务）：目标上游节点当前 attempt → `voided` + `node.rejected(reason)` 事件 → 上游新建 attempt（inputs 携带原产出引用 + rejection 上下文）→ 其下游链条按"最新有效产出"规则自然重算。
熔断：`rework_per_edge`（默认 3）；到达上限后驳回请求返回 409 `REWORK_BUDGET_EXHAUSTED`，只能取消实例。

### 5.5 取消、超时与恢复

- **取消两阶段**：`POST /instances/{id}/cancel` → 实例置 `cancelling`（冻结一切推进，status guard 拦截）→ 后台级联：running 节点发容器终止，pending/waiting 节点 → cancelled/skipped → 终态 `cancelled`；
- **节点超时**：`timeout_minutes` 到期 → attempt 失败（计入纠正预算）；
- **租约与对账**：NodeRun 派发/认领时写租约（owner + expires，心跳续约）；APScheduler 对账任务（30s，bootstrap/lifecycle.py 注册，旁路失败不阻断启动——平台既有约定）扫描：租约过期的 running 节点 → 按预算重派或失败；审批过期 → 自动 rejected（`actor: system:expiry`）。

### 5.6 事件类型（冻结枚举，v1 约 20 种）

`instance.created / instance.terminal(.completed|.failed|.cancelled) / node.created / node.dispatched / node.claimed / node.progress / node.completed / node.failed / node.voided / contract.violated / node.correction_requested / approval.requested / approval.decided / approval.expired / human.assigned / human.submitted / node.rejected / steering.delivered / artifact.declared / artifact.completed`。事件 payload 按 type 定义必填字段（append 时强制校验，借鉴 kernel events 的 REQUIRED_PAYLOAD）。

---

## 6. Worker 基建（Claude Code + Docker）

### 6.1 部署形态

- compose 新增服务 **`task_worker`**（三份 compose 同步：prod/local/e2e）——**先例：`pipeline_executor`**（同 backend 镜像、不同入口命令，docker-compose.prod.yml:224）；
- task_worker 挂载 `/var/run/docker.sock`，通过 **docker CLI 子进程**管理执行容器（不新增 Python Docker SDK 依赖）；
- 执行容器镜像 `openontology-task-agent`：node:22-slim + Claude Agent SDK（Python 宿主 or Node？**取 Node 侧 `@anthropic-ai/claude-agent-sdk`**，与 Claude Code 同源运行时）+ 最小工具链；Dockerfile 落 `docker/task-agent/`，构建进 compose。

### 6.2 容器生命周期

- 每个进入 `dispatched` 的 agent 节点 = 一个容器实例（name = `taskinst-<node_run_id>`）；
- workspace：宿主 `uploads/task-instances/<instance_id>/<node_id>/`（bind mount 只到该目录，容器内 `/workspace` 唯一可写路径；只读 rootfs）；
- 容器随节点终态销毁；产物在节点完成时由 task_worker 上传 MinIO 后才认可（防容器销毁丢证据）。

### 6.3 模型注入与约束（评审重点 ②）

- spawn 时由 task_worker 从 model_configs 解析所选配置 → 注入容器环境变量 `ANTHROPIC_BASE_URL / ANTHROPIC_AUTH_TOKEN / ANTHROPIC_MODEL`（+ 可选小模型变量）；**密钥只存在于容器环境，不落库、不进事件、不进模板**；
- 节点级覆盖：`nodes.<id>.model` 引用 model_configs 条目；未设置 → `select_llm_model_config(purpose_tags=("task_instances",))` 平台默认（复用 backend/app/model_configs/selector.py 语义）；
- **硬约束**：Claude Code 只讲 Anthropic 协议 → 可选配置限于 provider=anthropic（或暴露 Anthropic 兼容 base_url 的条目）；平台现有 openai 兼容配置**不能**驱动 agent 节点。放开路径（v2+）：入站 Anthropic 协议网关。前端在模板编辑与节点配置处对此做明确提示。

### 6.4 Claude Agent SDK 集成（评审重点 ③：含未验证假设）

- task_worker 内以 SDK 起 headless query：system + 渲染后的任务上下文（含上游产出注入）进，流式事件出（进度透传为 `node.progress` 事件，SSE 到前端）；
- 权限模式：默认拒绝容器内一切需要确认的高危操作（SDK permission mode 锁定），工具面限制在 workspace 读写 + 声明过的命令白名单；
- 产出：约定 agent 以结构化 JSON（约定 schema 的最终消息 / 或写 workspace 文件）交付 → task_worker 解析并提交 handoff；
- **插话（Steering）**：API → engine 落库（queued）→ NATS `task_instances.control`（kind=steer）→ task_worker 向运行中 query 注入追加输入；SDK 的运行中追加输入与中断能力**须在 M2 首周 spike 验证**；**降级路径**：若 SDK 不支持真注入，则插话排队到当前 attempt 结束后作为下一 attempt 的附加上下文（产品语义降级为"节点边界生效"，须回报需求方重审）。

### 6.5 安全模型（评审重点 ④）

| 威胁 | 缓解 | 残余风险 |
|---|---|---|
| 恶意模板（YAML 注入/危险提示词） | 模板编辑需 `task_instances` 菜单权限（admin/editor）；YAML 仅数据不执行；契约/提示词无宿主动作 | 低 |
| 容器逃逸/提权 | 容器内无 docker.sock、只读 rootfs、单一可写 workspace、非 root 用户运行 | 中：默认 egress 未锁（见下行） |
| 容器外联（数据外泄/ SSRF） | v1：独立 docker bridge 网络，egress 仅放行模型端点目标（iptables/网络策略尽力而为，明确标注非硬保证） | 中：容器级网络隔离弱于 Homerail 的完整方案，staging 验收时实测并记录 |
| 密钥泄漏 | 密钥仅在容器 env；日志与事件脱敏（平台 _safe_error_message 同款正则先例） | 低 |
| worker 单点 | task_worker 无状态（状态全在 PG），compose restart 策略 + 租约对账收口孤儿容器 | 低 |
| 资源耗尽 | max_parallelism、policies 预算、容器 CPU/内存限额（compose deploy resources） | 低 |

生产启用条件：按 AGENTS.md §5 真实环境条款，Docker/隔离/网络策略在隔离 staging 验收通过后方可上生产；未验收环境的 task_worker 服务可禁用（模板/人工/审批链路照常，仅 agent 节点不可派发）。

---

## 7. 数据模型（PostgreSQL，Alembic 迁移挂 0120 之后，编号 0121）

| 表 | 字段（关键） | 索引/约束 |
|---|---|---|
| `task_templates` | id(uuid PK)、name、description、latest_revision_id(FK→revisions, SET NULL)、created_by(FK users)、created_at/updated_at、deleted_at(软删) | name 唯一（活跃范围内） |
| `task_template_revisions` | id(PK)、template_id(FK)、revision_no(int)、spec_yaml(text)、spec_compiled(JSONB 归一化)、canonical_hash(char64)、note、created_by、created_at | UNIQUE(template_id, revision_no)、UNIQUE(template_id, canonical_hash) |
| `task_instances` | id(PK)、template_revision_id(FK)、name、goal(text)、inputs(JSONB)、spec_snapshot(JSONB)、status(check §5.1)、created_by、created_at/updated_at/finished_at、fail_reason | idx(status, created_at desc)、idx(created_by) |
| `task_node_runs` | id(PK)、instance_id(FK)、node_id(varchar)、attempt_no(int default 1)、status(check §5.1)、inputs(JSONB)、inputs_hash(char64)、output(JSONB)、error(text)、correction_count、rework_count、model_config_id(FK nullable)、lease_owner、lease_expires_at、dispatched_at/started_at/finished_at | UNIQUE(instance_id, node_id, attempt_no)、UNIQUE(instance_id, node_id, inputs_hash, status) 派发幂等、idx(instance_id, status) |
| `task_events` | id(bigserial PK)、instance_id(FK)、seq(int)、type(check §5.6)、node_run_id(FK nullable)、actor(varchar)、payload(JSONB)、created_at | UNIQUE(instance_id, seq)、idx(instance_id, id) |
| `task_approvals` | id(PK)、instance_id(FK)、node_run_id(FK)、proposal(JSONB)、proposal_hash(char64)、status(pending/approved/rejected/expired)、reason(text)、decided_by、decided_at、expires_at | idx(instance_id, status) |
| `task_steering_messages` | id(PK)、instance_id(FK)、node_run_id(FK)、content(text ≤64KB)、status(queued/delivered/acknowledged/failed)、created_by、created_at/delivered_at、idempotency_key | UNIQUE(idempotency_key) |
| `task_artifacts` | id(PK)、instance_id(FK)、node_run_id(FK)、name、mime_type、size_bytes、sha256(char64)、storage_uri(text, MinIO)、created_at | idx(instance_id)、idx(node_run_id) |

迁移要求（AGENTS.md §5）：单 head；全新库 `alembic upgrade head`；0120 存量副本升级验证。事件/节点表按 instance 分区不做（v1 规模不需要，记录为 v2 演进项）。

---

## 8. API 契约（前缀 `/api/v2/task-instances`，menu guard `task_instances`）

| 方法与路径 | 语义 | 备注 |
|---|---|---|
| GET/POST `/templates` | 列表 / 创建（YAML 入参） | 创建即编译+校验 |
| GET/PUT `/templates/{id}` | 详情（含 revisions）/ 更新（新 revision，canonical_hash 幂等） | PUT 400 携结构化校验错误（行号定位）；预置模板 PUT/DELETE 403（见附录 C 2026-10-17） |
| POST `/templates/{id}/validate` | 干校验（不落库） | 编辑器「校验」按钮 |
| GET `/templates/{id}/revisions[/{rev}]` | 版本历史 / 单版 | diff 由前端行级对比两版 spec_yaml |
| POST `/templates/{id or rev}/instances` | 激活实例（goal + inputs） | 202 + `Idempotency-Key` 必填（kernel 先例） |
| GET `/instances` | 列表（状态/模板/我参与 过滤 + 分页） | |
| GET `/instances/{id}` | 详情（spec 快照 + 各节点状态树 + 产物清单） | |
| GET `/instances/{id}/events` | **SSE**：`instance.snapshot` 首帧 + 事件流 + 15s ping，终态断开 | 支持 `Last-Event-ID` 续传（kernel 先例） |
| POST `/instances/{id}/cancel` | 两阶段取消 | `If-Match` version |
| POST `/instances/{id}/nodes/{node}/approvals/{aid}/decision` | 批准 / 驳回（reason 必填于驳回） | 幂等 |
| POST `/instances/{id}/nodes/{node}/human/submit` | 人工节点交活（output JSON + 可选产物引用） | 同构于 agent handoff，过同一契约校验 |
| POST `/instances/{id}/nodes/{node}/reject` | 人工节点打回直接上游（reason 必填） | 受 rework 熔断 |
| POST `/instances/{id}/steering` | 插话（node 定位 + content） | 仅 running 节点接受；`Idempotency-Key` |
| GET `/instances/{id}/artifacts/{aid}[ /download]` | 产物元数据 / 流式下载（带 sha256 头） | |

错误码族（4xx，命名风格随平台）：`TEMPLATE_INVALID / TEMPLATE_NOT_FOUND / TEMPLATE_IS_PRESET / INSTANCE_NOT_ACTIVE / NODE_NOT_WAITING / NODE_NOT_RUNNING / CONTRACT_VIOLATION(附细节) / REWORK_BUDGET_EXHAUSTED / STEERING_UNDELIVERABLE / VERSION_CONFLICT / IDEMPOTENCY_CONFLICT`。

---

## 9. RBAC、菜单与导航

- 新 menu key `task_instances` 加入 `auth/permissions.py` `ALL_MENU_KEYS`；迁移中为存量非 admin 角色回填（0065_world_model 先例）；admin 恒全量；
- 路由挂载 `main.py`（menu_guard；GET 放宽 read_menu_keys）；
- 前端：`navigation.ts` PLATFORM_NAV_ITEMS 新增一级项（v1 建议挂"数据与智能"分组，名称「任务实例」）；`DEFAULT_NON_ADMIN_MENU_KEYS` 加入（editor 可用；viewer 默认不含——只读浏览走 read_menu_keys 放宽，评审可挑战）；`tabTitle.ts` menuKeyForPath 加 `/task-instances` 映射；`App.tsx` 加 Route。

---

## 10. 前端设计（`frontend/src/pages/task-instances/`，四视图）

1. **模板列表页**：卡片/表格 + 状态（版本数、最近激活时间）；
2. **YAML 编排页**（作者面核心）：左列模板与版本历史（revision、作者、canonical_hash 前 8 位、行级 diff、一键回滚）；中央 CodeMirror YAML 编辑器（语法高亮 `@codemirror/lang-yaml` 同族小依赖，PR 说明理由；编辑器栈 `@uiw/react-codemirror` 仓内已有多处使用先例）；右侧校验面板（结构化错误→行号定位）+ 节点片段插入（6 种 kind 骨架）；底部**编译预览图**（只读 DAG，@xyflow/react + dagre 仓内先例，与视图 4 共享组件）；
3. **实例列表页**：分栏看板（进行中 / 需人工（waiting_human+approval 派生）/ 已完结 / 已取消）+ 顶部统计（进行中、需介入、7 日完结）——qwenpaw 巡阅厅范式；
4. **实例详情页**：只读 DAG 快照（节点按状态着色、条件分叉高亮当前路径、running 节点 spinner）+ 事件时间线（SSE 实时，断线重拉快照——KernelRunTaskCard 范式）+ 节点检查器（审批 approve/reject 表单；human 交活/打回表单；**插话输入框**；产物查看/下载带 sha256 校验）+ 实例级操作（取消）。

交互与规范遵循：颜色全走 `styles/tokens.css` 语义令牌（check:color-tokens 棘轮，新文件零硬编码）；组件先查 `component-catalog.ts`（弹窗 ui/dialog、表格 antd、toast sonner、确认 ConfirmDialog）；图表/DAG 不用 ECharts（结构图非数据图，xyflow 作用域）；文案如实（产物"已尝试写入剪贴板"类副作用规范本页无涉，但下载类操作按 §13 E2E 验收）。深链：`/task-instances?instance=<id>&node=<id>`。

超级助手侧栏「任务实例」占位按钮（WorkbenchSidebar.tsx:236）**本期保持禁用不动**，其 E2E 断言（super_assistant_workbench.spec.ts 九项入口）不变。

---

## 11. 通知与平台集成点

- **inbox**：approval.requested / human.assigned → `publish_event`（kind=task，role 定向 approvers / user 定向受派人），决策后回执关闭条目（enqueue_ontology_approval_* 先例）；
- **MinIO**：产物经 `shared/storage.py` StorageService（put_object → s3:// URI）；沿用 file_assets 的「DB 失败补偿删对象」模式；
- **NATS**：向 `data_channel/pipeline_tasks/dispatch.py` 的**只能追加** subject 清单新增 `task_instances.control`（载荷 kind=dispatch/steer/cancel）；durable consumer `task-instances-worker`；流复用 PIPELINE_TASKS；
- **APScheduler**：`bootstrap/lifecycle.py` 注册对账任务（租约/审批过期/孤儿容器清理），旁路失败不阻断启动（平台约定）；
- **model_configs**：默认模型 purpose_tags=("task_instances",)；模型管理页新增用途标签说明（文档同 PR）。

## 12. 受影响文件与治理动作清单（评审可核对完整性）

后端：`app/task_instances/`（新建：models/schemas/engine/contracts/worker 通道/router/validate）、`main.py`（挂载）、`auth/permissions.py`、`data_channel/pipeline_tasks/dispatch.py`（subject 追加 + nats_executor 注册 handler）、`bootstrap/lifecycle.py`、Alembic 0121、`model_configs` 用途标签文档。
前端：`pages/task-instances/`、`config/navigation.ts`、`stores/tabTitle.ts`、`App.tsx`、`api/taskInstances.ts`、component-catalog 登记（如新增场景）。
部署：`docker-compose.{prod,local,e2e}.yml` + `docker/task-agent/Dockerfile` + 部署文档（docs/operations：task_worker 服务、镜像构建、禁用开关、staging 验收清单）——与代码同 PR（AGENTS.md §7）。
治理：`AGENTS.md` 第 1 节业务域表新增一行「任务实例（DAG 任务轨道运行时）| backend/app/task_instances/」。

## 13. 测试与验证方案（映射 AGENTS.md §5 全量门禁）

**引擎单测**（`backend/tests/task_instances/`）：
- 黄金事件轨迹 3 条：① 条件分流人/agent 全链（§4.6 示例，全事件序列断言）；② 契约违规→纠正环→耗尽失败；③ 打回-修复环 + 熔断 409；
- 竞态：取消 vs 推进竞态重复 50+ 次终态一致（qwenpaw T11 同型测试移植）；status_guard 事务内重读验证；
- 幂等：inputs_hash 重复派发跳过；(instance, seq) 并发追加无空洞；canonical_hash 内容不变不建版；
- 校验器：环检测报路径、契约子集白名单越界拒绝、打回环检测、condition 无 default 报错；
- 契约解释器：子集各关键字正反用例 + 深度/数量界限。

**路由与契约**：API 契约测试（含错误码族、Idempotency-Key/If-Match 语义）；SSE 首帧+续传+终态断开；架构边界测试（新域不 import 跨域 router 等，tests/architecture 规则自动覆盖新增 canonical 包）。

**迁移**：单 head；全新库 upgrade；0120→0121 副本升级（AGENTS.md §5 后端门禁）。

**worker 与 Docker**：CI 全量用**确定性假执行器**（不依赖 Docker，monkeypatch 派发层）；真实链路（Docker + NATS + Claude Code + 模型注入 + 插话 + 容器销毁/产物上传）在**隔离 staging 真实环境 E2E**（AGENTS.md §5 明列 Docker/NATS/对象存储必须真实验收），证据入 `.artifacts/` 不入 Git。

**前端**：unit（DAG 投影/事件 reducer/模板变量解析等纯函数，`src/test/unit/taskInstances/`）；mocked E2E 新 spec 登记 `playwright.mocked.config.ts`（四视图主路径 + SSE 模拟 + 审批/交活/插话表单 + 断言规范遵守 check:e2e-assertions）；门禁：feature-boundaries / component-convergence / color-tokens / lint / build / e2e:classification。

**收官全量**：`git diff --check`、markdown links、repo hygiene、backend 全量 pytest + **`.test_durations` 重录**（CI 分片守卫）、config 回归、frontend 全门禁。

## 14. 里程碑（feature 分支顺序推进，每里程碑附测试证据）

| 里程碑 | 内容 | 验收标准 |
|---|---|---|
| **M1 引擎与域** | 数据模型/迁移、YAML 编译与校验器、契约解释器、引擎（规则 R/纠正/打回/取消/租约对账）、API、确定性假执行器、后端测试全绿 | 全部引擎单测+API 契约测试通过；无 Docker 依赖即可全量回归 |
| **M2 worker 基建** | **首周 SDK spike（插话/流式/权限模式）** → task_worker 服务、容器生命周期、模型注入、产物上传、staging 真实 E2E | staging 完成「激活→容器执行→契约校验→审批→收货」真链路 + 插话实测结论（或降级决议回报需求方） |
| **M3 前端四视图** | 模板/YAML 编排页/实例看板/详情 + inbox 通知 + mocked E2E | 前端门禁全过；四视图主路径 mocked E2E 绿 |
| **M4 模板移植与收官** | Homerail 模板适配（pr-review 仲裁评审/release-notes/topic-outline）、AGENTS.md 域表、部署与运维文档、全量门禁收官 | 全量门禁绿；staging 演示一个移植模板端到端跑通 |

## 15. 风险与开放问题（评审重点 ⑤）

| # | 风险/开放问题 | 等级 | 缓解 / 待决 |
|---|---|---|---|
| R1 | Claude Agent SDK 运行中插话能力未实测（§6.4） | 高 | M2 首周 spike；降级路径已定义但须需求方重审 |
| R2 | 模型约束：agent 节点仅 Anthropic 协议配置（§6.3） | 中 | v1 接受（需求方已知情）；v2 入站网关方案另立 |
| R3 | 容器 egress 隔离为"尽力而为"（§6.5） | 中 | staging 实测并记录；文档明示残余风险；高敏环境可禁用 task_worker |
| R4 | 插话/事件 payload 大小与 SSE 压力 | 中 | payload ≤64KB 上限 + progress 事件节流（≤1/s/节点）+ seq 游标续传 |
| R5 | 单人审批模式：approvers 目前只支持平台角色，粒度粗 | 低 | v1 接受；v2 用户级指派 |
| R6 | YAML 作者面学习成本 | 中 | 片段插入 + 校验行号定位 + 编译预览图 + M4 提供三个移植范例 |
| R7 | task_worker 与 backend 的 NATS 流复用（PIPELINE_TASKS）可能互相挤占并发额度 | 低 | durable consumer 独立、并发度独立配置；监控指标进运维文档 |
| R8 | 事件表增长无分区（§7） | 低 | v1 规模可控；v2 按实例归档策略 |
| R9 | 竞态：steering 与节点终态竞态（消息到达时节点刚完成） | 中 | engine 事务内校验 running + 409 NODE_NOT_RUNNING；消息保留可查 |
| R10 | 工期：M1~M4 为顺序大块，评审同事请挑战切分与并行度 | 中 | M2 spike 可与 M1 并行提前启动（不动主干顺序） |

## 附录 A：与参考系统的概念对照（摘要）

| 本设计 | Homerail | qwenpaw-cluster | 说明 |
|---|---|---|---|
| Template+Revision(canonical_hash) | WorkflowSpec+revision/canonical_hash | FlowRule（无版本化） | 版本化取 Homerail |
| Instance(spec 快照) | Run | Task(flowRuleSnapshot) | 快照隔离两者同 |
| NodeRun(attempt) | 节点状态机(9 态) | WorkUnit(Pending/Done/Voided) | 状态粒度取中间值 |
| Contract(端口可选) | 端口契约+证据文件+scorecard | 无（仅 summary 非空） | v1 取端口契约层；证据文件随 Artifact 落 MinIO；scorecard 留 v2 |
| 纠正环(2 次) | correction(默认 2) | — | 取 Homerail |
| 打回-修复环(3 次/边) | 无一等原语 | reject→修复单+熔断 | 取 qwenpaw（需求方指定一等原语） |
| Steering 到运行中节点 | live command(三层围栏) | 无 | v1 简化为 Idempotency-Key + running 校验；围栏机制留 v2 吸收 |
| Worker | Docker 容器/节点 + claude-sdk 后端 | 外部 qwenpaw 实例(webhook) | 取 Homerail 形态、平台内实现 |
| 进度 | activity journal + supervise 游标 | SSE 收件箱 | v1 取 SSE 事件流；游标式监督摘要留 v2 |

## 附录 B：平台事实证据索引（调研已核，评审可抽查）

- 部署先例：`docker-compose.prod.yml`（services：db/redis/nats/neo4j/minio/browser/python_kernel_gateway/backend/pipeline_executor/frontend；pipeline_executor=同镜像不同入口 :224）；
- 模型选型：`backend/app/model_configs/selector.py`（purpose_tags 语义）；`llm_gateway.py` 为出站桥接（无入站网关，文件头 docstring）；provider 出站支持 openai/anthropic 双协议；
- NATS 派发：`backend/app/data_channel/pipeline_tasks/dispatch.py`（subject 清单只能追加、Nats-Msg-Id 去重）；消费 `nats_executor.py`（durable pull、ack_wait、租约对账先例）；
- APScheduler 注册：`backend/app/bootstrap/lifecycle.py`（旁路失败不阻断启动约定）；
- inbox：`backend/app/inbox/service.py` publish_event（role/user 定向、审批回执先例）；
- 存储：`backend/app/shared/storage.py` StorageService（MinIO、put_object→s3:// URI）；file_assets 的 uri+sha256+补偿删除范式；
- 迁移：head `0120_user_report_token_hash`；菜单回填先例 `0065_world_model.py`；
- RBAC：`backend/app/auth/permissions.py` ALL_MENU_KEYS；`backend/deps.py` require_menu_permission；
- 前端：CodeMirror 栈已在 frontend/package.json（@uiw/react-codemirror 等）；@xyflow/react + dagre 已有使用域；SSE 消费范式 KernelRunTaskCard.tsx；导航链路 navigation.ts/tabTitle.ts/App.tsx；E2E 分类登记 playwright.mocked.config.ts；
- 基线不受影响：super_assistant OpenAPI SHA-256 基线仅覆盖其前缀（tests/architecture/test_super_assistant_router_boundaries.py）；
- 参考系统快照：qwenpaw-cluster（github.com/Carlehyy/super-script, work-agent 分支）、Homerail（github.com/xiaotianfotos/homerail@main）——本地调研快照在 /tmp，评审如需可重新拉取。


---

## 附录 C：实施变更记录（与原设计的偏差与后续演进）

### 已交付的后续变更（2026-10-11 弹窗产品化）

| 变更 | 说明 |
|---|---|
| §1.4 非目标变更 | 「DAG 画布编辑器」从非目标变为已交付（WorkflowCanvas：@xyflow/react 拖拽画布 + 属性面板 + YAML 双向同步） |
| 入口收归超级助手 | 后台主导航移除「任务实例」；超级助手侧栏按钮从占位激活为弹窗入口（TaskInstancesDialog 两栏布局） |
| 预置模板种子 | 迁移 0126 种入 pr-review/release-notes/topic-outline 三个模板，开箱即用 |
| 弹窗内激活 | 点击模板卡片 → 弹窗内填表单（实例名+Goal）→ 激活，全程不离开超级助手 |
| 视图组件提升 | TemplatesView/InstancesBoardView/instanceView/WorkflowPreviewGraph 从 pages/task-instances/ 提升至 components/task-instances/（feature-boundaries 允许） |
| alembic 版本 | 实际交付 head 为 0126_seed_task_templates（0125_task_instances + 0126 种子） |


### 预置模板写入保护与弹窗模板列表重排（2026-10-17，YIYA-44/45）

| 变更 | 说明 |
|---|---|
| 预置写入拒绝 | `PUT/DELETE /templates/{id}` 对 `is_preset=true` 在任何写入前拒绝（403 `TEMPLATE_IS_PRESET`，`_get_template` 之后、编译校验之前，故预置+非法 YAML 亦稳定 403）；不存在/已软删仍 404。读取/激活不受影响。独立页与弹窗同步禁用预置编辑/删除/回滚入口，服务端为最终防线（直接 API/深链亦不可写） |
| 弹窗模板列表 | 头部改为「搜索模板名称（仅名称、忽略首尾空格、不区分大小写子串）× 预置/个人两项单选（默认预置）+ 新建流程」；筛选在弹窗内对完整列表派生（templateFilters.ts），不改独立页 name OR description 搜索契约与按 revision 匹配的重新激活路径 |
| 模板卡四操作 | 查看（弹窗内只读详情：元信息 + WorkflowPreviewGraph + 可折叠 YAML）/编辑（复用抽取的 TemplateEditorPanel，同 ID 新 revision）/激活（与卡片主体点击同路径）/删除（个人确认框，取消不发请求）；预置编辑/删除禁用并说明原因，操作区与主体分栏不冒泡 |
| 闭环归位 | 新建保存成功归位「个人」并清空关键词；各视图往返保留筛选，关闭弹窗恢复默认；空态区分全局无模板/个人分类空/关键词无匹配 |

### 技术事实补录

- Claude Agent SDK 实际版本：@anthropic-ai/claude-agent-sdk@0.3.296
- MiniMax Anthropic 兼容端点：https://api.minimaxi.com/anthropic（连通性测试经平台三步流验证通过）
- task_worker 镜像内 docker CLI：Debian 13 需用 docker-cli 包（docker.io 拆包不含二进制）或 COPY --from=docker:cli 静态拷贝
- DatasetVersion claim-lost 修复：双 executor 部署下需在 handler 内消化（83e46df5）
