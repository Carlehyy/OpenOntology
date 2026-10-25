import { apiClientV2 } from '@/api/client'

export interface OntologyTrialActionEffect {
  type?: string
  description?: string
  status?: string
  committed?: boolean
  [key: string]: unknown
}

export interface OntologyTrialActionSample {
  actionId?: string
  actionName?: string
  edge?: 'enter' | 'leave' | string
  targetInstanceId?: string | null
  match?: Record<string, unknown>
  parameters?: Record<string, unknown>
  status?: string
  effects?: OntologyTrialActionEffect[]
  validationErrors?: string[]
  errorMessage?: string | null
  sideEffects?: string
}

export interface OntologyTrialSentinelResult {
  id?: string
  name?: string
  activation?: 'active' | 'muted' | 'disabled' | string
  matched?: number
  candidateCount?: number
  candidateCapReached?: boolean
  parameterErrorCount?: number
  errors?: string[]
  plannedActions?: number
  plannedActionSamples?: OntologyTrialActionSample[]
  plannedActionsTruncated?: boolean
  sideEffects?: string
  skipped?: boolean
}

export interface OntologyTrialRun {
  id: string
  status: 'running' | 'passed' | 'failed' | 'stale'
  result?: {
    counts?: { objects?: number; links?: number; facts?: number; datasets?: number }
    errors?: Array<{ message: string }>
    warnings?: Array<{ message: string }>
    sentinels?: OntologyTrialSentinelResult[]
    actionsExecuted?: number
    sideEffects?: string
  }
  impact_hash?: string
  created_at?: string
}

export interface OntologyVersionNode {
  id: string
  version_number: string
  version_label?: string
  description?: string
  parent_version_id?: string | null
  base_release_id?: string | null
  promoted_from_id?: string | null
  node_kind: 'release' | 'draft'
  lifecycle_status: 'editing' | 'trial_ready' | 'released' | 'superseded'
  revision: number
  /** 语义层只透摘要标记：是否已沉淀业务语义（画布/需求文档）。 */
  hasSemanticLayer?: boolean
  /** 语义层修订号；无语义层或旧后端为 0/缺省。 */
  semanticRevision?: number
  latest_trial?: OntologyTrialRun | null
  created_at?: string
  published_at?: string
}

export interface OntologyVersionTree {
  current_release_id: string
  current_release_number: string
  current_release_version: string
  versions: OntologyVersionNode[]
}

/** 版本业务语义层只读总览（后端 semantic_gate.semantic_overview 同口径）。 */
export interface OntologySemanticOverview {
  hasSemanticLayer: boolean
  documentTitle?: string | null
  documentStale: boolean
  canvasCounts: {
    objects: number
    actors: number
    behaviors: number
    events: number
    rules: number
    scenarios: number
    processes: number
  }
  structureCounts: {
    objectTypes: number
    linkTypes: number
    actions: number
    functions: number
    sentinels: number
  }
  consistency: { issueCount: number; byCode: Record<string, number> }
}

/** 语义一致性明细条目（后端 semantic_gate 的 gate_error 同口径）。 */
export interface OntologySemanticIssue {
  code: string
  kind: 'objectType' | 'linkType' | 'action' | 'function' | 'sentinel' | 'document' | string
  id: string
  name: string
  message: string
  field?: string
}

/** GET .../versions/{vid}/semantic 的 data：语义层快照原文 + 一致性总览 + 明细。
 *  issues 为新增字段，旧后端缺省时前端按空数组处理。
 *  canEdit/documentFingerprint 为受控正文编辑的新增字段：canEdit 是服务端
 *  「写权限 × 当前发布指针 × released release × 正文非空」合取，客户端只信
 *  该字段；documentFingerprint 为正文 sha256（语义层缺失时服务端现算），
 *  作为保存时的 CAS 基线。 */
export interface OntologyVersionSemantic {
  semantic: Record<string, unknown> | null
  overview: OntologySemanticOverview
  issues?: OntologySemanticIssue[]
  canEdit?: boolean
  documentFingerprint?: string | null
}

/** PUT .../versions/{vid}/semantic/document 的请求体（三基线 CAS）。 */
export interface SaveOntologyDocumentBody {
  documentMd: string
  baseSemanticRevision: number
  baseFingerprint: string
  expectedCurrentReleaseId: string
}

/** PUT .../versions/{vid}/semantic/document 的 data：与 GET semantic 同形，
 *  另带保存后的 semanticRevision / documentFingerprint。 */
export interface SaveOntologyDocumentResult extends OntologyVersionSemantic {
  semanticRevision: number
  documentFingerprint: string
}

/** POST .../versions/{vid}/trial-preflight 的单项检查（权威门禁，失败即阻断试跑）。 */
export interface OntologyTrialPreflightCheck {
  id: 'editable_draft' | 'single_flight' | 'base_up_to_date' | 'structure' | 'mapping_contract' | 'semantic_consistency' | string
  label: string
  status: 'pass' | 'fail'
  errors: OntologyReleaseGateIssue[]
}

/** POST .../versions/{vid}/trial-preflight 的 data。 */
export interface OntologyTrialPreflight {
  ok: boolean
  versionId: string
  revision: string
  checks: OntologyTrialPreflightCheck[]
}

export interface OntologyImpactReport {
  impactHash: string
  baseOutdated: boolean
  breakingCount: number
  breaking: Array<{ message: string }>
  total: { added: number; modified: number; deleted: number }
  /** 旧后端无此字段，前端按缺失处理（不渲染业务语义区块）。 */
  semanticOverview?: OntologySemanticOverview
  /** 本次删除的对象类型将使其失效的已发布推演服务（实时查询，不参与 impact 哈希）。 */
  worldModelImpact?: Array<{
    serviceId: string
    name: string
    status: string
    missingObjectTypeIds: string[]
  }>
  releaseReadiness?: {
    ready: boolean
    blockingCount: number
    errors: OntologyReleaseGateIssue[]
    trialRunId?: string | null
    runtimeStateConflicts?: OntologyRuntimeStateConflictReport
    repairStrategy?: 'create_draft' | 'rebase' | null
    repairSourceVersionId?: string | null
  }
}

export interface OntologyRuntimeStateConflict {
  resourceKind: 'objectProperty' | 'object' | 'link'
  objectId?: string
  objectTypeId?: string | null
  property?: string
  linkId?: string
  linkTypeId?: string | null
  current: unknown
  currentPresent?: boolean
  candidate: unknown
  candidatePresent?: boolean
  candidateObjectPresent?: boolean
  source: string
  factId?: string | null
}

export interface OntologyRuntimeStateConflictReport {
  totalCount: number
  propertyConflictCount: number
  objectConflictCount: number
  linkConflictCount: number
  itemLimit: number
  truncated: boolean
  items: OntologyRuntimeStateConflict[]
}

export interface OntologyReleaseGateIssue {
  code: string
  kind: string
  id?: string
  name?: string
  message: string
  field?: string
  targetId?: string
  targetName?: string
}

export const ontologyVersionApi = {
  tree: (ontologyId: string) =>
    apiClientV2.get<OntologyVersionTree>(`/ontologies/${ontologyId}/version-tree`),
  createDraft: (ontologyId: string, sourceVersionId: string, body: {
    versionLabel?: string
    description?: string
    recoveryMode?: 'current_release_trial'
    expectedCurrentReleaseId?: string
  }) => apiClientV2.post<OntologyVersionNode>(
    `/ontologies/${ontologyId}/versions/${sourceVersionId}/drafts`, body),
  deleteVersion: (ontologyId: string, versionId: string) =>
    apiClientV2.delete<{ id: string; version_number: string }>(
      `/ontologies/${ontologyId}/versions/${versionId}`),
  runTrial: (ontologyId: string, versionId: string) =>
    apiClientV2.post<OntologyTrialRun>(
      `/ontologies/${ontologyId}/versions/${versionId}/trial-runs`, {}),
  trialPreflight: (ontologyId: string, versionId: string) =>
    apiClientV2.post<OntologyTrialPreflight>(
      `/ontologies/${ontologyId}/versions/${versionId}/trial-preflight`, {}),
  impact: (ontologyId: string, versionId: string) =>
    apiClientV2.get<OntologyImpactReport>(
      `/ontologies/${ontologyId}/versions/${versionId}/impact`),
  versionSemantic: (ontologyId: string, versionId: string) =>
    apiClientV2.get<OntologyVersionSemantic>(
      `/ontologies/${ontologyId}/versions/${versionId}/semantic`),
  saveDocument: (ontologyId: string, versionId: string, body: SaveOntologyDocumentBody) =>
    apiClientV2.put<SaveOntologyDocumentResult>(
      `/ontologies/${ontologyId}/versions/${versionId}/semantic/document`, body),
  promote: (ontologyId: string, versionId: string, body: {
    trialRunId: string
    impactHash: string
    versionLabel?: string
  }) => apiClientV2.post<OntologyVersionNode>(
    `/ontologies/${ontologyId}/versions/${versionId}/promote`, body),
}
