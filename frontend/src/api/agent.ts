/**
 * 本体智能体 API — /api/v2/formal/ontologies/{oid}/agent/*
 *
 * chat 走 SSE 流式（fetch + ReadableStream 手动解析，axios 不支持流），
 * 其余走 apiClientV2。
 */
import { apiClientV2 } from './client'

// ---------- 类型 ----------

export interface AgentProfile {
  id: string
  ontologyId: string
  enabled: boolean
  allowedObjectTypeIds: string[] | null
  allowedLinkTypeIds: string[] | null
  allowedActionIds: string[] | null
  allowActionProposals: boolean
  maxRowsPerQuery: number
  maxSteps: number
  systemPromptExtra: string
  defaultModelId: string | null
  updatedAt: string
}

export interface AgentProfileUpdate {
  enabled?: boolean
  allowedObjectTypeIds?: string[] | null
  allowedLinkTypeIds?: string[] | null
  allowedActionIds?: string[] | null
  allowActionProposals?: boolean
  maxRowsPerQuery?: number
  maxSteps?: number
  systemPromptExtra?: string
  defaultModelId?: string | null
  /** 把某些白名单字段重置为 null（=全部允许）：字段名用 snake_case */
  resetToAll?: string[]
}

export interface AgentCapabilities {
  enabled: boolean
  objectTypes: { id: string; name: string; displayName: string; instanceCount: number }[]
  linkTypes: { id: string; name: string; displayName: string }[]
  actions: { id: string; name: string; displayName: string; requiresApproval: boolean }[]
  allowActionProposals: boolean
  maxRowsPerQuery: number
  maxSteps: number
  skillCard: string
  releaseId?: string | null
  releaseVersion?: string | null
}

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

export interface DynamicSentinelTrialReport {
  passed: boolean
  releaseId: string
  replayCoverage?: 'full' | 'partial' | 'empty' | 'invalid'
  replayWindowSeconds?: number
  activeStates?: number
  candidateCount: number
  matchCount: number
  plannedActionCount: number
  plannedActions: {
    actionId: string
    actionName: string
    targetInstanceId?: string | null
    match: Record<string, string>
    parameters: Record<string, unknown>
    validationErrors: string[]
  }[]
  plannedActionsTruncated: boolean
  candidateCapReached: boolean
  errors: string[]
  durationMs: number
  sideEffects: 'none'
}

export interface DynamicSentinel extends DynamicSentinelDefinition {
  id: string
  ontologyId: string
  origin: 'assistant_dynamic'
  boundReleaseId: string
  createdBy?: string | null
  definitionRevision: number
  enabled: boolean
  status: string
  validationReport?: { passed: boolean; errors: { message?: string }[]; compatibility?: string } | null
  lastTrialAt?: string | null
  lastTrialReport?: DynamicSentinelTrialReport | null
  trialCurrent: boolean
  canEnable: boolean
  createdAt?: string | null
  updatedAt?: string | null
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

export interface AgentMessageDTO {
  id: string
  role: 'user' | 'assistant'
  content: string
  steps: AgentStep[]
  citations: AgentCitation[]
  proposals: AgentProposal[]
  model?: string | null
  tokenUsage?: { inputTokens?: number; outputTokens?: number } | null
  createdAt: string
}

export interface AgentConversationDTO {
  id: string
  title: string
  ontologyReleaseId?: string | null
  createdAt: string
  updatedAt: string
  messages?: AgentMessageDTO[]
}

export interface AgentConversationExport {
  schemaVersion: 'openontology.agent-conversation.v1'
  exportedAt: string
  ontology: {
    id: string
    name: string
    domain: string
    version: string
  }
  conversation: {
    id: string
    ontologyId: string
    ontologyReleaseId: string | null
    userId: string | null
    title: string
    createdAt: string
    updatedAt: string
  }
  messages: AgentMessageDTO[]
  decisionSimulations: DecisionSimulationRun[]
  summary: {
    messageCount: number
    userMessageCount: number
    assistantMessageCount: number
    toolStepCount: number
    decisionSimulationCount: number
    tokenUsage: { inputTokens: number; outputTokens: number }
    contentCompleteness: {
      messageHistory: 'complete'
      toolResults: 'complete' | 'contains_legacy_truncation'
      legacyTruncatedToolResultCount: number
    }
  }
}

export interface AnswerVerification {
  passed: boolean
  answerNumberCount?: number
  unverified?: number[]
  retried?: boolean
}

export type AgentEvent =
  | { type: 'meta'; conversationId: string; model: string; releaseId?: string | null }
  | ({ type: 'step' } & AgentStep)
  | { type: 'answer'; content: string; citations: AgentCitation[]; proposals: AgentProposal[]; usage?: unknown; verification?: AnswerVerification | null }
  | { type: 'error'; message: string }
  | { type: 'cancelled' }
  | { type: 'done' }

/** 回合状态（GET /agent/chat/runs/{runId}）：unknown = 注册表不认识该 run */
export interface AgentChatRun {
  runId: string
  conversationId: string | null
  status: 'running' | 'succeeded' | 'error' | 'cancelled' | 'unknown'
  startedAt: string | null
  finishedAt: string | null
}

export interface ExecuteProposalResult {
  status: string
  errorMessage?: string | null
  validationErrors?: string[]
  effects?: { type: string; description?: string }[]
  pendingApproval?: boolean
  id?: string
}

export type AgentGraphNodeKind = 'object_type' | 'instance' | 'property'
export type AgentGraphEdgeKind = 'schema_relation' | 'contains' | 'relation' | 'attribute'

export interface AgentGraphNode {
  id: string
  entityId: string
  kind: AgentGraphNodeKind
  label: string
  secondaryLabel?: string | null
  technicalName?: string
  objectTypeId?: string
  objectTypeLabel?: string
  count?: number
  color?: string | null
  description?: string | null
  propertiesCount?: number
  source?: string | null
  externalId?: string | null
  preview?: { name: string; label: string; value: string }[]
  updatedAt?: string | null
  instanceId?: string
  propertyName?: string
  propertyType?: string
  value?: unknown
  isNull?: boolean
}

export interface AgentGraphEdge {
  id: string
  entityId?: string
  kind: AgentGraphEdgeKind
  source: string
  target: string
  label: string
  linkTypeId?: string
  linkTypeName?: string
  cardinality?: string
  properties?: Record<string, unknown>
}

export interface AgentGraphData {
  ontologyId: string
  ontologyName: string
  depth: 1 | 2 | 3
  nodes: AgentGraphNode[]
  edges: AgentGraphEdge[]
  meta: {
    query?: string | null
    objectTypeId?: string | null
    focusInstanceId?: string | null
    instanceCounts: Record<string, number>
    loadedInstances: number
    matchedInstances: number
    limitPerType: number
    truncated: boolean
    propertyTruncated: boolean
    nodeBudget: number
    edgeBudget: number
  }
}

export interface AgentInstanceDetail {
  id: string
  label: string
  objectType: {
    id: string
    name: string
    displayName: string
    primaryKey?: string | null
    properties: { id?: string; name: string; displayName?: string; display_name?: string; type?: string }[]
  }
  properties: Record<string, unknown>
  computed: Record<string, unknown>
  source?: string | null
  externalId?: string | null
  createdAt?: string | null
  updatedAt?: string | null
}

export interface AgentGraphPath {
  nodeIds: string[]
  edgeIds: string[]
  steps: { linkInstanceId: string; linkTypeId: string; direction: 'out' | 'in' }[]
  hops: number
}

export interface AgentGraphPathResult {
  kind: 'path'
  sourceInstanceId: string
  targetInstanceId: string
  sourceLabel: string
  targetLabel: string
  direction: 'both' | 'outgoing' | 'incoming'
  maxDepth: number
  paths: AgentGraphPath[]
  nodes: AgentGraphNode[]
  edges: AgentGraphEdge[]
  found: boolean
  truncated: boolean
  visualizationTruncated?: boolean
  visualizationCounts?: AgentGraphVisualizationCounts
}

export interface AgentGraphVisualizationCounts {
  available: { nodes: number; edges: number; impacts: number }
  displayed: { nodes: number; edges: number; impacts: number }
}

export interface AgentGraphImpactResult {
  kind: 'impact'
  mode: 'association_only'
  change: {
    instanceId: string
    instanceLabel: string
    objectType: string
    property: string
    propertyLabel: string
    currentValue: unknown
    proposedValue: unknown
  }
  direction: 'both' | 'outgoing' | 'incoming'
  maxDepth: number
  summary: { related: number; direct: number; indirect: number }
  impacts: {
    instanceId: string
    label: string
    objectType: string
    depth: number
    classification: 'direct' | 'indirect'
    certainty: 'related'
    path: AgentGraphPath
  }[]
  nodes: AgentGraphNode[]
  edges: AgentGraphEdge[]
  truncated: boolean
  disclaimer: string
  visualizationTruncated?: boolean
  visualizationCounts?: AgentGraphVisualizationCounts
}

export type ReportVisualization = 'auto' | 'kpi' | 'bar' | 'line' | 'pie' | 'table' | 'none'

export interface ReportQuery {
  tool: 'aggregate_objects' | 'search_objects'
  arguments: Record<string, unknown>
}

export interface ReportSection {
  id: string
  title: string
  goal: string
  visualization: ReportVisualization
  queryPlan: ReportQuery[]
}

export interface AnalysisReportTemplate {
  id: string
  ontologyId: string
  createdBy: string
  name: string
  description: string
  sourcePrompt: string
  generationMode: 'ai' | 'fallback' | 'manual'
  status: 'draft' | 'published'
  revision: number
  sections: ReportSection[]
  style: Record<string, unknown>
  defaultModelId: string | null
  lastPreviewRunId: string | null
  lastPreviewRevision: number | null
  publishedAt: string | null
  createdAt: string
  updatedAt: string
}

export interface ReportQuality {
  passed: boolean
  score: number
  threshold: number
  summary: string
  blockers: string[]
  warnings: string[]
  checks: { key: string; label: string; passed: boolean; detail: string }[]
  templateRevision: number
}

export interface AnalysisReportRun {
  id: string
  templateId: string
  ontologyId: string
  createdBy: string
  triggerType: 'preview' | 'manual' | 'scheduled'
  status: 'running' | 'succeeded' | 'failed'
  templateRevision: number
  templateSnapshot: Record<string, unknown>
  sectionResults: Record<string, unknown>[]
  qualityReport: ReportQuality
  htmlContent: string
  errorMessage: string | null
  startedAt: string
  completedAt: string | null
}

export interface DecisionAlternative {
  id: string
  label: string
  description?: string
}

export interface DecisionObjective {
  id: string
  label: string
  weight: number
}

export interface DecisionOptionEvaluation {
  optionId: string
  label: string
  meanScore: number
  robustScore: number
  minScore: number
  maxScore: number
  disagreement: number
  perspectiveCount: number
  objectiveScores: Record<string, number>
  rank: number
}

export interface DecisionPerspective {
  id: string
  name: string
  mission: string
  stance: string
  keyFindings: string[]
  challenges: string[]
  evidenceCoverage: number
  optionAssessments: {
    optionId: string
    scores: Record<string, number>
    rationale: string
    evidenceRefs: string[]
    assumptions: string[]
    risks: string[]
  }[]
  scenarioOutlooks: {
    name: string
    trigger: string
    impacts: Record<string, string>
    earlySignals: string[]
  }[]
}

export interface DecisionSimulationSummary {
  id: string
  ontologyId: string
  ontologyReleaseId: string | null
  conversationId: string | null
  title: string
  question: string
  status: 'running' | 'succeeded' | 'failed'
  modelName: string | null
  recommendedOption: string | null
  robustScore: number | null
  perspectiveCount: number
  diagnostics: Record<string, unknown>
  errorMessage: string | null
  startedAt: string
  completedAt: string | null
}

export interface DecisionSimulationRun {
  id: string
  ontologyId: string
  ontologyReleaseId: string | null
  conversationId: string | null
  createdBy: string
  modelConfigId: string | null
  modelName: string | null
  title: string
  question: string
  status: 'running' | 'succeeded' | 'failed'
  specification: {
    title?: string
    decision?: string
    horizon?: string
    alternatives?: DecisionAlternative[]
    objectives?: DecisionObjective[]
    constraints?: string[]
    uncertainties?: string[]
    dataQuestions?: string[]
  }
  snapshot: {
    ontologyName?: string
    releaseId?: string | null
    capturedAt?: string
    isolation?: string
    checksum?: string
    coverage?: {
      instanceCount?: number
      profiledCount?: number
      sampledCount?: number
      objectTypeCount?: number
      linkTypeCount?: number
      sentinelFiringCount?: number
    }
  }
  perspectives: DecisionPerspective[]
  evaluation: {
    method?: { name?: string; formula?: string; probability?: boolean }
    options?: DecisionOptionEvaluation[]
    objectives?: DecisionObjective[]
    disagreementLevel?: 'low' | 'medium' | 'high'
    maxDisagreement?: number
    evidenceCoverage?: number
  }
  recommendation: {
    recommendedOptionId?: string
    recommendedOption?: string
    robustScore?: number
    summary?: string
    rationale?: string[]
    tradeoffs?: string[]
    noRegretActions?: string[]
    earlySignals?: string[]
    stopConditions?: string[]
    confidenceBand?: 'weak' | 'moderate' | 'strong'
    nature?: string
    disclaimer?: string
  }
  diagnostics: {
    phase?: string
    perspectiveCurrent?: string | null
    perspectiveCompleted?: number
    perspectiveTotal?: number
    perspectiveFailures?: { perspectiveId: string; message: string }[]
    warnings?: string[]
    engineVersion?: string
    isolation?: string
    usage?: { inputTokens?: number; outputTokens?: number }
  }
  errorMessage: string | null
  startedAt: string
  completedAt: string | null
}

// ---------- REST ----------

const base = (oid: string) => `/formal/ontologies/${oid}/agent`

export const agentApi = {
  getProfile: (oid: string) => apiClientV2.get<AgentProfile>(`${base(oid)}/profile`),
  updateProfile: (oid: string, body: AgentProfileUpdate) =>
    apiClientV2.put<AgentProfile>(`${base(oid)}/profile`, body),
  capabilities: (oid: string, releaseId?: string | null) => apiClientV2.get<AgentCapabilities>(`${base(oid)}/capabilities`, {
    params: { release_id: releaseId || undefined },
  }),
  graph: (oid: string, params: {
    depth?: 1 | 2 | 3
    query?: string
    objectType?: string
    focusInstanceId?: string
    limitPerType?: number
    releaseId?: string | null
  }) => apiClientV2.get<AgentGraphData>(`${base(oid)}/graph`, {
    params: {
      depth: params.depth,
      query: params.query,
      object_type: params.objectType,
      focus_instance_id: params.focusInstanceId,
      limit_per_type: params.limitPerType,
      release_id: params.releaseId || undefined,
    },
  }),
  graphInstance: (oid: string, instanceId: string, releaseId?: string | null) =>
    apiClientV2.get<AgentInstanceDetail>(`${base(oid)}/graph/instances/${encodeURIComponent(instanceId)}`, {
      params: { release_id: releaseId || undefined },
    }),
  findPaths: (oid: string, body: {
    sourceInstanceId: string
    targetInstanceId: string
    direction?: 'both' | 'outgoing' | 'incoming'
    maxDepth?: number
    maxPaths?: number
    releaseId?: string | null
  }) => apiClientV2.post<AgentGraphPathResult>(`${base(oid)}/graph/paths`, body),
  analyzeImpact: (oid: string, body: {
    instanceId: string
    property: string
    proposedValue: unknown
    direction?: 'both' | 'outgoing' | 'incoming'
    maxDepth?: number
    releaseId?: string | null
  }) => apiClientV2.post<AgentGraphImpactResult>(`${base(oid)}/graph/impact`, body),
  conversations: (oid: string, releaseId?: string | null) => apiClientV2.get<AgentConversationDTO[]>(`${base(oid)}/conversations`, {
    params: { release_id: releaseId || undefined },
  }),
  conversation: (oid: string, cid: string) =>
    apiClientV2.get<AgentConversationDTO>(`${base(oid)}/conversations/${cid}`),
  exportConversation: (oid: string, cid: string) =>
    apiClientV2.get<AgentConversationExport>(`${base(oid)}/conversations/${cid}/export`),
  deleteConversation: (oid: string, cid: string) =>
    apiClientV2.delete(`${base(oid)}/conversations/${cid}`),
  executeProposal: (oid: string, body: { actionId: string; parameters: Record<string, unknown>; targetInstanceId?: string | null; releaseId?: string | null }) =>
    apiClientV2.post<ExecuteProposalResult>(`${base(oid)}/execute-proposal`, body),
  cancelChat: (oid: string, runId: string) =>
    apiClientV2.post<{ runId: string; cancelled: boolean; note: string }>(
      `${base(oid)}/chat/cancel`, { runId }),
  chatRun: (oid: string, runId: string) =>
    apiClientV2.get<AgentChatRun>(`${base(oid)}/chat/runs/${runId}`),
  dynamicSentinels: (oid: string, releaseId: string) =>
    apiClientV2.get<DynamicSentinel[]>(`${base(oid)}/dynamic-sentinels`, { params: { release_id: releaseId } }),
  createDynamicSentinel: (oid: string, releaseId: string, definition: DynamicSentinelDefinition) =>
    apiClientV2.post<DynamicSentinel>(`${base(oid)}/dynamic-sentinels`, { releaseId, definition }),
  updateDynamicSentinel: (oid: string, releaseId: string, row: DynamicSentinel, definition: DynamicSentinelDefinition) =>
    apiClientV2.put<DynamicSentinel>(`${base(oid)}/dynamic-sentinels/${row.id}`, {
      releaseId, expectedRevision: row.definitionRevision, definition,
    }),
  trialDynamicSentinel: (oid: string, releaseId: string, id: string) =>
    apiClientV2.post<DynamicSentinel>(`${base(oid)}/dynamic-sentinels/${id}/trial`, { releaseId }),
  setDynamicSentinelEnabled: (oid: string, releaseId: string, row: DynamicSentinel, enabled: boolean) =>
    apiClientV2.post<DynamicSentinel>(`${base(oid)}/dynamic-sentinels/${row.id}/enabled`, {
      releaseId, expectedRevision: row.definitionRevision, enabled,
    }),
  deleteDynamicSentinel: (oid: string, releaseId: string, row: DynamicSentinel) =>
    apiClientV2.delete(`${base(oid)}/dynamic-sentinels/${row.id}`, {
      params: { release_id: releaseId, expected_revision: row.definitionRevision },
    }),
  executeDynamicSentinelProposal: (oid: string, proposal: AgentSentinelProposal) =>
    apiClientV2.post<DynamicSentinel | { status: string; id: string }>(
      `${base(oid)}/dynamic-sentinels/execute-proposal`, {
        operation: proposal.operation,
        releaseId: proposal.releaseId,
        sentinelId: proposal.sentinelId,
        expectedRevision: proposal.expectedRevision,
        definition: proposal.definition,
      }),
  reportTemplates: (oid: string) =>
    apiClientV2.get<AnalysisReportTemplate[]>(`${base(oid)}/report-templates`),
  createReportTemplate: (oid: string, body: { brief: string; modelId?: string | null; conversationId?: string | null }) =>
    apiClientV2.post<AnalysisReportTemplate>(`${base(oid)}/report-templates/ai-draft`, body),
  reportTemplate: (oid: string, templateId: string) =>
    apiClientV2.get<AnalysisReportTemplate>(`${base(oid)}/report-templates/${templateId}`),
  updateReportTemplate: (oid: string, templateId: string, body: {
    expectedRevision: number; name: string; description: string; sections: ReportSection[];
    style: Record<string, unknown>; defaultModelId?: string | null
  }) => apiClientV2.put<AnalysisReportTemplate>(`${base(oid)}/report-templates/${templateId}`, body),
  deleteReportTemplate: (oid: string, templateId: string) =>
    apiClientV2.delete(`${base(oid)}/report-templates/${templateId}`),
  previewReportTemplate: (oid: string, templateId: string, modelId?: string | null) =>
    apiClientV2.post<AnalysisReportRun>(`${base(oid)}/report-templates/${templateId}/preview`, { modelId }),
  publishReportTemplate: (oid: string, templateId: string) =>
    apiClientV2.post<AnalysisReportTemplate>(`${base(oid)}/report-templates/${templateId}/publish`, {}),
  runReportTemplate: (oid: string, templateId: string, modelId?: string | null) =>
    apiClientV2.post<AnalysisReportRun>(`${base(oid)}/report-templates/${templateId}/runs`, { modelId }),
  reportRuns: (oid: string, templateId: string) =>
    apiClientV2.get<AnalysisReportRun[]>(`${base(oid)}/report-templates/${templateId}/runs`),
  reportRun: (oid: string, runId: string) =>
    apiClientV2.get<AnalysisReportRun>(`${base(oid)}/report-runs/${runId}`),
  decisionSimulations: (oid: string, params?: {
    releaseId?: string | null; conversationId?: string | null; limit?: number
  }) => apiClientV2.get<DecisionSimulationSummary[]>(`${base(oid)}/decision-simulations`, {
    params: {
      release_id: params?.releaseId || undefined,
      conversation_id: params?.conversationId || undefined,
      limit: params?.limit,
    },
  }),
  decisionSimulation: (oid: string, runId: string) =>
    apiClientV2.get<DecisionSimulationRun>(`${base(oid)}/decision-simulations/${runId}`),
  createDecisionSimulation: (oid: string, body: {
    question: string; alternatives?: string[]; horizon?: string | null;
    conversationId?: string | null; modelId?: string | null; releaseId?: string | null
  }) => apiClientV2.post<DecisionSimulationRun>(`${base(oid)}/decision-simulations`, body),
}

export function reportHtmlUrl(oid: string, runId: string): string {
  return `${apiRoot()}${base(oid)}/report-runs/${runId}/html`
}

// ---------- SSE 流式 chat ----------

function apiRoot(): string {
  const runtimeBase = (typeof window !== 'undefined' && (window as any).__API_BASE_URL__) || ''
  return `${runtimeBase}/api/v2`
}

export async function streamAgentChat(
  oid: string,
  body: { message: string; conversationId?: string | null; modelId?: string | null; releaseId?: string | null; runId?: string | null },
  onEvent: (e: AgentEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const token = localStorage.getItem('token') || ''
  const resp = await fetch(`${apiRoot()}${base(oid)}/chat`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
    body: JSON.stringify({
      message: body.message,
      conversationId: body.conversationId || undefined,
      modelId: body.modelId || undefined,
      releaseId: body.releaseId || undefined,
      runId: body.runId || undefined,
      stream: true,
    }),
    signal,
  })
  if (!resp.ok || !resp.body) {
    const text = await resp.text().catch(() => '')
    throw new Error(`对话请求失败 (${resp.status}) ${text.slice(0, 200)}`)
  }

  const reader = resp.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    // SSE 事件以空行分隔
    let idx: number
    while ((idx = buffer.indexOf('\n\n')) >= 0) {
      const chunk = buffer.slice(0, idx)
      buffer = buffer.slice(idx + 2)
      for (const line of chunk.split('\n')) {
        if (!line.startsWith('data:')) continue
        try {
          onEvent(JSON.parse(line.slice(5).trim()) as AgentEvent)
        } catch { /* 忽略无法解析的行 */ }
      }
    }
  }
}
