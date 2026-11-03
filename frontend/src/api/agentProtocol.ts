/**
 * 本体助手新 run / query-view 协议解析（单一 camelCase 合同）。
 *
 * 这里只做类型校验与转换：未知 status、缺字段、旧形状一律抛
 * AgentProtocolError，不做 snake_case / 平铺 / 别名回落。独立模块便于
 * Node 纯逻辑单测，也避免 api/agent.ts 继续膨胀。
 */
// ---------- 严格协议解析（单一 camelCase 契约；无效响应直接报错） ----------

export interface AgentStep {
  tool: string
  arguments: Record<string, unknown>
  summary: string
  durationMs: number
  /** 工具原始输出（后端截断后下发），用于「展开查看工具输出」 */
  result?: unknown
  /** 完整审计结果存在时，对应的页面轻量展示值（仅导出文件包含） */
  displayResult?: unknown
  error?: string
}

export interface AgentCitation {
  instanceId: string
  objectType: string
  label: string
  /** 统一引用契约：展示就绪串（objectType · label） */
  sourceLabel?: string
  /** 属性摘要，引用卡片副文本 / 悬浮显示 */
  snippet?: string
}

export interface AgentActionProposal {
  kind?: 'action'
  proposalId: string
  releaseId?: string | null
  actionId: string
  actionName: string
  parameters: Record<string, unknown>
  targetInstanceId: string | null
  requiresApproval: boolean
  status: string
  validationErrors: string[]
  effects: { type: string; description?: string; [k: string]: unknown }[]
}

export interface DynamicSentinelPatternStage {
  alias: string
  objectTypeId: string
  filter?: string | null
  within?: number | null
}

export interface DynamicSentinelPattern {
  stages: DynamicSentinelPatternStage[]
  within?: number
  absence?: { enabled: boolean }
  aggregate?: {
    property: string
    function: 'count' | 'avg' | 'sum' | 'min' | 'max'
    window: number
    threshold: number
    comparison?: 'gte' | 'gt' | 'lte' | 'lt'
  }
  condition?: string | null
}

export interface DynamicSentinelDefinition {
  name: string
  displayName: string
  description?: string | null
  bindings: { alias: string; objectTypeId: string; filter?: string | null }[]
  links: { from: string; linkTypeId: string; to: string }[]
  condition?: string | null
  pattern?: DynamicSentinelPattern | null
  conditionRows?: Record<string, unknown>[]
  conditionLogic?: 'and' | 'or'
  primaryAlias: string
  actionIds: string[]
  actionParameters: Record<string, unknown>
  onChange: boolean
  onSchedule: boolean
  scanIntervalSeconds: number
  triggerMode: 'on_enter' | 'on_enter_leave' | 'run_on_all' | 'on_pattern'
  muted: boolean
}


export interface AgentSentinelProposal {
  kind: 'sentinel'
  proposalId: string
  operation: 'create' | 'update' | 'enable' | 'disable' | 'delete'
  sentinelId: string | null
  sentinelName: string
  releaseId: string
  expectedRevision: number | null
  definition: DynamicSentinelDefinition | null
  status: string
  validationErrors: string[]
  validationReport?: Record<string, unknown>
}

export type AgentProposal = AgentActionProposal | AgentSentinelProposal


export interface AnswerVerification {
  passed: boolean
  answerNumberCount?: number
  unverified?: number[]
  retried?: boolean
}


export type AgentRunStatus =
  | 'queued'
  | 'running'
  | 'succeeded'
  | 'error'
  | 'cancelled'
  | 'interrupted'

/**
 * 服务端回合快照 — 刷新、离页、新 tab 时由 conversation.activeRun /
 * latestRun 或 GET chat/runs/{runId} 恢复的唯一事实源。
 *
 * 字段严格按后端 camelCase 契约定义；parseAgentRunSnapshot 负责校验，
 * 未知 status / 缺字段直接抛 AgentProtocolError，不做字段别名猜测。
 */
export interface AgentRunSnapshot {
  runId: string
  ontologyId?: string | null
  releaseId?: string | null
  conversationId: string | null
  status: AgentRunStatus
  question: string
  content: string
  steps: AgentStep[]
  citations: AgentCitation[]
  proposals: AgentProposal[]
  usage?: unknown
  lastSeq: number | null
  error: string | null
  startedAt: string | null
  finishedAt: string | null
  retryOf: string | null
  /** 服务端已受理停止请求但 worker 尚未确认时的标记（run 仍为 running）。 */
  cancelRequestedAt?: string | null
  ownerId?: string | null
  model?: string | null
  verification?: AnswerVerification | null
}


export type AgentRunStreamEvent =
  | { type: 'snapshot'; seq: number | null; id?: string; snapshot: AgentRunSnapshot }
  | { type: 'meta'; seq: number | null; id?: string; conversationId: string | null; model?: string | null; releaseId?: string | null }
  | { type: 'step'; seq: number | null; id?: string; step: AgentStep }
  | { type: 'delta'; seq: number | null; id?: string; content: string; reset: boolean }
  | {
    type: 'answer'; seq: number | null; id?: string; content: string
    citations: AgentCitation[]; proposals: AgentProposal[]
    usage?: unknown; verification?: AnswerVerification | null
  }
  | { type: 'error'; seq: number | null; id?: string; message: string }
  | { type: 'cancelled'; seq: number | null; id?: string; content?: string; finishedAt?: string | null }
  | { type: 'done'; seq: number | null; id?: string; status?: AgentRunStatus; finishedAt?: string | null }
  | { type: 'heartbeat'; seq: number | null; id?: string }

// ---------- query-view（明细表 / 分页 / 排序 / 统计下钻） ----------


export interface AgentQueryColumn {
  /** 行取值键（query 输出别名 output，缺省回退 name）。 */
  key: string
  /** 展示名（后端 name）。 */
  label: string
  name?: string
  output?: string
  nullable?: boolean
  type?: string
  align?: 'left' | 'right' | 'center'
  width?: number
  sortable?: boolean
}

export interface AgentQuerySpec {
  version?: string
  shape?: 'objects' | 'spl' | string
  ontologyId?: string | null
  releaseId?: string | null
  query?: Record<string, unknown>
  resolvedWindow?: unknown
  pageSize?: number | null
  /** 后端 description 为结构化对象（semantics/note 等）。 */
  description?: unknown
  [key: string]: unknown
}

export interface AgentQueryDrilldown {
  token?: string
  description?: string | null
  label?: string | null
  [key: string]: unknown
}

export interface AgentQueryViewResult {
  rows: Record<string, unknown>[]
  columns: AgentQueryColumn[]
  pageSize: number | null
  hasNext: boolean
  cursor: string | null
  totalRowCount: number | string | null
  observedAt: string | null
  resolvedWindow: unknown
  consistency: unknown
  querySpec: AgentQuerySpec | null
  querySpecHash: string | null
  dataMode: string | null
  rowCounting: string | null
  releaseId: string | null
  /** 聚合分组下钻描述（token/description）；行内 drilldown 优先。 */
  drilldowns: unknown
}

type UnknownRecord = Record<string, unknown>

const RUN_STATUSES: ReadonlySet<string> = new Set([
  'queued', 'running', 'succeeded', 'error', 'cancelled', 'interrupted',
])

export class AgentProtocolError extends Error {
  constructor(message: string) {
    super(`无效的助手协议响应：${message}`)
    this.name = 'AgentProtocolError'
  }
}

function expectRecord(value: unknown, label: string): UnknownRecord {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new AgentProtocolError(`${label} 必须是对象`)
  }
  return value as UnknownRecord
}

function expectString(value: unknown, label: string): string {
  if (typeof value !== 'string') throw new AgentProtocolError(`${label} 必须是字符串`)
  return value
}

function optionalString(value: unknown, label: string): string | undefined {
  if (value === undefined) return undefined
  if (typeof value !== 'string') throw new AgentProtocolError(`${label} 必须是字符串`)
  return value
}

function nullableString(value: unknown, label: string): string | null {
  if (value === null) return null
  return expectString(value, label)
}

function optionalNullableString(value: unknown, label: string): string | null {
  if (value === undefined || value === null) return null
  return expectString(value, label)
}

function expectNumber(value: unknown, label: string): number {
  if (typeof value !== 'number' || !Number.isFinite(value)) {
    throw new AgentProtocolError(`${label} 必须是数字`)
  }
  return value
}

function optionalNullableNumber(value: unknown, label: string): number | null {
  if (value === undefined || value === null) return null
  return expectNumber(value, label)
}

function expectBoolean(value: unknown, label: string): boolean {
  if (typeof value !== 'boolean') throw new AgentProtocolError(`${label} 必须是布尔值`)
  return value
}

function expectArray(value: unknown, label: string): unknown[] {
  if (!Array.isArray(value)) throw new AgentProtocolError(`${label} 必须是数组`)
  return value
}

function parseRunStatus(value: unknown): AgentRunStatus {
  const status = expectString(value, 'status')
  if (!RUN_STATUSES.has(status)) {
    throw new AgentProtocolError(`未知的 run status「${status}」`)
  }
  return status as AgentRunStatus
}

function parseAgentStep(record: UnknownRecord): AgentStep {
  return {
    tool: expectString(record.tool, 'step.tool'),
    arguments: expectRecord(record.arguments, 'step.arguments'),
    summary: expectString(record.summary, 'step.summary'),
    durationMs: expectNumber(record.durationMs, 'step.durationMs'),
    ...(record.result !== undefined ? { result: record.result } : {}),
    ...(record.displayResult !== undefined ? { displayResult: record.displayResult } : {}),
    ...(record.error !== undefined ? { error: expectString(record.error, 'step.error') } : {}),
  }
}

function parseAgentCitation(value: unknown): AgentCitation {
  const record = expectRecord(value, 'citation')
  return {
    instanceId: expectString(record.instanceId, 'citation.instanceId'),
    objectType: expectString(record.objectType, 'citation.objectType'),
    label: expectString(record.label, 'citation.label'),
    ...(record.sourceLabel !== undefined
      ? { sourceLabel: expectString(record.sourceLabel, 'citation.sourceLabel') }
      : {}),
    ...(record.snippet !== undefined
      ? { snippet: expectString(record.snippet, 'citation.snippet') }
      : {}),
  }
}

/** 服务端 run 快照（GET / chat/runs、SSE 首帧、conversation.activeRun）。 */
export function parseAgentRunSnapshot(value: unknown): AgentRunSnapshot {
  const record = expectRecord(value, 'run snapshot')
  return {
    runId: expectString(record.runId, 'run.runId'),
    ontologyId: optionalNullableString(record.ontologyId, 'run.ontologyId'),
    releaseId: optionalNullableString(record.releaseId, 'run.releaseId'),
    conversationId: nullableString(record.conversationId, 'run.conversationId'),
    status: parseRunStatus(record.status),
    question: expectString(record.question, 'run.question'),
    content: expectString(record.content, 'run.content'),
    steps: expectArray(record.steps, 'run.steps')
      .map(step => parseAgentStep(expectRecord(step, 'run.steps[]'))),
    citations: expectArray(record.citations, 'run.citations')
      .map(parseAgentCitation),
    proposals: expectArray(record.proposals, 'run.proposals')
      .map(proposal => expectRecord(proposal, 'run.proposals[]') as unknown as AgentProposal),
    ...(record.usage !== undefined ? { usage: record.usage } : {}),
    lastSeq: optionalNullableNumber(record.lastSeq, 'run.lastSeq'),
    error: optionalNullableString(record.error, 'run.error'),
    startedAt: optionalNullableString(record.startedAt, 'run.startedAt'),
    finishedAt: optionalNullableString(record.finishedAt, 'run.finishedAt'),
    retryOf: optionalNullableString(record.retryOf, 'run.retryOf'),
    cancelRequestedAt: optionalNullableString(record.cancelRequestedAt, 'run.cancelRequestedAt'),
    ownerId: optionalNullableString(record.ownerId, 'run.ownerId'),
    model: optionalNullableString(record.model, 'run.model'),
    ...(record.verification !== undefined
      ? { verification: record.verification as AnswerVerification | null }
      : {}),
  }
}

/** SSE data 中的事件；首帧只认 {type:'snapshot', snapshot:{...}}。 */
export function parseAgentRunStreamEvent(value: unknown): AgentRunStreamEvent {
  const record = expectRecord(value, 'run event')
  const type = expectString(record.type, 'event.type')
  const seq = optionalNullableNumber(record.seq, 'event.seq')
  const id = optionalString(record.id, 'event.id')
  if (type === 'snapshot') {
    return {
      type,
      seq,
      id,
      snapshot: parseAgentRunSnapshot(expectRecord(record.snapshot, 'event.snapshot')),
    }
  }
  if (type === 'meta') {
    return {
      type,
      seq,
      id,
      conversationId: expectString(record.conversationId, 'event.conversationId'),
      model: optionalString(record.model, 'event.model'),
      releaseId: optionalNullableString(record.releaseId, 'event.releaseId'),
    }
  }
  if (type === 'step') {
    return { type, seq, id, step: parseAgentStep(record) }
  }
  if (type === 'delta') {
    return {
      type,
      seq,
      id,
      content: expectString(record.content, 'event.content'),
      reset: record.reset === undefined ? false : expectBoolean(record.reset, 'event.reset'),
    }
  }
  if (type === 'answer') {
    return {
      type,
      seq,
      id,
      content: expectString(record.content, 'event.content'),
      citations: expectArray(record.citations, 'event.citations').map(parseAgentCitation),
      proposals: expectArray(record.proposals, 'event.proposals')
        .map(proposal => expectRecord(proposal, 'event.proposals[]') as unknown as AgentProposal),
      ...(record.usage !== undefined ? { usage: record.usage } : {}),
      verification: (record.verification as AnswerVerification | null | undefined) ?? null,
    }
  }
  if (type === 'error') {
    return { type, seq, id, message: expectString(record.message, 'event.message') }
  }
  if (type === 'cancelled') {
    return {
      type,
      seq,
      id,
      content: optionalString(record.content, 'event.content'),
      finishedAt: optionalNullableString(record.finishedAt, 'event.finishedAt'),
    }
  }
  if (type === 'done') {
    return {
      type,
      seq,
      id,
      status: record.status === undefined ? undefined : parseRunStatus(record.status),
      finishedAt: optionalNullableString(record.finishedAt, 'event.finishedAt'),
    }
  }
  if (type === 'heartbeat') {
    return { type, seq, id }
  }
  throw new AgentProtocolError(`未知的 run event type「${type}」`)
}

function parseQueryColumn(value: unknown): AgentQueryColumn {
  const record = expectRecord(value, 'query column')
  const name = expectString(record.name, 'column.name')
  const output = expectString(record.output, 'column.output')
  return {
    key: output,
    label: name,
    name,
    output,
    ...(record.type !== undefined ? { type: expectString(record.type, 'column.type') } : {}),
    ...(record.nullable !== undefined
      ? { nullable: expectBoolean(record.nullable, 'column.nullable') }
      : {}),
  }
}

function alignQueryRow(
  value: unknown,
  columns: AgentQueryColumn[],
): Record<string, unknown> {
  const source = expectRecord(value, 'query row')
  const aligned: Record<string, unknown> = { ...source }
  // 契约为 readable rows；列同时带 name/output 时两种键都要能被 output 读取。
  for (const column of columns) {
    if (aligned[column.key] !== undefined) continue
    if (column.label !== column.key && aligned[column.label] !== undefined) {
      aligned[column.key] = aligned[column.label]
    }
  }
  return aligned
}

/** query-view 响应（camelCase；rows 为 readable 行，columns 固定 name/output）。 */
export function parseAgentQueryViewResult(value: unknown): AgentQueryViewResult {
  const record = expectRecord(value, 'query-view result')
  const columns = expectArray(record.columns, 'query-view.columns').map(parseQueryColumn)
  return {
    rows: expectArray(record.rows, 'query-view.rows')
      .map(row => alignQueryRow(row, columns)),
    columns,
    pageSize: optionalNullableNumber(record.pageSize, 'query-view.pageSize'),
    hasNext: expectBoolean(record.hasNext, 'query-view.hasNext'),
    cursor: optionalNullableString(record.cursor, 'query-view.cursor'),
    totalRowCount: optionalNullableNumber(record.totalRowCount, 'query-view.totalRowCount'),
    observedAt: optionalNullableString(record.observedAt, 'query-view.observedAt'),
    resolvedWindow: record.resolvedWindow ?? null,
    consistency: record.consistency ?? null,
    querySpec: record.querySpec === undefined || record.querySpec === null
      ? null
      : expectRecord(record.querySpec, 'query-view.querySpec') as AgentQuerySpec,
    querySpecHash: optionalNullableString(record.querySpecHash, 'query-view.querySpecHash'),
    dataMode: optionalNullableString(record.dataMode, 'query-view.dataMode'),
    rowCounting: optionalNullableString(record.rowCounting, 'query-view.rowCounting'),
    releaseId: optionalNullableString(record.releaseId, 'query-view.releaseId'),
    drilldowns: record.drilldowns ?? null,
  }
}
