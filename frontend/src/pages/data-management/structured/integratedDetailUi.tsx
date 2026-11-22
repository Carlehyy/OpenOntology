import type { ButtonHTMLAttributes, ReactNode } from 'react'
import { ChevronDown, CircleHelp } from 'lucide-react'
import { Button } from '@/components/ui/Button'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { cn } from '@/lib/utils'
import type { StatusItemView, StatusTone } from './integratedDetailModel'

/*
 * Presentational pieces of the integrated asset detail dialog. They render
 * what the detail model decides and hold no server state of their own.
 */

const TONE_DOT: Record<StatusTone, string> = {
  success: 'bg-[var(--color-success)]',
  warning: 'bg-[var(--color-warning)]',
  danger: 'bg-[var(--color-danger)]',
  info: 'bg-[var(--color-info)]',
  neutral: 'bg-[var(--color-text-disabled)]',
}

const TONE_TEXT: Record<StatusTone, string> = {
  success: 'text-[var(--color-success)]',
  warning: 'text-[var(--color-warning)]',
  danger: 'text-[var(--color-danger)]',
  info: 'text-[var(--color-info)]',
  neutral: 'text-foreground',
}

/** antd tables take the platform palette; only design tokens are named here. */
export const TABLE_THEME = {
  components: {
    Table: {
      colorBgContainer: 'var(--card)',
      colorText: 'var(--foreground)',
      colorTextHeading: 'var(--foreground)',
      borderColor: 'var(--border)',
      headerBg: 'var(--color-bg-elevated)',
      headerColor: 'var(--color-text-secondary)',
      headerSplitColor: 'var(--border)',
      rowHoverBg: 'var(--color-bg-hover)',
      fontSize: 12,
      cellPaddingBlockSM: 8,
      cellPaddingInlineSM: 12,
    },
  },
}

export const TEMPORAL_TYPES = new Set(['timestamp', 'date', 'time'])

/** Focus the trigger that opened a nested dialog; fall back when that control is gone. */
export function refocus(event: Event, target: HTMLElement | null | undefined, fallback?: HTMLElement | null) {
  const next = target?.isConnected ? target : fallback?.isConnected ? fallback : null
  if (!next) return
  event.preventDefault()
  next.focus({ preventScroll: true })
}

/** Hint on hover for any control; a disabled control is wrapped so it still receives pointer events. */
export function Hint({ content, children, wrap = false }: { content: ReactNode; children: ReactNode; wrap?: boolean }) {
  const trigger = wrap
    ? <span className="inline-flex shrink-0" tabIndex={0}>{children}</span>
    : children
  return (
    <Tooltip>
      <TooltipTrigger asChild>{trigger}</TooltipTrigger>
      <TooltipContent className="max-w-xs leading-5">{content}</TooltipContent>
    </Tooltip>
  )
}

export function SectionTitle({ id, meta, children }: { id: string; meta?: ReactNode; children: ReactNode }) {
  return (
    <div className="flex min-h-9 items-center gap-2">
      <h3 id={id} className="text-sm font-semibold text-foreground">{children}</h3>
      {meta && <span className="min-w-0 truncate text-xs text-muted-foreground">{meta}</span>}
    </div>
  )
}

export function StatusItem({ testId, label, view, help, action }: {
  testId: string
  label: string
  view: StatusItemView
  help: ReactNode
  action?: ReactNode
}) {
  return (
    <div
      role="status"
      aria-label={`${label}：${view.label}`}
      data-testid={testId}
      className="flex h-full min-w-0 flex-1 items-center gap-2 max-xl:h-8 max-xl:flex-none"
    >
      <span aria-hidden className={cn('size-2 shrink-0 rounded-full', TONE_DOT[view.tone])} />
      <span className="shrink-0 text-xs text-muted-foreground">{label}</span>
      <Tooltip>
        <TooltipTrigger asChild>
          <button
            type="button"
            aria-label={`${label}说明`}
            className="flex size-5 shrink-0 items-center justify-center rounded text-muted-foreground transition-colors hover:bg-[var(--color-bg-hover)] hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            <CircleHelp size={14} />
          </button>
        </TooltipTrigger>
        <TooltipContent className="max-w-xs leading-5">
          <p>{help}</p>
          {view.details.length > 0 && (
            <div className="mt-1.5 space-y-0.5 border-t border-border pt-1.5 font-mono text-xs">
              {view.details.map(detail => <div key={detail}>{detail}</div>)}
            </div>
          )}
        </TooltipContent>
      </Tooltip>
      <strong data-testid={`${testId}-value`} className={cn('shrink-0 text-[13px] font-semibold', TONE_TEXT[view.tone])}>
        {view.label}
      </strong>
      {view.fact && <span className="min-w-0 truncate text-xs text-muted-foreground">{view.fact}</span>}
      {action && <span className="ml-auto flex shrink-0 items-center">{action}</span>}
    </div>
  )
}

/** Quiet lifecycle action for the footer; destructive ones carry the danger colour. */
export function FooterAction({ icon, danger, hint, wrap, ...props }: {
  icon: ReactNode
  danger?: boolean
  hint?: ReactNode
  wrap?: boolean
} & ButtonHTMLAttributes<HTMLButtonElement>) {
  const button = (
    <Button
      type="button"
      variant="ghost"
      size="sm"
      {...props}
      className={cn('text-muted-foreground', danger && 'text-[var(--color-danger)] hover:bg-[var(--color-danger-bg)] hover:text-[var(--color-danger)]', props.className)}
    >
      {icon}{props.children}
    </Button>
  )
  return hint ? <Hint content={hint} wrap={wrap}>{button}</Hint> : button
}

export function InfoRow({ label, children }: { label: string; children: ReactNode }) {
  return (
    <>
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="min-w-0 break-all font-mono text-foreground [user-select:all]">{children}</dd>
    </>
  )
}

export function Disclosure({ testId, title, meta, open, onToggle, children }: {
  testId?: string
  title: string
  meta?: ReactNode
  /** Omit for an uncontrolled disclosure that starts closed. */
  open?: boolean
  onToggle?: (open: boolean) => void
  children: ReactNode
}) {
  return (
    <details
      data-testid={testId}
      open={open}
      onToggle={onToggle ? event => onToggle((event.target as HTMLDetailsElement).open) : undefined}
      className="group rounded-lg border border-border"
    >
      <summary className="flex min-h-11 cursor-pointer select-none items-center gap-2 px-4 py-2.5 text-sm font-medium text-foreground list-none [&::-webkit-details-marker]:hidden">
        <ChevronDown size={14} className="shrink-0 text-muted-foreground transition-transform group-open:rotate-0 -rotate-90" />
        {title}
        {meta && <span className="text-xs font-normal text-muted-foreground">{meta}</span>}
      </summary>
      <div className="space-y-3 border-t border-border px-4 py-4">{children}</div>
    </details>
  )
}
