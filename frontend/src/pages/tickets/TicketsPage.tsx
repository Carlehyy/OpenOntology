import { useEffect, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import {
  ChevronLeft, ChevronRight, Inbox, Loader2, Megaphone, Paperclip,
  Plus, RefreshCcw, Search,
} from 'lucide-react'
import { useAuthStore } from '@/stores/authStore'
import { useDebouncedValue } from '@/utils/useDebouncedValue'
import {
  fmtTime, ticketsApi, TICKET_STATUS_META, TICKET_STATUS_ORDER,
  type TicketStatus,
} from '@/api/tickets'
import TicketFormModal from '@/components/tickets/TicketFormModal'
import TicketDetailDrawer from './TicketDetailDrawer'
import { CategoryBadge, StatusBadge } from './shared'
import { KpiStatCard } from '@/components/KpiStatCard'

// 与「事件登记」一致的基础面板：白底、细边框、轻阴影。
const PANEL = 'rounded-xl border border-border bg-card shadow-sm/50'
const PAGE_SIZE = 8

const STATUS_TABS: Array<{ value: TicketStatus | 'all'; label: string }> = [
  { value: 'all', label: '全部' },
  ...TICKET_STATUS_ORDER.map(status => ({ value: status, label: TICKET_STATUS_META[status].label })),
]

export default function TicketsPage() {
  const isAdmin = useAuthStore(s => s.user?.role === 'admin')
  const username = useAuthStore(s => s.user?.username)
  const [searchParams, setSearchParams] = useSearchParams()
  const [search, setSearch] = useState('')
  const debouncedSearch = useDebouncedValue(search, 300)
  const [status, setStatus] = useState<TicketStatus | 'all'>('all')
  const [page, setPage] = useState(1)
  const [formOpen, setFormOpen] = useState(false)
  const [detailTicketId, setDetailTicketId] = useState<string | null>(null)

  const statsQ = useQuery({ queryKey: ['tickets', 'stats'], queryFn: () => ticketsApi.stats() })
  const listQ = useQuery({
    queryKey: ['tickets', 'list', { page, q: debouncedSearch.trim() || undefined, status }],
    queryFn: () => ticketsApi.list({
      page, pageSize: PAGE_SIZE,
      q: debouncedSearch.trim() || undefined,
      status: status === 'all' ? undefined : status,
    }),
  })

  useEffect(() => { setPage(1) }, [debouncedSearch, status])

  // 顶栏工单弹窗带 ?focus=<id> 深链进入时，直接打开对应工单详情
  useEffect(() => {
    const focus = searchParams.get('focus')
    if (!focus) return
    setDetailTicketId(focus)
    setSearchParams({}, { replace: true })
  }, [searchParams, setSearchParams])

  const totalPages = Math.max(1, Math.ceil((listQ.data?.total ?? 0) / PAGE_SIZE))
  const refresh = () => { statsQ.refetch(); listQ.refetch() }
  const stats = statsQ.data

  return (
    <div className="flex h-full flex-col gap-3 overflow-hidden bg-[var(--color-bg-base)] p-6">
      {/* 顶部操作：状态筛选 + 提交工单 */}
      <div className={`${PANEL} shrink-0 px-4 py-3`}>
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex items-center gap-1 rounded-lg border border-border bg-muted p-1 text-sm">
            {STATUS_TABS.map(tab => (
              <button
                key={tab.value}
                type="button"
                onClick={() => setStatus(tab.value)}
                aria-pressed={status === tab.value}
                className={`relative z-10 inline-flex items-center gap-1 rounded-md px-3.5 py-2 font-medium transition-colors duration-200 ${
                  status === tab.value
                    ? 'bg-[var(--color-success)] text-[var(--color-text-inverse)] shadow-sm'
                    : 'text-muted-foreground hover:text-[var(--color-success)]'
                }`}
              >
                {tab.label}
              </button>
            ))}
          </div>

          <div className="ml-auto flex flex-wrap items-center justify-end gap-2">
            {!isAdmin && (
              <span className="hidden text-xs text-[var(--color-text-tertiary)] sm:inline">
                展示我提交的工单（{username}）
              </span>
            )}
            {isAdmin && (
              <span className="hidden text-xs text-[var(--color-text-tertiary)] sm:inline">展示全部用户的工单</span>
            )}
            <button
              type="button"
              onClick={() => setFormOpen(true)}
              className="inline-flex h-9 items-center justify-center gap-1.5 rounded-lg bg-[var(--color-success)] px-3 text-sm font-medium text-[var(--color-text-inverse)] shadow-sm transition-colors hover:bg-[var(--color-success)] active:bg-[var(--color-success)]"
            >
              <Plus className="h-4 w-4" />提交工单
            </button>
          </div>
        </div>
      </div>

      {/* 按状态总览 */}
      <div className="grid shrink-0 grid-cols-2 gap-2 md:grid-cols-3 xl:grid-cols-6">
        <MetricCard label="工单总数" value={stats?.total ?? 0} icon={<Inbox className="h-4 w-4" />} loading={statsQ.isLoading} />
        {TICKET_STATUS_ORDER.map(value => (
          <MetricCard
            key={value}
            label={TICKET_STATUS_META[value].label}
            value={stats?.byStatus?.[value] ?? 0}
            dot={<span className={`h-2 w-2 rounded-full ${TICKET_STATUS_META[value].dot}`} />}
            loading={statsQ.isLoading}
          />
        ))}
      </div>

      {/* 筛选栏 */}
      <div className={`${PANEL} flex shrink-0 flex-wrap items-center gap-2 px-4 py-3`}>
        <div className="relative min-w-[220px] max-w-[340px] flex-1">
          <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-[var(--color-text-tertiary)]" />
          <input
            value={search}
            onChange={event => setSearch(event.target.value)}
            placeholder="搜索工单标题、内容、编号、提交人..."
            className="w-full rounded-lg border border-border bg-card py-2 pl-9 pr-3 text-sm text-foreground outline-none transition focus-visible:border-ring focus-visible:ring-2 focus-visible:ring-ring placeholder:text-[var(--color-text-tertiary)]"
          />
        </div>
        <div className="ml-auto flex items-center gap-1">
          <span className="mr-1 text-sm text-[var(--color-text-tertiary)]">
            共 <span className="font-semibold tabular-nums text-foreground">{listQ.data?.total ?? 0}</span> 条
          </span>
          <button
            type="button"
            onClick={refresh}
            className="rounded-md p-2 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
            title="刷新"
            aria-label="刷新工单列表"
          >
            <RefreshCcw className={`h-4 w-4 ${statsQ.isFetching || listQ.isFetching ? 'animate-spin' : ''}`} />
          </button>
        </div>
      </div>

      {/* 表格区 */}
      <div className={`${PANEL} flex min-h-0 flex-1 flex-col overflow-hidden`}>
        <div className="thin-scroll min-h-0 flex-1 overflow-auto">
          <table className="w-full min-w-[880px] table-fixed text-sm">
            <thead>
              <tr className="sticky top-0 z-10 border-b border-border bg-card text-sm text-muted-foreground">
                <th className="w-[30%] px-4 py-3 text-left font-medium">工单</th>
                <th className="w-[14%] px-3 py-3 text-center font-medium">提交人</th>
                <th className="w-[11%] px-3 py-3 text-center font-medium">状态</th>
                <th className="w-[22%] px-3 py-3 text-left font-medium">反馈内容</th>
                <th className="w-[10%] px-3 py-3 text-center font-medium">附件</th>
                <th className="w-[13%] px-3 py-3 text-center font-medium">提交时间</th>
              </tr>
            </thead>
            <tbody>
              {listQ.isLoading ? (
                <tr><td colSpan={6} className="py-16 text-center text-sm text-[var(--color-text-tertiary)]">加载中...</td></tr>
              ) : listQ.data?.items?.length === 0 ? (
                <tr><td colSpan={6} className="py-16 text-center">
                  <div className="mx-auto mb-3 flex h-12 w-12 items-center justify-center rounded-xl bg-muted">
                    <Megaphone className="h-5 w-5 text-[var(--color-text-tertiary)]" />
                  </div>
                  <p className="text-sm text-[var(--color-text-tertiary)]">暂无工单</p>
                  <p className="mt-1 text-xs text-[var(--color-text-tertiary)]">遇到了 Bug 或不好用的地方？提交工单告诉我们</p>
                  <button
                    onClick={() => setFormOpen(true)}
                    className="mt-3 inline-flex items-center gap-1 rounded-lg bg-[var(--color-success-bg)] px-3 py-1.5 text-sm font-medium text-[var(--color-success)] transition-colors hover:bg-[var(--color-success-bg)]"
                  >
                    <Plus className="h-3.5 w-3.5" />立即提交
                  </button>
                </td></tr>
              ) : (listQ.data?.items ?? []).map((row, index) => (
                <tr
                  key={row.id}
                  tabIndex={0}
                  onClick={() => setDetailTicketId(row.id)}
                  onKeyDown={event => {
                    if (event.key === 'Enter' || event.key === ' ') {
                      event.preventDefault()
                      setDetailTicketId(row.id)
                    }
                  }}
                  className="group cursor-pointer border-t border-border transition-colors hover:bg-muted focus-visible:bg-muted focus-visible:outline-none"
                  style={{ animation: `rowIn 0.35s ease-out ${index * 30}ms both` }}
                >
                  <td className="px-4 py-3 text-left align-middle">
                    <div className="flex items-center gap-1.5">
                      <span className="truncate font-medium text-foreground" title={row.title}>{row.title}</span>
                      <CategoryBadge category={row.category} />
                    </div>
                    <div className="mt-0.5 font-mono text-xs text-[var(--color-text-tertiary)]">{row.ticketNo}</div>
                  </td>
                  <td className="px-3 py-3 text-center align-middle">
                    <span className="truncate text-sm text-muted-foreground">{row.submitterName || '—'}</span>
                  </td>
                  <td className="px-3 py-3 text-center align-middle">
                    <div className="flex justify-center"><StatusBadge status={row.status} /></div>
                  </td>
                  <td className="max-w-0 px-3 py-3 text-left align-middle text-muted-foreground">
                    <div className="truncate" title={row.content}>{row.content || <span className="italic text-[var(--color-text-tertiary)]">无内容</span>}</div>
                  </td>
                  <td className="px-3 py-3 text-center align-middle">
                    {row.attachmentCount && row.attachmentCount > 0 ? (
                      <span className="inline-flex items-center gap-1 whitespace-nowrap text-sm text-muted-foreground" title={`${row.attachmentCount} 个附件`}>
                        <Paperclip size={14} /> {row.attachmentCount}
                      </span>
                    ) : <span className="text-sm text-[var(--color-text-tertiary)]">—</span>}
                  </td>
                  <td className="whitespace-nowrap px-3 py-3 text-center align-middle text-sm tabular-nums text-muted-foreground">{fmtTime(row.createdAt)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        {/* 分页 */}
        <div className="flex shrink-0 items-center justify-between border-t border-border bg-card px-4 py-2">
          <div className="text-sm tabular-nums text-[var(--color-text-tertiary)]">
            显示 {(page - 1) * PAGE_SIZE + 1}–{Math.min(page * PAGE_SIZE, listQ.data?.total ?? 0)} / {listQ.data?.total ?? 0}
          </div>
          <div className="flex items-center gap-1">
            <button
              disabled={page <= 1}
              onClick={() => setPage(current => Math.max(1, current - 1))}
              className="flex h-7 w-7 items-center justify-center rounded-lg border border-border bg-card text-muted-foreground transition-colors hover:bg-muted disabled:opacity-40"
              aria-label="上一页"
            >
              <ChevronLeft className="w-3.5 h-3.5" />
            </button>
            {Array.from({ length: Math.min(totalPages, 5) }).map((_, index) => {
              let target = index + 1
              if (totalPages > 5) { if (page > 3) target = Math.min(totalPages - 4, page - 2) + index }
              return (
                <button
                  key={target}
                  onClick={() => setPage(target)}
                  className={`flex h-8 w-8 items-center justify-center rounded-lg text-sm font-medium transition-colors ${
                    target === page ? 'bg-[var(--color-success)] text-[var(--color-text-inverse)] shadow-sm' : 'border border-border bg-card text-muted-foreground hover:bg-muted'
                  }`}
                >
                  {target}
                </button>
              )
            })}
            <button
              disabled={page >= totalPages}
              onClick={() => setPage(current => Math.min(totalPages, current + 1))}
              className="flex h-7 w-7 items-center justify-center rounded-lg border border-border bg-card text-muted-foreground transition-colors hover:bg-muted disabled:opacity-40"
              aria-label="下一页"
            >
              <ChevronRight className="w-3.5 h-3.5" />
            </button>
          </div>
        </div>
      </div>

      {/* 移动端 FAB */}
      <button
        onClick={() => setFormOpen(true)}
        className="fixed bottom-6 right-6 z-20 flex h-11 w-11 items-center justify-center rounded-full bg-[var(--color-success)] text-[var(--color-text-inverse)] shadow-lg transition-colors hover:bg-[var(--color-success)] md:hidden"
        aria-label="提交工单"
      >
        <Plus className="w-5 h-5" />
      </button>

      <TicketFormModal open={formOpen} onClose={() => setFormOpen(false)} />
      <TicketDetailDrawer
        open={Boolean(detailTicketId)}
        ticketId={detailTicketId}
        onClose={() => setDetailTicketId(null)}
      />

      <style>{`
        @keyframes rowIn {
          from { opacity: 0; transform: translateY(4px); }
          to { opacity: 1; transform: translateY(0); }
        }
        .thin-scroll::-webkit-scrollbar { width: 5px; height: 5px; }
        .thin-scroll::-webkit-scrollbar-thumb { background: rgba(148,163,184,0.3); border-radius: 5px; }
        .thin-scroll::-webkit-scrollbar-thumb:hover { background: rgba(148,163,184,0.5); }
        .thin-scroll::-webkit-scrollbar-track { background: transparent; }
      `}</style>
    </div>
  )
}

// ─── 小型指标卡 ──────────────────────────────────────────
function MetricCard({
  label, value, icon, dot, loading,
}: {
  label: string
  value: number
  icon?: React.ReactNode
  dot?: React.ReactNode
  loading?: boolean
}) {
  return (
    <KpiStatCard
      label={label}
      value={loading ? <Loader2 className="h-5 w-5 animate-spin text-[var(--color-text-tertiary)]" /> : value.toLocaleString()}
      icon={dot ?? icon}
    />
  )
}
