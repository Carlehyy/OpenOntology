import { apiClientV2 } from './client'

export type AutoLoopPriority = 'low' | 'medium' | 'high'
export type DecisionStatus = 'open' | 'pending_dispatch' | 'sending' | 'writeback_unknown' | 'writeback_failed' | 'completed' | 'withdrawn' | 'handled'
export type ActionStepState = 'never_sent' | 'queued' | 'sending' | 'unknown' | 'failed' | 'confirmed'
export interface AutoLoopPage<T> { items: T[]; nextCursor: string | null; hasMore: boolean }
export interface AutoLoopRuleCreate {
  name: string
  enabled: boolean
  position: number
  match: {
    sourceKind?: string | null
    ingestKeyId?: string | null
    sourceSystem?: string | null
    channel?: string | null
    titleContains?: string | null
    bodyContains?: string | null
    fieldEquals?: { field: string; value: string }[]
  }
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
export interface AutoLoopEventFilters {
  source?: string
  sourceSystem?: string
  action?: string
  ruleId?: string
  status?: string
  scheduledTaskId?: string
  scheduledRunId?: string
}
const base = '/super-assistant/autoloop'
const itemPath = (id: string) => `${base}/decision-items/${encodeURIComponent(id)}`

export const autoloopApi = {
  listRules: () => apiClientV2.get<AutoLoopRule[]>(`${base}/rules`),
  getRule: (id: string) => apiClientV2.get<AutoLoopRule>(`${base}/rules/${encodeURIComponent(id)}`),
  createRule: (rule: AutoLoopRuleCreate) => apiClientV2.post<AutoLoopRule>(`${base}/rules`, rule),
  updateRule: (id: string, rule: Partial<AutoLoopRuleCreate>) => apiClientV2.patch<AutoLoopRule>(`${base}/rules/${encodeURIComponent(id)}`, rule),
  deleteRule: (id: string) => apiClientV2.delete<void>(`${base}/rules/${encodeURIComponent(id)}`),
  previewRule: (rule: AutoLoopRuleCreate, sampleEvent: unknown) => apiClientV2.post<AutoLoopPreview>(`${base}/rules/preview`, { rule, sampleEvent }),
  previewStoredRule: (id: string, sampleEvent: unknown) => apiClientV2.post<AutoLoopPreview>(`${base}/rules/${encodeURIComponent(id)}/preview`, { sampleEvent }),
  listEvents: (filters: AutoLoopEventFilters, cursor?: string | null) => apiClientV2.get<AutoLoopPage<AutoLoopEvent>>(`${base}/events`, { params: { ...filters, cursor: cursor || undefined, limit: 20 } }),
  getEvent: (id: string) => apiClientV2.get<AutoLoopEventDetail>(`${base}/events/${encodeURIComponent(id)}`),
  listDecisions: (status: string, cursor?: string | null) => apiClientV2.get<AutoLoopPage<DecisionItem>>(`${base}/decision-items`, { params: { status, cursor: cursor || undefined, limit: 20 } }),
  decisionSummary: () => apiClientV2.get<DecisionSummary>(`${base}/decision-items/summary`),
  getDecision: (id: string) => apiClientV2.get<DecisionItem>(itemPath(id)),
  decide: (id: string, body: DecisionMutation & { optionId?: string; freeText?: string }) => apiClientV2.post<DecisionItem>(`${itemPath(id)}/decide`, body),
  markHandled: (id: string, body: DecisionMutation & { reason?: string }) => apiClientV2.post<DecisionItem>(`${itemPath(id)}/mark-handled`, body),
}
