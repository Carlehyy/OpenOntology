import { formatDateTime } from '@/utils/datetime'
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
  Download,
  Gauge,
  Globe2,
  KeyRound,
  RefreshCw,
  RotateCcw,
  Route,
  Search,
  ShieldCheck,
  TimerReset,
} from 'lucide-react'
import { apiError, apiHub, type RunDetail, type RunOverview, type RunSummary } from '@/api/apiHub'
import { Alert } from '@/components/ui/Alert'
import { Button } from '@/components/ui/Button'
import { Sheet, SheetContent, SheetTitle } from '@/components/ui/sheet'
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

/** H11：与总览「近 7 日」口径对齐的快捷范围放首位。 */
const QUICK_RANGES = [
  { key: '7d', label: '近 7 天', days: 7 },
  { key: 'today', label: '今天', days: null },
  { key: '30d', label: '近 30 天', days: 30 },
] as const

const pad2 = (value: number) => String(value).padStart(2, '0')

const toLocalDateString = (date: Date) =>
  `${date.getFullYear()}-${pad2(date.getMonth() + 1)}-${pad2(date.getDate())}`

function quickRangeDates(range: (typeof QUICK_RANGES)[number]) {
  const today = new Date()
  const end = toLocalDateString(today)
  if (range.days == null) return { start: end, end }
  const start = new Date(today)
  start.setDate(start.getDate() - (range.days - 1))
  return { start: toLocalDateString(start), end }
}

/** H26：静态密钥类请求头原样入库（个人变量占位符除外），展示层默认打码防投屏/截图泄露。 */
const SENSITIVE_HEADER_RE = /(authorization|cookie|token|secret|passwd|password|api[-_]?key|private[-_]?key)/i

function maskHeaderValue(key: string, value: string) {
  return SENSITIVE_HEADER_RE.test(key) ? '••••••（敏感头已脱敏展示）' : value
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
  const [copied, setCopied] = useState<{ key: string; ok: boolean } | null>(null)
  const [refreshMode, setRefreshMode] = useState<RefreshMode>('manual')
  const [refreshing, setRefreshing] = useState(false)
  const detailRequestRef = useRef<number | null>(null)
  const refreshRequestRef = useRef(false)
  // H13：复制被拒时全选详情面板内容，作为手动 Cmd/Ctrl+C 的兜底
  const detailPanelRef = useRef<HTMLDivElement | null>(null)

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

  // H09：关键词/日期与结果筛选统一为即时生效——输入防抖 300ms 自动提交，
  // 「查询」按钮保留为立即提交入口（先取消挂起的防抖，避免二次请求）。
  const draftsDirty = draftKeyword.trim() !== filters.keyword
    || draftStart !== filters.start
    || draftEnd !== filters.end
  const debounceRef = useRef<number | null>(null)
  useEffect(() => {
    if (!draftsDirty) return undefined
    debounceRef.current = window.setTimeout(() => applyFilters(), 300)
    return () => {
      if (debounceRef.current) window.clearTimeout(debounceRef.current)
    }
  }, [draftsDirty, draftKeyword, draftStart, draftEnd])

  const submitFilters = () => {
    if (debounceRef.current) window.clearTimeout(debounceRef.current)
    applyFilters()
  }

  // H11：快捷时间范围直接落到已生效筛选（绕过草稿态，点击即查）。
  const applyQuickRange = (start: string, end: string) => {
    setDraftStart(start)
    setDraftEnd(end)
    setFormError('')
    setPage(1)
    setFilters(current => ({ ...current, start, end }))
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
    setSelected(item)
    setDetail(null)
    setDetailError('')
    setDetailLoading(true)
    setDetailTab('request')
    setCopied(null)
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
    setCopied(null)
  }

  const copyText = async (key: string, value: string) => {
    try {
      await writeTextToClipboard(value)
      setCopied({ key, ok: true })
      window.setTimeout(() => setCopied(current => (current?.key === key ? null : current)), 1600)
    } catch {
      // 剪贴板写入可能被浏览器拒绝（无权限/页面未聚焦）：如实提示，
      // 并直接全选详情面板内容，用户 Cmd/Ctrl+C 即可完成手动复制
      setCopied({ key, ok: false })
      const panel = detailPanelRef.current
      if (panel) {
        panel.focus({ preventScroll: true })
        const selection = window.getSelection()
        selection?.removeAllRanges()
        selection?.selectAllChildren(panel)
      }
      window.setTimeout(() => setCopied(current => (current?.key === key ? null : current)), 3200)
    }
  }

  // H24：导出当前已加载的完整记录（列表摘要 + 详情快照/响应），作为复制路径外的留证兜底
  const exportRun = () => {
    if (!selected || !detail) return
    const payload = { ...selected, ...detail, exported_at: new Date().toISOString() }
    const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' })
    const url = URL.createObjectURL(blob)
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = `run-${detail.id}.json`
    document.body.appendChild(anchor)
    anchor.click()
    anchor.remove()
    window.setTimeout(() => URL.revokeObjectURL(url), 0)
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
            <h1 className="text-lg font-semibold text-foreground">调用历史</h1>
            <p className="mt-1 text-xs leading-5 text-muted-foreground">
              短期调试记录，不作为合规审计；每接口最多保留 {overview?.retention_limit_per_interface ?? 20} 条
              {overview && `，当前保留记录涉及 ${overview.executed_interfaces} / ${overview.total_interfaces} 个接口`}。
            </p>
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
              submitFilters()
            }}
          >
            <div className="flex min-w-0 flex-1 flex-wrap items-center gap-2">
              <label className="flex h-9 min-w-[240px] flex-1 items-center gap-2 rounded-lg border border-border bg-card px-3 transition focus-within:ring-2 focus-within:ring-ring lg:max-w-sm">
                <Search size={14} className="shrink-0 text-[var(--color-text-tertiary)]" />
                <input
                  value={draftKeyword}
                  onChange={event => setDraftKeyword(event.target.value)}
                  className="min-w-0 flex-1 bg-transparent text-xs text-foreground outline-none placeholder:text-[var(--color-text-tertiary)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  placeholder="搜索接口名称，输入即查"
                  aria-label="搜索接口名称"
                />
              </label>
              <DateField label="开始日期" value={draftStart} onChange={setDraftStart} />
              <span className="px-0.5 text-xs text-[var(--color-text-tertiary)]">—</span>
              <DateField label="结束日期" value={draftEnd} onChange={setDraftEnd} />
              <div className="flex items-center gap-1" role="group" aria-label="快捷时间范围">
                {QUICK_RANGES.map(range => {
                  const { start, end } = quickRangeDates(range)
                  const active = Boolean(filters.start || filters.end) && filters.start === start && filters.end === end
                  return (
                    <button
                      key={range.key}
                      type="button"
                      onClick={() => applyQuickRange(start, end)}
                      aria-pressed={active}
                      className={`inline-flex h-9 items-center rounded-lg border px-2.5 text-xs transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${
                        active
                          ? 'border-brand-line bg-brand-soft text-brand-ink'
                          : 'border-border bg-card text-muted-foreground hover:border-brand-line hover:text-brand-ink'
                      }`}
                    >
                      {range.label}
                    </button>
                  )
                })}
              </div>
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
            {formError && <span role="alert" aria-live="polite" className="basis-full text-[11px] text-[var(--color-danger)]">{formError}</span>}
          </form>
        </header>

        {historyError && (
          <div className="mx-5 mt-3 shrink-0">
            <Alert variant="danger" role="alert" className="text-xs">
              <span className="flex flex-wrap items-center gap-x-3 gap-y-1">
                <span className="flex-1">调用记录加载失败：{historyError}</span>
                <button type="button" onClick={() => void loadHistory()} className="font-medium underline underline-offset-2">
                  重试
                </button>
              </span>
            </Alert>
          </div>
        )}

        <div className="min-h-0 flex-1 overflow-auto">
          <table className="w-full min-w-[1080px] border-collapse text-xs">
            <thead className="sticky top-0 z-10 bg-muted text-muted-foreground backdrop-blur">
              <tr>
                <th scope="col" className="w-32 border-b border-border px-5 py-3 text-center font-medium">结果</th>
                <th scope="col" className="min-w-52 border-b border-border px-4 py-3 text-left font-medium">接口</th>
                <th scope="col" className="w-36 border-b border-border px-4 py-3 text-center font-medium">来源</th>
                <th scope="col" className="min-w-56 border-b border-border px-4 py-3 text-left font-medium">诊断</th>
                <th scope="col" className="w-44 border-b border-border px-3 py-3 text-right font-medium">调用时间</th>
                <th scope="col" className="w-40 border-b border-border px-4 py-3 text-right font-medium">耗时</th>
                <th scope="col" className="w-20 border-b border-border px-4 py-3 text-center font-medium">详情</th>
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
            {total ? `显示 ${rangeStart}–${rangeEnd} / ${total} 条` : '共 0 条'}
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
          panelRef={detailPanelRef}
          onTabChange={setDetailTab}
          onCopy={copyText}
          onExport={exportRun}
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
      <div className="px-5 py-5">
        <Alert variant="danger" role="alert" className="text-xs">
          <span className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1">
            <span>
              <span className="block font-medium">运行总览暂不可用</span>
              <span className="mt-1 block">{error || '请稍后重试'}</span>
            </span>
            <button
              type="button"
              onClick={onRetry}
              className="shrink-0 rounded-md border border-current px-2.5 py-1 font-medium transition hover:opacity-80"
            >
              重试
            </button>
          </span>
        </Alert>
      </div>
    )
  }

  const p95Slow = overview.p95_elapsed_ms != null && overview.p95_elapsed_ms >= overview.slow_threshold_ms
  const metrics = [
    {
      label: '近 7 日调用',
      value: formatNumber(overview.seven_day_traffic),
      note: `今日 ${formatNumber(overview.today_traffic)} 次`,
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
        <div
          className="mt-2 flex h-[62px] items-end gap-2"
          role="img"
          aria-label={`近 7 日调用趋势，共 ${overview.seven_day_traffic} 次，失败 ${overview.seven_day_failed} 次`}
        >
          {daily.map(item => {
            const barHeight = Math.max(7, item.count * 34 / max)
            const failedRatio = item.failed / item.count
            return (
              <div key={item.date} className="flex min-w-0 flex-1 flex-col items-center justify-end gap-1">
                <span className="text-[9px] tabular-nums text-[var(--color-text-tertiary)]">{item.count}</span>
                {item.count > 0 && (
                  <div
                    className="flex w-full max-w-12 flex-col overflow-hidden rounded-t-sm bg-brand"
                    style={{ height: `${barHeight}px` }}
                    title={`${item.date}：${item.count} 次调用，${item.failed} 次失败`}
                  >
                    {item.failed > 0 && (
                      <span className="w-full bg-[var(--color-danger)]" style={{ height: `${Math.max(2, failedRatio * barHeight)}px` }} />
                    )}
                  </div>
                )}
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
    <label className="relative flex h-9 items-center rounded-lg border border-border bg-card pl-3 pr-2 transition focus-within:ring-2 focus-within:ring-ring">
      <CalendarDays size={13} className="mr-2 shrink-0 text-[var(--color-text-tertiary)]" />
      <span className="sr-only">{label}</span>
      <input
        aria-label={label}
        type="date"
        value={value}
        onChange={event => onChange(event.target.value)}
        className="w-[118px] bg-transparent text-xs text-foreground outline-none focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
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
        <div className="flex flex-col items-center gap-1">
          <div className={`inline-flex items-center justify-center gap-2 font-medium ${ok ? 'text-brand-ink' : 'text-[var(--color-danger)]'}`}>
            <span className={`h-2 w-2 rounded-full ${ok ? 'bg-brand' : 'bg-[var(--color-danger)]'}`} />
            <span>{ok ? '成功' : '失败'}</span>
            <span className={`rounded px-1.5 py-0.5 font-mono text-[10px] ${ok ? 'bg-brand-soft' : 'bg-[var(--color-danger-bg)]'}`}>
              {item.status_code ?? 'ERR'}
            </span>
          </div>
          {Boolean(item.relogin) && (
            <span className="inline-flex items-center gap-1 text-[10px] font-medium text-[var(--color-warning)]">
              <ShieldCheck size={12} />
              自动重登
            </span>
          )}
        </div>
      </td>
      <td className="max-w-72 px-4 py-2.5 text-left">
        <div className="flex min-w-0 items-center gap-2">
          <p className="min-w-0 truncate font-medium text-foreground" title={item.name}>{item.name}</p>
          <span className="inline-flex shrink-0 rounded-md border border-border bg-muted px-1.5 py-0.5 font-mono text-[10px] font-semibold text-muted-foreground">
            {item.method}
          </span>
        </div>
      </td>
      <td className="px-4 py-2.5 text-center">
        <div className="flex min-w-0 flex-col items-center justify-center gap-1">
          <SourceBadge source={item.source} />
          {item.proxy_key_name && (
            <span className="max-w-28 truncate text-xs text-muted-foreground" title={item.proxy_key_name}>
              {item.proxy_key_name}
            </span>
          )}
        </div>
      </td>
      <td className="max-w-56 px-4 py-2.5 text-left">
        <p className={`line-clamp-2 break-words text-xs leading-4 ${item.error ? 'text-[var(--color-danger)]' : 'text-[var(--color-text-tertiary)]'}`} title={item.error || undefined}>
          {item.error || '—'}
        </p>
      </td>
      <td className="whitespace-nowrap px-3 py-2.5 text-right tabular-nums">
        <span className="text-muted-foreground">{formatDateTime(item.created_at, { seconds: true })}</span>
      </td>
      <td className="px-4 py-2.5 text-right">
        <span className={`tabular-nums ${slow ? 'font-medium text-[var(--color-warning)]' : 'text-muted-foreground'}`}>
          {formatElapsed(item.elapsed_ms)}
        </span>
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
  return <span className={`shrink-0 whitespace-nowrap rounded px-1.5 py-0.5 text-xs font-semibold ${tone}`}>{label}</span>
}

function HistorySkeleton() {
  return (
    <tr className="animate-pulse">
      <td className="px-5 py-4"><div className="mx-auto h-5 w-20 rounded bg-muted" /></td>
      <td className="px-4 py-4"><div className="h-3 w-40 rounded bg-muted" /></td>
      <td className="px-4 py-4"><div className="mx-auto h-5 w-14 rounded bg-muted" /><div className="mx-auto mt-2 h-2.5 w-20 rounded bg-muted" /></td>
      <td className="px-4 py-4"><div className="h-3 w-32 rounded bg-muted" /></td>
      <td className="px-3 py-4"><div className="ml-auto h-3 w-24 rounded bg-muted" /></td>
      <td className="px-4 py-4"><div className="ml-auto h-3 w-16 rounded bg-muted" /></td>
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
  panelRef,
  onTabChange,
  onCopy,
  onExport,
  onClose,
}: {
  summary: RunSummary
  detail: RunDetail | null
  loading: boolean
  error: string
  activeTab: DetailTab
  copied: { key: string; ok: boolean } | null
  panelRef: React.RefObject<HTMLDivElement | null>
  onTabChange: (tab: DetailTab) => void
  onCopy: (key: string, value: string) => void
  onExport: () => void
  onClose: () => void
}) {
  // 详情接口只回 runs 表字段（无 name/method，见 apiHub.getRun 的后端实现），
  // 必须以列表行为底合并，否则详情一到标题与方法就被 undefined 冲掉（H01）。
  const current = { ...summary, ...(detail ?? {}) }
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
  const runIdCopied = copied?.key === 'run-id'
  const tabCopied = copied?.key === activeTab

  // 抽屉复用 Sheet（Radix Dialog）：焦点圈定、Esc/遮罩关闭与焦点还原由组件保证
  return (
    <Sheet open onOpenChange={next => { if (!next) onClose() }}>
      <SheetContent
        aria-describedby={undefined}
        className="z-[var(--z-modal)] w-full max-w-3xl border-l border-border bg-card text-foreground"
      >
        <header className="shrink-0 border-b border-border px-6 pb-4 pt-5">
          <div className="flex items-start gap-4 pr-10">
            <span className={`mt-0.5 grid h-10 w-10 shrink-0 place-items-center rounded-xl ${ok ? 'bg-brand-soft text-brand-ink' : 'bg-[var(--color-danger-bg)] text-[var(--color-danger)]'}`}>
              {ok ? <CheckCircle2 size={19} /> : <AlertCircle size={19} />}
            </span>
            <div className="min-w-0 flex-1">
              <div className="flex flex-wrap items-center gap-2">
                <SheetTitle className="truncate text-base font-semibold text-foreground">{current.name}</SheetTitle>
                <span className={`rounded-md px-2 py-0.5 text-[10px] font-semibold ${ok ? 'bg-brand-soft text-brand-ink' : 'bg-[var(--color-danger-bg)] text-[var(--color-danger)]'}`}>
                  {ok ? '调用成功' : '调用失败'}
                </span>
              </div>
              <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-[11px] text-[var(--color-text-tertiary)]">
                <span>{formatFullTime(current.created_at)}</span>
                <span>·</span>
                <button
                  type="button"
                  onClick={() => onCopy('run-id', String(current.id))}
                  className="inline-flex items-center gap-1 font-mono transition hover:text-brand-ink"
                >
                  {runIdCopied && copied?.ok
                    ? <CheckCircle2 size={11} />
                    : <ClipboardCopy size={11} className={runIdCopied ? 'text-[var(--color-danger)]' : ''} />}
                  #{current.id}
                </button>
                {runIdCopied && !copied?.ok && <span className="text-[var(--color-danger)]">复制失败，请手动记录</span>}
              </div>
              {requestUrl && <p className="mt-2 truncate font-mono text-[10px] text-muted-foreground" title={requestUrl}>{requestUrl}</p>}
            </div>
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
          <div className="mx-6 mt-4 shrink-0">
            <Alert variant="danger" role="alert" className="text-xs">
              <p className="font-medium">失败原因</p>
              <p className="mt-0.5 break-words">{current.error}</p>
            </Alert>
          </div>
        )}

        <div className="flex min-h-0 flex-1 flex-col px-6 pb-6 pt-4">
          <div className="flex shrink-0 items-center justify-between border-b border-border">
            <div className="flex items-center gap-5" role="tablist" aria-label="调用详情内容">
              {tabs.map(tab => (
                <button
                  key={tab.key}
                  type="button"
                  role="tab"
                  id={`run-detail-tab-${tab.key}`}
                  aria-selected={activeTab === tab.key}
                  aria-controls="run-detail-panel"
                  onClick={() => onTabChange(tab.key)}
                  className={`relative pb-3 text-xs font-medium transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${
                    activeTab === tab.key ? 'text-brand-ink' : 'text-muted-foreground hover:text-foreground'
                  }`}
                >
                  {tab.label}
                  {activeTab === tab.key && <span className="absolute inset-x-0 bottom-0 h-0.5 rounded-full bg-brand" />}
                </button>
              ))}
            </div>
            {!loading && !error && (
              <div className="mb-2 flex shrink-0 items-center gap-1">
                <button
                  type="button"
                  onClick={() => onCopy(activeTab, activeValue)}
                  className={`inline-flex items-center gap-1.5 rounded-md px-2 py-1 text-[11px] transition hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${
                    tabCopied && !copied?.ok ? 'text-[var(--color-danger)]' : 'text-muted-foreground hover:text-foreground'
                  }`}
                >
                  {tabCopied
                    ? copied?.ok
                      ? <><CheckCircle2 size={12} className="text-brand-ink" />已复制</>
                      : <>复制失败，已全选可 Cmd+C</>
                    : <><Copy size={12} />复制</>}
                </button>
                {detail && (
                  <button
                    type="button"
                    onClick={onExport}
                    className="inline-flex items-center gap-1.5 rounded-md px-2 py-1 text-[11px] text-muted-foreground transition hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  >
                    <Download size={12} />
                    导出本条记录
                  </button>
                )}
              </div>
            )}
          </div>

          <div
            ref={panelRef}
            tabIndex={-1}
            className="min-h-0 flex-1 overflow-auto pt-4 focus-visible:outline-none"
            role="tabpanel"
            id="run-detail-panel"
            aria-labelledby={`run-detail-tab-${activeTab}`}
          >
            {loading ? (
              <div className="h-full min-h-[280px] animate-pulse rounded-xl bg-muted" />
            ) : error ? (
              <div className="flex h-full min-h-[280px] items-center justify-center px-6">
                <Alert variant="danger" role="alert" className="max-w-md text-xs">
                  <span className="block font-medium">调用详情加载失败</span>
                  <span className="mt-1 block break-words">{error}</span>
                </Alert>
              </div>
            ) : activeTab === 'request' ? (
              <RequestSnapshotView snapshot={detail?.request_snapshot ?? null} />
            ) : activeTab === 'response' ? (
              <ResponseBodyView body={detail?.response_body ?? ''} />
            ) : (
              <ResponseHeadersView headers={detail?.response_headers ?? null} />
            )}
          </div>
        </div>
      </SheetContent>
    </Sheet>
  )
}

function EmptyValue({ text }: { text: string }) {
  return (
    <p className="rounded-xl border border-dashed border-border px-3 py-6 text-center text-xs text-[var(--color-text-tertiary)]">
      {text}
    </p>
  )
}

/** H12：查询参数 / 请求头 / 响应头的键值表，替代裸 JSON dump。 */
function KVList({ rows }: { rows: Array<{ key: string; value: string }> }) {
  return (
    <dl className="divide-y divide-border overflow-hidden rounded-xl border border-border">
      {rows.map(row => (
        <div key={row.key} className="flex items-start gap-3 px-3 py-2">
          <dt className="w-44 shrink-0 truncate font-mono text-xs font-semibold text-muted-foreground" title={row.key}>
            {row.key}
          </dt>
          <dd className="min-w-0 flex-1 break-all font-mono text-xs text-foreground">{row.value}</dd>
        </div>
      ))}
    </dl>
  )
}

/** H12：长文本默认折叠（超长响应体 / 50+ 元素的顶层 JSON 数组），展开后不折行、容器横向滚动。 */
const CODE_COLLAPSE_CHARS = 20000

function CodeBlock({ value, summary }: { value: string; summary?: string }) {
  const [expanded, setExpanded] = useState(false)
  if (!expanded && (value.length > CODE_COLLAPSE_CHARS || summary)) {
    return (
      <div className="flex flex-col items-center gap-2 rounded-xl border border-dashed border-border px-3 py-5 text-center">
        <p className="text-xs text-muted-foreground">{summary ?? `内容较长（${formatNumber(value.length)} 字符），已折叠`}</p>
        <button
          type="button"
          onClick={() => setExpanded(true)}
          className="rounded-lg border border-border bg-card px-3 py-1.5 text-xs font-medium text-foreground transition hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          展开全部
        </button>
      </div>
    )
  }
  return (
    <pre className="overflow-auto whitespace-pre rounded-xl border border-border bg-muted p-4 font-mono text-xs leading-5 text-foreground shadow-inner">
      {value}
    </pre>
  )
}

function kvPairsOf(value: unknown): Array<{ key: string; value: string }> {
  if (!Array.isArray(value)) return []
  return value
    .filter((item): item is { key: string; value: string } =>
      typeof item === 'object' && item !== null
      && typeof (item as { key?: unknown }).key === 'string'
      && typeof (item as { value?: unknown }).value === 'string')
    .map(item => ({ key: item.key, value: item.value }))
}

function RequestSnapshotView({ snapshot }: { snapshot: Record<string, unknown> | null }) {
  if (!snapshot) return <EmptyValue text="暂无请求快照" />
  const method = typeof snapshot.method === 'string' ? snapshot.method : ''
  const url = typeof snapshot.url === 'string' ? snapshot.url : ''
  const queryParams = kvPairsOf(snapshot.query_params).map(row => ({ key: row.key, value: row.value || '（空）' }))
  const headers = kvPairsOf(snapshot.headers).map(row => ({ key: row.key, value: maskHeaderValue(row.key, row.value || '（空）') }))
  const bodyType = typeof snapshot.body_type === 'string' ? snapshot.body_type : 'none'
  const bodyContent = snapshot.body_content
  const bodyText = bodyType === 'none' || bodyContent == null || bodyContent === ''
    ? ''
    : typeof bodyContent === 'string'
      ? bodyContent
      : stringifyValue(bodyContent, '')
  return (
    <div className="flex flex-col gap-4">
      <div>
        <p className="mb-1.5 text-xs font-medium text-muted-foreground">请求地址</p>
        <div className="flex min-w-0 items-center gap-2 rounded-xl border border-border px-3 py-2">
          {method && (
            <span className="shrink-0 rounded bg-muted px-1.5 py-0.5 font-mono text-[10px] font-semibold text-muted-foreground">
              {method}
            </span>
          )}
          <span className="min-w-0 flex-1 truncate font-mono text-xs text-foreground" title={url}>{url || '—'}</span>
        </div>
      </div>
      <div>
        <p className="mb-1.5 text-xs font-medium text-muted-foreground">查询参数</p>
        {queryParams.length ? <KVList rows={queryParams} /> : <EmptyValue text="无查询参数" />}
      </div>
      <div>
        <p className="mb-1.5 text-xs font-medium text-muted-foreground">请求头</p>
        {headers.length ? <KVList rows={headers} /> : <EmptyValue text="无请求头" />}
      </div>
      <div>
        <p className="mb-1.5 text-xs font-medium text-muted-foreground">请求体</p>
        {bodyText ? <CodeBlock value={bodyText} /> : <EmptyValue text="无请求体" />}
      </div>
    </div>
  )
}

function ResponseBodyView({ body }: { body: string }) {
  if (!body) return <EmptyValue text="空响应体" />
  let summary: string | undefined
  try {
    const parsed: unknown = JSON.parse(body)
    if (Array.isArray(parsed) && parsed.length > 50) {
      summary = `JSON 数组共 ${formatNumber(parsed.length)} 个元素，已折叠`
    }
  } catch {
    // 非 JSON 响应按原文展示
  }
  return <CodeBlock value={prettyResponse(body)} summary={summary} />
}

function ResponseHeadersView({ headers }: { headers: Record<string, string> | null }) {
  if (!headers) return <EmptyValue text="暂无响应头" />
  const rows = Object.entries(headers).map(([key, value]) => ({ key, value: maskHeaderValue(key, value) }))
  return rows.length ? <KVList rows={rows} /> : <EmptyValue text="暂无响应头" />
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
    super_assistant: '超级助手',
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
