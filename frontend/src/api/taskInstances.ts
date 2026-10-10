/**
 * 任务实例 API — /api/v2/task-instances
 *
 * 独立运行时功能域：DAG 模板（YAML WorkflowSpec）与任务实例的
 * 查看/激活/审批/交活/打回/插话。字段命名对齐后端 snake_case，
 * apiClientV2 已解包 {data} 信封；SSE 走 fetch 流（复用 Bearer 鉴权）。
 */
import { apiClientV2 } from './client'

// ---------- 类型 ----------

export interface TemplateSummary {
  id: string
  name: string
  description: string
  latest_revision_no: number | null
  latest_revision_id: string | null
  canonical_hash: string | null
  instance_count: number
  created_at: string | null
  updated_at: string | null
}

export interface TemplateDetail extends TemplateSummary {
  spec_yaml?: string
  /** 编译归一化产物（编排页只读 DAG 预览数据源） */
  spec_compiled?: WorkflowSpecSnapshot
}

export interface TemplateRevision {
  id: string
  revision_no: number
  canonical_hash: string
  note: string | null
  created_by: string | null
  created_at: string | null
  /** 版本原文（回滚载入与 diff 对比用；模板体量小随列表内联） */
  spec_yaml?: string
}

export interface WorkflowPort {
  contract?: string | null
  description?: string | null
  required_artifacts?: string[] | null
}

export interface WorkflowSpecSnapshot {
  api_version: string
  kind: string
  metadata?: { name?: string; description?: string | null }
  contracts?: Record<string, unknown>
  nodes?: Record<string, { kind: string; [key: string]: unknown }>
  edges?: Array<{ from: string; to: string; rework?: boolean }>
  policies?: Record<string, number>
}

export interface NodeRunView {
  node_run_id: string
  node_id: string
  attempt_no: number
  status: string
  waiting: boolean
  error: string | null
  correction_count: number
  rework_count: number
  output: Record<string, unknown> | null
  dispatched_at: string | null
  started_at: string | null
  finished_at: string | null
  created_at: string | null
}

export interface ApprovalView {
  id: string
  node_run_id: string
  status: string
  reason: string | null
  proposal: Record<string, unknown> | null
  expires_at: string | null
  decided_by: string | null
  decided_at: string | null
}

export interface ArtifactView {
  id: string
  node_run_id: string
  name: string
  mime_type: string
  size_bytes: number
  sha256: string
  created_at: string | null
}

export interface InstanceDetail {
  id: string
  name: string
  goal: string
  status: string
  needs_attention: boolean
  template_revision_id: string
  created_by: string | null
  created_at: string | null
  finished_at: string | null
  fail_reason: string | null
  cancel_reason: string | null
  nodes: NodeRunView[]
  approvals: ApprovalView[]
  artifacts: ArtifactView[]
  spec_snapshot: WorkflowSpecSnapshot | null
}

export type InstanceListItem = Omit<
  InstanceDetail, 'approvals' | 'artifacts' | 'spec_snapshot'>

export interface InstanceListResponse {
  total: number
  page: number
  size: number
  items: InstanceListItem[]
}

export interface InstanceEvent {
  event: string
  id?: string
  data: Record<string, unknown>
}

// ---------- 封装 ----------

export const taskInstancesApi = {
  listTemplates: (keyword = '') =>
    apiClientV2.get<TemplateSummary[]>('/task-instances/templates', {
      params: { keyword },
    }),
  getTemplate: (id: string) =>
    apiClientV2.get<TemplateDetail>(`/task-instances/templates/${id}`),
  createTemplate: (spec_yaml: string) =>
    apiClientV2.post<TemplateDetail>('/task-instances/templates', { spec_yaml }),
  updateTemplate: (id: string, spec_yaml: string, note?: string) =>
    apiClientV2.put<TemplateDetail>(`/task-instances/templates/${id}`, {
      spec_yaml, note: note ?? null,
    }),
  deleteTemplate: (id: string) =>
    apiClientV2.delete<{ status: string }>(`/task-instances/templates/${id}`),
  validateTemplate: (spec_yaml: string) =>
    apiClientV2.post<{ valid: boolean }>('/task-instances/templates/validate', {
      spec_yaml,
    }),
  listRevisions: (id: string) =>
    apiClientV2.get<TemplateRevision[]>(`/task-instances/templates/${id}/revisions`),
  activateInstance: (templateId: string, body: {
    name: string; goal: string; inputs?: Record<string, unknown>; revision_no?: number
  }, idempotencyKey: string) =>
    apiClientV2.post<InstanceDetail>(
      `/task-instances/templates/${templateId}/instances`, body, {
        headers: { 'Idempotency-Key': idempotencyKey },
      }),
  listInstances: (params: {
    status?: string; template_id?: string; mine?: boolean; page?: number; size?: number
  }) =>
    apiClientV2.get<InstanceListResponse>('/task-instances/instances', { params }),
  getInstance: (id: string) =>
    apiClientV2.get<InstanceDetail>(`/task-instances/instances/${id}`),
  cancelInstance: (id: string, reason: string) =>
    apiClientV2.post<InstanceDetail>(`/task-instances/instances/${id}/cancel`, {
      reason,
    }),
  decideApproval: (instanceId: string, nodeId: string, approvalId: string,
    decision: 'approved' | 'rejected', reason?: string) =>
    apiClientV2.post<InstanceDetail>(
      `/task-instances/instances/${instanceId}/nodes/${nodeId}/approvals/${approvalId}/decision`,
      { decision, reason: reason ?? null }),
  humanSubmit: (instanceId: string, nodeId: string, output: Record<string, unknown>) =>
    apiClientV2.post<InstanceDetail>(
      `/task-instances/instances/${instanceId}/nodes/${nodeId}/human/submit`,
      { output }),
  humanReject: (instanceId: string, nodeId: string, reason: string,
    target_node_id?: string) =>
    apiClientV2.post<InstanceDetail>(
      `/task-instances/instances/${instanceId}/nodes/${nodeId}/reject`, {
        reason, target_node_id: target_node_id ?? null,
      }),
  submitSteering: (instanceId: string, nodeId: string, content: string,
    idempotencyKey: string) =>
    apiClientV2.post<{ id: string; status: string }>(
      `/task-instances/instances/${instanceId}/steering`, { node_id: nodeId, content }, {
        headers: { 'Idempotency-Key': idempotencyKey },
      }),
}

/** 提取后端 detail 信封中的可读错误（detail 可为 string 或 {code,message}）。 */
export function apiError(error: unknown): string {
  if (!error || typeof error !== 'object') return '请求失败，请稍后重试'
  const candidate = error as {
    detail?: unknown; message?: unknown; error?: unknown
  }
  if (typeof candidate.detail === 'string') return candidate.detail
  if (candidate.detail && typeof candidate.detail === 'object') {
    const detail = candidate.detail as { message?: unknown }
    if (typeof detail.message === 'string') return detail.message
  }
  if (typeof candidate.message === 'string') return candidate.message
  if (typeof candidate.error === 'string') return candidate.error
  return '请求失败，请稍后重试'
}

function runtimeApiBase(): string {
  const injected = (window as unknown as { __API_BASE_URL__?: string }).__API_BASE_URL__
  return injected || '/api/v2'
}

/** 消费任务实例 SSE 事件流（fetch + Bearer；终态后服务端断流）。 */
export const streamInstanceEvents = async (
  instanceId: string,
  onEvent: (event: InstanceEvent) => void,
  options: { afterSeq?: number; lastEventId?: string; signal?: AbortSignal } = {},
) => {
  const token = localStorage.getItem('token')
  const params = options.afterSeq === undefined
    ? '' : `?after_seq=${encodeURIComponent(options.afterSeq)}`
  const response = await fetch(
    `${runtimeApiBase()}/task-instances/instances/${encodeURIComponent(instanceId)}/events${params}`,
    {
      headers: {
        Accept: 'text/event-stream',
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
        ...(options.lastEventId ? { 'Last-Event-ID': options.lastEventId } : {}),
      },
      signal: options.signal,
    },
  )
  if (!response.ok) {
    const error = new Error(`实例事件请求失败 (${response.status})`) as Error & { status?: number }
    error.status = response.status
    throw error
  }
  if (!response.body) throw new Error('浏览器未提供流式响应体')
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  const dispatch = (block: string) => {
    let event = 'message'
    let id: string | undefined
    const dataLines: string[] = []
    for (const line of block.split('\n')) {
      if (line.startsWith('id:')) id = line.slice(3).trim()
      else if (line.startsWith('event:')) event = line.slice(6).trim()
      else if (line.startsWith('data:')) dataLines.push(line.slice(5).trimStart())
    }
    if (!dataLines.length) return
    try {
      onEvent({ event, id, data: JSON.parse(dataLines.join('\n')) })
    } catch {
      onEvent({ event: 'error', id, data: { message: '无法解析实例事件' } })
    }
  }
  while (true) {
    const { value, done } = await reader.read()
    buffer += decoder.decode(value, { stream: !done }).replace(/\r\n/g, '\n')
    let split = buffer.indexOf('\n\n')
    while (split >= 0) {
      dispatch(buffer.slice(0, split))
      buffer = buffer.slice(split + 2)
      split = buffer.indexOf('\n\n')
    }
    if (done) break
  }
  if (buffer.trim()) dispatch(buffer)
}
