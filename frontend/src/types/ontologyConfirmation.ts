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

/** 确认展示层：本体 / 动作 / 目标 / 最终参数 / 发布版本 / 审批要求 */
export interface OntologyConfirmationDisplay {
  ontology: { id: string; name: string }
  action: { id: string; name: string }
  target: { id: string; label: string } | null
  parameters: OntologyConfirmationParameter[]
  release: { id: string; version: string }
  requiresApproval: boolean
}

/** confirmation_out：direct/kernel 为精简同源对象，本体 Agent 端点为全量 */
export interface OntologyConfirmationState {
  confirmationId: string
  /** prepared/confirmed/dispatch_queued/executing/pending_approval/succeeded/ failed_not_executed/approval_rejected/denied/cancelled/expired/unknown/compensation_required/recovering */
  state: string
  display: OntologyConfirmationDisplay
  requiresApproval: boolean
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
