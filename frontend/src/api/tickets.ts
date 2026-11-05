/**
 * 工单 API — /api/v2/tickets
 *
 * 全角色可用的平台使用反馈通道：提交工单（自动「待处理」）+ 附件，
 * 管理员在四态流水线（待处理/分析中/已完成/已取消）上处理并留下必填评论。
 * 历史存量工单可能仍返回已下线的 verifying/accepted 取值，展示侧需兜底。
 * 后端对外统一 camelCase，apiClientV2 已自动解包 {data:...} 信封。
 */
import { apiClientV2 } from './client'

// ---------- 类型 ----------

export type TicketStatus = 'pending' | 'analyzing' | 'completed' | 'cancelled'

export type TicketCategory = 'system_fault' | 'experience' | 'feature' | 'other'

export interface TicketAttachment {
  id: string
  ticketId: string
  filename: string
  fileSize: number
  mimeType: string | null
  sha256: string | null
  uploadedBy: string | null
  createdAt: string
}

export interface TicketProgressLog {
  id: string
  seq: number
  fromStatus: TicketStatus | null
  toStatus: TicketStatus
  comment: string
  actorId: string | null
  actorName: string | null
  createdAt: string
}

export interface TicketItem {
  id: string
  ticketNo: string
  title: string
  content: string
  status: TicketStatus
  category: TicketCategory
  pageUrl: string | null
  submitterId: string | null
  submitterName: string | null
  createdAt: string
  updatedAt: string
  attachmentCount?: number
  attachments?: TicketAttachment[]
  progressLogs?: TicketProgressLog[]
}

export interface TicketListResp {
  items: TicketItem[]
  total: number
  page: number
  pageSize: number
}

export interface TicketStats {
  total: number
  byStatus: Record<TicketStatus, number>
}

export interface TicketCreateBody {
  title: string
  content: string
  category?: TicketCategory
  pageUrl?: string | null
}

export interface TicketListParams {
  q?: string
  /** 单状态，或逗号分隔多状态（顶栏弹窗取处理中：pending,analyzing） */
  status?: string
  page?: number
  pageSize?: number
}

// ---------- 客户端 ----------

function saveBlob(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename || 'attachment'
  document.body.appendChild(a)
  a.click()
  a.remove()
  URL.revokeObjectURL(url)
}

export const ticketsApi = {
  list: (params: TicketListParams = {}): Promise<TicketListResp> =>
    apiClientV2.get('/tickets', {
      params: {
        q: params.q,
        status: params.status,
        page: params.page,
        page_size: params.pageSize,
      },
    }),

  get: (id: string): Promise<TicketItem> =>
    apiClientV2.get(`/tickets/${id}`),

  create: (body: TicketCreateBody): Promise<TicketItem> =>
    apiClientV2.post('/tickets', body),

  updateProgress: (id: string, status: TicketStatus, comment: string): Promise<TicketItem> =>
    apiClientV2.post(`/tickets/${id}/progress`, { status, comment }),

  stats: (): Promise<TicketStats> =>
    apiClientV2.get('/tickets/stats/summary'),

  uploadAttachment: (id: string, file: File): Promise<TicketAttachment> => {
    const fd = new FormData()
    fd.append('file', file)
    return apiClientV2.post(`/tickets/${id}/attachments`, fd)
  },

  downloadAttachment: (ticketId: string, att: TicketAttachment): Promise<void> =>
    apiClientV2
      .get(`/tickets/${ticketId}/attachments/${att.id}/download`, { responseType: 'blob' })
      .then((blob: Blob) => saveBlob(blob, att.filename)),

  fetchAttachmentBlob: (ticketId: string, att: TicketAttachment): Promise<Blob> =>
    apiClientV2.get(`/tickets/${ticketId}/attachments/${att.id}/download`, { responseType: 'blob' }),
}

// ---------- 展示辅助 ----------

export interface TicketStatusMeta { label: string; cls: string; dot: string }

export const TICKET_STATUS_META: Record<TicketStatus, TicketStatusMeta> = {
  pending: { label: '待处理', cls: 'bg-amber-50 text-amber-700', dot: 'bg-amber-500' },
  analyzing: { label: '分析中', cls: 'bg-blue-50 text-blue-700', dot: 'bg-blue-500' },
  completed: { label: '已完成', cls: 'bg-slate-100 text-slate-600', dot: 'bg-slate-400' },
  cancelled: { label: '已取消', cls: 'bg-slate-100 text-slate-500', dot: 'bg-slate-300' },
}

/** 未下线的合法状态；历史存量行可能带 verifying/accepted 等旧值。 */
export function isKnownTicketStatus(status: string): status is TicketStatus {
  return status in TICKET_STATUS_META
}

/** 中性兜底：存量旧状态原样展示，不冒充任何在役状态。 */
const UNKNOWN_STATUS_META: TicketStatusMeta = {
  label: '', cls: 'bg-slate-100 text-slate-500', dot: 'bg-slate-300',
}

export function ticketStatusMeta(status: string): TicketStatusMeta {
  return isKnownTicketStatus(status)
    ? TICKET_STATUS_META[status]
    : { ...UNKNOWN_STATUS_META, label: status }
}

export const TICKET_STATUS_ORDER: TicketStatus[] = [
  'pending', 'analyzing', 'completed', 'cancelled',
]

export const TICKET_CATEGORY_META: Record<TicketCategory, { label: string }> = {
  system_fault: { label: '系统故障' },
  experience: { label: '体验优化' },
  feature: { label: '新增功能' },
  other: { label: '其他' },
}

export const TICKET_CATEGORY_ORDER: TicketCategory[] = [
  'system_fault', 'experience', 'feature', 'other',
]

export function formatBytes(n: number): string {
  if (!n) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB']
  const i = Math.floor(Math.log(n) / Math.log(1024))
  return `${(n / Math.pow(1024, i)).toFixed(i ? 1 : 0)} ${units[i]}`
}

export function fmtTime(iso: string | null | undefined): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return '—'
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}/${pad(d.getMonth() + 1)}/${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}
