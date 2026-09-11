import { formatDate, formatDateTime } from '@/utils/datetime'
import { useCallback, useEffect, useRef, useState } from 'react'
import {
  AlertCircle,
  CalendarDays,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  ClipboardCopy,
  Clock3,
  Copy,
  Gauge,
  Globe2,
  KeyRound,
  RefreshCw,
  RotateCcw,
  Route,
  Search,
  ShieldCheck,
  TimerReset,
  X,
} from 'lucide-react'
import { apiError, apiHub, type RunDetail, type RunOverview, type RunSummary } from '@/api/apiHub'
import { Button } from '@/components/ui/Button'
import { writeTextToClipboard } from '@/utils/clipboard'

const PAGE_SIZE = 20
const DEFAULT_SLOW_THRESHOLD = 500

type ResultFilter = 'all' | 'failed' | 'slow'
type DetailTab = 'request' | 'response' | 'headers'
type RefreshMode = 'manual' | '3s' | '10s'

interface AppliedFilters {
  keyword: string
  start: string
  end: string
  result: ResultFilter
}

const EMPTY_FILTERS: AppliedFilters = {
  keyword: '',
  start: '',
  end: '',
  result: 'all',
}

export default function RunHistory() {
  const [items, setItems] = useState<RunSummary[]>([])
  const [overview, setOverview] = useState<RunOverview | null>(null)
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [draftKeyword, setDraftKeyword] = useState('')
  const [draftStart, setDraftStart] = useState('')
  const [draftEnd, setDraftEnd] = useState('')
  const [filters, setFilters] = useState<AppliedFilters>(EMPTY_FILTERS)
  const [formError, setFormError] = useState('')
  const [historyLoading, setHistoryLoading] = useState(true)
  const [overviewLoading, setOverviewLoading] = useState(true)
  const [historyError, setHistoryError] = useState('')
  const [overviewError, setOverviewError] = useState('')
  const [selected, setSelected] = useState<RunSummary | null>(null)
  const [detail, setDetail] = useState<RunDetail | null>(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const [detailError, setDetailError] = useState('')
  const [detailTab, setDetailTab] = useState<DetailTab>('request')
  const [copied, setCopied] = useState('')
  const [refreshMode, setRefreshMode] = useState<RefreshMode>('manual')
  const [refreshing, setRefreshing] = useState(false)
  const detailRequestRef = useRef<number | null>(null)
  const detailTriggerRef = useRef<HTMLElement | null>(null)
  const refreshRequestRef = useRef(false)

  const loadHistory = useCallback(async (silent = false) => {
    if (!silent) setHistoryLoading(true)
    setHistoryError('')
    try {
      const history = await apiHub.listRuns({
        page,
        size: PAGE_SIZE,
        keyword: filters.keyword,
        start: filters.start ? new Date(`${filters.start}T00:00:00`).toISOString() : '',
        end: filters.end ? new Date(`${filters.end}T23:59:59.999`).toISOString() : '',
        result: filters.result,
      })
      setItems(history.items)
      setTotal(history.total)
    } catch (error) {
      setHistoryError(apiError(error))
    } finally {
      if (!silent) setHistoryLoading(false)
    }
  }, [filters, page])

  const loadOverview = useCallback(async (silent = false) => {
    if (!silent) setOverviewLoading(true)
    setOverviewError('')
    try {
      setOverview(await apiHub.runOverview())
    } catch (error) {
      setOverviewError(apiError(error))
    } finally {
      if (!silent) setOverviewLoading(false)
    }
  }, [])

  useEffect(() => {
    void loadHistory()
  }, [loadHistory])

  useEffect(() => {
    void loadOverview()
  }, [loadOverview])

  const refreshAll = useCallback(async (silent = false) => {
    if (refreshRequestRef.current) return
    refreshRequestRef.current = true
    setRefreshing(true)
    try {
      await Promise.all([loadHistory(silent), loadOverview(silent)])
    } finally {
      refreshRequestRef.current = false
      setRefreshing(false)
    }
  }, [loadHistory, loadOverview])

  useEffect(() => {
    const interval = refreshMode === '3s' ? 3000 : refreshMode === '10s' ? 10000 : 0
    if (!interval) return undefined
    const timer = window.setInterval(() => {
      if (document.visibilityState === 'visible') void refreshAll(true)
    }, interval)
    return () => window.clearInterval(timer)
  }, [refreshAll, refreshMode])

  const changeRefreshMode = (mode: RefreshMode) => {
    setRefreshMode(mode)
    void refreshAll(mode !== 'manual')
  }

  const applyFilters = () => {
    if (draftStart && draftEnd && draftStart > draftEnd) {
      setFormError('开始日期不能晚于结束日期')
      return
    }
    setFormError('')
    setPage(1)
    setFilters(current => ({
      ...current,
      keyword: draftKeyword.trim(),
      start: draftStart,
      end: draftEnd,
    }))
  }

  const setResultFilter = (result: ResultFilter) => {
    setPage(1)
    setFilters(current => ({ ...current, result }))
  }

  const resetFilters = () => {
    setDraftKeyword('')
    setDraftStart('')
    setDraftEnd('')
    setFormError('')
    setPage(1)
    setFilters(EMPTY_FILTERS)
  }

  const openDetail = async (item: RunSummary) => {
    detailTriggerRef.current = document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null
    setSelected(item)
    setDetail(null)
    setDetailError('')
    setDetailLoading(true)
    setDetailTab('request')
    setCopied('')
    detailRequestRef.current = item.id
    try {
      const next = await apiHub.getRun(item.interface_id, item.id)
      if (detailRequestRef.current === item.id) setDetail(next)
    } catch (error) {
      if (detailRequestRef.current === item.id) setDetailError(apiError(error))
    } finally {
      if (detailRequestRef.current === item.id) setDetailLoading(false)
    }
  }

  const closeDetail = () => {
    detailRequestRef.current = null
    setSelected(null)
    setDetail(null)
    setDetailError('')
    setCopied('')
    window.setTimeout(() => detailTriggerRef.current?.focus(), 0)
  }

  const copyText = async (key: string, value: string) => {
    try {
      await writeTextToClipboard(value)
      setCopied(key)
      window.setTimeout(() => setCopied(current => current === key ? '' : current), 1600)
    } catch {
      setCopied('')
    }
  }

  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE))
  const hasFilters = Boolean(filters.keyword || filters.start || filters.end || filters.result !== 'all')
  const slowThreshold = overview?.slow_threshold_ms ?? DEFAULT_SLOW_THRESHOLD
  const rangeStart = total ? (page - 1) * PAGE_SIZE + 1 : 0
  const rangeEnd = Math.min(page * PAGE_SIZE, total)

  return (
    <div className="flex h-full min-h-0 flex-col gap-3 overflow-hidden bg-[var(--color-bg-base)] p-4 text-foreground">
      <section className="shrink-0 overflow-hidden rounded-xl border border-border bg-card shadow-[var(--shadow-sm)]">
        <header className="flex items-start justify-between gap-4 px-5 py-3.5">
          <div>
            <div className="mb-1 flex items-center gap-2 text-[11px] font-medium text-brand-ink">
              <span className="h-1.5 w-1.5 rounded-full bg-brand" />
              接口代理 · 可观测性
            </div>
            <h1 className="text-lg font-semibold tracking-[-0.02em] text-foreground">调用历史</h1>
            <p className="mt-1 text-xs leading-5 text-muted-foreground">短期调试记录，不作为合规审计；每接口最多保留 {overview?.retention_limit_per_interface ?? 20} 条。</p>
          </div>
          <RefreshSelector
            value={refreshMode}
            refreshing={refreshing}
            onChange={changeRefreshMode}
          />
        </header>

        <div className="grid border-t border-border lg:grid-cols-[minmax(0,1.15fr)_minmax(380px,0.85fr)]">
          <OverviewMetrics
            overview={overview}
            loading={overviewLoading}
            error={overviewError}
            onRetry={() => void loadOverview()}
          />
          <TrafficTrend overview={overview} loading={overviewLoading} error={overviewError} />
        </div>
      </section>

      <section className="flex min-h-0 flex-1 flex-col overflow-hidden rounded-xl border border-border bg-card shadow-[var(--shadow-sm)]">
        <header className="shrink-0 border-b border-border px-5">
          <form
            className="flex flex-wrap items-center gap-2 py-3"
            onSubmit={event => {
              event.preventDefault()
              applyFilters()
            }}
          >
            <div className="flex min-w-0 flex-1 flex-wrap items-center gap-2">
              <label className="flex h-9 min-w-[240px] flex-1 items-center gap-2 rounded-lg border border-border bg-card px-3 transition focus-within:border-ring focus-within:ring-2 focus-within:ring-ring lg:max-w-sm">
                <Search size={14} className="shrink-0 text-[var(--color-text-tertiary)]" />
                <input
                  value={draftKeyword}
                  onChange={event => setDraftKeyword(event.target.value)}
                  className="min-w-0 flex-1 bg-transparent text-xs text-foreground outline-none placeholder:text-[var(--color-text-tertiary)]"
                  placeholder="搜索接口名称"
                  aria-label="搜索接口名称"
                />
              </label>
              <DateField label="开始日期" value={draftStart} onChange={setDraftStart} />
              <span className="px-0.5 text-xs text-[var(--color-text-tertiary)]">—</span>
              <DateField label="结束日期" value={draftEnd} onChange={setDraftEnd} />
              <Button type="submit" size="sm" className="h-9 rounded-lg px-4">
                <Search size={13} />
                查询
              </Button>
              {hasFilters && (
                <button
                  type="button"
                  onClick={resetFilters}
                  className="inline-flex h-9 items-center gap-1.5 rounded-lg px-2.5 text-xs text-muted-foreground transition hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                >
                  <RotateCcw size={12} />
                  清除
                </button>
              )}
            </div>
            <div className="ml-auto shrink-0">
              <ResultTabs
                value={filters.result}
                slowThreshold={slowThreshold}
                onChange={setResultFilter}
              />
            </div>
            {formError && <span className="basis-full text-[11px] text-[var(--color-danger)]">{formError}</span>}
          </form>
        </header>

        {historyError && (
          <div className="mx-5 mt-3 flex shrink-0 items-center gap-2 rounded-lg border border-[color-mix(in_srgb,var(--color-danger)_30%,transparent)] bg-[var(--color-danger-bg)] px-3 py-2 text-xs text-[var(--color-danger)]">
            <AlertCircle size={14} className="shrink-0" />
            <span className="flex-1">调用记录加载失败：{historyError}</span>
            <button type="button" onClick={() => void loadHistory()} className="font-medium hover:underline">重试</button>
          </div>
        )}

        <div className="min-h-0 flex-1 overflow-auto">
          <table className="w-full min-w-[1280px] border-collapse text-center text-xs">
            <thead className="sticky top-0 z-10 bg-muted text-muted-foreground backdrop-blur">
              <tr>
                <th className="w-32 border-b border-border px-5 py-3 text-center font-medium">结果</th>
                <th className="min-w-52 border-b border-border px-4 py-3 text-center font-medium">接口</th>
                <th className="w-36 border-b border-border px-4 py-3 text-center font-medium">来源</th>
                <th className="min-w-56 border-b border-border px-4 py-3 text-center font-medium">诊断</th>
                <th className="w-28 border-b border-border px-4 py-3 text-center font-medium">请求</th>
                <th className="w-40 border-b border-border px-4 py-3 text-center font-medium">调用时间</th>
                <th className="w-44 border-b border-border px-4 py-3 text-right font-medium">耗时</th>
                <th className="w-32 border-b border-border px-4 py-3 text-center font-medium">认证恢复</th>
                <th className="w-20 border-b border-border px-4 py-3 text-center font-medium">详情</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {historyLoading
                ? Array.from({ length: 7 }).map((_, index) => <HistorySkeleton key={index} />)
                : items.map(item => (
                  <HistoryRow
                    key={item.id}
                    item={item}
                    slowThreshold={slowThreshold}
                    active={selected?.id === item.id}
                    onOpen={() => void openDetail(item)}
                  />
                ))}
            </tbody>
          </table>

          {!historyLoading && !items.length && !historyError && (
            <EmptyHistory filtered={hasFilters} onReset={resetFilters} />
          )}
        </div>

        <footer className="flex h-12 shrink-0 items-center justify-between border-t border-border bg-muted px-5">
          <span className="text-[11px] tabular-nums text-[var(--color-text-tertiary)]">
            {total ? `显示 ${rangeStart}–${rangeEnd} / ${total} 条` : '暂无记录'}
          </span>
          <div className="flex items-center gap-2">
            <button
              type="button"
              disabled={page <= 1 || historyLoading}
              onClick={() => setPage(value => value - 1)}
              className="flex h-8 w-8 items-center justify-center rounded-lg border border-border bg-card text-muted-foreground transition hover:border-brand-line hover:bg-brand-soft hover:text-brand-ink disabled:cursor-not-allowed disabled:opacity-35"
              aria-label="上一页"
            >
              <ChevronLeft size={14} />
            </button>
            <span className="min-w-16 text-center text-xs tabular-nums text-muted-foreground">{page} / {pages}</span>
            <button
              type="button"
              disabled={page >= pages || historyLoading}
              onClick={() => setPage(value => value + 1)}
              className="flex h-8 w-8 items-center justify-center rounded-lg border border-border bg-card text-muted-foreground transition hover:border-brand-line hover:bg-brand-soft hover:text-brand-ink disabled:cursor-not-allowed disabled:opacity-35"
              aria-label="下一页"
            >
              <ChevronRight size={14} />
            </button>
          </div>
        </footer>
      </section>

      {selected && (
        <RunDetailDrawer
          summary={selected}
          detail={detail}
          loading={detailLoading}
          error={detailError}
          activeTab={detailTab}
          copied={copied}
          onTabChange={setDetailTab}
          onCopy={copyText}
          onClose={closeDetail}
        />
      )}
    </div>
  )
}

function OverviewMetrics({
  overview,
  loading,
  error,
  onRetry,
}: {
  overview: RunOverview | null
  loading: boolean
  error: string
  onRetry: () => void
}) {
  if (loading) {
    return (
      <div className="grid grid-cols-2 divide-x divide-y divide-border sm:grid-cols-4 sm:divide-y-0">
        {Array.from({ length: 4 }).map((_, index) => (
          <div key={index} className="px-5 py-4">
            <div className="h-3 w-16 animate-pulse rounded bg-muted" />
            <div className="mt-3 h-7 w-20 animate-pulse rounded bg-muted" />
            <div className="mt-2 h-2.5 w-24 animate-pulse rounded bg-muted" />
          </div>
        ))}
      </div>
    )
  }

  if (error || !overview) {
    return (
      <div className="flex min-h-[118px] items-center gap-3 px-5 py-5 text-sm text-[var(--color-danger)]">
        <AlertCircle size={17} />
        <div className="flex-1">
          <p className="font-medium">运行总览暂不可用</p>
          <p className="mt-1 text-xs text-[var(--color-danger)]">{error || '请稍后重试'}</p>
        </div>
        <button type="button" onClick={onRetry} className="rounded-lg border border-[color-mix(in_srgb,var(--color-danger)_30%,transparent)] bg-card px-3 py-1.5 text-xs hover:bg-[var(--color-danger-bg)]">重试</button>
      </div>
    )
  }

  const p95Slow = overview.p95_elapsed_ms != null && overview.p95_elapsed_ms >= overview.slow_threshold_ms
  const metrics = [
    {
      label: '近 7 日调用',
      value: formatNumber(overview.seven_day_traffic),
      note: `覆盖 ${overview.executed_interfaces} / ${overview.total_interfaces} 个接口`,
      tone: 'default',
    },
    {
      label: '成功率',
      value: `${formatDecimal(overview.success_rate)}%`,
      note: !overview.seven_day_traffic
        ? '暂无调用可计算'
        : overview.seven_day_failed ? `${overview.seven_day_failed} 次失败待排查` : '近 7 日无失败调用',
      tone: !overview.seven_day_traffic ? 'default' : overview.seven_day_failed ? 'warning' : 'success',
    },
    {
      label: '失败调用',
      value: formatNumber(overview.seven_day_failed),
      note: !overview.seven_day_traffic
        ? '近 7 日暂无调用'
        : overview.seven_day_failed ? '可在下方切换到失败记录' : '当前运行稳定',
      tone: !overview.seven_day_traffic ? 'default' : overview.seven_day_failed ? 'danger' : 'success',
    },
    {
      label: 'P95 耗时',
      value: formatElapsed(overview.p95_elapsed_ms),
      note: `慢调用阈值 ${overview.slow_threshold_ms} ms`,
      tone: p95Slow ? 'warning' : 'default',
    },
  ] as const

  return (
    <div className="grid grid-cols-2 divide-x divide-y divide-border sm:grid-cols-4 sm:divide-y-0">
      {metrics.map(metric => (
        <div key={metric.label} className="min-w-0 px-5 py-4">
          <p className="text-[11px] font-medium text-muted-foreground">{metric.label}</p>
          <p className={`mt-1 text-2xl font-semibold tracking-[-0.03em] tabular-nums ${metricTone(metric.tone)}`}>
            {metric.value}
          </p>
          <p className="mt-1 truncate text-[10px] text-[var(--color-text-tertiary)]" title={metric.note}>{metric.note}</p>
        </div>
      ))}
    </div>
  )
}

function RefreshSelector({
  value,
  refreshing,
  onChange,
}: {
  value: RefreshMode
  refreshing: boolean
  onChange: (value: RefreshMode) => void
}) {
  const options: Array<{ value: RefreshMode; label: string }> = [
    { value: 'manual', label: '手动刷新' },
    { value: '3s', label: '3 秒刷新' },
    { value: '10s', label: '10 秒刷新' },
  ]
  const indicatorClass = value === 'manual'
    ? 'translate-x-0'
    : value === '3s' ? 'translate-x-full' : 'translate-x-[200%]'

  return (
    <div className="relative grid shrink-0 grid-cols-3 rounded-lg border border-border bg-muted p-0.5" aria-label="调用历史刷新频率">
      <span
        aria-hidden="true"
        className={`absolute bottom-0.5 left-0.5 top-0.5 w-[calc(33.333%_-_2px)] rounded-md bg-brand shadow-sm transition-transform duration-300 ease-out ${indicatorClass}`}
      />
      {options.map(option => (
        <button
          key={option.value}
          type="button"
          onClick={() => onChange(option.value)}
          aria-pressed={value === option.value}
          className={`relative z-10 inline-flex h-7 min-w-20 items-center justify-center gap-1.5 rounded-md px-2.5 text-[11px] font-medium transition-colors duration-200 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-1 ${
            value === option.value ? 'text-[var(--color-text-inverse)]' : 'text-muted-foreground hover:text-foreground'
          }`}
        >
          {option.value === 'manual' && <RefreshCw size={11} className={refreshing ? 'animate-spin' : ''} />}
          {option.label}
        </button>
      ))}
    </div>
  )
}

function TrafficTrend({
  overview,
  loading,
  error,
}: {
  overview: RunOverview | null
  loading: boolean
  error: string
}) {
  const daily = overview?.daily ?? []
  const max = Math.max(1, ...daily.map(item => item.count))

  return (
    <div className="border-t border-border px-5 py-3 lg:border-l lg:border-t-0">
      <div className="flex items-center justify-between gap-3">
        <div>
          <p className="text-[11px] font-medium text-muted-foreground">近 7 日调用趋势</p>
          <p className="mt-0.5 text-[10px] text-[var(--color-text-tertiary)]">红色区段表示失败调用</p>
        </div>
        {!loading && !error && overview && (
          <span className={`rounded-md px-2 py-1 text-[10px] font-medium ${
            !overview.seven_day_traffic
              ? 'bg-muted text-muted-foreground'
              : overview.seven_day_failed
              ? 'bg-[var(--color-danger-bg)] text-[var(--color-danger)]'
              : 'bg-brand-soft text-brand-ink'
          }`}>
            {!overview.seven_day_traffic
              ? '暂无调用'
              : overview.seven_day_failed ? `${overview.seven_day_failed} 次异常` : '运行稳定'}
          </span>
        )}
      </div>

      {loading ? (
        <div className="mt-2 h-[62px] animate-pulse rounded-lg bg-muted" />
      ) : error || !overview ? (
        <div className="mt-2 flex h-[62px] items-center justify-center rounded-lg bg-muted text-[11px] text-[var(--color-text-tertiary)]">趋势数据不可用</div>
      ) : (
        <div className="mt-2 flex h-[62px] items-end gap-2">
          {daily.map(item => {
            const barHeight = item.count ? Math.max(7, item.count * 34 / max) : 3
            const failedRatio = item.count ? item.failed / item.count : 0
            return (
              <div key={item.date} className="flex min-w-0 flex-1 flex-col items-center justify-end gap-1">
                <span className="text-[9px] tabular-nums text-[var(--color-text-tertiary)]">{item.count}</span>
                <div
                  className={`flex w-full max-w-12 flex-col overflow-hidden rounded-t-sm ${item.count ? 'bg-brand' : 'bg-[var(--color-bg-active)]'}`}
                  style={{ height: `${barHeight}px` }}
                  title={`${item.date}：${item.count} 次调用，${item.failed} 次失败`}
                >
                  {item.failed > 0 && (
                    <span className="w-full bg-[var(--color-danger)]" style={{ height: `${Math.max(2, failedRatio * barHeight)}px` }} />
                  )}
                </div>
                <span className="text-[9px] tabular-nums text-[var(--color-text-tertiary)]">{item.date.slice(5)}</span>
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}

function ResultTabs({
  value,
  slowThreshold,
  onChange,
}: {
  value: ResultFilter
  slowThreshold: number
  onChange: (value: ResultFilter) => void
}) {
  const tabs: Array<{ key: ResultFilter; label: string }> = [
    { key: 'all', label: '全部' },
    { key: 'failed', label: '失败' },
    { key: 'slow', label: `慢调用 ≥ ${slowThreshold}ms` },
  ]
  const tabsRef = useRef<HTMLDivElement>(null)
  const [indicatorPos, setIndicatorPos] = useState({ left: 0, width: 0 })

  useEffect(() => {
    const container = tabsRef.current
    if (!container) return
    const activeButton = container.querySelector(`[data-tab-value="${value}"]`) as HTMLElement | null
    if (!activeButton) return
    const containerRect = container.getBoundingClientRect()
    const buttonRect = activeButton.getBoundingClientRect()
    setIndicatorPos({
      left: buttonRect.left - containerRect.left,
      width: buttonRect.width,
    })
  }, [slowThreshold, value])

  return (
    <div ref={tabsRef} className="relative flex items-center gap-1 rounded-lg border border-border bg-muted p-0.5" aria-label="调用结果筛选">
      <span
        aria-hidden="true"
        className="pointer-events-none absolute top-0.5 h-[calc(100%-4px)] rounded-md bg-brand shadow-sm transition-all duration-300 ease-out"
        style={{ left: `${indicatorPos.left}px`, width: `${indicatorPos.width}px` }}
      />
      {tabs.map(tab => (
        <button
          key={tab.key}
          type="button"
          data-tab-value={tab.key}
          onClick={() => onChange(tab.key)}
          aria-pressed={value === tab.key}
          className={`relative z-10 rounded-md px-3 py-1.5 text-xs font-medium transition-colors duration-200 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-1 ${
            value === tab.key
              ? 'text-[var(--color-text-inverse)]'
              : 'text-muted-foreground hover:text-foreground'
          }`}
        >
          {tab.label}
        </button>
      ))}
    </div>
  )
}

function DateField({
  label,
  value,
  onChange,
}: {
  label: string
  value: string
  onChange: (value: string) => void
}) {
  return (
    <label className="relative flex h-9 items-center rounded-lg border border-border bg-card pl-3 pr-2 transition focus-within:border-ring focus-within:ring-2 focus-within:ring-ring">
      <CalendarDays size={13} className="mr-2 shrink-0 text-[var(--color-text-tertiary)]" />
      <span className="sr-only">{label}</span>
      <input
        aria-label={label}
        type="date"
        value={value}
        onChange={event => onChange(event.target.value)}
        className="w-[118px] bg-transparent text-[11px] text-muted-foreground outline-none"
      />
    </label>
  )
}

function HistoryRow({
  item,
  slowThreshold,
  active,
  onOpen,
}: {
  item: RunSummary
  slowThreshold: number
  active: boolean
  onOpen: () => void
}) {
  const ok = Boolean(item.ok)
  const slow = item.elapsed_ms != null && item.elapsed_ms >= slowThreshold
  const time = formatTimeParts(item.created_at)
  const latencyWidth = item.elapsed_ms == null
    ? 0
    : Math.max(5, Math.min(100, item.elapsed_ms * 100 / Math.max(slowThreshold * 2, 1000)))

  return (
    <tr
      tabIndex={0}
      title="点击查看调用详情"
      onClick={onOpen}
      onKeyDown={event => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault()
          onOpen()
        }
      }}
      className={`group cursor-pointer outline-none transition focus-visible:bg-brand-soft ${
        active ? 'bg-brand-soft' : 'hover:bg-muted'
      }`}
    >
      <td className="px-5 py-2.5 text-center">
        <div className={`inline-flex items-center justify-center gap-2 font-medium ${ok ? 'text-brand-ink' : 'text-[var(--color-danger)]'}`}>
          <span className={`h-2 w-2 rounded-full ${ok ? 'bg-brand' : 'bg-[var(--color-danger)]'}`} />
          <span>{ok ? '成功' : '失败'}</span>
          <span className={`rounded px-1.5 py-0.5 font-mono text-[10px] ${ok ? 'bg-brand-soft' : 'bg-[var(--color-danger-bg)]'}`}>
            {item.status_code ?? 'ERR'}
          </span>
        </div>
      </td>
      <td className="max-w-52 px-4 py-2.5 text-center">
        <p className="truncate font-medium text-foreground" title={item.name}>{item.name}</p>
      </td>
      <td className="px-4 py-2.5 text-center">
        <div className="flex min-w-0 flex-col items-center justify-center gap-1">
          <SourceBadge source={item.source} />
          {item.proxy_key_name && (
            <span className="max-w-28 truncate text-[10px] text-muted-foreground" title={item.proxy_key_name}>
              {item.proxy_key_name}
            </span>
          )}
        </div>
      </td>
      <td className="max-w-56 px-4 py-2.5 text-center">
        <p className={`mx-auto truncate text-[10px] ${item.error ? 'text-[var(--color-danger)]' : 'font-mono text-[var(--color-text-tertiary)]'}`} title={item.error || undefined}>
          {item.error || '-'}
        </p>
      </td>
      <td className="px-4 py-2.5 text-center">
        <span className="inline-flex rounded-md border border-border bg-muted px-2 py-1 font-mono text-[10px] font-semibold text-muted-foreground">
          {item.method}
        </span>
      </td>
      <td className="px-4 py-2.5 text-center tabular-nums">
        <p className="text-muted-foreground">{time.date}</p>
        <p className="mt-0.5 text-[10px] text-[var(--color-text-tertiary)]">{time.time}</p>
      </td>
      <td className="px-4 py-2.5 text-right">
        <div className="flex items-center justify-end gap-2">
          <span className={`w-14 tabular-nums ${slow ? 'font-medium text-[var(--color-warning)]' : 'text-muted-foreground'}`}>
            {formatElapsed(item.elapsed_ms)}
          </span>
          {item.elapsed_ms != null && (
            <span className="h-1.5 w-16 overflow-hidden rounded-full bg-muted">
              <span
                className={`block h-full rounded-full ${slow ? 'bg-[var(--color-warning)]' : 'bg-brand'}`}
                style={{ width: `${latencyWidth}%` }}
              />
            </span>
          )}
        </div>
      </td>
      <td className="px-4 py-2.5 text-center">
        {item.relogin ? (
          <span className="inline-flex items-center justify-center gap-1.5 text-[11px] font-medium text-[var(--color-warning)]">
            <ShieldCheck size={13} />
            自动重登
          </span>
        ) : <span className="text-[11px] text-[var(--color-text-tertiary)]">未触发</span>}
      </td>
      <td className="px-4 py-2.5 text-center">
        <span className={`inline-flex items-center justify-center gap-1 text-[11px] font-medium transition group-hover:translate-x-0.5 group-hover:text-brand-ink ${
          active ? 'text-brand-ink' : 'text-[var(--color-text-tertiary)]'
        }`}>
          查看
          <ChevronRight size={13} />
        </span>
      </td>
    </tr>
  )
}

function SourceBadge({ source }: { source: string }) {
  const label = sourceLabel(source)
  const tone = source === 'http_proxy'
    ? 'bg-brand-soft text-brand-ink'
    : source === 'n8n_proxy'
      ? 'bg-[var(--color-info-bg)] text-[var(--color-info)]'
      : source.startsWith('mcp_')
        ? 'bg-[var(--color-warning-bg)] text-[var(--color-warning)]'
        : 'bg-muted text-muted-foreground'
  return <span className={`shrink-0 whitespace-nowrap rounded px-1.5 py-0.5 text-[9px] font-semibold ${tone}`}>{label}</span>
}

function HistorySkeleton() {
  return (
    <tr className="animate-pulse">
      <td className="px-5 py-4"><div className="mx-auto h-5 w-20 rounded bg-muted" /></td>
      <td className="px-4 py-4"><div className="mx-auto h-3 w-40 rounded bg-muted" /></td>
      <td className="px-4 py-4"><div className="mx-auto h-5 w-14 rounded bg-muted" /><div className="mx-auto mt-2 h-2.5 w-20 rounded bg-muted" /></td>
      <td className="px-4 py-4"><div className="mx-auto h-3 w-32 rounded bg-muted" /></td>
      <td className="px-4 py-4"><div className="mx-auto h-5 w-12 rounded bg-muted" /></td>
      <td className="px-4 py-4"><div className="mx-auto h-3 w-24 rounded bg-muted" /><div className="mx-auto mt-2 h-2.5 w-16 rounded bg-muted" /></td>
      <td className="px-4 py-4"><div className="mx-auto h-3 w-24 rounded bg-muted" /></td>
      <td className="px-4 py-4"><div className="mx-auto h-3 w-16 rounded bg-muted" /></td>
      <td className="px-4 py-4"><div className="mx-auto h-4 w-10 rounded bg-muted" /></td>
    </tr>
  )
}

function EmptyHistory({ filtered, onReset }: { filtered: boolean; onReset: () => void }) {
  return (
    <div className="flex min-h-[260px] flex-col items-center justify-center px-6 text-center">
      <span className="grid h-12 w-12 place-items-center rounded-xl border border-border bg-muted text-[var(--color-text-tertiary)]">
        {filtered ? <Search size={20} /> : <Clock3 size={20} />}
      </span>
      <p className="mt-4 text-sm font-medium text-foreground">{filtered ? '没有匹配的调用记录' : '还没有调用记录'}</p>
      <p className="mt-1 max-w-sm text-xs leading-5 text-[var(--color-text-tertiary)]">
        {filtered ? '调整接口名称、时间范围或结果筛选后再试。' : '接口首次被调用后，这里会保留请求、响应和耗时证据。'}
      </p>
      {filtered && (
        <button type="button" onClick={onReset} className="mt-3 text-xs font-medium text-brand-ink hover:underline">清除全部筛选</button>
      )}
    </div>
  )
}

function RunDetailDrawer({
  summary,
  detail,
  loading,
  error,
  activeTab,
  copied,
  onTabChange,
  onCopy,
  onClose,
}: {
  summary: RunSummary
  detail: RunDetail | null
  loading: boolean
  error: string
  activeTab: DetailTab
  copied: string
  onTabChange: (tab: DetailTab) => void
  onCopy: (key: string, value: string) => void
  onClose: () => void
}) {
  const dialogRef = useRef<HTMLElement>(null)
  const closeButtonRef = useRef<HTMLButtonElement>(null)

  useEffect(() => {
    const close = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
      if (event.key !== 'Tab' || !dialogRef.current) return
      const focusable = [...dialogRef.current.querySelectorAll<HTMLElement>(
        'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
      )]
      if (!focusable.length) return
      const first = focusable[0]
      const last = focusable[focusable.length - 1]
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault()
        first.focus()
      }
    }
    window.addEventListener('keydown', close)
    closeButtonRef.current?.focus()
    return () => window.removeEventListener('keydown', close)
  }, [onClose])

  const current = detail ?? summary
  const ok = Boolean(current.ok)
  const requestValue = detail ? stringifyValue(detail.request_snapshot, '暂无请求快照') : ''
  const responseValue = detail ? prettyResponse(detail.response_body) : ''
  const headersValue = detail ? stringifyValue(detail.response_headers, '暂无响应头') : ''
  const activeValue = activeTab === 'request' ? requestValue : activeTab === 'response' ? responseValue : headersValue
  const requestUrl = detail?.request_snapshot && typeof detail.request_snapshot.url === 'string'
    ? detail.request_snapshot.url
    : ''
  const tabs: Array<{ key: DetailTab; label: string }> = [
    { key: 'request', label: '请求快照' },
    { key: 'response', label: '响应体' },
    { key: 'headers', label: '响应头' },
  ]

  return (
    <div className="fixed inset-0 z-[var(--z-modal)]">
      <button type="button" className="absolute inset-0 bg-[var(--color-code-bg)]/25 backdrop-blur-[1px]" onClick={onClose} aria-label="关闭调用详情" />
      <aside
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="run-detail-title"
        className="absolute inset-y-0 right-0 flex w-full max-w-3xl animate-slide-in-right flex-col border-l border-border bg-card shadow-[-24px_0_64px_rgba(15,23,42,0.14)]"
      >
        <header className="shrink-0 border-b border-border px-6 pb-4 pt-5">
          <div className="flex items-start gap-4">
            <span className={`mt-0.5 grid h-10 w-10 shrink-0 place-items-center rounded-xl ${ok ? 'bg-brand-soft text-brand-ink' : 'bg-[var(--color-danger-bg)] text-[var(--color-danger)]'}`}>
              {ok ? <CheckCircle2 size={19} /> : <AlertCircle size={19} />}
            </span>
            <div className="min-w-0 flex-1">
              <div className="flex flex-wrap items-center gap-2">
                <h2 id="run-detail-title" className="truncate text-base font-semibold tracking-[-0.01em] text-foreground">{current.name}</h2>
                <span className={`rounded-md px-2 py-0.5 text-[10px] font-semibold ${ok ? 'bg-brand-soft text-brand-ink' : 'bg-[var(--color-danger-bg)] text-[var(--color-danger)]'}`}>
                  {ok ? '调用成功' : '调用失败'}
                </span>
              </div>
              <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-[11px] text-[var(--color-text-tertiary)]">
                <span>{formatFullTime(current.created_at)}</span>
                <span>·</span>
                <button
                  type="button"
                  onClick={() => onCopy('run-id', `RUN-${String(current.id).padStart(6, '0')}`)}
                  className="inline-flex items-center gap-1 font-mono transition hover:text-brand-ink"
                >
                  {copied === 'run-id' ? <CheckCircle2 size={11} /> : <ClipboardCopy size={11} />}
                  RUN-{String(current.id).padStart(6, '0')}
                </button>
              </div>
              {requestUrl && <p className="mt-2 truncate font-mono text-[10px] text-muted-foreground" title={requestUrl}>{requestUrl}</p>}
            </div>
            <button
              ref={closeButtonRef}
              type="button"
              onClick={onClose}
              className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg text-[var(--color-text-tertiary)] transition hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              aria-label="关闭详情"
            >
              <X size={16} />
            </button>
          </div>
        </header>

        <div className="grid shrink-0 grid-cols-2 border-b border-border bg-muted sm:grid-cols-4">
          <DetailMetric icon={ok ? CheckCircle2 : AlertCircle} label="状态码" value={String(current.status_code ?? 'ERR')} tone={ok ? 'success' : 'danger'} />
          <DetailMetric icon={Gauge} label="响应耗时" value={formatElapsed(current.elapsed_ms)} />
          <DetailMetric icon={TimerReset} label="请求方法" value={current.method} mono />
          <DetailMetric icon={ShieldCheck} label="认证恢复" value={current.relogin ? '自动重登' : '未触发'} />
          <DetailMetric icon={Route} label="调用来源" value={sourceLabel(current.source)} />
          <DetailMetric icon={KeyRound} label="调用方" value={current.proxy_key_name || '—'} />
          <DetailMetric icon={Globe2} label="来源 IP" value={current.source_ip || '—'} mono />
        </div>

        {current.error && (
          <div className="mx-6 mt-4 flex shrink-0 items-start gap-2 rounded-lg border border-[color-mix(in_srgb,var(--color-danger)_30%,transparent)] bg-[var(--color-danger-bg)] px-3 py-2.5 text-xs leading-5 text-[var(--color-danger)]">
            <AlertCircle size={14} className="mt-0.5 shrink-0" />
            <div>
              <p className="font-medium">失败原因</p>
              <p className="mt-0.5 break-words text-[var(--color-danger)]">{current.error}</p>
            </div>
          </div>
        )}

        <div className="flex min-h-0 flex-1 flex-col px-6 pb-6 pt-4">
          <div className="flex shrink-0 items-center justify-between border-b border-border">
            <div className="flex items-center gap-5">
              {tabs.map(tab => (
                <button
                  key={tab.key}
                  type="button"
                  onClick={() => onTabChange(tab.key)}
                  className={`relative pb-3 text-xs font-medium transition ${
                    activeTab === tab.key ? 'text-brand-ink' : 'text-muted-foreground hover:text-foreground'
                  }`}
                >
                  {tab.label}
                  {activeTab === tab.key && <span className="absolute inset-x-0 bottom-0 h-0.5 rounded-full bg-brand" />}
                </button>
              ))}
            </div>
            {!loading && !error && (
              <button
                type="button"
                onClick={() => onCopy(activeTab, activeValue)}
                className="mb-2 inline-flex items-center gap-1.5 rounded-md px-2 py-1 text-[11px] text-muted-foreground transition hover:bg-muted hover:text-foreground"
              >
                {copied === activeTab ? <CheckCircle2 size={12} className="text-brand-ink" /> : <Copy size={12} />}
                {copied === activeTab ? '已复制' : '复制'}
              </button>
            )}
          </div>

          <div className="min-h-0 flex-1 pt-4">
            {loading ? (
              <div className="h-full min-h-[280px] animate-pulse rounded-xl bg-muted" />
            ) : error ? (
              <div className="flex h-full min-h-[280px] flex-col items-center justify-center rounded-xl border border-[color-mix(in_srgb,var(--color-danger)_30%,transparent)] bg-[var(--color-danger-bg)] px-6 text-center">
                <AlertCircle size={24} className="text-[var(--color-danger)]" />
                <p className="mt-3 text-sm font-medium text-[var(--color-danger)]">调用详情加载失败</p>
                <p className="mt-1 text-xs text-[var(--color-danger)]">{error}</p>
              </div>
            ) : (
              <pre className="h-full min-h-[280px] overflow-auto whitespace-pre-wrap break-all rounded-xl border border-border bg-muted p-4 font-mono text-[11px] leading-5 text-foreground shadow-inner">
                {activeValue}
              </pre>
            )}
          </div>
        </div>
      </aside>
    </div>
  )
}

function DetailMetric({
  icon: Icon,
  label,
  value,
  tone,
  mono,
}: {
  icon: React.ElementType
  label: string
  value: string
  tone?: 'success' | 'danger'
  mono?: boolean
}) {
  return (
    <div className="flex items-center gap-3 border-r border-border px-5 py-3 last:border-r-0">
      <Icon size={14} className={tone === 'success' ? 'text-brand-ink' : tone === 'danger' ? 'text-[var(--color-danger)]' : 'text-[var(--color-text-tertiary)]'} />
      <div>
        <p className="text-[10px] text-[var(--color-text-tertiary)]">{label}</p>
        <p className={`mt-0.5 text-xs font-semibold tabular-nums ${mono ? 'font-mono' : ''} ${tone === 'success' ? 'text-brand-ink' : tone === 'danger' ? 'text-[var(--color-danger)]' : 'text-foreground'}`}>
          {value}
        </p>
      </div>
    </div>
  )
}

function metricTone(tone: 'default' | 'success' | 'warning' | 'danger') {
  if (tone === 'success') return 'text-brand-ink'
  if (tone === 'warning') return 'text-[var(--color-warning)]'
  if (tone === 'danger') return 'text-[var(--color-danger)]'
  return 'text-foreground'
}

function sourceLabel(source?: string | null) {
  const labels: Record<string, string> = {
    ui: '平台界面',
    http_proxy: 'HTTP 代理',
    n8n_proxy: 'n8n',
    mcp_individual: '独立 MCP',
    mcp_open: '统一 MCP',
    mcp_system: '系统 MCP',
  }
  return labels[source || ''] || source || '平台界面'
}

function stringifyValue(value: unknown, fallback: string) {
  if (value == null) return fallback
  try {
    return JSON.stringify(value, null, 2)
  } catch {
    return String(value)
  }
}

function prettyResponse(text: string) {
  if (!text) return '(空响应体)'
  try {
    return JSON.stringify(JSON.parse(text), null, 2)
  } catch {
    return text
  }
}

function formatTimeParts(iso?: string | null) {
  if (!iso) return { date: '—', time: '' }
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return { date: iso, time: '' }
  return {
    date: formatDate(date),
    time: formatDateTime(date, { seconds: true }),
  }
}

function formatFullTime(iso?: string | null) {
  if (!iso) return '—'
  const date = new Date(iso)
  return Number.isNaN(date.getTime())
    ? iso
    : formatDateTime(date, { seconds: true })
}

function formatElapsed(value?: number | null) {
  if (value == null) return '—'
  return value >= 1000 ? `${formatDecimal(value / 1000)} s` : `${value} ms`
}

function formatNumber(value: number) {
  return new Intl.NumberFormat('zh-CN').format(value)
}

function formatDecimal(value: number) {
  return Number.isInteger(value) ? String(value) : value.toFixed(1)
}
