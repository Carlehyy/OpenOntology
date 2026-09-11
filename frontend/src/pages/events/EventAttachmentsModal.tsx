import { formatDateTime } from '@/utils/datetime'
import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  Archive, Download, File, FileImage, FileSpreadsheet, FileText,
  Film, Loader2, Mail, Music2, Paperclip, RefreshCw,
} from 'lucide-react'
import { eventsApi, formatBytes, type Attachment } from '@/api/events'
import { Modal } from '@/components/ui/Modal'
import { toast } from 'sonner'

const MAIL_EXTENSIONS = new Set(['eml', 'msg', 'mbox', 'oft', 'ics', 'vcf'])
const IMAGE_EXTENSIONS = new Set(['jpg', 'jpeg', 'png', 'gif', 'bmp', 'webp', 'tif', 'tiff', 'svg', 'heic', 'heif'])
const SPREADSHEET_EXTENSIONS = new Set(['csv', 'tsv', 'xls', 'xlsx', 'ods'])
const DOCUMENT_EXTENSIONS = new Set(['pdf', 'doc', 'docx', 'odt', 'rtf', 'txt', 'md', 'log', 'json', 'xml', 'yaml', 'yml'])
const AUDIO_EXTENSIONS = new Set(['mp3', 'wav', 'm4a', 'aac', 'ogg', 'flac'])
const VIDEO_EXTENSIONS = new Set(['mp4', 'mov', 'avi', 'mkv', 'webm', 'mpeg', 'mpg'])
const ARCHIVE_EXTENSIONS = new Set(['zip', '7z', 'rar', 'tar', 'gz', 'bz2', 'xz', 'tgz'])

function extensionOf(filename: string): string {
  const match = filename.toLowerCase().match(/\.([^.]+)$/)
  return match?.[1] || ''
}

function filePresentation(filename: string) {
  const extension = extensionOf(filename)
  if (MAIL_EXTENSIONS.has(extension)) return { Icon: Mail, label: '邮件文件' }
  if (IMAGE_EXTENSIONS.has(extension)) return { Icon: FileImage, label: '图片' }
  if (SPREADSHEET_EXTENSIONS.has(extension)) return { Icon: FileSpreadsheet, label: '表格' }
  if (DOCUMENT_EXTENSIONS.has(extension)) return { Icon: FileText, label: '文档' }
  if (AUDIO_EXTENSIONS.has(extension)) return { Icon: Music2, label: '音频' }
  if (VIDEO_EXTENSIONS.has(extension)) return { Icon: Film, label: '视频' }
  if (ARCHIVE_EXTENSIONS.has(extension)) return { Icon: Archive, label: '压缩包' }
  return { Icon: File, label: extension ? `${extension.toUpperCase()} 文件` : '文件' }
}

function formatTime(value: string): string {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '时间未知'
  return formatDateTime(date)
}

function errorDetail(cause: any): string {
  return cause?.detail || cause?.message || '请稍后重试'
}

export default function EventAttachmentsModal({
  open,
  eventId,
  onClose,
}: {
  open: boolean
  eventId: string | null
  onClose: () => void
}) {
  const [downloadingId, setDownloadingId] = useState<string | null>(null)
  const [downloadingAll, setDownloadingAll] = useState(false)
  const eventQuery = useQuery({
    queryKey: ['event', eventId],
    queryFn: () => eventsApi.get(eventId!),
    enabled: open && Boolean(eventId),
  })
  const event = eventQuery.data
  const attachments = event?.attachments || []
  const totalSize = useMemo(
    () => attachments.reduce((sum, attachment) => sum + attachment.fileSize, 0),
    [attachments],
  )

  const download = async (attachment: Attachment) => {
    if (!eventId) return
    setDownloadingId(attachment.id)
    try {
      await eventsApi.downloadAttachment(eventId, attachment)
    } catch (cause: any) {
      toast.error('附件下载失败', { description: errorDetail(cause) })
    } finally {
      setDownloadingId(null)
    }
  }

  const downloadAll = async () => {
    if (!eventId || !event || attachments.length === 0) return
    setDownloadingAll(true)
    try {
      await eventsApi.downloadAttachmentsZip(eventId, `${event.eventNo}-附件.zip`)
      toast.success('压缩包已开始下载', { description: `${attachments.length} 个附件已临时打包，服务器不会永久保存该压缩包。` })
    } catch (cause: any) {
      toast.error('附件打包下载失败', { description: errorDetail(cause) })
    } finally {
      setDownloadingAll(false)
    }
  }

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="事件附件"
      description={event ? `${event.title} · ${event.eventNo}` : '查看并下载事件相关附件'}
      size="xl"
      headerIcon={<Paperclip size={19} className="text-[var(--color-success)]" />}
      footer={(
        <div className="flex w-full justify-center gap-3">
          <button
            type="button"
            onClick={onClose}
            className="h-10 rounded-lg border border-border bg-card px-6 text-sm font-medium text-muted-foreground transition-all hover:bg-muted active:scale-[0.98]"
          >
            关闭
          </button>
          <button
            type="button"
            onClick={() => void downloadAll()}
            disabled={downloadingAll || eventQuery.isLoading || attachments.length === 0}
            className="inline-flex h-10 min-w-36 items-center justify-center gap-2 rounded-lg bg-[var(--color-success)] px-6 text-sm font-medium text-[var(--color-text-inverse)] shadow-sm transition-all hover:bg-[var(--color-success)] active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-50"
          >
            {downloadingAll ? <Loader2 size={16} className="animate-spin" /> : <Archive size={16} />}
            {downloadingAll ? '正在打包' : '打包下载全部'}
          </button>
        </div>
      )}
    >
      <div className="space-y-4">
        <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl bg-muted px-4 py-3">
          <div className="flex items-center gap-4 text-sm text-muted-foreground">
            <span>
              <span className="font-semibold tabular-nums text-foreground">{attachments.length}</span> 个附件
            </span>
            <span className="h-4 w-px bg-[var(--color-bg-active)]" aria-hidden="true" />
            <span>合计 <span className="font-medium tabular-nums text-foreground">{formatBytes(totalSize)}</span></span>
          </div>
          <p className="text-xs text-[var(--color-text-tertiary)]">压缩包按需临时生成，下载完成后自动清理</p>
        </div>

        <section aria-label="附件清单" className="max-h-[52vh] overflow-y-auto pr-1">
          {eventQuery.isLoading ? (
            <div className="space-y-2" aria-label="正在加载附件">
              {[0, 1, 2].map(index => (
                <div key={index} className="flex animate-pulse items-center gap-3 rounded-xl border border-border px-3 py-3">
                  <span className="h-10 w-10 rounded-lg bg-muted" />
                  <span className="flex-1 space-y-2">
                    <span className="block h-3 w-2/5 rounded bg-muted" />
                    <span className="block h-2.5 w-1/3 rounded bg-muted" />
                  </span>
                  <span className="h-9 w-20 rounded-lg bg-muted" />
                </div>
              ))}
            </div>
          ) : eventQuery.isError ? (
            <div className="flex flex-col items-center px-6 py-12 text-center">
              <p className="text-sm font-medium text-[var(--color-danger)]">附件清单加载失败</p>
              <p className="mt-1 text-xs text-[var(--color-text-tertiary)]">请检查网络连接后重试</p>
              <button
                type="button"
                onClick={() => void eventQuery.refetch()}
                className="mt-4 inline-flex h-9 items-center gap-1.5 rounded-lg border border-border px-3 text-sm font-medium text-muted-foreground transition-colors hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                <RefreshCw size={14} /> 重新加载
              </button>
            </div>
          ) : attachments.length === 0 ? (
            <div className="flex flex-col items-center px-6 py-12 text-center">
              <span className="mb-3 flex h-12 w-12 items-center justify-center rounded-xl bg-muted text-[var(--color-text-tertiary)]">
                <Paperclip size={20} />
              </span>
              <p className="text-sm font-medium text-foreground">当前事件没有附件</p>
              <p className="mt-1 text-xs text-[var(--color-text-tertiary)]">登记或编辑事件时可以上传多个附件</p>
            </div>
          ) : (
            <div className="space-y-2">
              {attachments.map(attachment => {
                const { Icon, label } = filePresentation(attachment.filename)
                const isDownloading = downloadingId === attachment.id
                return (
                  <article
                    key={attachment.id}
                    className="group flex items-center gap-3 rounded-xl border border-border px-3 py-3 transition-[border-color,background-color] duration-200 hover:border-[color-mix(in_srgb,var(--color-success)_35%,transparent)] hover:bg-[var(--color-success-bg)]"
                  >
                    <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg bg-muted text-muted-foreground transition-colors group-hover:bg-card group-hover:text-[var(--color-success)]">
                      <Icon size={17} />
                    </span>
                    <div className="min-w-0 flex-1">
                      <p className="truncate text-sm font-medium text-foreground" title={attachment.filename}>
                        {attachment.filename}
                      </p>
                      <p className="mt-1 truncate text-xs tabular-nums text-[var(--color-text-tertiary)]">
                        {label} · {formatBytes(attachment.fileSize)} · {formatTime(attachment.createdAt)}
                      </p>
                    </div>
                    <button
                      type="button"
                      onClick={() => void download(attachment)}
                      disabled={isDownloading || downloadingAll}
                      className="inline-flex h-9 shrink-0 items-center gap-1.5 rounded-lg border border-border bg-card px-3 text-sm font-medium text-muted-foreground transition-all hover:border-[color-mix(in_srgb,var(--color-success)_35%,transparent)] hover:text-[var(--color-success)] active:scale-[0.98] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50"
                      aria-label={`下载 ${attachment.filename}`}
                    >
                      {isDownloading ? <Loader2 size={14} className="animate-spin" /> : <Download size={14} />}
                      下载
                    </button>
                  </article>
                )
              })}
            </div>
          )}
        </section>
      </div>
    </Modal>
  )
}
