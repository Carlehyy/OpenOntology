/**
 * YIYA-36 持久确认协议的服务端展示契约（confirmation_out / display）。
 *
 * 同一结构被三处消费：direct SSE `tool_confirmation_required` 的
 * `ontologyConfirmation`、kernel run 视图 calls[] 的精简 `confirmation`
 * 对象、本体 Agent 确认端点（prepare/decision/execute/getProposal）的
 * AgentConfirmationState。字段由服务端持久化下发，前端只读展示，
 * 二次确认的动作参数不可编辑。
 */

/** 确认载荷中的最终参数：值 + 来源（服务端判定，前端只展示来源标签） */
export interface OntologyConfirmationParameter {
  name: string
  value: unknown
  /** user_input=用户输入；trusted_query=可信查询带入；default=动作默认值 */
  source: 'user_input' | 'trusted_query' | 'default' | string
}

/**
 * 目标资产公开摘要（服务端 public_plan_summary，挂在 display.target.asset）：
 * 资产身份 + 操作 + 定位键（用户确认的那一行业务主键）+ 写入字段名清单。
 * expected token / before 行 / schema fingerprint / 内部 id 一律不下发。
 */
export interface OntologyConfirmationTargetAsset {
  datasetId: string
  name: string
  kind: string
  operation: string
  /** 业务主键定位行（公开）；CAS 私有材料不在此处 */
  key?: Record<string, unknown> | null
  fields: string[]
}

/** 确认展示层：本体 / 动作 / 目标 / 最终参数 / 发布版本 / 审批要求 */
export interface OntologyConfirmationDisplay {
  ontology: { id: string; name: string }
  action: { id: string; name: string }
  target: {
    id: string
    label: string
    /** 目标资产公开元数据（资产路由动作才有）；私有 CAS 材料不下发 */
    asset?: OntologyConfirmationTargetAsset | null
  } | null
  parameters: OntologyConfirmationParameter[]
  release: { id: string; version: string }
  requiresApproval: boolean
}

/**
 * 动作执行结果（execute 后可选下发；manual/curated/integrated 三类资产
 * 各自的生效事实）。动作层 state=succeeded 不等于数据已生效：curated 的
 * pending_review 表示提交成功但仍在审核、查询不可见；curated 的 applied
 * 表示审核已批准生效。缺 outcome = 本地动作或未派发的确认。
 */
export interface OntologyActionOutcome {
  /** manual=平台人工资产；curated=平台审核资产；integrated=外部活源 */
  assetKind: 'manual' | 'curated' | 'integrated' | string
  /** applied=已生效；pending_review=待审核（未生效）；unknown=结果待核实；failed=写入未生效 */
  status: 'applied' | 'pending_review' | 'unknown' | 'failed' | string
  operation: 'insert' | 'update' | 'delete' | string
  affectedRows: number
  datasetId: string
  /** 仅真实存在时下发（manual 新数据版本） */
  versionId?: string | null
  /** 审核记录标识：仅作文本展示，链接需核实现有可达路由后才可加 */
  reviewId?: string | null
  /** 写入回执标识：仅作文本展示，供人工核对，不伪造深链 */
  receiptId?: string | null
  errorCode?: string | null
}

/** confirmation_out：direct/kernel 为精简同源对象，本体 Agent 端点为全量 */
export interface OntologyConfirmationState {
  confirmationId: string
  /** prepared/confirmed/dispatch_queued/executing/pending_approval/succeeded/ failed_not_executed/approval_rejected/denied/cancelled/expired/unknown/compensation_required/recovering */
  state: string
  display: OntologyConfirmationDisplay
  requiresApproval: boolean
  /** 动作执行结果（execute 后可选；prepared/denied/cancelled/expired 等通常缺省） */
  outcome?: OntologyActionOutcome | null
  payloadHash?: string
  expiresAt?: string | null
  replayed?: boolean
  operationId?: string | null
  executionLogId?: string | null
  approvalLogId?: string | null
  failureCode?: string | null
  blockReason?: string | null
  decidedAt?: string | null
  createdAt?: string | null
}

/**
 * direct 流 `tool_confirmation_required` 事件 data 与
 * GET /conversations/{id}/pending-confirmation 响应的同形载荷。
 */
export interface PendingToolConfirmation {
  toolRunId: string
  toolName: string
  serverName: string
  arguments: Record<string, unknown>
  /** 本体动作确认（服务端持久 display）；纯 MCP 工具无此字段 */
  ontologyConfirmation?: OntologyConfirmationState | null
}
