import * as React from 'react'
import { AlertTriangle, Info, X } from 'lucide-react'
import { cn } from '@/lib/utils'
import { Button } from './Button'

interface ModalProps {
  open: boolean
  onClose: () => void
  title?: string
  description?: string
  children: React.ReactNode
  footer?: React.ReactNode
  size?: 'sm' | 'md' | 'lg' | 'xl' | '2xl' | '3xl'
  headerIcon?: React.ReactNode
  /** 为 true 时屏蔽 Esc、遮罩点击与关闭按钮（用于提交/上传进行中，防止产生半截数据） */
  disableClose?: boolean
  panelClassName?: string
  backdropClassName?: string
  headerClassName?: string
  contentClassName?: string
  footerClassName?: string
}

const sizeMap = {
  sm: 'max-w-sm',
  md: 'max-w-md',
  lg: 'max-w-lg',
  xl: 'max-w-xl',
  '2xl': 'max-w-2xl',
  '3xl': 'max-w-3xl',
}

export function Modal({
  open,
  onClose,
  title,
  description,
  children,
  footer,
  size = 'md',
  headerIcon,
  disableClose = false,
  panelClassName,
  backdropClassName,
  headerClassName,
  contentClassName,
  footerClassName,
}: ModalProps) {
  const titleId = React.useId()
  const descriptionId = React.useId()

  React.useEffect(() => {
    if (!open) return undefined
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && !disableClose) onClose()
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [disableClose, onClose, open])

  if (!open) return null

  return (
    <div className="fixed inset-0 z-[var(--z-modal)] flex items-center justify-center p-4">
      <div
        className={cn(
          'absolute inset-0 bg-[var(--color-bg-overlay)] animate-fade-in',
          backdropClassName,
        )}
        onClick={disableClose ? undefined : onClose}
        aria-hidden="true"
      />
      <section
        role="dialog"
        aria-modal="true"
        aria-labelledby={title ? titleId : undefined}
        aria-describedby={description ? descriptionId : undefined}
        className={cn(
          'relative flex max-h-[calc(100dvh-2rem)] w-full flex-col overflow-hidden rounded-2xl border border-[var(--color-border)] bg-[var(--color-popover)] shadow-[var(--shadow-lg)] animate-slide-up',
          sizeMap[size],
          panelClassName,
        )}
      >
        <button
          type="button"
          onClick={onClose}
          disabled={disableClose}
          aria-label="关闭弹窗"
          className="absolute right-4 top-4 z-10 flex h-8 w-8 items-center justify-center rounded-lg text-[var(--color-text-tertiary)] transition-colors hover:bg-[var(--color-bg-hover)] hover:text-[var(--color-text-primary)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:bg-transparent disabled:hover:text-[var(--color-text-tertiary)]"
        >
          <X size={16} />
        </button>

        {(title || description) && (
          <header className={cn(
            'flex shrink-0 items-start gap-3 px-6 pb-3 pt-5 pr-14',
            headerClassName,
          )}>
            {headerIcon && (
              <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-[var(--color-bg-hover)] text-[var(--color-text-secondary)]">
                {headerIcon}
              </div>
            )}
            <div className="min-w-0 pt-0.5">
              {title && (
                <h3 id={titleId} className="text-base font-semibold tracking-[-0.01em] text-[var(--color-text-primary)]">
                  {title}
                </h3>
              )}
              {description && (
                <p id={descriptionId} className="mt-1 text-sm leading-6 text-[var(--color-text-secondary)]">
                  {description}
                </p>
              )}
            </div>
          </header>
        )}

        <div className={cn(
          'min-h-0 flex-1 overflow-y-auto px-6 py-4',
          !(title || description) && 'pt-6',
          contentClassName,
        )}>{children}</div>
        {footer && (
          <footer className={cn(
            'flex shrink-0 justify-end gap-2 border-t border-[var(--color-border)] bg-[var(--color-muted)] px-6 py-4',
            footerClassName,
          )}>
            {footer}
          </footer>
        )}
      </section>
    </div>
  )
}

interface ConfirmModalProps {
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

export function ConfirmModal({
  open,
  onClose,
  onConfirm,
  title,
  description,
  confirmText = '确认',
  cancelText = '取消',
  variant = 'default',
  loading,
}: ConfirmModalProps) {
  const danger = variant === 'danger'
  const warning = variant === 'warning'
  return (
    <Modal
      open={open}
      onClose={onClose}
      title={title}
      size="sm"
      headerIcon={danger
        ? <AlertTriangle size={19} className="text-[var(--color-danger)]" />
        : warning
          ? <AlertTriangle size={19} className="text-amber-600" />
          : <Info size={19} className="text-[var(--color-nav-bg)]" />}
      footer={(
        <>
          <Button variant="outline" onClick={onClose} disabled={loading}>{cancelText}</Button>
          <Button
            variant={danger ? 'danger' : 'default'}
            onClick={onConfirm}
            loading={loading}
            className={danger ? 'shadow-sm shadow-red-900/10' : undefined}
          >
            {confirmText}
          </Button>
        </>
      )}
    >
      {description ? (
        <div className={cn(
          'rounded-xl border px-4 py-3 text-sm leading-6',
          danger
            ? 'border-[var(--color-danger-bg)] bg-[var(--color-danger-bg)] text-[var(--color-danger)]'
            : warning
              ? 'border-amber-200 bg-amber-50 text-amber-800'
              : 'border-[var(--color-nav-light)] bg-[var(--color-nav-light)] text-[var(--color-text-secondary)]',
        )}>
          {description}
        </div>
      ) : <div />}
    </Modal>
  )
}
