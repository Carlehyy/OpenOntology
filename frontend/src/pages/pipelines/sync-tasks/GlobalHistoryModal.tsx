import { formatDateTime } from '@/utils/datetime'
import { useCallback, useEffect, useState } from 'react'
import {
  CheckCircle2, ChevronLeft, ChevronRight, Clock, FilterX, History,
  Loader2, Search, X, XCircle,
} from 'lucide-react'
import {
  pipelineTasksApi,
  type PipelineFilterOption,
  type PipelineTaskGlobalRun,
} from '@/api/v2/pipeline-tasks'

type StatusFilter = '' | 'pending' | 'running' | 'success' | 'failed' | 'cancelled'
type TriggerFilter = '' | 'manual' | 'scheduled'

const PAGE_SIZE_OPTIONS = [10, 20, 50]
const TRIGGER_LABEL: Record<string, string> = { manual: '手动', scheduled: '定时' }
const STATUS_META: Record<string, { label: string; className: string; icon: 'success' | 'failed' | 'running' | 'pending' }> = {
  pending: { label: '排队中', className: 'bg-muted text-muted-foreground', icon: 'pending' },
  running: { label: '执行中', className: 'bg-[var(--color-info-bg)] text-[var(--color-info)]', icon: 'running' },
  success: { label: '成功', className: 'bg-[var(--color-success-bg)] text-[var(--color-success)]', icon: 'success' },
  failed: { label: '失败', className: 'bg-viz-rose-soft text-viz-rose', icon: 'failed' },
  cancelled: { label: '已取消', className: 'bg-[var(--color-warning-bg)] text-[var(--color-warning)]', icon: 'failed' },
}

function formatDate(iso: string | null | undefined): string {
  if (!iso) return '—'
  try { return formatDateTime(new Date(iso), { seconds: true }) } catch { return iso }
}

function formatDuration(start: string | null, end: string | null): string {
  if (!start || !end) return '—'
  const milliseconds = new Date(end).getTime() - new Date(start).getTime()
  if (!Number.isFinite(milliseconds) || milliseconds < 0) return '—'
  if (milliseconds < 1000) return `${milliseconds}ms`
  if (milliseconds < 60_000) return `${(milliseconds / 1000).toFixed(1)}s`
  const minutes = Math.floor(milliseconds / 60_000)
  const seconds = Math.floor((milliseconds % 60_000) / 1000)
  return `${minutes}m ${seconds}s`
}

function StatusBadge({ status }: { status: string }) {
  const meta = STATUS_META[status] || STATUS_META.pending
  const icon = meta.icon === 'success'
    ? <CheckCircle2 size={12} />
    : meta.icon === 'failed'
      ? <XCircle size={12} />
      : meta.icon === 'running'
        ? <Loader2 size={12} className="animate-spin" />
        : <Clock size={12} />
  return (
    <span className={`inline-flex items-center gap-1 rounded-md px-2 py-1 text-[11px] font-medium ${meta.className}`}>
      {icon}{meta.label}
    </span>
  )
}

function LakeImpact({ run }: { run: PipelineTaskGlobalRun }) {
  const impact = run.lake_impact
  if (!impact) return <span className="text-[var(--color-text-tertiary)]">—</span>
  if (impact.added === 0 && impact.updated === 0 && impact.deleted === 0) {
    return <span className="text-[var(--color-text-tertiary)]">无变更</span>
  }
  return (
    <span className="inline-flex items-center justify-center gap-1.5 tabular-nums">
      {impact.added > 0 && <span className="text-[var(--color-success)]">+{impact.added}</span>}
      {impact.updated > 0 && <span className="text-[var(--color-warning)]">~{impact.updated}</span>}
      {impact.deleted > 0 && <span className="text-viz-rose">-{impact.deleted}</span>}
    </span>
  )
}

export default function GlobalHistoryModal({
  pipelineOptions,
  onClose,
}: {
  pipelineOptions: PipelineFilterOption[]
  onClose: () => void
}) {
  const [items, setItems] = useState<PipelineTaskGlobalRun[]>([])
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState('')
  const [reloadKey, setReloadKey] = useState(0)
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(10)
  const [searchInput, setSearchInput] = useState('')
  const [search, setSearch] = useState('')
  const [pipelineId, setPipelineId] = useState('')
  const [statusFilter, setStatusFilter] = useState<StatusFilter>('')
  const [triggerFilter, setTriggerFilter] = useState<TriggerFilter>('')
  const [dateFrom, setDateFrom] = useState('')
  const [dateTo, setDateTo] = useState('')

  useEffect(() => {
    const nextSearch = searchInput.trim()
    // Do not schedule a redundant page reset on mount (or after the
    // debounced value has already caught up).  Otherwise a fast click on
    // "next page" can be undone by the initial 250 ms search timer.
    if (nextSearch === search) return
    const timer = window.setTimeout(() => {
      setSearch(nextSearch)
      setPage(1)
    }, 250)
    return () => window.clearTimeout(timer)
  }, [search, searchInput])

  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', handleKeyDown)
    return () => window.removeEventListener('keydown', handleKeyDown)
  }, [onClose])

  const load = useCallback(() => {
    let active = true
    setLoading(true)
    setLoadError('')
    const createdFrom = dateFrom ? new Date(`${dateFrom}T00:00:00+08:00`).toISOString() : undefined
    const createdTo = dateTo ? new Date(`${dateTo}T23:59:59.999+08:00`).toISOString() : undefined
    pipelineTasksApi.allHistories({
      page,
      page_size: pageSize,
      search: search || undefined,
      pipeline_id: pipelineId || undefined,
      status: statusFilter || undefined,
      trigger_type: triggerFilter || undefined,
      created_from: createdFrom,
      created_to: createdTo,
    })
      .then(response => {
        if (!active) return
        setItems(response.items)
        setTotal(response.total)
        const pages = Math.max(1, Math.ceil(response.total / pageSize))
        if (page > pages) setPage(pages)
      })
      .catch(error => {
        if (!active) return
        setItems([])
        setTotal(0)
        setLoadError(error?.detail || error?.message || '历史执行记录加载失败')
      })
      .finally(() => { if (active) setLoading(false) })
    return () => { active = false }
  }, [dateFrom, dateTo, page, pageSize, pipelineId, reloadKey, search, statusFilter, triggerFilter])

  useEffect(() => load(), [load])

  const resetFilters = () => {
    setSearchInput('')
    setSearch('')
    setPipelineId('')
    setStatusFilter('')
    setTriggerFilter('')
    setDateFrom('')
    setDateTo('')
    setPage(1)
  }

  const totalPages = Math.max(1, Math.ceil(total / pageSize))
  const rangeStart = total === 0 ? 0 : (page - 1) * pageSize + 1
  const rangeEnd = Math.min(total, page * pageSize)
  const hasFilters = Boolean(searchInput || search || pipelineId || statusFilter || triggerFilter || dateFrom || dateTo)

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-accent p-4 backdrop-blur-sm">
      <div
        data-testid="all-history-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="all-history-modal-title"
        className="flex min-h-[min(520px,88vh)] max-h-[min(760px,88vh)] w-full max-w-6xl flex-col overflow-hidden rounded-2xl border border-[color-mix(in_srgb,var(--color-success)_35%,transparent)] bg-card shadow-[0_24px_70px_rgba(6,78,59,0.18)]"
      >
        <div className="relative shrink-0 border-b border-[color-mix(in_srgb,var(--color-success)_35%,transparent)] bg-[var(--color-success-bg)] px-6 py-5">
          <div className="flex min-w-0 items-center gap-3 pr-10">
            <History size={18} className="shrink-0 text-[var(--color-success)]" />
            <h3 id="all-history-modal-title" className="text-lg font-semibold text-foreground">历史记录</h3>
            <p className="min-w-0 truncate text-xs text-[var(--color-text-tertiary)]">
              汇总展示任务池全部执行记录，可按任务、流水线、状态、触发方式和执行日期筛选
            </p>
          </div>
          <button type="button" onClick={onClose} aria-label="关闭历史记录弹窗" className="absolute right-5 top-5 rounded-lg p-1 text-[var(--color-text-tertiary)] transition-colors hover:bg-[var(--color-success-bg)] hover:text-[var(--color-success)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
            <X size={18} />
          </button>
        </div>

        <div className="shrink-0 border-b border-border bg-muted px-6 py-3">
          <div className="flex flex-wrap items-end gap-2.5">
            <label className="min-w-[210px] flex-1 space-y-1 text-[11px] text-muted-foreground">
              <span className="block">任务或流水线</span>
              <span className="relative block">
                <Search size={13} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-[var(--color-text-tertiary)]" />
                <input aria-label="搜索历史任务或流水线" value={searchInput} onChange={event => setSearchInput(event.target.value)} placeholder="输入任务名或流水线名"
                  className="h-8 w-full rounded-lg border border-border bg-card pl-8 pr-7 text-xs text-foreground outline-none transition focus-visible:border-ring focus-visible:ring-2 focus-visible:ring-ring" />
                {searchInput && (
                  <button type="button" aria-label="清除历史搜索" onClick={() => setSearchInput('')} className="absolute right-2 top-1/2 -translate-y-1/2 text-[var(--color-text-tertiary)] hover:text-muted-foreground"><X size={12} /></button>
                )}
              </span>
            </label>
            <label className="space-y-1 text-[11px] text-muted-foreground">
              <span className="block">关联流水线</span>
              <select aria-label="历史记录流水线筛选" value={pipelineId} onChange={event => { setPipelineId(event.target.value); setPage(1) }}
                className="h-8 max-w-[180px] rounded-lg border border-border bg-card px-2 text-xs text-foreground outline-none focus-visible:border-ring">
                <option value="">全部流水线</option>
                {pipelineOptions.map(option => <option key={option.id} value={option.id}>{option.name}</option>)}
              </select>
            </label>
            <label className="space-y-1 text-[11px] text-muted-foreground">
              <span className="block">执行状态</span>
              <select aria-label="全部历史执行状态筛选" value={statusFilter} onChange={event => { setStatusFilter(event.target.value as StatusFilter); setPage(1) }}
                className="h-8 rounded-lg border border-border bg-card px-2 text-xs text-foreground outline-none focus-visible:border-ring">
                <option value="">全部状态</option>
                <option value="pending">排队中</option>
                <option value="running">执行中</option>
                <option value="success">成功</option>
                <option value="failed">失败</option>
                <option value="cancelled">已取消</option>
              </select>
            </label>
            <label className="space-y-1 text-[11px] text-muted-foreground">
              <span className="block">触发方式</span>
              <select aria-label="全部历史触发方式筛选" value={triggerFilter} onChange={event => { setTriggerFilter(event.target.value as TriggerFilter); setPage(1) }}
                className="h-8 rounded-lg border border-border bg-card px-2 text-xs text-foreground outline-none focus-visible:border-ring">
                <option value="">全部方式</option>
                <option value="manual">手动</option>
                <option value="scheduled">定时</option>
              </select>
            </label>
            <label className="space-y-1 text-[11px] text-muted-foreground">
              <span className="block">开始日期</span>
              <input aria-label="全部历史开始日期" type="date" value={dateFrom} max={dateTo || undefined} onChange={event => { setDateFrom(event.target.value); setPage(1) }}
                className="h-8 rounded-lg border border-border bg-card px-2 text-xs text-foreground outline-none focus-visible:border-ring" />
            </label>
            <label className="space-y-1 text-[11px] text-muted-foreground">
              <span className="block">结束日期</span>
              <input aria-label="全部历史结束日期" type="date" value={dateTo} min={dateFrom || undefined} onChange={event => { setDateTo(event.target.value); setPage(1) }}
                className="h-8 rounded-lg border border-border bg-card px-2 text-xs text-foreground outline-none focus-visible:border-ring" />
            </label>
            {hasFilters && (
              <button type="button" onClick={resetFilters} className="inline-flex h-8 items-center gap-1 rounded-lg px-2.5 text-xs text-muted-foreground transition hover:bg-card hover:text-viz-rose">
                <FilterX size={13} />清除筛选
              </button>
            )}
          </div>
        </div>

        {loadError && (
          <div className="mx-6 mt-3 flex shrink-0 items-center gap-2 rounded-lg border border-viz-rose-soft bg-viz-rose-soft px-3 py-2 text-xs text-viz-rose">
            <XCircle size={13} /><span className="flex-1">{loadError}</span>
            <button type="button" onClick={() => setReloadKey(value => value + 1)} className="font-medium hover:underline">重试</button>
          </div>
        )}

        <div data-testid="all-history-scroll" className="min-h-0 flex-1 overflow-auto px-6 py-3 scrollbar-thin">
          {loading ? (
            <div className="flex min-h-48 items-center justify-center gap-2 text-sm text-[var(--color-text-tertiary)]"><Loader2 size={15} className="animate-spin" />加载历史记录...</div>
          ) : items.length === 0 ? (
            <div className="flex min-h-48 items-center justify-center text-sm text-[var(--color-text-tertiary)]">{hasFilters ? '当前筛选条件下暂无执行记录' : '暂无执行记录'}</div>
          ) : (
            <div className="overflow-hidden rounded-xl border border-border">
              <table className="w-full min-w-[980px] table-fixed text-center text-xs">
                <thead className="sticky top-0 z-10 bg-muted text-muted-foreground">
                  <tr className="border-b border-border">
                    <th className="w-[160px] px-3 py-2.5 font-medium">执行时间</th>
                    <th className="w-[190px] px-3 py-2.5 font-medium">任务 / 流水线</th>
                    <th className="w-[84px] px-3 py-2.5 font-medium">状态</th>
                    <th className="w-[72px] px-3 py-2.5 font-medium">触发</th>
                    <th className="w-[82px] px-3 py-2.5 font-medium">耗时</th>
                    <th className="w-[80px] px-3 py-2.5 font-medium">输出行数</th>
                    <th className="w-[190px] px-3 py-2.5 font-medium">原始入湖影响（相对上一原始快照）</th>
                    <th className="px-3 py-2.5 font-medium">错误信息</th>
                  </tr>
                </thead>
                <tbody className="divide-y border-border bg-card">
                  {items.map(run => (
                    <tr key={run.id} data-testid={`global-history-record-${run.id}`} className="transition-colors hover:bg-muted">
                      <td className="px-3 py-3 text-muted-foreground tabular-nums">{formatDate(run.started_at || run.created_at)}</td>
                      <td className="px-3 py-3">
                        <div className="truncate font-medium text-foreground" title={run.task_name}>{run.task_name}</div>
                        <div className="mt-0.5 truncate text-[10.5px] text-[var(--color-text-tertiary)]" title={run.pipeline_name}>{run.pipeline_name}</div>
                      </td>
                      <td className="px-3 py-3"><StatusBadge status={run.status} /></td>
                      <td className="px-3 py-3 text-muted-foreground">{TRIGGER_LABEL[run.trigger_type] || run.trigger_type}</td>
                      <td className="px-3 py-3 text-muted-foreground tabular-nums">{formatDuration(run.started_at, run.finished_at)}</td>
                      <td className="px-3 py-3 text-muted-foreground tabular-nums">{run.rows_out ?? 0}</td>
                      <td className="px-3 py-3"><LakeImpact run={run} /></td>
                      <td className="px-3 py-3 text-left">
                        {run.error_message ? (
                          <span className="block truncate text-viz-rose" title={run.error_message}>{run.error_message}</span>
                        ) : <span className="block text-center text-[var(--color-text-tertiary)]">—</span>}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>

        <div className="flex shrink-0 flex-wrap items-center justify-end gap-3 border-t border-border bg-muted px-6 py-3">
          <span className="mr-auto text-[11px] tabular-nums text-[var(--color-text-tertiary)]">显示 {rangeStart}–{rangeEnd} / {total} 条记录</span>
          <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
            每页
            <select aria-label="全部历史每页条数" value={pageSize} onChange={event => { setPageSize(Number(event.target.value)); setPage(1) }}
              className="h-8 rounded-lg border border-border bg-card px-2 text-xs outline-none focus-visible:border-ring">
              {PAGE_SIZE_OPTIONS.map(size => <option key={size} value={size}>{size}</option>)}
            </select>
            条
          </label>
          <span className="min-w-20 text-center text-xs tabular-nums text-muted-foreground">第 {page} / {totalPages} 页</span>
          <div className="flex items-center gap-1">
            <button type="button" aria-label="全部执行记录上一页" onClick={() => setPage(current => Math.max(1, current - 1))} disabled={page <= 1 || loading}
              className="grid h-8 w-8 place-items-center rounded-lg border border-border bg-card text-muted-foreground transition hover:border-[color-mix(in_srgb,var(--color-success)_35%,transparent)] hover:text-[var(--color-success)] disabled:cursor-not-allowed disabled:opacity-35"><ChevronLeft size={13} /></button>
            <button type="button" aria-label="全部执行记录下一页" onClick={() => setPage(current => Math.min(totalPages, current + 1))} disabled={page >= totalPages || loading}
              className="grid h-8 w-8 place-items-center rounded-lg border border-border bg-card text-muted-foreground transition hover:border-[color-mix(in_srgb,var(--color-success)_35%,transparent)] hover:text-[var(--color-success)] disabled:cursor-not-allowed disabled:opacity-35"><ChevronRight size={13} /></button>
          </div>
        </div>
      </div>
    </div>
  )
}
