// 工单域共享的展示元件：状态/分类徽章与图片预览弹窗。
// 列表页、详情抽屉与提交弹窗共用，保持唯一事实源。
import { useEffect } from 'react'
import { createPortal } from 'react-dom'
import { X } from 'lucide-react'
import {
  TICKET_CATEGORY_META, ticketStatusMeta,
  type TicketCategory, type TicketStatus,
} from '@/api/tickets'

export function StatusBadge({ status }: { status: TicketStatus }) {
  // 存量旧行（verifying/accepted）不在 META 中：原样中性展示，不冒充在役状态
  const meta = ticketStatusMeta(status)
  return (
    <span className={`inline-flex items-center gap-1.5 whitespace-nowrap rounded-full px-2 py-1 text-xs font-medium ${meta.cls}`}>
      <span className={`h-1.5 w-1.5 rounded-full ${meta.dot}`} />
      {meta.label}
    </span>
  )
}

export function CategoryBadge({ category }: { category: TicketCategory | null | undefined }) {
  if (!category) return null
  const meta = TICKET_CATEGORY_META[category]
  if (!meta) return null
  return (
    <span className="inline-flex items-center whitespace-nowrap rounded-md bg-muted px-1.5 py-0.5 text-[11px] font-medium text-muted-foreground">
      {meta.label}
    </span>
  )
}

/** 图片附件预览：点击图片附件「查看」后以原尺寸居中展示，Esc/遮罩可关闭。 */
export function ImagePreviewModal({
  open, src, filename, onClose,
}: {
  open: boolean
  src: string | null
  filename: string
  onClose: () => void
}) {
  useEffect(() => {
    if (!open) return
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [onClose, open])

  if (!open || !src) return null

  return createPortal(
    <div
      className="fixed inset-0 flex items-center justify-center p-6"
      role="dialog"
      aria-label={`图片预览 ${filename}`}
      style={{ zIndex: 'calc(var(--z-modal) + 10)' }}
    >
      <div className="absolute inset-0 bg-[var(--color-bg-overlay)]" onClick={onClose} />
      <figure className="relative flex max-h-full max-w-full flex-col items-center gap-3">
        <img
          src={src}
          alt={filename}
          className="max-h-[calc(100vh-8rem)] max-w-full rounded-xl object-contain shadow-2xl"
        />
        <figcaption className="max-w-full truncate rounded-lg bg-[var(--color-bg-overlay)] px-3 py-1.5 text-xs text-[var(--color-text-inverse)]">
          {filename}
        </figcaption>
        <button
          type="button"
          onClick={onClose}
          className="absolute -right-2 -top-2 flex h-8 w-8 items-center justify-center rounded-full bg-card text-muted-foreground shadow-lg transition-colors hover:bg-muted"
          aria-label="关闭图片预览"
        >
          <X size={16} />
        </button>
      </figure>
    </div>,
    document.body,
  )
}
