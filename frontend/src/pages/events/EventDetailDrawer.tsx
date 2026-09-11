import { useState } from 'react'
import { createPortal } from 'react-dom'
import { useQuery } from '@tanstack/react-query'
import {
  Check, Copy, Download, Loader2, Paperclip, Pencil, RefreshCw, ScrollText, X,
} from 'lucide-react'
import {
  AUDIT_ACTION_LABEL, eventsApi, formatBytes, type AuditEntry, type EventItem,
} from '@/api/events'
import { ontologyApi } from '@/api/ontologies'
import { toast } from 'sonner'
import { writeTextToClipboard } from '@/utils/clipboard'
import { fmt, SeverityBadge } from './shared'

// 审计 changes 的字段名 → 中文（后端存的是数据库字段名）
const FIELD_LABEL: Record<string, string> = {
  title: '标题',
  description: '描述',
  event_type: '事件类型',
  severity: '严重程度',
  status: '状态',
  occurred_at: '发生时间',
  ontology_id: '关联本体',
  tags: '标签',
  payload: '载荷',
  subject_ref: '业务对象',
}
const VALUE_LABEL: Record<string, string> = {
  critical: '严重', high: '高级', medium: '中级', low: '低级', info: '信息',
  active: '活跃', archived: '归档',
}

function fmtAuditValue(field: string, value: unknown): string {
  if (value === null || value === undefined || value === '') return '空'
  if (typeof value === 'string') {
    if (VALUE_LABEL[value]) return VALUE_LABEL[value]
    if (field === 'occurred_at') return fmt(value)
    return value
  }
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}

function CopyBtn({ text }: { text: string }) {
  const [done, setDone] = useState(false)
  return (
    <button
      type="button"
      onClick={() => {
        void writeTextToClipboard(text).then(() => {
          setDone(true)
          window.setTimeout(() => setDone(false), 1500)
        }).catch(() => setDone(false))
      }}
      className="inline-flex items-center gap-1 rounded-md text-xs text-[var(--color-success)] transition-colors hover:text-[var(--color-success)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
    >
      {done ? <Check size={13} /> : <Copy size={13} />}{done ? '已复制' : '复制'}
    </button>
  )
}

function MetaItem({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="min-w-0">
      <dt className="text-xs text-[var(--color-text-tertiary)]">{label}</dt>
      <dd className="mt-0.5 truncate text-sm text-foreground">{children}</dd>
    </div>
  )
}

function AuditTimeline({ entries }: { entries: AuditEntry[] }) {
  const ordered = [...entries].sort((a, b) => b.seq - a.seq)
  return (
    <ul className="space-y-0">
      {ordered.map(entry => {
        const changes = entry.changes ? Object.entries(entry.changes) : []
        return (
          <li key={entry.id} className="relative ml-1 border-l border-border pb-4 pl-4 last:pb-0">
            <span className="absolute -left-[5px] top-1.5 h-2.5 w-2.5 rounded-full bg-[var(--color-success-bg)] ring-2 ring-white" aria-hidden="true" />
            <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5 text-sm">
              <span className="font-medium text-foreground">{AUDIT_ACTION_LABEL[entry.action] ?? entry.action}</span>
              <span className="text-xs text-[var(--color-text-tertiary)]">
                {entry.actorName || entry.actorType} · {fmt(entry.createdAt)}
              </span>
            </div>
            {entry.note && <p className="mt-0.5 text-xs text-[var(--color-text-tertiary)]">{entry.note}</p>}
            {changes.length > 0 && (
              <div className="mt-1 space-y-0.5">
                {changes.map(([field, change]) => (
                  <p key={field} className="text-xs tabular-nums text-muted-foreground">
                    {FIELD_LABEL[field] ?? field}：{fmtAuditValue(field, change?.from)}
                    <span className="mx-1 text-[var(--color-text-tertiary)]">→</span>
                    {fmtAuditValue(field, change?.to)}
                  </p>
                ))}
              </div>
            )}
          </li>
        )
      })}
    </ul>
  )
}

export default function EventDetailDrawer({
  open,
  eventId,
  onClose,
  onEdit,
}: {
  open: boolean
  eventId: string | null
  onClose: () => void
  onEdit: (event: EventItem) => void
}) {
  const [downloadingId, setDownloadingId] = useState<string | null>(null)
  const detailQuery = useQuery({
    queryKey: ['events', 'detail', eventId],
    queryFn: () => eventsApi.get(eventId!),
    enabled: open && Boolean(eventId),
  })
  const { data: ontologyList } = useQuery({
    queryKey: ['events-ontology-options'],
    queryFn: () => ontologyApi.list({ page_size: 100 }),
    enabled: open,
  })
  const event = detailQuery.data
  const attachments = event?.attachments ?? []
  const auditTrail = event?.auditTrail ?? []
  const ontologyName = event?.ontologyId
    ? ontologyList?.items?.find(o => o.id === event.ontologyId)?.name
    : null
  const hasPayload = Boolean(event?.payload && Object.keys(event.payload).length > 0)

  if (!open) return null

  const download = async (attachmentId: string) => {
    if (!eventId) return
    const attachment = attachments.find(item => item.id === attachmentId)
    if (!attachment) return
    setDownloadingId(attachmentId)
    try {
      await eventsApi.downloadAttachment(eventId, attachment)
    } catch (cause: any) {
      toast.error('附件下载失败', { description: cause?.detail || cause?.message || '请稍后重试' })
    } finally {
      setDownloadingId(null)
    }
  }

  return createPortal(
    <div className="fixed inset-0 z-[80] flex justify-end">
      <div className="absolute inset-0 bg-[var(--color-bg-overlay)]" onClick={onClose} />
      <aside className="anim-drawer-in relative flex w-full max-w-2xl flex-col border-l border-border bg-card shadow-xl">
        <header className="flex shrink-0 items-start justify-between gap-3 border-b border-border px-6 py-4">
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <h2 className="truncate text-lg font-semibold text-foreground" title={event?.title}>
                {event?.title ?? '事件详情'}
              </h2>
              {event && <SeverityBadge sev={event.severity} />}
              {event?.status === 'archived' && (
                <span className="shrink-0 rounded bg-muted px-1.5 py-0.5 text-[10px] font-medium text-muted-foreground">已归档</span>
              )}
            </div>
            {event && (
              <div className="mt-1 flex items-center gap-2 text-xs text-[var(--color-text-tertiary)]">
                <span className="font-mono text-muted-foreground">{event.eventNo}</span>
                <CopyBtn text={event.eventNo} />
                {event.eventType && (
                  <>
                    <span className="h-3 w-px bg-[var(--color-bg-active)]" aria-hidden="true" />
                    <span>{event.eventType}</span>
                  </>
                )}
              </div>
            )}
          </div>
          <div className="flex shrink-0 items-center gap-1">
            {event && (
              <button
                type="button"
                onClick={() => onEdit(event)}
                className="inline-flex h-9 items-center gap-1.5 rounded-lg border border-border bg-card px-3 text-sm font-medium text-muted-foreground transition-colors hover:bg-muted hover:text-[var(--color-success)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                <Pencil size={14} /> 编辑
              </button>
            )}
            <button
              type="button"
              onClick={onClose}
              className="flex h-9 w-9 items-center justify-center rounded-lg text-[var(--color-text-tertiary)] transition-colors hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              aria-label="关闭事件详情"
            >
              <X size={20} />
            </button>
          </div>
        </header>

        <div className="flex-1 overflow-y-auto px-6 py-5">
          {detailQuery.isLoading ? (
            <div className="space-y-3" aria-label="正在加载事件详情">
              {[0, 1, 2].map(index => (
                <div key={index} className="animate-pulse space-y-2 rounded-xl border border-border p-4">
                  <span className="block h-3 w-1/3 rounded bg-muted" />
                  <span className="block h-2.5 w-2/3 rounded bg-muted" />
                </div>
              ))}
            </div>
          ) : detailQuery.isError || !event ? (
            <div className="flex flex-col items-center px-6 py-16 text-center">
              <p className="text-sm font-medium text-[var(--color-danger)]">事件详情加载失败</p>
              <p className="mt-1 text-xs text-[var(--color-text-tertiary)]">请检查网络连接后重试</p>
              <button
                type="button"
                onClick={() => void detailQuery.refetch()}
                className="mt-4 inline-flex h-9 items-center gap-1.5 rounded-lg border border-border px-3 text-sm font-medium text-muted-foreground transition-colors hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                <RefreshCw size={14} /> 重新加载
              </button>
            </div>
          ) : (
            <div className="space-y-6">
              <dl className="grid grid-cols-2 gap-x-4 gap-y-3">
                <MetaItem label="发生时间">{fmt(event.occurredAt)}</MetaItem>
                <MetaItem label="登记时间">{fmt(event.recordedAt)}</MetaItem>
                <MetaItem label="来源">{event.sourceLabel || '—'}</MetaItem>
                {event.reporterName && <MetaItem label="上报人">{event.reporterName}</MetaItem>}
                {ontologyName && <MetaItem label="关联本体">{ontologyName}</MetaItem>}
                {event.sourceSystem && <MetaItem label="来源系统">{event.sourceSystem}</MetaItem>}
                {event.sourceRef && (
                  <MetaItem label="外部单号"><span className="font-mono text-xs">{event.sourceRef}</span></MetaItem>
                )}
                {event.confidence !== null && event.confidence !== undefined && (
                  <MetaItem label="机器置信度">{event.confidence}</MetaItem>
                )}
                {event.clientIp && <MetaItem label="客户端 IP">{event.clientIp}</MetaItem>}
                {event.tags.length > 0 && (
                  <MetaItem label="标签">
                    <span className="flex flex-wrap gap-1">
                      {event.tags.map(tag => (
                        <span key={tag} className="rounded bg-muted px-1.5 py-0.5 text-xs text-muted-foreground">{tag}</span>
                      ))}
                    </span>
                  </MetaItem>
                )}
              </dl>

              <section>
                <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">详细描述</h3>
                <p className="whitespace-pre-wrap rounded-xl bg-muted px-4 py-3 text-sm leading-6 text-foreground">
                  {event.description || <span className="italic text-[var(--color-text-tertiary)]">无描述</span>}
                </p>
              </section>

              {hasPayload && (
                <section>
                  <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">机器载荷（payload）</h3>
                  <pre className="max-h-56 overflow-auto rounded-xl border border-border bg-muted p-3 font-mono text-xs leading-5 text-muted-foreground">
                    {JSON.stringify(event.payload, null, 2)}
                  </pre>
                </section>
              )}

              <section>
                <h3 className="mb-2 flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
                  <Paperclip size={13} /> 附件（{attachments.length}）
                </h3>
                {attachments.length === 0 ? (
                  <p className="rounded-xl border border-dashed border-border px-4 py-4 text-center text-xs text-[var(--color-text-tertiary)]">
                    当前事件没有附件
                  </p>
                ) : (
                  <div className="overflow-hidden rounded-xl border border-border">
                    {attachments.map(attachment => {
                      const isDownloading = downloadingId === attachment.id
                      return (
                        <div key={attachment.id} className="flex items-center gap-3 border-t border-border px-3 py-2.5 first:border-t-0">
                          <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-muted text-muted-foreground">
                            <Paperclip size={14} />
                          </span>
                          <div className="min-w-0 flex-1">
                            <p className="truncate text-sm font-medium text-foreground" title={attachment.filename}>{attachment.filename}</p>
                            <p className="mt-0.5 text-xs tabular-nums text-[var(--color-text-tertiary)]">{formatBytes(attachment.fileSize)} · {fmt(attachment.createdAt)}</p>
                          </div>
                          <button
                            type="button"
                            onClick={() => void download(attachment.id)}
                            disabled={isDownloading}
                            className="flex h-8 w-8 items-center justify-center rounded-lg text-[var(--color-text-tertiary)] transition-colors hover:bg-[var(--color-success-bg)] hover:text-[var(--color-success)] disabled:opacity-40"
                            title={`下载 ${attachment.filename}`}
                            aria-label={`下载 ${attachment.filename}`}
                          >
                            {isDownloading ? <Loader2 size={15} className="animate-spin" /> : <Download size={15} />}
                          </button>
                        </div>
                      )
                    })}
                  </div>
                )}
              </section>

              <section>
                <h3 className="mb-2 flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
                  <ScrollText size={13} /> 审计轨迹（{auditTrail.length}）
                </h3>
                {auditTrail.length === 0 ? (
                  <p className="rounded-xl border border-dashed border-border px-4 py-4 text-center text-xs text-[var(--color-text-tertiary)]">
                    暂无审计记录
                  </p>
                ) : (
                  <AuditTimeline entries={auditTrail} />
                )}
              </section>
            </div>
          )}
        </div>
      </aside>
    </div>,
    document.body,
  )
}
