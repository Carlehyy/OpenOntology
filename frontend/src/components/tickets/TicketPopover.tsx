import { useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  CheckCircle2, ChevronRight, Loader2, Megaphone, Plus, RefreshCw, Ticket,
} from 'lucide-react'

import { ticketsApi, type TicketItem } from '@/api/tickets'
import { StatusBadge } from '@/pages/tickets/shared'
import TicketFormModal from './TicketFormModal'

/** 处理中 = 未到终态：待处理 + 查验中 + 已接纳。 */
const IN_PROGRESS_STATUSES = 'pending,verifying,accepted'
const RECENT_LIMIT = 10

function formatRelativeTime(value: string): string {
  const time = new Date(value).getTime()
  if (!Number.isFinite(time)) return value
  const diff = Math.max(0, Date.now() - time)
  if (diff < 60_000) return '刚刚'
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)} 分钟前`
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)} 小时前`
  if (diff < 604_800_000) return `${Math.floor(diff / 86_400_000)} 天前`
  return new Date(value).toLocaleDateString('zh-CN')
}

/**
 * 顶栏工单弹窗：与收件箱同构的个人反馈入口。
 * 展示最近 10 条处理中的工单（普通用户见自己、管理员见全部），
 * 底部可一键进入工单页，或直接在任意页面提交新工单。
 */
export default function TicketPopover({
  open,
  onOpenChange,
  onNavigate,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  onNavigate: (href: string) => void
}) {
  const [formOpen, setFormOpen] = useState(false)

  const list = useQuery({
    queryKey: ['tickets', 'popover'],
    queryFn: () => ticketsApi.list({ status: IN_PROGRESS_STATUSES, pageSize: RECENT_LIMIT }),
    enabled: open,
    refetchOnMount: 'always',
  })

  useEffect(() => {
    if (!open) return
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onOpenChange(false)
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [onOpenChange, open])

  const items: TicketItem[] = list.data?.items ?? []

  const openTicket = (ticket: TicketItem) => {
    onOpenChange(false)
    onNavigate(`/tickets?focus=${ticket.id}`)
  }

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
        aria-label={`工单反馈${items.length ? `，${items.length} 条处理中` : ''}`}
        aria-expanded={open}
        aria-controls="global-ticket-popover"
        title="工单反馈"
      >
        <Ticket size={21} strokeWidth={1.8} />
      </button>

      {open && (
        <section
          id="global-ticket-popover"
          aria-label="工单反馈"
          className="fixed left-3 right-3 top-[58px] z-50 origin-top-right overflow-hidden rounded-2xl border border-[var(--color-border)] bg-[var(--color-popover)] shadow-[var(--shadow-lg)] animate-slide-up motion-reduce:animate-none sm:absolute sm:left-auto sm:right-0 sm:top-auto sm:mt-2 sm:w-[420px]"
        >
          <header className="flex min-h-[72px] items-center gap-3 border-b border-[var(--color-border)] px-4 py-3.5">
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-2">
                <h2 className="text-[15px] font-semibold tracking-tight text-[var(--color-text-primary)]">工单反馈</h2>
                {items.length > 0 && (
                  <span className="inline-flex items-center gap-1 rounded-md bg-[var(--color-nav-light)] px-2 py-1 text-[11px] font-medium tabular-nums text-[var(--color-nav-bg)]">
                    <Ticket size={11} aria-hidden="true" />
                    {items.length} 条处理中
                  </span>
                )}
              </div>
              <p className="mt-1 text-xs leading-4 text-[var(--color-text-secondary)]">
                遇到 Bug 或不好用的地方？提交工单告诉我们
              </p>
            </div>
            <button
              type="button"
              onClick={() => void list.refetch()}
              disabled={list.isFetching}
              className="grid h-11 w-11 shrink-0 place-items-center rounded-xl text-[var(--color-text-tertiary)] transition-colors duration-200 hover:bg-[var(--color-bg-hover)] hover:text-[var(--color-text-primary)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50"
              aria-label="刷新工单"
            >
              <RefreshCw size={16} className={list.isFetching ? 'animate-spin motion-reduce:animate-none' : ''} />
            </button>
          </header>

          <div className="scrollbar-thin max-h-[min(520px,calc(100dvh-210px))] overflow-y-auto">
            {list.isLoading ? (
              <div className="space-y-2.5 p-3" aria-label="正在加载工单">
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
                <Megaphone size={24} className="mb-2 text-[var(--color-danger)]" />
                <p className="text-sm font-medium text-[var(--color-text-primary)]">工单加载失败</p>
                <p className="mt-1 text-xs text-[var(--color-text-secondary)]">请检查网络后重新加载</p>
                <button type="button" onClick={() => void list.refetch()} className="mt-3 inline-flex h-11 items-center gap-1.5 rounded-lg bg-[var(--color-bg-hover)] px-3.5 text-xs font-medium text-[var(--color-text-primary)] transition-colors hover:bg-[var(--color-bg-active)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
                  <RefreshCw size={13} />重新加载
                </button>
              </div>
            ) : items.length === 0 ? (
              <div className="flex flex-col items-center px-5 py-10 text-center">
                <span className="mb-3 grid h-11 w-11 place-items-center rounded-xl bg-[var(--color-success-bg)] text-[var(--color-success)]">
                  <CheckCircle2 size={20} />
                </span>
                <p className="text-sm font-medium text-[var(--color-text-primary)]">暂无处理中的工单</p>
                <p className="mt-1 text-xs text-[var(--color-text-tertiary)]">使用中遇到问题，随时可以提单</p>
              </div>
            ) : (
              <div className="divide-y divide-[var(--color-border)]" role="list">
                {items.map(ticket => (
                  <div key={ticket.id} role="listitem">
                    <button
                      type="button"
                      onClick={() => openTicket(ticket)}
                      className="group grid w-full grid-cols-[minmax(0,1fr)_auto] items-start gap-2 px-4 py-3 text-left transition-colors hover:bg-[var(--color-bg-hover)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring"
                      aria-label={`打开工单：${ticket.title}`}
                    >
                    <span className="min-w-0">
                      <span className="flex min-w-0 items-center gap-2">
                        <StatusBadge status={ticket.status} />
                        <span className="truncate text-[14px] font-semibold leading-5 text-[var(--color-text-primary)]" title={ticket.title}>
                          {ticket.title}
                        </span>
                      </span>
                      <span className="mt-1 flex min-w-0 items-center gap-1.5 text-[11px] text-[var(--color-text-tertiary)]">
                        <span className="truncate font-mono">{ticket.ticketNo}</span>
                        {ticket.submitterName && (
                          <>
                            <span aria-hidden="true">·</span>
                            <span className="truncate">{ticket.submitterName}</span>
                          </>
                        )}
                      </span>
                    </span>
                    <span className="flex shrink-0 items-center gap-1 pt-0.5">
                      <time
                        dateTime={ticket.createdAt}
                        className="text-[11px] tabular-nums text-[var(--color-text-tertiary)]"
                      >
                        {formatRelativeTime(ticket.createdAt)}
                      </time>
                      <ChevronRight size={14} className="text-[var(--color-text-disabled)] transition-transform group-hover:translate-x-0.5 motion-reduce:transform-none" aria-hidden="true" />
                    </span>
                    </button>
                  </div>
                ))}
              </div>
            )}
          </div>

          <footer className="flex items-center justify-between gap-2 border-t border-[var(--color-border)] px-3 py-2.5">
            {/* 两个入口统一为纯文字样式：不做悬浮按钮底色/位移等花哨反馈（MYW-80） */}
            <button
              type="button"
              onClick={() => { onOpenChange(false); setFormOpen(true) }}
              className="inline-flex h-10 items-center gap-1 rounded-lg px-3 text-xs font-medium text-[var(--color-nav-bg)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              <Plus size={14} />提交工单
            </button>
            <button
              type="button"
              onClick={() => { onOpenChange(false); onNavigate('/tickets') }}
              className="inline-flex h-10 items-center gap-1 rounded-lg px-3 text-xs font-medium text-[var(--color-nav-bg)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              查看全部工单
              <ChevronRight size={14} aria-hidden="true" />
            </button>
          </footer>
          {list.isFetching && !list.isLoading && (
            <div className="pointer-events-none absolute inset-x-0 top-[72px] flex justify-center" aria-live="polite">
              <span className="inline-flex items-center gap-1.5 rounded-lg bg-[var(--color-text-primary)] px-3 py-1.5 text-[11px] text-[var(--color-bg-base)] shadow-lg"><Loader2 size={11} className="animate-spin motion-reduce:animate-none" />正在刷新</span>
            </div>
          )}
        </section>
      )}

      {/* 任意页面均可打开的提交弹窗；提交成功后刷新弹窗数据 */}
      <TicketFormModal open={formOpen} onClose={() => setFormOpen(false)} />
    </>
  )
}
