// 接口代理域内确认弹窗：基于 vendored Radix Dialog（component-catalog.ts
// 「弹窗 / 对话框」条目）组合，替换旧 ui/Modal 的 ConfirmModal 在本域的
// 用法；不属于新的弹窗体系，禁止在域外复用前先沉淀进 components/ui。
import { AlertTriangle, Info } from 'lucide-react'
import { cn } from '@/lib/utils'
import { Button } from '@/components/ui/Button'
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from '@/components/ui/dialog'

interface ConfirmDialogProps {
  open: boolean
  onClose: () => void
  onConfirm: () => void
  title: string
  description?: string
  confirmText?: string
  cancelText?: string
  variant?: 'danger' | 'warning' | 'default'
  loading?: boolean
}

export function ConfirmDialog({
  open,
  onClose,
  onConfirm,
  title,
  description,
  confirmText = '确认',
  cancelText = '取消',
  variant = 'default',
  loading,
}: ConfirmDialogProps) {
  const danger = variant === 'danger'
  const warning = variant === 'warning'
  return (
    <Dialog open={open} onOpenChange={next => { if (!next) onClose() }}>
      <DialogContent className="w-[min(92vw,26rem)]">
        <DialogHeader>
          <div
            className={cn(
              'flex h-10 w-10 shrink-0 items-center justify-center rounded-xl',
              danger ? 'bg-[var(--color-danger-bg)] text-[var(--color-danger)]'
                : warning ? 'bg-[var(--color-warning-bg)] text-[var(--color-warning)]'
                  : 'bg-[var(--color-bg-hover)] text-[var(--color-nav-bg)]',
            )}
          >
            {danger || warning ? <AlertTriangle size={19} /> : <Info size={19} />}
          </div>
          <div className="min-w-0">
            <DialogTitle>{title}</DialogTitle>
          </div>
        </DialogHeader>
        {description && (
          <DialogDescription
            asChild
            className={cn(
              'mt-0 block rounded-xl border px-4 py-3 text-sm leading-6',
              danger && 'border-[var(--color-danger-bg)] bg-[var(--color-danger-bg)] text-[var(--color-danger)]',
              warning && 'border-[var(--color-warning-bg)] bg-[var(--color-warning-bg)] text-[var(--color-warning)]',
              !danger && !warning && 'border-[var(--color-nav-light)] bg-[var(--color-nav-light)] text-[var(--color-text-secondary)]',
            )}
          >
            <div>{description}</div>
          </DialogDescription>
        )}
        <DialogFooter>
          <Button variant="outline" onClick={onClose} disabled={loading}>{cancelText}</Button>
          <Button variant={danger ? 'danger' : 'default'} onClick={onConfirm} loading={loading}>{confirmText}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
