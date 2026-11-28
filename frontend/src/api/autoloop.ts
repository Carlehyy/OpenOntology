import { apiClientV2 } from './client'

export type AutoLoopPriority = 'low' | 'medium' | 'high'
export type DecisionStatus = 'open' | 'pending_dispatch' | 'sending' | 'writeback_unknown' | 'writeback_failed' | 'completed' | 'withdrawn' | 'handled'
export type ActionStepState = 'never_sent' | 'queued' | 'sending' | 'unknown' | 'failed' | 'confirmed'
export interface AutoLoopPage<T> { items: T[]; nextCursor: string | null; hasMore: boolean }
export interface AutoLoopRuleMatch {
  sourceKind?: string | null
  ingestKeyId?: string | null
  sourceSystem?: string | null
  channel?: string | null
  titleContains?: string | null
  bodyContains?: string | null
  fieldEquals?: { field: string; value: string }[]
}
export interface AutoLoopRuleCreate {
  name: string
  enabled: boolean
  position: number
  match: AutoLoopRuleMatch
  titleTemplate: string
  descriptionTemplate: string
  priority?: AutoLoopPriority | null
  assignee?: string | null
  binding?: { mode: 'extract'; pattern?: string | null; targetIssue?: string | null } | null
}
export interface AutoLoopRule extends AutoLoopRuleCreate {
  id: string
  version: number
  createdAt: string
  updatedAt: string
}
export type AutoLoopRulePatch = Partial<AutoLoopRuleCreate> & { expectedVersion?: number; confirmAllMatch?: boolean }

export interface AutoLoopRoutingPolicy {
  version: number
  promptText: string | null
  updatedAt: string | null
  updatedBy: string | null
  modelConfigId: string | null
  policySummary: string
}
export interface AutoLoopRoutingPolicyUpdate { expectedVersion: number; promptText: string | null }
export interface AutoLoopRoutingPolicyPreviewInput extends AutoLoopRoutingPolicyUpdate {
  eventType: 'notification' | 'schedule'
  sampleEvent: Record<string, unknown>
}
export interface AutoLoopRoutingPolicyPreview {
  action: 'create_decision' | 'ignore'
  reason: string
  modelConfigId: string
  routingPromptVersion: number
  isDraft: boolean
}

export interface AutoLoopPreview {
  renderedTitle: string
  renderedDescription: string
  untrustedOriginal: unknown
  priority?: AutoLoopPriority | null
  assignee?: string | null
  bindingTarget?: string | null
  ruleVersion: number
  warnings: string[]
}
export interface AutoLoopStep { id: string; kind: string; state: ActionStepState; error?: string | null }
export interface AutoLoopEvent {
  id: string
  seq: number
  type: string
  source: { kind: string; system: string; ref: string }
  causality: { parentEventId?: string | null; hops: number }
  happenedAt: string
  ruleVersion?: number | null
  action: string
  issueRef?: string | null
  decisionItemId?: string | null
  error?: string | null
}
export interface AutoLoopEventDetail extends AutoLoopEvent {
  untrustedOriginal: unknown
  actionSteps: AutoLoopStep[]
  dedup: { key: string; duplicateOf?: string | null }
  scheduledTaskId?: string | null
  scheduledRunId?: string | null
  issueUrl?: string | null
}
export interface DecisionItem {
  id: string
  source: 'watchdog' | 'assistant' | 'kernel_fallback'
  status: DecisionStatus
  title: string
  question: string
  factsSummary: string
  eventIds: string[]
  untrustedOriginal?: unknown
  resolution?: {
    kind: 'completed' | 'withdrawn' | 'handled'
    optionId?: string | null
    optionLabel?: string | null
    freeText?: string | null
    actor: string
    at: string
    reason?: string | null
  } | null
  options: { id: string; label: string }[]
  recommendedOptionId?: string | null
  freeTextAllowed: true
  issueRef?: string | null
  issueUrl?: string | null
  priority: AutoLoopPriority
  contentVersion: number
  createdAt: string
  updatedAt: string
  writebackSteps: AutoLoopStep[]
  alreadyProcessed?: boolean
}
export interface DecisionSummary {
  openCount: number
  pendingDispatchCount: number
  sendingCount: number
  unknownCount: number
  failedCount: number
  unfinishedTotal: number
}
export interface DecisionMutation { expectedContentVersion: number; idempotencyKey: string }
export type AutoLoopRoute = 'pending' | 'rule' | 'assistant'
export type AutoLoopOutcome = 'pending' | 'queued' | 'sending' | 'review' | 'confirmed' | 'recorded' | 'failed' | 'unknown' | 'ignored' | 'handled' | 'cancelled'

export interface AutoLoopEventTrace {
  id: string
  receivedAt: string
  title: string | null
  source: AutoLoopEvent['source']
  route: AutoLoopRoute
  outcome: AutoLoopOutcome
  rule?: { id: string; name: string | null; version: number | null } | null
  action?: string | null
  decisionItemId?: string | null
  issueRef?: string | null
  scheduledTaskId?: string | null
  scheduledRunId?: string | null
  reason?: string | null
  routingPromptVersion?: number | null
  modelConfigId?: string | null
}

export interface AutoLoopTraceStep extends AutoLoopStep {
  remoteObjectId?: string | null
}

export interface AutoLoopTraceDetail extends AutoLoopEventTrace {
  untrustedOriginal: unknown
  error?: string | null
  issueUrl?: string | null
  actionSteps: AutoLoopTraceStep[]
  auditEvents: Pick<AutoLoopEvent, 'id' | 'type' | 'happenedAt' | 'action' | 'error'>[]
}

export interface AutoLoopTraceFilters {
  source?: string
  sourceSystem?: string
  route?: AutoLoopRoute
  action?: string
  outcome?: AutoLoopOutcome
  ruleId?: string
  since?: string
  until?: string
  scheduledTaskId?: string
  scheduledRunId?: string
}

export interface AutoLoopRoutingStats {
  days: 7 | 30
  since: string
  until: string
  received: number
  matched: number
  unmatched: number
  pendingRoute: number
  outcomes: Partial<Record<AutoLoopOutcome, number>>
  rules: { ruleId: string; ruleName: string | null; hits: number }[]
}

const base = '/super-assistant/autoloop'
const itemPath = (id: string) => `${base}/decision-items/${encodeURIComponent(id)}`

export const autoloopApi = {
  listRules: () => apiClientV2.get<AutoLoopRule[]>(`${base}/rules`),
  getRule: (id: string) => apiClientV2.get<AutoLoopRule>(`${base}/rules/${encodeURIComponent(id)}`),
  createRule: (rule: AutoLoopRuleCreate) => apiClientV2.post<AutoLoopRule>(`${base}/rules`, rule),
  updateRule: (id: string, rule: AutoLoopRulePatch) => apiClientV2.patch<AutoLoopRule>(`${base}/rules/${encodeURIComponent(id)}`, rule),
  getRoutingPolicy: () => apiClientV2.get<AutoLoopRoutingPolicy>(`${base}/routing-policy`),
  updateRoutingPolicy: (body: AutoLoopRoutingPolicyUpdate) => apiClientV2.patch<AutoLoopRoutingPolicy>(`${base}/routing-policy`, body),
  previewRoutingPolicy: (body: AutoLoopRoutingPolicyPreviewInput, signal?: AbortSignal) =>
    apiClientV2.post<AutoLoopRoutingPolicyPreview>(`${base}/routing-policy/preview`, body, { signal }),
  deleteRule: (id: string) => apiClientV2.delete<void>(`${base}/rules/${encodeURIComponent(id)}`),
  previewRule: (rule: AutoLoopRuleCreate, sampleEvent: unknown) => apiClientV2.post<AutoLoopPreview>(`${base}/rules/preview`, { rule, sampleEvent }),
  previewStoredRule: (id: string, sampleEvent: unknown) => apiClientV2.post<AutoLoopPreview>(`${base}/rules/${encodeURIComponent(id)}/preview`, { sampleEvent }),
  listEventTraces: (filters: AutoLoopTraceFilters, cursor?: string | null, limit = 20) =>
    apiClientV2.get<AutoLoopPage<AutoLoopEventTrace>>(`${base}/event-traces`, { params: { ...filters, cursor: cursor ?? undefined, limit } }),
  getEventTrace: (id: string) => apiClientV2.get<AutoLoopTraceDetail | null>(`${base}/event-traces/${encodeURIComponent(id)}`),
  routingStats: (days: 7 | 30) => apiClientV2.get<AutoLoopRoutingStats>(`${base}/routing-stats`, { params: { days } }),
  getEvent: (id: string) => apiClientV2.get<AutoLoopEventDetail>(`${base}/events/${encodeURIComponent(id)}`),
  // limit 取服务端上限 100 以内的值；既有弹窗分页沿用默认 20，全局提醒一次取全
  listDecisions: (status: string, cursor?: string | null, limit = 20) => apiClientV2.get<AutoLoopPage<DecisionItem>>(`${base}/decision-items`, { params: { status, cursor: cursor || undefined, limit } }),
  decisionSummary: () => apiClientV2.get<DecisionSummary>(`${base}/decision-items/summary`),
  getDecision: (id: string) => apiClientV2.get<DecisionItem>(itemPath(id)),
  decide: (id: string, body: DecisionMutation & { optionId?: string; freeText?: string }) => apiClientV2.post<DecisionItem>(`${itemPath(id)}/decide`, body),
  markHandled: (id: string, body: DecisionMutation & { reason?: string }) => apiClientV2.post<DecisionItem>(`${itemPath(id)}/mark-handled`, body),
}
