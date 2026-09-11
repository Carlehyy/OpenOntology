// 全站唯一确认弹窗（component-catalog「弹窗 / 对话框」+ DESIGN.md §4.5 头部模板）。
// 收敛自 4 套并行实现：pages/ontologies|api-hub/ConfirmDialog、
// components/ConfirmDialog（legacy 手写 overlay）、super-assistant ConfirmActionDialog。
// 视觉：标准 Dialog 壳 + 语义图标盒头部 + 语义浅底描述 callout + outline/主色|危险按钮。
import * as React from 'react'
import { AlertTriangle, Info } from 'lucide-react'
import { cn } from '@/lib/utils'
import { Button } from './Button'
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from './dialog'

export interface ConfirmDialogProps {
  open: boolean
  onClose: () => void
  onConfirm: () => void
  title: string
  description?: React.ReactNode
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
        <DialogHeader
          icon={danger || warning ? <AlertTriangle size={18} /> : <Info size={18} />}
          iconClassName={cn(
            danger && 'bg-[var(--color-danger-bg)] text-[var(--color-danger)]',
            warning && 'bg-[var(--color-warning-bg)] text-[var(--color-warning)]',
            !danger && !warning && 'bg-[var(--color-bg-hover)] text-[var(--color-nav-bg)]',
          )}
        >
          <DialogTitle>{title}</DialogTitle>
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
