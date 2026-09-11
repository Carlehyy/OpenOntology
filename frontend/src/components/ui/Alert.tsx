// 全站持久内联提示标准件（DESIGN.md §4.6）：成功/信息/警告/危险四档语义浅底，
// 供需要停留阅读的上下文信息使用（表单校验、前置检查结果等）；
// 瞬态操作反馈一律走全局 Sonner，不用本组件。
// role/aria 由调用方按语义传入（如 role="alert"），组件不做假设。
import * as React from 'react'
import { CheckCircle2, Info, TriangleAlert, X, XCircle } from 'lucide-react'
import { cn } from '@/lib/utils'

const VARIANTS = {
  info: {
    icon: Info,
    box: 'border-[color-mix(in_srgb,var(--color-info)_35%,transparent)] bg-[var(--color-info-bg)] text-[var(--color-info)]',
  },
  success: {
    icon: CheckCircle2,
    box: 'border-[color-mix(in_srgb,var(--color-success)_35%,transparent)] bg-[var(--color-success-bg)] text-[var(--color-success)]',
  },
  warning: {
    icon: TriangleAlert,
    box: 'border-[color-mix(in_srgb,var(--color-warning)_35%,transparent)] bg-[var(--color-warning-bg)] text-[var(--color-warning)]',
  },
  danger: {
    icon: XCircle,
    box: 'border-[color-mix(in_srgb,var(--color-danger)_35%,transparent)] bg-[var(--color-danger-bg)] text-[var(--color-danger)]',
  },
} as const

export interface AlertProps extends React.HTMLAttributes<HTMLDivElement> {
  variant?: keyof typeof VARIANTS
  children: React.ReactNode
  /** 传入后渲染右侧关闭符 */
  onDismiss?: () => void
  dismissLabel?: string
}

export function Alert({
  variant = 'info',
  children,
  className,
  onDismiss,
  dismissLabel = '关闭提示',
  ...props
}: AlertProps) {
  const { icon: Icon, box } = VARIANTS[variant]
  return (
    <div
      role={undefined}
      className={cn(
        'flex items-start gap-2 rounded-lg border px-3 py-2 text-sm leading-6',
        box,
        className,
      )}
      {...props}
    >
      <Icon size={15} className="mt-0.5 shrink-0" />
      <span className="min-w-0 flex-1">{children}</span>
      {onDismiss && (
        <button
          type="button"
          onClick={onDismiss}
          aria-label={dismissLabel}
          className="shrink-0 rounded p-0.5 transition-colors hover:bg-black/5 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          <X size={13} />
        </button>
      )}
    </div>
  )
}
