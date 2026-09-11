import { formatDateTime } from '@/utils/datetime'
import { useMemo, useState, type ReactNode } from 'react'
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import {
  AlertTriangle,
  Archive,
  CheckCircle2,
  ChevronRight,
  Inbox,
  Loader2,
  Mail,
  MailOpen,
  RefreshCw,
} from 'lucide-react'

import { inboxApi, type InboxDelivery, type InboxTab } from '@/api/inbox'


const TABS: Array<{ key: InboxTab; label: string }> = [
  { key: 'actionable', label: '处理中' },
  { key: 'unread', label: '未读' },
  { key: 'resolved', label: '已恢复' },
  { key: 'all', label: '全部' },
  { key: 'archived', label: '已归档' },
]

function formatTime(value: string): string {
  const date = new Date(value)
  return Number.isFinite(date.getTime())
    ? formatDateTime(date, { seconds: true })
    : value
}

export default function InboxPage() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [tab, setTab] = useState<InboxTab>('actionable')

  const summary = useQuery({
    queryKey: ['inbox', 'summary'],
    queryFn: inboxApi.summary,
    refetchInterval: 15_000,
  })
  const messages = useInfiniteQuery({
    queryKey: ['inbox', 'page', tab],
    initialPageParam: null as string | null,
    queryFn: ({ pageParam }) => inboxApi.list({ tab, cursor: pageParam, limit: 20 }),
    getNextPageParam: page => page.nextCursor || undefined,
  })
  const update = useMutation({
    mutationFn: ({ id, state }: { id: string; state: 'read' | 'unread' | 'archived' }) =>
      inboxApi.updateState(id, state),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ['inbox'] }),
  })

  const items = useMemo(
    () => messages.data?.pages.flatMap(page => page.items) ?? [],
    [messages.data],
  )

  const openItem = async (item: InboxDelivery) => {
    if (item.deliveryState === 'unread') {
      try { await inboxApi.updateState(item.id, 'read') } catch { /* deep link remains useful */ }
      void queryClient.invalidateQueries({ queryKey: ['inbox'] })
    }
    navigate(item.actions[0]?.href || item.resource.href)
  }

  return (
    <div className="mx-auto flex w-full max-w-6xl flex-col gap-5">
      <header className="flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <div className="mb-2 inline-flex items-center gap-2 rounded-full border border-brand-line bg-brand-soft px-3 py-1 text-[11px] font-medium text-brand-ink">
            <Inbox size={13} />个人运行收件箱
          </div>
          <h1 className="text-2xl font-semibold tracking-tight text-foreground">任务告警与恢复记录</h1>
          <p className="mt-1.5 text-sm text-muted-foreground">阅读只表示你已知晓；任务下一次执行成功后，故障才会自动恢复。</p>
        </div>
        <button
          type="button"
          onClick={() => { void summary.refetch(); void messages.refetch() }}
          disabled={messages.isFetching}
          className="inline-flex h-10 items-center justify-center gap-2 self-start rounded-lg border border-border bg-card px-4 text-xs font-medium text-muted-foreground shadow-sm transition hover:border-brand-line hover:bg-brand-soft hover:text-brand-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50 sm:self-auto"
        >
          <RefreshCw size={14} className={messages.isFetching ? 'animate-spin' : ''} />刷新
        </button>
      </header>

      <section className="grid grid-cols-2 gap-3 lg:grid-cols-4" aria-label="收件箱统计">
        <Metric label="未恢复故障" value={summary.data?.openAlertCount ?? 0} tone="rose" icon={<AlertTriangle size={15} />} />
        <Metric label="需要处理" value={summary.data?.actionableCount ?? 0} tone="amber" icon={<Inbox size={15} />} />
        <Metric label="未读消息" value={summary.data?.unreadCount ?? 0} tone="blue" icon={<Mail size={15} />} />
        <Metric label="已恢复" value={summary.data?.resolvedCount ?? 0} tone="emerald" icon={<CheckCircle2 size={15} />} />
      </section>

      <section className="overflow-hidden rounded-2xl border border-border bg-card shadow-sm">
        <div className="flex items-center gap-1 overflow-x-auto border-b border-border px-3 pt-3 sm:px-5" role="tablist" aria-label="收件箱筛选">
          {TABS.map(item => (
            <button
              key={item.key}
              type="button"
              role="tab"
              aria-selected={tab === item.key}
              onClick={() => setTab(item.key)}
              className={`relative h-10 shrink-0 px-3 text-xs font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${tab === item.key ? 'text-brand-ink' : 'text-muted-foreground hover:text-foreground'}`}
            >
              {item.label}
              {tab === item.key && <span className="absolute inset-x-2 bottom-0 h-0.5 rounded-full bg-brand" />}
            </button>
          ))}
        </div>

        {messages.isLoading ? (
          <div className="space-y-3 p-5" aria-label="正在加载收件箱">
            {[0, 1, 2, 3].map(index => <div key={index} className="h-32 animate-pulse rounded-xl bg-muted" />)}
          </div>
        ) : messages.isError ? (
          <div role="alert" className="flex min-h-72 flex-col items-center justify-center px-6 text-center">
            <AlertTriangle size={28} className="mb-3 text-viz-rose" />
            <p className="text-sm font-medium text-foreground">收件箱加载失败</p>
            <p className="mt-1 text-xs text-muted-foreground">请检查服务状态后重新加载。</p>
            <button type="button" onClick={() => void messages.refetch()} className="mt-4 inline-flex h-9 items-center gap-1.5 rounded-lg bg-muted px-3 text-xs text-foreground hover:bg-[var(--color-bg-active)]"><RefreshCw size={13} />重试</button>
          </div>
        ) : items.length === 0 ? (
          <Empty tab={tab} />
        ) : (
          <div className="divide-y border-border">
            {items.map(item => (
              <InboxRecord
                key={item.id}
                item={item}
                busy={update.isPending}
                onOpen={() => void openItem(item)}
                onState={state => update.mutate({ id: item.id, state })}
              />
            ))}
            {messages.hasNextPage && (
              <div className="flex justify-center p-4">
                <button
                  type="button"
                  onClick={() => void messages.fetchNextPage()}
                  disabled={messages.isFetchingNextPage}
                  className="inline-flex h-9 items-center gap-2 rounded-lg border border-border bg-card px-4 text-xs text-muted-foreground transition hover:border-brand-line hover:text-brand-ink disabled:opacity-50"
                >
                  {messages.isFetchingNextPage && <Loader2 size={13} className="animate-spin" />}
                  加载更多
                </button>
              </div>
            )}
          </div>
        )}
      </section>
    </div>
  )
}

function Metric({ label, value, tone, icon }: {
  label: string
  value: number
  tone: 'rose' | 'amber' | 'blue' | 'emerald'
  icon: ReactNode
}) {
  const styles = {
    rose: 'bg-viz-rose-soft text-viz-rose border-viz-rose-soft',
    amber: 'bg-[var(--color-warning-bg)] text-[var(--color-warning)] border-[color-mix(in_srgb,var(--color-warning)_35%,transparent)]',
    blue: 'bg-[var(--color-info-bg)] text-[var(--color-info)] border-[color-mix(in_srgb,var(--color-info)_35%,transparent)]',
    emerald: 'bg-[var(--color-success-bg)] text-[var(--color-success)] border-[color-mix(in_srgb,var(--color-success)_35%,transparent)]',
  }[tone]
  return (
    <div className="flex items-center gap-3 rounded-xl border border-border bg-card p-4 shadow-sm">
      <span className={`grid h-9 w-9 place-items-center rounded-lg border ${styles}`}>{icon}</span>
      <div>
        <p className="text-xl font-semibold leading-none tabular-nums text-foreground">{value}</p>
        <p className="mt-1.5 text-[11px] text-muted-foreground">{label}</p>
      </div>
    </div>
  )
}

function InboxRecord({ item, busy, onOpen, onState }: {
  item: InboxDelivery
  busy: boolean
  onOpen: () => void
  onState: (state: 'read' | 'unread' | 'archived') => void
}) {
  const isOpen = item.businessState === 'open'
  return (
    <article className={`p-4 transition-colors sm:p-5 ${item.deliveryState === 'unread' ? 'bg-viz-rose-soft' : 'hover:bg-muted'}`}>
      <div className="flex items-start gap-3 sm:gap-4">
        <span className={`relative grid h-10 w-10 shrink-0 place-items-center rounded-xl ${isOpen ? 'bg-viz-rose-soft text-viz-rose' : 'bg-[var(--color-success-bg)] text-[var(--color-success)]'}`}>
          {isOpen ? <AlertTriangle size={18} /> : <CheckCircle2 size={18} />}
          {item.deliveryState === 'unread' && <span className="absolute -right-0.5 -top-0.5 h-2.5 w-2.5 rounded-full border-2 border-border bg-viz-rose" />}
        </span>
        <div className="min-w-0 flex-1">
          <div className="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
            <div>
              <div className="flex flex-wrap items-center gap-2">
                <h2 className="text-sm font-semibold text-foreground">{item.title}</h2>
                <span className={`rounded-full px-2 py-0.5 text-[10px] font-medium ${isOpen ? 'bg-viz-rose-soft text-viz-rose' : 'bg-[var(--color-success-bg)] text-[var(--color-success)]'}`}>{isOpen ? '处理中' : '已恢复'}</span>
                {item.occurrenceCount > 1 && <span className="rounded-full bg-[var(--color-warning-bg)] px-2 py-0.5 text-[10px] font-medium text-[var(--color-warning)]">连续失败 {item.occurrenceCount} 次</span>}
              </div>
              <p className="mt-1.5 text-sm leading-6 text-muted-foreground">{item.summary}</p>
            </div>
            <time className="shrink-0 text-[11px] tabular-nums text-[var(--color-text-tertiary)]">{formatTime(item.lastOccurredAt)}</time>
          </div>

          <div className="mt-3 grid gap-2 rounded-xl border border-border bg-muted p-3 text-xs sm:grid-cols-3">
            <div><span className="text-[var(--color-text-tertiary)]">数据流水线</span><p className="mt-0.5 truncate font-medium text-foreground" title={String(item.safeContext.pipelineName || '')}>{String(item.safeContext.pipelineName || '—')}</p></div>
            <div><span className="text-[var(--color-text-tertiary)]">触发方式</span><p className="mt-0.5 font-medium text-foreground">{item.safeContext.triggerType === 'scheduled' ? '计划调度' : '手动触发'}</p></div>
            <div><span className="text-[var(--color-text-tertiary)]">故障周期</span><p className="mt-0.5 font-medium text-foreground">首次 {formatTime(item.firstOccurredAt)}</p></div>
          </div>

          <div className="mt-3 flex flex-wrap items-center justify-end gap-2">
            <button
              type="button"
              onClick={() => onState(item.deliveryState === 'unread' ? 'read' : 'unread')}
              disabled={busy}
              className="inline-flex h-9 items-center gap-1.5 rounded-lg px-3 text-xs text-muted-foreground transition hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50"
            >
              {item.deliveryState === 'unread' ? <MailOpen size={13} /> : <Mail size={13} />}
              {item.deliveryState === 'unread' ? '标为已读' : '标为未读'}
            </button>
            {item.canArchive && (
              <button type="button" onClick={() => onState('archived')} disabled={busy} className="inline-flex h-9 items-center gap-1.5 rounded-lg px-3 text-xs text-muted-foreground transition hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50">
                <Archive size={13} />归档
              </button>
            )}
            <button type="button" onClick={onOpen} className="inline-flex h-9 items-center gap-1.5 rounded-lg bg-brand px-3.5 text-xs font-medium text-[var(--color-text-inverse)] shadow-sm transition hover:bg-brand-deep focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2">
              查看执行记录 <ChevronRight size={13} />
            </button>
          </div>
        </div>
      </div>
    </article>
  )
}

function Empty({ tab }: { tab: InboxTab }) {
  const copy = tab === 'actionable'
    ? ['当前没有未恢复故障', '数据任务运行正常，后续失败会自动出现在这里。']
    : tab === 'archived'
      ? ['还没有归档记录', '已恢复的消息可以在确认后归档。']
      : ['当前分类没有消息', '切换其他分类查看收件箱记录。']
  return (
    <div className="flex min-h-72 flex-col items-center justify-center px-6 text-center">
      <span className="mb-3 grid h-12 w-12 place-items-center rounded-xl bg-[var(--color-success-bg)] text-[var(--color-success)]"><CheckCircle2 size={22} /></span>
      <p className="text-sm font-medium text-foreground">{copy[0]}</p>
      <p className="mt-1 text-xs text-[var(--color-text-tertiary)]">{copy[1]}</p>
    </div>
  )
}
