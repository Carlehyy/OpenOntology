import { useEffect, useState } from 'react'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { createPortal } from 'react-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AlertTriangle, Check, Copy, Download, Eye, Loader2, Paperclip, RefreshCw,
  ScrollText, X,
} from 'lucide-react'
import {
  fmtTime, formatBytes, ticketsApi,
  TICKET_CATEGORY_META, TICKET_STATUS_META, TICKET_STATUS_ORDER,
  type TicketAttachment, type TicketItem, type TicketStatus,
} from '@/api/tickets'
import { useAuthStore } from '@/stores/authStore'
import { toast } from 'sonner'
import { writeTextToClipboard } from '@/utils/clipboard'
import { ImagePreviewModal, StatusBadge } from './shared'

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

/** 管理员处理面板：状态 + 必填评论。 */
function ProgressPanel({
  ticket,
  onDone,
}: {
  ticket: TicketItem
  onDone: () => void
}) {
  const queryClient = useQueryClient()
  const [status, setStatus] = useState<TicketStatus>(ticket.status)
  const [comment, setComment] = useState('')
  const [error, setError] = useState('')

  useEffect(() => {
    if (!ticket) return
    setStatus(ticket.status)
    setComment('')
    setError('')
  }, [ticket.id, ticket.status])

  const mutation = useMutation({
    mutationFn: async () => {
      if (!comment.trim()) throw new Error('处理评论为必填项')
      return ticketsApi.updateProgress(ticket.id, status, comment.trim())
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['tickets'] })
      toast.success('工单已处理', { description: `状态已更新为「${TICKET_STATUS_META[status].label}」` })
      setComment('')
      onDone()
    },
    onError: (cause: any) => setError(cause?.message || cause?.detail || '处理失败，请稍后重试'),
  })

  return (
    <section className="rounded-xl border border-[color-mix(in_srgb,var(--color-success)_35%,transparent)] bg-[var(--color-success-bg)] px-4 py-3.5">
      <h3 className="text-sm font-semibold text-foreground">处理工单</h3>
      <p className="mt-0.5 text-xs text-[var(--color-text-tertiary)]">调整进度状态并留下处理评论（评论为必填，提交后对处理轨迹可见）</p>
      <div className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-[200px_minmax(0,1fr)]">
        <div>
          <label htmlFor="ticket-progress-status" className="mb-1 block text-xs font-medium text-muted-foreground">进度状态</label>
          <Select
            value={status}
            onValueChange={value => setStatus(value as TicketStatus)}
          >
            <SelectTrigger id="ticket-progress-status" className="h-9 w-full cursor-pointer rounded-lg bg-card px-2.5 text-sm shadow-sm">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {TICKET_STATUS_ORDER.map(value => (
                <SelectItem key={value} value={value}>{TICKET_STATUS_META[value].label}</SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <div>
          <label htmlFor="ticket-progress-comment" className="mb-1 block text-xs font-medium text-muted-foreground">
            处理评论 <span className="text-[var(--color-danger)]">*</span>
          </label>
          <div className="flex gap-2">
            <input
              id="ticket-progress-comment"
              value={comment}
              onChange={event => { setComment(event.target.value); if (error) setError('') }}
              onKeyDown={event => {
                if (event.key === 'Enter' && comment.trim() && !mutation.isPending) mutation.mutate()
              }}
              placeholder="例如：已复现，等待下个版本修复"
              className="h-9 min-w-0 flex-1 rounded-lg border border-border bg-card px-3 text-sm text-foreground shadow-sm transition placeholder:text-[var(--color-text-tertiary)] focus-visible:border-ring focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            />
            <button
              type="button"
              onClick={() => mutation.mutate()}
              disabled={mutation.isPending || !comment.trim()}
              className="inline-flex h-9 shrink-0 items-center gap-1.5 rounded-lg bg-[var(--color-success)] px-4 text-sm font-medium text-[var(--color-text-inverse)] shadow-sm transition-all hover:bg-[var(--color-success)] active:scale-[0.98] disabled:opacity-50"
            >
              {mutation.isPending && <span className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-border border-t-white" />}
              提交处理
            </button>
          </div>
        </div>
      </div>
      {error && (
        <p className="mt-2 flex items-center gap-1.5 text-xs text-[var(--color-danger)]" role="alert">
          <AlertTriangle size={13} /> {error}
        </p>
      )}
    </section>
  )
}

export default function TicketDetailDrawer({
  open,
  ticketId,
  onClose,
}: {
  open: boolean
  ticketId: string | null
  onClose: () => void
}) {
  const isAdmin = useAuthStore(s => s.user?.role === 'admin')
  const [downloadingId, setDownloadingId] = useState<string | null>(null)
  const [previewingId, setPreviewingId] = useState<string | null>(null)
  const [preview, setPreview] = useState<{ url: string; filename: string } | null>(null)
  const detailQuery = useQuery({
    queryKey: ['tickets', 'detail', ticketId],
    queryFn: () => ticketsApi.get(ticketId!),
    enabled: open && Boolean(ticketId),
  })
  const ticket = detailQuery.data
  const attachments = ticket?.attachments ?? []
  const progressLogs = ticket?.progressLogs ?? []

  useEffect(() => {
    if (!open) return
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [onClose, open])

  useEffect(() => {
    // 关闭抽屉时释放未清理的预览 objectURL
    if (open) return
    if (preview) URL.revokeObjectURL(preview.url)
    setPreview(null)
    setPreviewingId(null)
  }, [open, preview])

  if (!open) return null

  const previewAttachment = async (attachment: TicketAttachment) => {
    if (!ticketId) return
    setPreviewingId(attachment.id)
    try {
      const blob = await ticketsApi.fetchAttachmentBlob(ticketId, attachment)
      setPreview({ url: URL.createObjectURL(blob), filename: attachment.filename })
    } catch (cause: any) {
      toast.error('图片加载失败', { description: cause?.detail || cause?.message || '请稍后重试' })
    } finally {
      setPreviewingId(null)
    }
  }

  const closePreview = () => {
    if (preview) URL.revokeObjectURL(preview.url)
    setPreview(null)
  }

  const download = async (attachmentId: string) => {
    if (!ticketId) return
    const attachment = attachments.find(item => item.id === attachmentId)
    if (!attachment) return
    setDownloadingId(attachmentId)
    try {
      await ticketsApi.downloadAttachment(ticketId, attachment)
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
              <h2 className="truncate text-lg font-semibold text-foreground" title={ticket?.title}>
                {ticket?.title ?? '工单详情'}
              </h2>
              {ticket && <StatusBadge status={ticket.status} />}
            </div>
            {ticket && (
              <div className="mt-1 flex items-center gap-2 text-xs text-[var(--color-text-tertiary)]">
                <span className="font-mono text-muted-foreground">{ticket.ticketNo}</span>
                <CopyBtn text={ticket.ticketNo} />
              </div>
            )}
          </div>
          <button
            type="button"
            onClick={onClose}
            className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg text-[var(--color-text-tertiary)] transition-colors hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            aria-label="关闭工单详情"
          >
            <X size={20} />
          </button>
        </header>

        <div className="flex-1 overflow-y-auto px-6 py-5">
          {detailQuery.isLoading ? (
            <div className="space-y-3" aria-label="正在加载工单详情">
              {[0, 1, 2].map(index => (
                <div key={index} className="animate-pulse space-y-2 rounded-xl border border-border p-4">
                  <span className="block h-3 w-1/3 rounded bg-muted" />
                  <span className="block h-2.5 w-2/3 rounded bg-muted" />
                </div>
              ))}
            </div>
          ) : detailQuery.isError || !ticket ? (
            <div className="flex flex-col items-center px-6 py-16 text-center">
              <p className="text-sm font-medium text-[var(--color-danger)]">工单详情加载失败</p>
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
              {isAdmin && <ProgressPanel ticket={ticket} onDone={() => void detailQuery.refetch()} />}

              <dl className="grid grid-cols-2 gap-x-4 gap-y-3">
                <MetaItem label="提交人">{ticket.submitterName || '—'}</MetaItem>
                <MetaItem label="工单分类">{TICKET_CATEGORY_META[ticket.category]?.label ?? '—'}</MetaItem>
                <MetaItem label="提交时间">{fmtTime(ticket.createdAt)}</MetaItem>
                <MetaItem label="最近更新">{fmtTime(ticket.updatedAt)}</MetaItem>
                {ticket.pageUrl && (
                  <div className="col-span-2 min-w-0">
                    <dt className="text-xs text-[var(--color-text-tertiary)]">提交页面</dt>
                    <dd className="mt-0.5 truncate font-mono text-xs text-muted-foreground" title={ticket.pageUrl}>
                      {ticket.pageUrl}
                    </dd>
                  </div>
                )}
              </dl>

              <section>
                <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">反馈内容</h3>
                <p className="whitespace-pre-wrap rounded-xl bg-muted px-4 py-3 text-sm leading-6 text-foreground">
                  {ticket.content || <span className="italic text-[var(--color-text-tertiary)]">无内容</span>}
                </p>
              </section>

              <section>
                <h3 className="mb-2 flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
                  <Paperclip size={13} /> 附件（{attachments.length}）
                </h3>
                {attachments.length === 0 ? (
                  <p className="rounded-xl border border-dashed border-border px-4 py-4 text-center text-xs text-[var(--color-text-tertiary)]">
                    当前工单没有附件
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
                            <p className="mt-0.5 text-xs tabular-nums text-[var(--color-text-tertiary)]">{formatBytes(attachment.fileSize)} · {fmtTime(attachment.createdAt)}</p>
                          </div>
                          {(attachment.mimeType || '').startsWith('image/') && (
                            <button
                              type="button"
                              onClick={() => void previewAttachment(attachment)}
                              disabled={previewingId === attachment.id}
                              className="flex h-8 w-8 items-center justify-center rounded-lg text-[var(--color-text-tertiary)] transition-colors hover:bg-[var(--color-success-bg)] hover:text-[var(--color-success)] disabled:opacity-40"
                              title={`预览 ${attachment.filename}`}
                              aria-label={`预览 ${attachment.filename}`}
                            >
                              {previewingId === attachment.id ? <Loader2 size={15} className="animate-spin" /> : <Eye size={15} />}
                            </button>
                          )}
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
                  <ScrollText size={13} /> 处理记录（{progressLogs.length}）
                </h3>
                {progressLogs.length === 0 ? (
                  <p className="rounded-xl border border-dashed border-border px-4 py-4 text-center text-xs text-[var(--color-text-tertiary)]">
                    暂无处理记录，等待管理员处理
                  </p>
                ) : (
                  <ul className="space-y-0">
                    {[...progressLogs].sort((a, b) => b.seq - a.seq).map(entry => {
                      const fromLabel = entry.fromStatus ? TICKET_STATUS_META[entry.fromStatus].label : null
                      const toLabel = TICKET_STATUS_META[entry.toStatus]?.label ?? entry.toStatus
                      return (
                        <li key={entry.id} className="relative ml-1 border-l border-border pb-4 pl-4 last:pb-0">
                          <span className="absolute -left-[5px] top-1.5 h-2.5 w-2.5 rounded-full bg-[var(--color-success-bg)] ring-2 ring-white" aria-hidden="true" />
                          <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5 text-sm">
                            <span className="font-medium text-foreground">
                              {fromLabel && fromLabel !== toLabel ? `${fromLabel} → ${toLabel}` : toLabel}
                            </span>
                            <span className="text-xs text-[var(--color-text-tertiary)]">
                              {entry.actorName || '管理员'} · {fmtTime(entry.createdAt)}
                            </span>
                          </div>
                          <p className="mt-0.5 text-xs leading-5 text-muted-foreground">{entry.comment}</p>
                        </li>
                      )
                    })}
                  </ul>
                )}
              </section>
            </div>
          )}
        </div>
      </aside>

      <ImagePreviewModal
        open={Boolean(preview)}
        src={preview?.url ?? null}
        filename={preview?.filename ?? ''}
        onClose={closePreview}
      />
    </div>,
    document.body,
  )
}
