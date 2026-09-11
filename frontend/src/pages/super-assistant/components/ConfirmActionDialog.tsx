import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'

interface ConfirmActionDialogProps {
  open: boolean
  title: string
  message: string
  confirmLabel?: string
  busy?: boolean
  onConfirm: () => void
  onCancel: () => void
}

/** 基于 ReUI Dialog 的破坏性操作确认框，替代 window.confirm（超级助手前台域内共用）。 */
export default function ConfirmActionDialog({
  open,
  title,
  message,
  confirmLabel = '删除',
  busy = false,
  onConfirm,
  onCancel,
}: ConfirmActionDialogProps) {
  return (
    <Dialog open={open} onOpenChange={value => { if (!value) onCancel() }}>
      <DialogContent className="w-[min(92vw,24rem)]">
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>{message}</DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <button
            type="button"
            onClick={onCancel}
            className="min-h-9 rounded-lg px-4 text-xs text-[var(--color-text-secondary)] transition-colors hover:bg-[var(--color-bg-hover)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            取消
          </button>
          <button
            type="button"
            onClick={onConfirm}
            disabled={busy}
            className="min-h-9 rounded-lg bg-red-600 px-4 text-xs font-medium text-white transition-colors hover:bg-red-700 disabled:opacity-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            {confirmLabel}
          </button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
