import { apiClient } from './client'
import { safeDownloadName } from './ontologies'

export interface SentinelBinding {
  alias: string
  objectTypeId: string
  filter?: string | null
}

export interface SentinelLink {
  from: string
  linkTypeId: string
  to: string
}

export type SentinelParameterBinding =
  | string
  | number
  | boolean
  | null
  | SentinelParameterBinding[]
  | { [key: string]: SentinelParameterBinding }

export interface SentinelPatternStage {
  alias: string
  objectTypeId: string
  filter?: string | null
  within?: number | null
}

export interface SentinelPatternAggregate {
  property: string
  function: 'count' | 'avg' | 'sum' | 'min' | 'max'
  window: number
  threshold: number
  comparison?: 'gte' | 'gt' | 'lte' | 'lt'
}

/** CEP 事件模式（triggerMode='on_pattern' 时必填）。 */
export interface SentinelPattern {
  stages: SentinelPatternStage[]
  within?: number
  absence?: { enabled: boolean }
  aggregate?: SentinelPatternAggregate
  condition?: string | null
}

export interface Sentinel {
  id: string
  ontologyId: string
  name: string
  displayName: string
  description?: string
  bindings: SentinelBinding[]
  links: SentinelLink[]
  condition?: string
  pattern?: SentinelPattern | null
  conditionRows?: any[]
  conditionLogic?: string
  primaryAlias?: string
  actionIds: string[]
  actionParameters?: Record<string, Record<string, SentinelParameterBinding>>
  onChange: boolean
  onSchedule: boolean
  scanIntervalSeconds: number
  lastScannedAt?: string
  triggerMode?: 'on_enter' | 'on_enter_leave' | 'run_on_all' | 'on_pattern'
  muted: boolean
  enabled: boolean
  releaseId?: string | null
  enableGeneration?: number
  origin?: 'release_builtin' | 'assistant_dynamic'
  status: string
  createdAt?: string
  updatedAt?: string
}

export interface SentinelFiring {
  id: string
  sentinelId: string
  sentinelName: string
  triggerSource: string
  status: string
  matchCount: number
  matches: Record<string, string>[]
  entered: string[]
  left: string[]
  actionResults: any[]
  error?: string
  durationMs?: number
  ontologyVersion?: string | null
  ontologyReleaseId?: string | null
  createdAt?: string
}

export interface SentinelCdcStatus {
  ontology_id: string
  ontology_release_id?: string | null
  scope?: 'current_release' | 'release' | 'history'
  healthy: boolean
  quiescent: boolean
  worker_alive: boolean
  queued: number
  max_queue_size: number
  max_cascade_depth: number
  last_error?: string | null
  durable: Record<string, number>
  last_errors: Array<{
    eventId?: string
    chainId?: string
    ontologyId?: string
    ontologyReleaseId?: string | null
    status?: string
    cascadeDepth?: number
    attempts?: number
    error?: string | null
  }>
  dead_letters: Array<{
    eventId?: string
    chainId?: string
    ontologyId?: string
    ontologyReleaseId?: string | null
    status?: string
    cascadeDepth?: number
    attempts?: number
    error?: string | null
  }>
}

export interface SentinelRunResult {
  evaluated: number
  fired: number
  errors: number
  no_change: number
  no_match: number
  pending: number
  muted: number
  runtimeErrors: Array<Record<string, unknown>>
  firings: Array<{
    id?: string | null
    sentinelId: string
    sentinelName: string
    status: string
    matchCount: number
    entered: string[]
    left: string[]
    actionResults: any[]
    error?: string | null
  }>
}

const base = (ontologyId: string) => `/ontologies/${ontologyId}/sentinels`
const releaseQuery = (releaseId?: string | null) => releaseId
  ? `?release_id=${encodeURIComponent(releaseId)}`
  : ''

export const sentinelApi = {
  list: (ontologyId: string, releaseId?: string | null) =>
    apiClient.get<Sentinel[]>(`${base(ontologyId)}/${releaseQuery(releaseId)}`),
  create: (ontologyId: string, body: Partial<Sentinel>) =>
    apiClient.post<Sentinel>(`${base(ontologyId)}/`, body),
  update: (ontologyId: string, id: string, body: Partial<Sentinel>) =>
    apiClient.put<Sentinel>(`${base(ontologyId)}/${id}`, body),
  remove: (ontologyId: string, id: string) =>
    apiClient.delete(`${base(ontologyId)}/${id}`),
  toggle: (ontologyId: string, id: string) =>
    apiClient.post<{ enabled: boolean }>(`${base(ontologyId)}/${id}/toggle`),
  updateOperationalState: (
    ontologyId: string,
    id: string,
    body: {
      enabled?: boolean
      muted?: boolean
      expectedReleaseId: string
      expectedGeneration: number
    },
  ) => apiClient.patch<Sentinel>(
    `${base(ontologyId)}/${id}/operational-state`,
    body,
  ),
  run: (ontologyId: string) =>
    apiClient.post<SentinelRunResult>(`${base(ontologyId)}/run`),
  firings: (ontologyId: string, releaseId?: string | null) =>
    apiClient.get<SentinelFiring[]>(`${base(ontologyId)}/firings${releaseQuery(releaseId)}`),
  notifications: (ontologyId: string) =>
    apiClient.get<SentinelNotification[]>(`${base(ontologyId)}/notifications`),
  cdcStatus: (ontologyId: string) =>
    apiClient.get<SentinelCdcStatus>(`${base(ontologyId)}/cdc-status`),
  // 导出标准 Skill zip（must use authenticated request — plain links omit Bearer token）
  exportSkill: async (ontologyId: string, sentinelId: string, downloadName: string) => {
    const blob = (await apiClient.get(
      `${base(ontologyId)}/${sentinelId}/export-skill`,
      { responseType: 'blob' },
    )) as unknown as Blob
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `${safeDownloadName(downloadName)}.zip`
    a.style.display = 'none'
    document.body.appendChild(a)
    a.click()
    a.remove()
    window.setTimeout(() => URL.revokeObjectURL(url), 0)
  },
}

export interface SentinelNotification {
  id: string
  channel: string
  recipient: string
  subject?: string
  body?: string
  relatedObjectId?: string
  actionId?: string
  ontologyReleaseId?: string | null
  sentinelId?: string | null
  actionLogId?: string | null
  status: string
  createdAt?: string
}
