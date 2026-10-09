import { apiClientV2 } from './client'
import { absoluteFileUrl } from './fileAssets'

export type NotificationPriority = 'urgent' | 'high' | 'normal' | 'low'
export type NotificationSourceType = 'manual' | 'internal' | 'ingest'
export type NotificationTab = 'all' | 'unread' | 'starred' | 'archived'

export interface NotificationAttachment {
  id: string
  filename: string
  fileSize: number
  mimeType: string | null
  sha256: string | null
  createdAt: string
}

export interface NotificationMessage {
  id: string
  eventId: string | null
  sourceSystem: string
  sourceType: NotificationSourceType
  title: string
  priority: NotificationPriority
  isRead: boolean
  isStarred: boolean
  isArchived: boolean
  readAt: string | null
  starredAt: string | null
  archivedAt: string | null
  createdAt: string
  updatedAt: string
  /** 列表条目仅携带预览；正文全文在详情接口返回 */
  bodyPreview?: string
  attachmentCount?: number
  body?: string
  attachments?: NotificationAttachment[]
}

export interface NotificationSummary {
  unreadCount: number
  starredCount: number
  archivedCount: number
  totalCount: number
}

export interface NotificationPageResult {
  items: NotificationMessage[]
  nextCursor: string | null
  hasMore: boolean
}

export interface NotificationStateFields {
  isRead?: boolean
  isStarred?: boolean
  isArchived?: boolean
}

export interface NotificationChannel {
  id: string
  name: string
  urlMasked: string
  enabled: boolean
  note: string | null
  lastStatus: 'sent' | 'failed' | null
  lastError: string
  lastSentAt: string | null
  createdAt: string
  updatedAt: string
}

export interface NotificationIngestKey {
  id: string
  name: string
  keyPrefix: string
  enabled: boolean
  allowedSourceSystem: string | null
  createdAt: string
  lastUsedAt: string | null
  revokedAt: string | null
  /** 明文仅在签发响应中一次性返回 */
  plaintextKey?: string
}

export const NOTIFICATION_TABS: { key: NotificationTab; label: string }[] = [
  { key: 'all', label: '全部' },
  { key: 'unread', label: '未读' },
  { key: 'starred', label: '已标记' },
  { key: 'archived', label: '已归档' },
]

export const notificationsApi = {
  summary: (): Promise<NotificationSummary> =>
    apiClientV2.get('/notifications/summary'),

  list: (params: {
    tab?: NotificationTab
    cursor?: string | null
    limit?: number
  } = {}): Promise<NotificationPageResult> =>
    apiClientV2.get('/notifications', { params }),

  get: (id: string): Promise<NotificationMessage> =>
    apiClientV2.get(`/notifications/${id}`),

  /** 管理员手动发送（正式消息：入站内并触发渠道转发） */
  create: (payload: {
    title: string
    body?: string
    priority?: NotificationPriority
  }): Promise<NotificationMessage> =>
    apiClientV2.post('/notifications', payload),

  updateState: (
    id: string,
    fields: NotificationStateFields,
  ): Promise<NotificationMessage> =>
    apiClientV2.patch(`/notifications/${id}`, fields),

  readAll: (): Promise<{ updated: number }> =>
    apiClientV2.post('/notifications/read-all'),

  remove: (id: string): Promise<{ deleted: string }> =>
    apiClientV2.delete(`/notifications/${id}`),

  channels: {
    list: (): Promise<NotificationChannel[]> =>
      apiClientV2.get('/notifications/channels'),

    create: (payload: { name: string; appriseUrl: string; note?: string | null }): Promise<NotificationChannel> =>
      apiClientV2.post('/notifications/channels', payload),

    update: (
      id: string,
      fields: { name?: string; appriseUrl?: string; note?: string | null; enabled?: boolean },
    ): Promise<NotificationChannel> =>
      apiClientV2.patch(`/notifications/channels/${id}`, fields),

    remove: (id: string): Promise<{ deleted: string }> =>
      apiClientV2.delete(`/notifications/channels/${id}`),

    test: (id: string): Promise<{ ok: boolean; message: string }> =>
      apiClientV2.post(`/notifications/channels/${id}/test`),
  },

  ingestKeys: {
    list: (): Promise<NotificationIngestKey[]> =>
      apiClientV2.get('/notifications/ingest-keys'),

    create: (payload: { name: string; allowedSourceSystem?: string | null }): Promise<NotificationIngestKey> =>
      apiClientV2.post('/notifications/ingest-keys', payload),

    revoke: (id: string): Promise<NotificationIngestKey> =>
      apiClientV2.delete(`/notifications/ingest-keys/${id}`),
  },

  /** 附件下载地址（需携带登录态请求；正文内嵌媒体同源引用此路径） */
  attachmentUrl: (messageId: string, attachmentId: string): string =>
    absoluteFileUrl(`/api/v2/notifications/${messageId}/attachments/${attachmentId}/download`),
}
