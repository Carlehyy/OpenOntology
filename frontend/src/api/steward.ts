/**
 * 数据管家 API — /api/v2/steward/*
 *
 * chat 走 SSE 流式（fetch + ReadableStream 手动解析，axios 不支持流），
 * 其余走 apiClientV2。
 */
import { apiClientV2 } from './client'

// 浏览器协作共享类型已迁至 ./browserCollaboration（面板泛化供超级助手复用）；
// 此处 re-export 保持既有 import 路径（@/api/steward）不破。
import type {
  BrowserCapture, BrowserCollaborationState, BrowserLiveFrame, BrowserSource,
} from './browserCollaboration'
export type {
  BrowserCapture, BrowserCollaborationState, BrowserLiveFrame, BrowserSource,
  BrowserSourceType,
} from './browserCollaboration'

// ---------- 类型 ----------

/** 治理记录自身状态：在管 / 已归档。发布状态见 pipelineStatus（影子流水线） */
export type StewardPipelineStatus = 'draft' | 'archived'

export interface StewardWorkflowSummary {
  node_count: number
  nodes: { name: string; type: string; disabled?: boolean }[]
  connections: Record<string, Record<string, { node: string; type: string; index: number }[][]>>
  has_trigger: boolean
  webhook_path: string | null
}

export interface StewardPipeline {
  id: string
  name: string
  description: string
  n8nWorkflowId: string
  status: StewardPipelineStatus
  pipelineId: string | null
  /** 发布状态（生命周期唯一真源=影子流水线）：发布/撤回只在流水线编辑向导 */
  pipelineStatus: 'draft' | 'published'
  conversationId: string | null
  summary: StewardWorkflowSummary
  createdAt: string | null
  updatedAt: string | null
  active?: boolean | null
}

export interface StewardPipelineDetail extends StewardPipeline {
  workflow?: Record<string, unknown> | null
  n8nError?: string
}

export interface StewardStatus {
  n8n: { configured: boolean; enabled: boolean; api_url: string; reachable?: boolean; error?: string }
  llmReady: boolean
  pipelineCounts: Record<string, number>
}

export interface StewardStep {
  tool: string
  arguments: Record<string, unknown>
  summary: string
  durationMs: number
  error?: string
  preview?: StewardTablePreview
  searchResults?: { title: string; url: string; snippet: string }[]
}

export interface StewardTablePreview {
  title: string
  node?: string
  executionId?: string
  columns: string[]
  rows: Record<string, unknown>[]
  totalRows: number
  shownRows: number
  totalColumns: number
  omittedColumns: number
  missingColumns: string[]
  redactedColumns: string[]
  truncated: boolean
}

export interface StewardMessageDTO {
  id: string
  role: 'user' | 'assistant'
  content: string
  steps: StewardStep[]
  touchedPipelineIds: string[]
  model?: string | null
  tokenUsage?: Record<string, unknown> | null
  createdAt: string
}

export interface StewardConversationDTO {
  id: string
  title: string
  browserSourceId?: string
  createdAt: string
  updatedAt: string
  messages?: StewardMessageDTO[]
}

export interface StewardConversationExport {
  format: 'openontology.data-steward.conversation'
  version: 1
  exportedAt: string
  conversation: StewardConversationDTO & {
    messageCount: number
    messages: StewardMessageDTO[]
  }
}

export interface StewardArtifact {
  id: string
  filename: string
  relativePath?: string
  source: 'upload' | 'download' | string
  sourceUrl?: string | null
  mimeType: string
  size: number
  sha256: string
  extractedChars: number
  extractError?: string | null
  urls: string[]
  createdAt: string
}

export interface StewardFilePreview {
  file: StewardArtifact
  content: string
  truncated: boolean
  previewable: boolean
}

export type StewardEvent =
  | { type: 'meta'; conversationId: string; model: string }
  | ({ type: 'step' } & StewardStep)
  | { type: 'answer'; content: string; touchedPipelineIds: string[]; usage?: unknown }
  | { type: 'error'; message: string }
  | { type: 'done' }

// ---------- REST ----------

export const stewardApi = {
  status: () => apiClientV2.get<StewardStatus>('/steward/status'),
  conversations: () => apiClientV2.get<StewardConversationDTO[]>('/steward/conversations'),
  conversation: (cid: string) =>
    apiClientV2.get<StewardConversationDTO>(`/steward/conversations/${cid}`),
  exportConversation: (cid: string) =>
    apiClientV2.get<StewardConversationExport>(`/steward/conversations/${cid}/export`),
  deleteConversation: (cid: string) =>
    apiClientV2.delete(`/steward/conversations/${cid}`),
  createConversation: (title = '新对话') =>
    apiClientV2.post<StewardConversationDTO>('/steward/conversations', { title }),
  files: (cid: string) =>
    apiClientV2.get<StewardArtifact[]>(`/steward/conversations/${cid}/files`),
  filePreview: (cid: string, artifactId: string) =>
    apiClientV2.get<StewardFilePreview>(`/steward/conversations/${cid}/files/${artifactId}/preview`),
  uploadFile: (cid: string, file: File) => {
    const form = new FormData()
    form.append('file', file)
    return apiClientV2.post<StewardArtifact>(`/steward/conversations/${cid}/files`, form)
  },
  deleteFile: (cid: string, artifactId: string) =>
    apiClientV2.delete(`/steward/conversations/${cid}/files/${artifactId}`),
  browserSources: () => apiClientV2.get<BrowserSource[]>('/steward/browser/sources'),
  createBrowserSource: (body: { name: string; sourceType: 'remote_cdp' | 'companion'; endpointUrl?: string; headers?: Record<string, string> }) =>
    apiClientV2.post<BrowserSource>('/steward/browser/sources', body),
  updateBrowserSource: (id: string, body: { name?: string; enabled?: boolean; endpointUrl?: string; headers?: Record<string, string> }) =>
    apiClientV2.patch<BrowserSource>(`/steward/browser/sources/${id}`, body),
  deleteBrowserSource: (id: string) => apiClientV2.delete(`/steward/browser/sources/${id}`),
  testBrowserSource: (id: string) =>
    apiClientV2.post<{ reachable: boolean; sourceType: string; label: string }>(`/steward/browser/sources/${id}/test`),
  bindBrowserSource: (cid: string, sourceId: string) =>
    apiClientV2.put<StewardConversationDTO>(`/steward/conversations/${cid}/browser/source`, { sourceId }),
  browserStart: (cid: string, url: string) =>
    apiClientV2.post<{ url: string; title: string }>(`/steward/conversations/${cid}/browser/start`, { url }),
  browserNavigate: (cid: string, url: string) =>
    apiClientV2.post<{ url: string; title: string }>(`/steward/conversations/${cid}/browser/navigate`, { url }),
  browserSession: (cid: string) =>
    apiClientV2.get<{ active: boolean; url: string; live: boolean; collaboration: BrowserCollaborationState }>(`/steward/conversations/${cid}/browser/session`),
  browserTicket: (cid: string) =>
    apiClientV2.post<{ ticket: string; expiresIn: number }>(`/steward/conversations/${cid}/browser/ticket`),
  browserLiveHttpAttach: (cid: string) =>
    apiClientV2.post<{ leaseId: string; expiresIn: number; frameIntervalMs: number; collaboration: BrowserCollaborationState }>(`/steward/conversations/${cid}/browser/live-http`),
  browserLiveHttpFrame: (cid: string, leaseId: string) =>
    apiClientV2.post<BrowserLiveFrame>(`/steward/conversations/${cid}/browser/live-http/frame`, { leaseId }),
  browserLiveHttpInput: (cid: string, leaseId: string, message: Record<string, unknown>) =>
    apiClientV2.post<{ accepted: boolean; collaboration: BrowserCollaborationState }>(`/steward/conversations/${cid}/browser/live-http/input`, { leaseId, message }),
  browserLiveHttpControl: (cid: string, leaseId: string, action: 'hold' | 'release') =>
    apiClientV2.post<{ collaboration: BrowserCollaborationState }>(`/steward/conversations/${cid}/browser/live-http/control`, { leaseId, action }),
  browserLiveHttpRelease: (cid: string, leaseId: string) =>
    apiClientV2.post<{ released: boolean }>(`/steward/conversations/${cid}/browser/live-http/release`, { leaseId }),
  browserCaptures: (cid: string, keyword = '') =>
    apiClientV2.get<BrowserCapture[]>(`/steward/conversations/${cid}/browser/captures`, { params: { keyword, limit: 100 } }),
  downloadCapture: (cid: string, captureId: string) =>
    apiClientV2.post<StewardArtifact>(`/steward/conversations/${cid}/browser/captures/${captureId}/download`),

  pipelines: () => apiClientV2.get<StewardPipeline[]>('/steward/pipelines'),
  pipeline: (id: string) => apiClientV2.get<StewardPipelineDetail>(`/steward/pipelines/${id}`),
  /** 列表页 / 数据管家「新建 n8n 流水线」：后台自动在 n8n 创建骨架工作流并登记（未发布） */
  bootstrap: (name: string, description = '') =>
    apiClientV2.post<{ record: StewardPipeline }>('/steward/pipelines/bootstrap', { name, description }),
}

export async function downloadBrowserCompanion(): Promise<void> {
  const token = localStorage.getItem('token') || ''
  const resp = await fetch(`${apiRoot()}/steward/browser/companion/script`, {
    headers: { Authorization: `Bearer ${token}` },
  })
  if (!resp.ok) throw new Error(`助手下载失败 (${resp.status})`)
  const blob = await resp.blob()
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = 'openontology-browser-companion.mjs'
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
  URL.revokeObjectURL(url)
}

function safeJsonFilenamePart(value: string): string {
  const withoutControlCharacters = Array.from(
    value.trim(),
    character => character.charCodeAt(0) < 32 ? '-' : character,
  ).join('')
  return withoutControlCharacters
    .replace(/[<>:"/\\|?*]/g, '-')
    .replace(/\s+/g, ' ')
    .replace(/[. ]+$/g, '')
    .slice(0, 80) || '数据管家会话'
}

export async function downloadStewardConversation(
  cid: string,
  title: string,
): Promise<StewardConversationExport> {
  const payload = await stewardApi.exportConversation(cid)
  const blob = new Blob(
    [`${JSON.stringify(payload, null, 2)}\n`],
    { type: 'application/json;charset=utf-8' },
  )
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${safeJsonFilenamePart(title)}-${cid.slice(0, 8)}.json`
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
  URL.revokeObjectURL(url)
  return payload
}

export async function downloadStewardFile(
  cid: string, artifactId?: string, filename = 'session-files.zip',
): Promise<void> {
  const token = localStorage.getItem('token') || ''
  const path = artifactId
    ? `/steward/conversations/${cid}/files/${artifactId}`
    : `/steward/conversations/${cid}/archive`
  const resp = await fetch(`${apiRoot()}${path}`, { headers: { Authorization: `Bearer ${token}` } })
  if (!resp.ok) throw new Error(`下载失败 (${resp.status})`)
  const blob = await resp.blob()
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  URL.revokeObjectURL(url)
}

export async function getStewardFileBlob(cid: string, artifactId: string): Promise<Blob> {
  const token = localStorage.getItem('token') || ''
  const resp = await fetch(`${apiRoot()}/steward/conversations/${cid}/files/${artifactId}`, {
    headers: { Authorization: `Bearer ${token}` },
  })
  if (!resp.ok) throw new Error(`文件预览加载失败 (${resp.status})`)
  return resp.blob()
}

// ---------- SSE 流式 chat ----------

function apiRoot(): string {
  const runtimeWindow = typeof window !== 'undefined'
    ? window as Window & { __API_BASE_URL__?: string }
    : undefined
  const runtimeBase = runtimeWindow?.__API_BASE_URL__ || ''
  return `${runtimeBase}/api/v2`
}

export async function streamStewardChat(
  body: {
    message: string
    conversationId?: string | null
    modelId?: string | null
    targetRecordId?: string | null
    webSearch?: boolean
  },
  onEvent: (e: StewardEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const token = localStorage.getItem('token') || ''
  const resp = await fetch(`${apiRoot()}/steward/chat`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
    body: JSON.stringify({
      message: body.message,
      conversationId: body.conversationId || undefined,
      modelId: body.modelId || undefined,
      targetRecordId: body.targetRecordId || undefined,
      webSearch: body.webSearch || undefined,
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
    let idx: number
    while ((idx = buffer.indexOf('\n\n')) >= 0) {
      const chunk = buffer.slice(0, idx)
      buffer = buffer.slice(idx + 2)
      for (const line of chunk.split('\n')) {
        if (!line.startsWith('data:')) continue
        try {
          onEvent(JSON.parse(line.slice(5).trim()) as StewardEvent)
        } catch { /* 忽略无法解析的行 */ }
      }
    }
  }
}
