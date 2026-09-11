import { formatDateTime } from '@/utils/datetime'
import { useEffect } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AlertTriangle,
  Archive,
  CheckCircle2,
  ChevronRight,
  CircleSlash2,
  Clock3,
  Inbox,
  Loader2,
  MailOpen,
  RefreshCw,
  Workflow,
} from 'lucide-react'

import { inboxApi, type InboxDelivery } from '@/api/inbox'


function formatRelativeTime(value: string): string {
  return formatDateTime(value, { fallback: value })
}

function formatExactTime(value: string): string {
  const date = new Date(value)
  return Number.isFinite(date.getTime())
    ? formatDateTime(date, { seconds: true })
    : value
}

export default function InboxPopover({
  open,
  onOpenChange,
  onNavigate,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  onNavigate: (href: string) => void
}) {
  const queryClient = useQueryClient()
  const summary = useQuery({
    queryKey: ['inbox', 'summary'],
    queryFn: inboxApi.summary,
    refetchInterval: 15_000,
    refetchOnWindowFocus: true,
  })
  const list = useQuery({
    queryKey: ['inbox', 'popover'],
    queryFn: () => inboxApi.list({ tab: 'all', limit: 8 }),
    enabled: open,
    refetchOnMount: 'always',
  })
  const stateMutation = useMutation({
    mutationFn: ({ id, state }: { id: string; state: 'read' | 'archived' }) =>
      inboxApi.updateState(id, state),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['inbox'] })
    },
  })

  useEffect(() => {
    if (!open) return
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onOpenChange(false)
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [onOpenChange, open])

  const handleOpen = async (item: InboxDelivery) => {
    if (item.deliveryState === 'unread') {
      try { await inboxApi.updateState(item.id, 'read') } catch { /* navigation remains available */ }
      void queryClient.invalidateQueries({ queryKey: ['inbox'] })
    }
    onOpenChange(false)
    onNavigate(item.actions[0]?.href || item.resource.href)
  }

  const openCount = summary.data?.openAlertCount ?? 0
  const items = list.data?.items ?? []

  return (
    <>
      <button
        type="button"
        onClick={() => onOpenChange(!open)}
        className={`relative grid h-11 w-11 place-items-center rounded-lg transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${
          open
            ? 'bg-[var(--color-bg-hover)] text-[var(--color-text-primary)]'
            : 'text-[var(--color-text-secondary)] hover:bg-[var(--color-bg-hover)] hover:text-[var(--color-text-primary)]'
        }`}
        aria-label={`收件箱${openCount ? `，${openCount} 个未恢复告警` : ''}`}
        aria-expanded={open}
        aria-controls="global-inbox-popover"
      >
        <Inbox size={21} strokeWidth={1.8} />
        {openCount > 0 && (
          <span className="absolute right-0 top-0 flex h-[18px] min-w-[18px] items-center justify-center rounded-full bg-[var(--color-danger)] px-1 text-[10px] font-semibold tabular-nums text-white shadow-sm">
            {openCount > 99 ? '99+' : openCount}
          </span>
        )}
      </button>

      {open && (
        <section
          id="global-inbox-popover"
          aria-label="收件箱消息"
          className="fixed left-3 right-3 top-[58px] z-50 origin-top-right overflow-hidden rounded-2xl border border-[var(--color-border)] bg-[var(--color-popover)] shadow-[var(--shadow-lg)] animate-slide-up motion-reduce:animate-none sm:absolute sm:left-auto sm:right-0 sm:top-auto sm:mt-2 sm:w-[448px]"
        >
          <header className="flex min-h-[72px] items-center gap-3 border-b border-[var(--color-border)] px-4 py-3.5">
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-2">
                <h2 className="text-[15px] font-semibold tracking-tight text-[var(--color-text-primary)]">收件箱</h2>
                {openCount > 0 && (
                  <span className="inline-flex items-center gap-1 rounded-md bg-[var(--color-danger-bg)] px-2 py-1 text-[11px] font-medium tabular-nums text-[var(--color-danger)]">
                    <AlertTriangle size={11} aria-hidden="true" />
                    {openCount} 项待恢复
                  </span>
                )}
              </div>
              <p className="mt-1 text-xs leading-4 text-[var(--color-text-secondary)]">
                阅读仅表示已知晓；任务成功后，故障会自动恢复
              </p>
            </div>
            <button
              type="button"
              onClick={() => void list.refetch()}
              disabled={list.isFetching}
              className="grid h-11 w-11 shrink-0 place-items-center rounded-xl text-[var(--color-text-tertiary)] transition-colors duration-200 hover:bg-[var(--color-bg-hover)] hover:text-[var(--color-text-primary)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50"
              aria-label="刷新收件箱"
            >
              <RefreshCw size={16} className={list.isFetching ? 'animate-spin motion-reduce:animate-none' : ''} />
            </button>
          </header>

          <div className="scrollbar-thin max-h-[min(520px,calc(100dvh-190px))] overflow-y-auto">
            {list.isLoading ? (
              <div className="space-y-2.5 p-3" aria-label="正在加载收件箱">
                {[0, 1, 2].map(index => (
                  <div key={index} className="grid grid-cols-[40px_minmax(0,1fr)] gap-3 rounded-xl px-3 py-4">
                    <span className="h-10 w-10 animate-pulse rounded-xl bg-[var(--color-bg-hover)] motion-reduce:animate-none" />
                    <span className="space-y-2">
                      <span className="block h-4 w-28 animate-pulse rounded bg-[var(--color-bg-hover)] motion-reduce:animate-none" />
                      <span className="block h-4 w-4/5 animate-pulse rounded bg-[var(--color-bg-hover)] motion-reduce:animate-none" />
                      <span className="block h-3 w-full animate-pulse rounded bg-[var(--color-bg-hover)] motion-reduce:animate-none" />
                    </span>
                  </div>
                ))}
              </div>
            ) : list.isError ? (
              <div role="alert" className="flex flex-col items-center px-5 py-10 text-center">
                <AlertTriangle size={24} className="mb-2 text-[var(--color-danger)]" />
                <p className="text-sm font-medium text-[var(--color-text-primary)]">收件箱加载失败</p>
                <p className="mt-1 text-xs text-[var(--color-text-secondary)]">请检查网络后重新加载</p>
                <button type="button" onClick={() => void list.refetch()} className="mt-3 inline-flex h-11 items-center gap-1.5 rounded-lg bg-[var(--color-bg-hover)] px-3.5 text-xs font-medium text-[var(--color-text-primary)] transition-colors hover:bg-[var(--color-bg-active)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
                  <RefreshCw size={13} />重新加载
                </button>
              </div>
            ) : items.length === 0 ? (
              <div className="flex flex-col items-center px-5 py-12 text-center">
                <span className="mb-3 grid h-11 w-11 place-items-center rounded-xl bg-[var(--color-success-bg)] text-[var(--color-success)]">
                  <CheckCircle2 size={20} />
                </span>
                <p className="text-sm font-medium text-[var(--color-text-primary)]">任务运行平稳</p>
                <p className="mt-1 text-xs text-[var(--color-text-tertiary)]">数据任务执行失败时会在这里提醒你</p>
              </div>
            ) : (
              <div className="divide-y divide-[var(--color-border)]" role="list">
                {items.map(item => (
                  <InboxPopoverItem
                    key={item.id}
                    item={item}
                    busy={stateMutation.isPending}
                    onOpen={() => void handleOpen(item)}
                    onMarkRead={() => stateMutation.mutate({ id: item.id, state: 'read' })}
                    onArchive={() => stateMutation.mutate({ id: item.id, state: 'archived' })}
                  />
                ))}
              </div>
            )}
          </div>

          <button
            type="button"
            onClick={() => { onOpenChange(false); onNavigate('/inbox') }}
            className="group flex h-12 w-full items-center justify-between border-t border-[var(--color-border)] px-4 text-xs font-medium text-[var(--color-nav-bg)] transition-colors duration-200 hover:bg-[var(--color-nav-light)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring"
          >
            <span>查看全部消息</span>
            <span className="inline-flex items-center gap-1 text-[11px] text-[var(--color-text-tertiary)] transition-colors group-hover:text-[var(--color-nav-bg)]">
              进入收件箱
              <ChevronRight size={14} className="transition-transform group-hover:translate-x-0.5 motion-reduce:transform-none" />
            </span>
          </button>
          {stateMutation.isPending && (
            <div className="pointer-events-none absolute inset-x-0 bottom-12 flex justify-center" aria-live="polite">
              <span className="inline-flex items-center gap-1.5 rounded-lg bg-[var(--color-text-primary)] px-3 py-1.5 text-[11px] text-[var(--color-bg-base)] shadow-lg"><Loader2 size={11} className="animate-spin motion-reduce:animate-none" />正在更新</span>
            </div>
          )}
        </section>
      )}
    </>
  )
}

function InboxPopoverItem({
  item,
  busy,
  onOpen,
  onMarkRead,
  onArchive,
}: {
  item: InboxDelivery
  busy: boolean
  onOpen: () => void
  onMarkRead: () => void
  onArchive: () => void
}) {
  const isOpen = item.businessState === 'open'
  const isResolved = item.businessState === 'resolved'
  const sourceName = String(
    item.safeContext.pipelineName
      || item.safeContext.taskName
      || item.resource.label
      || '数据任务池',
  )
  const stateLabel = isOpen
    ? '待恢复'
    : isResolved
      ? '已恢复'
      : item.businessState === 'cancelled'
        ? '已取消'
        : '已过期'
  const stateTone = isOpen
    ? 'bg-[var(--color-danger-bg)] text-[var(--color-danger)]'
    : isResolved
      ? 'bg-[var(--color-success-bg)] text-[var(--color-success)]'
      : 'bg-[var(--color-bg-hover)] text-[var(--color-text-secondary)]'
  const iconTone = isOpen
    ? 'bg-[var(--color-danger-bg)] text-[var(--color-danger)]'
    : isResolved
      ? 'bg-[var(--color-success-bg)] text-[var(--color-success)]'
      : 'bg-[var(--color-bg-hover)] text-[var(--color-text-tertiary)]'

  return (
    <article
      role="listitem"
      className={`group relative transition-colors duration-200 hover:bg-[var(--color-bg-hover)] ${
        item.deliveryState === 'unread' ? 'bg-[color-mix(in_srgb,var(--color-nav-bg)_7%,transparent)]' : ''
      }`}
    >
      {item.deliveryState === 'unread' && (
        <span className="absolute inset-y-4 left-0 w-0.5 rounded-r-full bg-[var(--color-nav-bg)]" aria-hidden="true" />
      )}

      <button
        type="button"
        onClick={onOpen}
        aria-label={`打开消息：${item.title}`}
        className="grid w-full grid-cols-[40px_minmax(0,1fr)] gap-3 px-4 pb-2 pt-4 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring"
      >
        <span className={`relative grid h-10 w-10 shrink-0 place-items-center rounded-xl ${iconTone}`}>
          {isOpen
            ? <AlertTriangle size={18} aria-hidden="true" />
            : isResolved
              ? <CheckCircle2 size={18} aria-hidden="true" />
              : item.businessState === 'cancelled'
                ? <CircleSlash2 size={18} aria-hidden="true" />
                : <Clock3 size={18} aria-hidden="true" />}
          {item.deliveryState === 'unread' && (
            <span className="absolute -right-0.5 -top-0.5 h-2.5 w-2.5 rounded-full border-2 border-[var(--color-popover)] bg-[var(--color-nav-bg)]" aria-hidden="true" />
          )}
        </span>

        <span className="min-w-0">
          <span className="flex min-w-0 items-center justify-between gap-3">
            <span className="flex min-w-0 items-center gap-1.5">
              <span className={`inline-flex h-6 shrink-0 items-center rounded-md px-2 text-[11px] font-medium ${stateTone}`}>
                {stateLabel}
              </span>
              {item.deliveryState === 'unread' && (
                <span className="shrink-0 text-[11px] font-medium text-[var(--color-nav-bg)]">未读</span>
              )}
              {item.occurrenceCount > 1 && (
                <span className="truncate rounded-md bg-[var(--color-warning-bg)] px-2 py-1 text-[11px] font-medium tabular-nums text-[var(--color-warning)]">
                  连续失败 {item.occurrenceCount} 次
                </span>
              )}
            </span>
            <time
              dateTime={item.lastOccurredAt}
              title={formatExactTime(item.lastOccurredAt)}
              className="shrink-0 text-[11px] tabular-nums text-[var(--color-text-tertiary)]"
            >
              {formatRelativeTime(item.lastOccurredAt)}
            </time>
          </span>

          <span className="mt-1.5 flex min-w-0 items-start gap-1.5">
            <span className="line-clamp-2 min-w-0 flex-1 text-[14px] font-semibold leading-5 text-[var(--color-text-primary)]" title={item.title}>
              {item.title}
            </span>
            <ChevronRight
              size={15}
              className="mt-0.5 shrink-0 text-[var(--color-text-disabled)] transition-[transform,color] duration-200 group-hover:translate-x-0.5 group-hover:text-[var(--color-text-secondary)] motion-reduce:transform-none"
              aria-hidden="true"
            />
          </span>
          <span className="mt-1 line-clamp-2 text-xs leading-[18px] text-[var(--color-text-secondary)]" title={item.summary}>
            {item.summary}
          </span>
        </span>
      </button>

      <footer className="grid grid-cols-[40px_minmax(0,1fr)] gap-3 px-4 pb-3">
        <span aria-hidden="true" />
        <span className="flex min-w-0 items-center justify-between gap-2">
          <span
            className="inline-flex min-w-0 items-center gap-1.5 text-[11px] text-[var(--color-text-secondary)]"
            title={sourceName}
          >
            <Workflow size={13} className="shrink-0 text-[var(--color-text-tertiary)]" aria-hidden="true" />
            <span className="truncate">{sourceName}</span>
          </span>
          <span className="flex shrink-0 items-center gap-1">
            {item.deliveryState === 'unread' && (
              <button
                type="button"
                disabled={busy}
                onClick={onMarkRead}
                className="inline-flex h-11 items-center gap-1.5 rounded-lg px-2.5 text-[11px] font-medium text-[var(--color-text-secondary)] transition-colors duration-200 hover:bg-[var(--color-popover)] hover:text-[var(--color-text-primary)] hover:shadow-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50"
                aria-label={`将“${item.title}”标为已读`}
              >
                <MailOpen size={13} aria-hidden="true" />
                设为已读
              </button>
            )}
            {item.canArchive && (
              <button
                type="button"
                disabled={busy}
                onClick={onArchive}
                className="inline-flex h-11 items-center gap-1.5 rounded-lg px-2.5 text-[11px] font-medium text-[var(--color-text-secondary)] transition-colors duration-200 hover:bg-[var(--color-popover)] hover:text-[var(--color-text-primary)] hover:shadow-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50"
                aria-label={`归档“${item.title}”`}
              >
                <Archive size={13} aria-hidden="true" />
                归档
              </button>
            )}
          </span>
        </span>
      </footer>
    </article>
  )
}
