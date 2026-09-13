/**
 * 业务探索 API — /api/v2/exploration/*
 *
 * chat 走 SSE 流式（fetch + ReadableStream 手动解析，同 agent.ts），
 * 其余走 apiClientV2。画布元素后端以 snake_case 存储（display_name 等）。
 */
import { apiClientV2 } from './client'
import { createStreamWatchdog, StreamIdleTimeoutError } from './streamWatchdog'

// ---------- 类型 ----------

export type CanvasKind = 'object' | 'actor' | 'behavior' | 'event' | 'rule' | 'scenario' | 'process'

export interface CanvasElement {
  id: string
  name: string
  display_name?: string
  description?: string
  [k: string]: unknown
}

/** 澄清账本条目：blocking=企业口径必须拍板；advisory=AI 建议待确认 */
export interface BxQuestion {
  id: string
  question: string
  kind: 'blocking' | 'advisory'
  target?: string
  options?: string[]
  suggestion?: string
  status: 'open' | 'resolved' | 'dismissed'
  resolution?: string
}

export interface BusinessCanvas {
  objects: CanvasElement[]
  actors: CanvasElement[]
  behaviors: CanvasElement[]
  events: CanvasElement[]
  rules: CanvasElement[]
  processes: CanvasElement[]
  scenarios: CanvasElement[]
  questions?: BxQuestion[]
}

export interface Completeness {
  counts: Record<string, number>
  gaps: string[]
}

/** 质量门：与后端草稿生成闸门同一口径（readiness.evaluate） */
export interface GateResult {
  id: string
  label: string
  passed: boolean
  blockingItems: string[]
  advisoryItems: string[]
}

export interface Readiness {
  ready: boolean
  stage: string
  gatesPassed: number
  gatesTotal: number
  blockingCount: number
  advisoryCount: number
  openQuestions: { blocking: number; advisory: number }
  gates: GateResult[]
}

export type DiagramKind = 'er' | 'flow' | 'sequence' | 'state'

export interface BxDiagram {
  kind: DiagramKind
  title: string
  target?: string
  mermaid: string
  warnings?: string[]
  layout?: {
    baseSize: { width: number; height: number }
    canvasSize: { width: number; height: number }
    density: number
  }
}

export interface BxStep {
  tool: string
  arguments: Record<string, unknown>
  summary: string
  durationMs: number
  error?: string
  /** 联网搜索步骤附带的可核验来源。 */
  searchResults?: { title: string; url: string; snippet: string }[]
  /** show_diagram 的产物：确定性生成的 mermaid，随步骤直接渲染进对话 */
  diagram?: BxDiagram
}

/** 建模计划清单项（todo_write 覆盖式维护，随 plan 事件 / 历史步骤回放）。 */
export interface BxPlanItem {
  content: string
  status: 'pending' | 'in_progress' | 'done'
}

export interface BxMessage {
  id: string
  role: 'user' | 'assistant'
  content: string
  steps: BxStep[]
  model?: string | null
  createdAt: string
}

export interface BxSession {
  id: string
  title: string
  canvasVersion: number
  status: string
  /** 版本业务语义层挂载点；未绑定会话为 null/缺省。 */
  ontologyId?: string | null
  ontologyVersionId?: string | null
  createdAt: string
  updatedAt: string
}

export interface BxSessionDetail extends BxSession {
  canvas: BusinessCanvas
  completeness: Completeness
  readiness: Readiness
  messages: BxMessage[]
}

/** GET /exploration/draft-ontologies 行：有编辑中草稿版本、且当前用户可写的本体。 */
export interface BxDraftOntology {
  ontologyId: string
  ontologyName: string
  domain: string
  versionId: string
  versionNumber: string
  versionLabel: string
  draftCreatedAt: string | null
}

export type ExploreEvent =
  | { type: 'meta'; sessionId: string; model: string }
  | { type: 'text_delta'; delta: string }
  | ({ type: 'step' } & BxStep)
  /** 活性心跳（tool=llm_round，每次 provider 调用前发出）：不是真实工具步骤，
      只更新运行指示，不进 steps/历史回放。2026-09 加性契约变更：由复用
      step 类型改为独立 heartbeat 类型（字段形状不变），旧后端在部署窗口内
      仍可能以 step 类型发出 llm_round。 */
  | { type: 'heartbeat'; tool: string; arguments?: Record<string, unknown>; summary?: string; durationMs?: number }
  | { type: 'plan'; items: BxPlanItem[] }
  | { type: 'canvas'; canvas: BusinessCanvas; version: number; completeness: Completeness; readiness: Readiness }
  | { type: 'answer'; content: string; usage?: unknown }
  | { type: 'error'; message: string }
  | { type: 'done' }

export interface BxDocumentListItem {
  id: string
  sessionId: string
  title: string
  version: number
  /** 文档生成时的画布版本；历史文档可能没有该元数据。 */
  sourceCanvasVersion: number | null
  sourceCanvasFingerprint: string
  /** 当前会话画布状态，接口会在读取时实时计算。 */
  currentCanvasVersion: number
  currentCanvasFingerprint: string
  isStale: boolean
  createdAt: string
}

export interface BxDocument extends BxDocumentListItem {
  contentMd: string
}

export interface DraftProperty {
  id: string
  name: string
  displayName: string
  type: string
  required: boolean
  description?: string
  validation?: { enum?: string[] }
}

export interface DraftObjectType {
  key: string
  name: string
  displayName: string
  description?: string
  color?: string
  primaryKey: string
  properties: DraftProperty[]
  origin: 'object' | 'actor'
  sourceRefs?: string[]
  conflict?: boolean
}

export interface DraftLinkType {
  key: string
  name: string
  displayName: string
  description?: string
  sourceKey: string
  targetKey: string
  sourceName: string
  targetName: string
  cardinality: string
  conflict?: boolean
}

export interface DraftAction {
  key: string
  name: string
  displayName: string
  description?: string
  objectTypeKey?: string | null
  actorRefs?: {
    id?: string
    name: string
    displayName?: string
    kind?: string
    description?: string
    responsibilities?: string[]
    keyAttribute?: string | null
  }[]
  sourceRefs?: string[]
  parameters: DraftProperty[]
  rules: { name: string; enabled: boolean; config: { errorMessage?: string } }[]
  requiresApproval: boolean
  conflict?: boolean
}

/** 激活函数草稿：derivation 规则转出，enabled=false 落地待人工补函数体 */
export interface DraftFunction {
  key: string
  name: string
  displayName: string
  description?: string
  functionType: 'object' | 'query'
  language: string
  returnType: string
  body: string
  enabled: boolean
  targetObjectTypeKey?: string | null
  targetObjectTypeName?: string | null
  conflict?: boolean
}

/** 哨兵草稿：alert 规则/事件转出，muted 影子 + enabled=false + status=draft 三重闸门 */
export interface DraftSentinel {
  key: string
  name: string
  displayName: string
  description?: string
  bindingObjectKey?: string | null
  bindingObjectName?: string | null
  onChange: boolean
  onSchedule: boolean
  scanIntervalSeconds?: number
  muted: boolean
  enabled: boolean
  status: string
  originKind?: 'rule' | 'event'
  conflict?: boolean
}

/** 可表达性检查条目（判别式联合）：场景条目形状与语义不变；流程条目以 process 键判别 */
export type DraftCoverageEntry =
  | { scenario: string; missingObjects: string[]; missingBehaviors: string[] }
  | { process: string; missingObjects: string[]; missingBehaviors: string[] }

export interface DraftReport {
  warnings: string[]
  conflicts: string[]
  scenarioCoverage: DraftCoverageEntry[]
  llmRefined: boolean
  /** 生成时刻的质量门快照；gateOverride=true 表示未就绪被显式越权 */
  readiness?: Pick<Readiness, 'ready' | 'stage' | 'gatesPassed' | 'gatesTotal' | 'blockingCount' | 'advisoryCount'>
  gateOverride?: boolean
  /** 无法完整映射到运行时模型的语义及其原始画布血缘。 */
  semanticIssues?: DraftSemanticIssue[]
  semanticFidelity?: {
    blockingCount: number
    unsupportedCount: number
    readyToApply: boolean
  }
  /** 强制生成的审计留痕。 */
  staleDocumentOverride?: boolean
  semanticOverride?: boolean
  sourceDocument?: DocumentSourceState
  validation?: DraftValidation
}

export interface DraftSemanticIssue {
  code: string
  severity: 'blocking' | 'unsupported'
  message: string
  key?: string
  sourceRefs?: string[]
}

export interface DocumentSourceState {
  sourceCanvasVersion: number | null
  sourceCanvasFingerprint: string
  currentCanvasVersion: number
  currentCanvasFingerprint: string
  isStale: boolean
}

export interface DraftValidationIssue {
  code: string
  key?: string
  name?: string
  field?: string
  message: string
}

export interface DraftValidation {
  valid: boolean
  errors: DraftValidationIssue[]
  warnings: DraftValidationIssue[]
  selectedCount: number
  counts: Record<string, number>
}

export interface BxDraft {
  id: string
  sessionId: string
  documentId: string
  targetOntologyId: string | null
  draft: {
    objectTypes: DraftObjectType[]
    linkTypes: DraftLinkType[]
    actions: DraftAction[]
    functions?: DraftFunction[]     // 旧草稿无此键
    sentinels?: DraftSentinel[]
  }
  report: DraftReport
  status: 'draft' | 'applied' | 'discarded'
  appliedOntologyId: string | null
  createdAt: string
  updatedAt: string
}

export interface ApplyDraftResult {
  ontologyId: string
  ontologyName: string
  created: { objectTypes: number; linkTypes: number; actions: number; functions: number; sentinels: number }
  skipped: { key: string; reason: string }[]
  /** 草稿落地写入的版本（合并路径为目标草稿版本，新建路径为 v0 基线）；旧后端可能缺省。 */
  versionId?: string
  versionNumber?: string
}

export interface BxAttachment {
  id: string
  sessionId: string
  filename: string
  relativePath: string
  mimeType?: string | null
  fileSize: number
  charCount: number
  sha256?: string | null
  version: number
  source: 'upload' | 'user' | 'agent'
  editable: boolean
  status: 'ready' | 'failed'
  error?: string | null
  createdAt: string
  updatedAt: string
}

export interface BxWorkspaceText {
  id: string
  relativePath: string
  content: string
  version: number
  sha256?: string | null
}

export interface BxWorkspacePreview {
  id: string
  relativePath: string
  content: string
  version: number
  mimeType?: string | null
  editable: boolean
  truncated: boolean
}

// ---------- 本体预览投影（只读） ----------

/** add=将新增 / exists=已存在将跳过 / conflict=同名冲突不进默认选择集 */
export type OntologyPreviewDisposition = 'add' | 'exists' | 'conflict'

export interface OntologyPreviewItem {
  key: string
  name: string
  displayName: string
  disposition: OntologyPreviewDisposition
}

export interface OntologyPreview {
  canvasFingerprint: string
  canvasVersion: number
  /** 是否绑定本体版本；绑定时与基线快照比对，未绑定全部按「将新增」。 */
  bound: boolean
  ontologyId?: string | null
  ontologyVersionId?: string | null
  readiness: Pick<Readiness, 'ready' | 'gatesPassed' | 'gatesTotal' | 'blockingCount' | 'advisoryCount'>
  projected: {
    objectTypes: OntologyPreviewItem[]
    linkTypes: OntologyPreviewItem[]
    actions: OntologyPreviewItem[]
    functions: OntologyPreviewItem[]
    sentinels: OntologyPreviewItem[]
  }
  semanticIssues: DraftSemanticIssue[]
}

// ---------- REST ----------

export const explorationApi = {
  sessions: () => apiClientV2.get<BxSession[]>('/exploration/sessions'),
  createSession: (title?: string, binding?: { ontologyId: string; ontologyVersionId?: string | null }) =>
    apiClientV2.post<BxSession>('/exploration/sessions', {
      title,
      ontologyId: binding?.ontologyId || undefined,
      ontologyVersionId: binding?.ontologyVersionId || undefined,
    }),
  session: (sid: string) => apiClientV2.get<BxSessionDetail>(`/exploration/sessions/${sid}`),
  deleteSession: (sid: string) => apiClientV2.delete(`/exploration/sessions/${sid}`),
  draftOntologies: () => apiClientV2.get<{ items: BxDraftOntology[] }>('/exploration/draft-ontologies'),
  canvas: (sid: string) =>
    apiClientV2.get<{ canvas: BusinessCanvas; version: number; completeness: Completeness; readiness: Readiness }>(
      `/exploration/sessions/${sid}/canvas`),
  readiness: (sid: string) =>
    apiClientV2.get<Readiness>(`/exploration/sessions/${sid}/readiness`),
  ontologyPreview: (sid: string) =>
    apiClientV2.get<OntologyPreview>(`/exploration/sessions/${sid}/ontology-preview`),

  generateDocument: (sid: string, modelId?: string | null) =>
    apiClientV2.post<BxDocument>(`/exploration/sessions/${sid}/documents`,
      { modelId: modelId || undefined }),
  documents: (sid: string) =>
    apiClientV2.get<BxDocumentListItem[]>(`/exploration/sessions/${sid}/documents`),
  document: (docId: string) => apiClientV2.get<BxDocument>(`/exploration/documents/${docId}`),

  generateDraft: (docId: string, body: { targetOntologyId?: string | null; modelId?: string | null; force?: boolean }) =>
    apiClientV2.post<BxDraft>(`/exploration/documents/${docId}/drafts`, {
      targetOntologyId: body.targetOntologyId || undefined,
      modelId: body.modelId || undefined,
      force: body.force || undefined,
    }),
  drafts: (sid: string) => apiClientV2.get<BxDraft[]>(`/exploration/sessions/${sid}/drafts`),
  diagram: (sid: string, kind: DiagramKind, target?: string) =>
    apiClientV2.get<BxDiagram>(`/exploration/sessions/${sid}/diagrams/${kind}`,
      { params: target ? { target } : undefined }),

  // 会话附件（仅本会话可见，随会话删除清理）
  attachments: (sid: string) =>
    apiClientV2.get<BxAttachment[]>(`/exploration/sessions/${sid}/attachments`),
  uploadAttachment: (sid: string, file: File) => {
    const fd = new FormData()
    fd.append('file', file)
    return apiClientV2.post<BxAttachment>(`/exploration/sessions/${sid}/attachments`, fd, {
      headers: { 'Content-Type': 'multipart/form-data' },
    })
  },
  deleteAttachment: (sid: string, aid: string) =>
    apiClientV2.delete(`/exploration/sessions/${sid}/attachments/${aid}`),
  downloadAttachment: (sid: string, aid: string) =>
    apiClientV2.get<Blob>(`/exploration/sessions/${sid}/attachments/${aid}/download`, {
      responseType: 'blob',
    }),
  attachmentContent: (sid: string, aid: string) =>
    apiClientV2.get<BxWorkspaceText>(`/exploration/sessions/${sid}/attachments/${aid}/content`),
  attachmentPreview: (sid: string, aid: string) =>
    apiClientV2.get<BxWorkspacePreview>(`/exploration/sessions/${sid}/attachments/${aid}/preview`),
  createWorkspaceText: (sid: string, body: { path: string; content: string; mimeType?: string }) =>
    apiClientV2.post<BxAttachment>(`/exploration/sessions/${sid}/workspace/files`, body),
  updateWorkspaceText: (sid: string, aid: string, body: { content: string; expectedVersion: number }) =>
    apiClientV2.put<BxAttachment>(`/exploration/sessions/${sid}/attachments/${aid}/content`, body),
  draft: (draftId: string) => apiClientV2.get<BxDraft>(`/exploration/drafts/${draftId}`),
  applyDraft: (draftId: string, body: {
    selectedKeys?: string[] | null
    newOntology?: { name: string; domain?: string; description?: string }
  }) => apiClientV2.post<ApplyDraftResult>(`/exploration/drafts/${draftId}/apply`, body),
  validateDraft: (draftId: string, selectedKeys?: string[] | null) =>
    apiClientV2.post<DraftValidation>(`/exploration/drafts/${draftId}/validate`, {
      selectedKeys: selectedKeys ?? undefined,
    }),
  discardDraft: (draftId: string) =>
    apiClientV2.post<BxDraft>(`/exploration/drafts/${draftId}/discard`),
}

// ---------- SSE 流式 chat ----------

function apiRoot(): string {
  const runtimeBase = (typeof window !== 'undefined'
    && (window as Window & { __API_BASE_URL__?: string }).__API_BASE_URL__) || ''
  return `${runtimeBase}/api/v2`
}

/**
 * 流静默看门狗超时（D-014）：后端每个 LLM 轮都有 llm_round 心跳，正常
 * 长任务不会长时间零字节；工具执行期间的静默窗口留足余量。测试可用
 * window.__EXPLORE_STREAM_IDLE_TIMEOUT_MS__ 覆盖（同 __API_BASE_URL__ 先例）。
 */
const STREAM_IDLE_TIMEOUT_DEFAULT_MS = 180_000

function streamIdleTimeoutMs(): number {
  const override = typeof window !== 'undefined'
    && (window as Window & { __EXPLORE_STREAM_IDLE_TIMEOUT_MS__?: number })
      .__EXPLORE_STREAM_IDLE_TIMEOUT_MS__
  return typeof override === 'number' && override > 0 ? override : STREAM_IDLE_TIMEOUT_DEFAULT_MS
}

export async function streamExplorationChat(
  sid: string,
  body: { message: string; modelId?: string | null; webSearch?: boolean },
  onEvent: (e: ExploreEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const idleTimeoutMs = streamIdleTimeoutMs()
  const watchdog = createStreamWatchdog(idleTimeoutMs, signal)
  let sawDone = false
  try {
    const token = localStorage.getItem('token') || ''
    const resp = await fetch(`${apiRoot()}/exploration/sessions/${sid}/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
      body: JSON.stringify({
        message: body.message,
        modelId: body.modelId || undefined,
        webSearch: body.webSearch || undefined,
        stream: true,
      }),
      signal: watchdog.signal,
    })
    watchdog.activity()
    if (!resp.ok || !resp.body) {
      const text = await resp.text().catch(() => '')
      throw new Error(`对话请求失败 (${resp.status}) ${text.slice(0, 200)}`)
    }

    const reader = resp.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''
    const consumeChunk = (chunk: string) => {
      for (const line of chunk.split('\n')) {
        if (!line.startsWith('data:')) continue
        try {
          const event = JSON.parse(line.slice(5).trim()) as ExploreEvent
          if (event.type === 'done') sawDone = true
          onEvent(event)
        } catch { /* 忽略无法解析的行 */ }
      }
    }
    for (;;) {
      const { done, value } = await reader.read()
      if (done) {
        buffer += decoder.decode()
        break
      }
      watchdog.activity()
      buffer += decoder.decode(value, { stream: true })
      let idx: number
      while ((idx = buffer.indexOf('\n\n')) >= 0) {
        const chunk = buffer.slice(0, idx)
        buffer = buffer.slice(idx + 2)
        consumeChunk(chunk)
      }
    }
    if (buffer.trim()) consumeChunk(buffer)
    if (!sawDone && !signal?.aborted) {
      throw new Error('对话连接在完成前中断，请重试；未完成内容不会写入当前会话')
    }
  } catch (error) {
    // 看门狗触发的 abort 不能当作用户取消静默吞掉：它是需要呈现的错误
    if (watchdog.timedOut()) throw new StreamIdleTimeoutError(idleTimeoutMs)
    throw error
  } finally {
    watchdog.dispose()
  }
}
