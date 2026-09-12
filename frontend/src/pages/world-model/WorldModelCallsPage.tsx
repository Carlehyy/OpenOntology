import { formatDateTime } from '@/utils/datetime'
import { useCallback, useEffect, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import {
  AlertCircle,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  Clock3,
  Gauge,
  RefreshCw,
  Route,
  ScrollText,
  Search,
  X,
  XCircle,
} from 'lucide-react'
import {
  apiError,
  worldModelApi,
  type CallRecordDailyBucket,
  type CallRecordDetail,
  type CallRecordItem,
  type CallRecordOverview,
} from '@/api/worldModel'
import { Button } from '@/components/ui/Button'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import CallsTrendChart from './CallsTrendChart'
import StatCard from './StatCard'
import { formatDurationMs } from './statsFormat'

const PAGE_SIZE = 20
/** 趋势图窗口：近 N 天按日分桶 */
const TREND_DAYS = 14

type ResultFilter = 'all' | 'failed'
type DetailTab = 'request' | 'response'

interface AppliedFilters {
  keyword: string
  start: string
  end: string
  result: ResultFilter
}

const EMPTY_FILTERS: AppliedFilters = { keyword: '', start: '', end: '', result: 'all' }

export default function WorldModelCallsPage() {
    const [searchParams] = useSearchParams()
  const [items, setItems] = useState<CallRecordItem[]>([])
  const [overview, setOverview] = useState<CallRecordOverview | null>(null)
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [draftKeyword, setDraftKeyword] = useState('')
  const [draftStart, setDraftStart] = useState('')
  const [draftEnd, setDraftEnd] = useState('')
  const [draftResult, setDraftResult] = useState<ResultFilter>('all')
  const [filters, setFilters] = useState<AppliedFilters>(EMPTY_FILTERS)
  // 服务下钻筛选：从推演服务页「查看全部」跳转时经 URL 带入，刷新后仍生效
  const [serviceIdFilter, setServiceIdFilter] = useState(() => searchParams.get('service_id') ?? '')
  const [serviceNameFilter, setServiceNameFilter] = useState(() => searchParams.get('service_name') ?? '')
  const [historyLoading, setHistoryLoading] = useState(true)
  const [historyError, setHistoryError] = useState('')
  const [selected, setSelected] = useState<CallRecordItem | null>(null)
  const [detail, setDetail] = useState<CallRecordDetail | null>(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const [detailTab, setDetailTab] = useState<DetailTab>('request')
  const [refreshing, setRefreshing] = useState(false)

  const loadHistory = useCallback(async (silent = false) => {
    if (!silent) setHistoryLoading(true)
    setHistoryError('')
    try {
      const history = await worldModelApi.listCalls({
        page,
        size: PAGE_SIZE,
        keyword: filters.keyword,
        service_id: serviceIdFilter,
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
  }, [filters, page, serviceIdFilter])

  const loadOverview = useCallback(async () => {
    try {
      setOverview(await worldModelApi.callsOverview())
    } catch {
      setOverview(null)
    }
  }, [])

  const [daily, setDaily] = useState<CallRecordDailyBucket[]>([])

  const loadDaily = useCallback(async () => {
    try {
      setDaily(await worldModelApi.callsDaily(TREND_DAYS))
    } catch {
      setDaily([])
    }
  }, [])

  useEffect(() => { void loadHistory() }, [loadHistory])
  useEffect(() => { void loadOverview() }, [loadOverview])
  useEffect(() => { void loadDaily() }, [loadDaily])

  const refreshAll = async () => {
    setRefreshing(true)
    try {
      await Promise.all([loadHistory(true), loadOverview(), loadDaily()])
    } finally {
      setRefreshing(false)
    }
  }

  const applyFilters = () => {
    setPage(1)
    setFilters({ keyword: draftKeyword.trim(), start: draftStart, end: draftEnd, result: draftResult })
  }

  const resetFilters = () => {
    setDraftKeyword('')
    setDraftStart('')
    setDraftEnd('')
    setDraftResult('all')
    setPage(1)
    setServiceIdFilter('')
    setServiceNameFilter('')
    setFilters(EMPTY_FILTERS)
  }

  const clearServiceFilter = () => {
    setServiceIdFilter('')
    setServiceNameFilter('')
    setPage(1)
  }

  const openDetail = async (item: CallRecordItem) => {
    setSelected(item)
    setDetail(null)
    setDetailTab('request')
    setDetailLoading(true)
    try {
      setDetail(await worldModelApi.getCall(item.id))
    } catch {
      setDetail(null)
    } finally {
      setDetailLoading(false)
    }
  }

  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE))

  return (
    <div className="space-y-4">
      {/* 概览统计 */}
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <StatCard icon={<Route size={17} />} label="总调用次数" value={overview?.total ?? 0} />
        <StatCard icon={<AlertCircle size={17} />} label="失败次数" value={overview?.failed ?? 0} tone="danger" />
        <StatCard icon={<Gauge size={17} />} label="平均耗时" value={overview?.avg_duration_ms ?? 0} format={formatDurationMs} />
        <StatCard
          icon={<CheckCircle2 size={17} />}
          label="成功率"
          value={
            (overview?.total ?? 0) > 0
              ? (((overview?.total ?? 0) - (overview?.failed ?? 0)) / (overview?.total ?? 1)) * 100
              : 0
          }
          format={n => ((overview?.total ?? 0) > 0 ? `${n.toFixed(1).replace(/\.0$/, '')}%` : '—')}
        />
      </div>

      {/* 调用趋势：总量 + 失败 + 耗时的按日节奏，定位异常日期后再用筛选下钻 */}
      <section className="rounded-xl border border-border bg-card px-4 py-3 shadow-sm/50" aria-label="调用趋势">
        <div className="mb-1 flex flex-wrap items-center justify-between gap-2">
          <p className="text-xs font-medium text-muted-foreground">近 {TREND_DAYS} 天调用趋势</p>
          <span className="text-[11px] text-muted-foreground">按日统计成功 / 失败调用量与平均耗时</span>
        </div>
        <div className="h-56">
          {daily.some(day => day.total > 0)
            ? <CallsTrendChart days={daily} />
            : (
              <p className="flex h-full items-center justify-center text-xs text-muted-foreground">
                近 {TREND_DAYS} 天暂无调用，产生调用记录后在此展示趋势
              </p>
            )}
        </div>
      </section>

      {/* 筛选栏 */}
      <section className="flex flex-wrap items-center gap-2 rounded-xl border border-border bg-card px-4 py-3 shadow-sm/50" aria-label="调用记录筛选">
        {serviceIdFilter && (
          <span
            className="inline-flex h-9 items-center gap-1.5 rounded-lg bg-brand-soft px-3 text-xs text-brand-ink"
            title="仅列表按该服务过滤；上方统计卡与趋势图为全局数据"
          >
            服务：{serviceNameFilter || serviceIdFilter}
            <button
              type="button"
              onClick={clearServiceFilter}
              aria-label={'清除服务筛选 ' + (serviceNameFilter || serviceIdFilter)}
              className="inline-flex h-4 w-4 items-center justify-center rounded text-brand-ink hover:bg-brand-mist hover:text-brand-ink"
            >
              <X size={12} />
            </button>
          </span>
        )}
        <div className="relative w-full sm:w-64">
          <Search size={14} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-muted-foreground" />
          <input
            value={draftKeyword}
            onChange={event => setDraftKeyword(event.target.value)}
            onKeyDown={event => { if (event.key === 'Enter') applyFilters() }}
            placeholder="搜索服务名或调用方"
            aria-label="按服务名或调用方筛选"
            className="h-9 w-full rounded-lg border border-border bg-card pl-8 pr-3 text-sm text-foreground placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          />
        </div>
        <input
          type="date"
          value={draftStart}
          onChange={event => setDraftStart(event.target.value)}
          aria-label="开始日期"
          className="h-9 rounded-lg border border-border bg-card px-3 text-sm text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        />
        <span className="text-xs text-muted-foreground">至</span>
        <input
          type="date"
          value={draftEnd}
          onChange={event => setDraftEnd(event.target.value)}
          aria-label="结束日期"
          className="h-9 rounded-lg border border-border bg-card px-3 text-sm text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        />
        <Select
          value={draftResult}
          onValueChange={value => setDraftResult(value as ResultFilter)}
        >
          <SelectTrigger aria-label="按调用结果筛选" className="h-9 w-fit min-w-32 rounded-lg">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">全部结果</SelectItem>
            <SelectItem value="failed">仅失败</SelectItem>
          </SelectContent>
        </Select>
        <Button onClick={applyFilters} className="h-9 bg-brand text-white hover:bg-brand-deep">查询</Button>
        <button
          type="button"
          onClick={resetFilters}
          className="inline-flex h-9 items-center gap-1 rounded-lg px-2.5 text-xs text-muted-foreground hover:bg-muted hover:text-muted-foreground"
        >
          <X size={13} /> 重置
        </button>
        <button
          type="button"
          onClick={() => void refreshAll()}
          className="ml-auto inline-flex h-9 items-center gap-1.5 rounded-lg border border-border px-3 text-xs text-muted-foreground hover:bg-muted"
        >
          <RefreshCw size={13} className={refreshing ? 'animate-spin' : ''} /> 刷新
        </button>
      </section>

      {/* 记录表格 */}
      <section className="overflow-hidden rounded-xl border border-border bg-card shadow-sm/50">
        {historyLoading ? (
          <p className="py-16 text-center text-sm text-muted-foreground">加载调用记录…</p>
        ) : historyError ? (
          <div className="flex flex-col items-center gap-3 py-16" role="alert">
            <p className="text-sm text-destructive">{historyError}</p>
            <button
              type="button"
              onClick={() => void loadHistory()}
              className="rounded-lg border border-[var(--color-danger-bg)] bg-card px-3 py-1.5 text-xs font-medium text-destructive hover:bg-[var(--color-danger-bg)]"
            >
              重新加载
            </button>
          </div>
        ) : items.length === 0 ? (
          <div className="flex flex-col items-center justify-center px-6 py-16 text-center">
            <div>
              <ScrollText size={28} className="text-muted-foreground" />
            </div>
            <p className="mt-3 text-sm font-medium text-muted-foreground">暂无调用记录</p>
            <p className="mt-1 max-w-md text-xs leading-5 text-muted-foreground">
              发布推演服务后，每次经 HTTP 接口或 Agent 调用都会在此留下含输入快照、耗时与结果的审计记录。
            </p>
          </div>
        ) : (
          <>
            <table className="w-full text-left text-sm">
              <thead>
                <tr className="border-b border-border text-xs text-muted-foreground">
                  <th className="px-4 py-2.5 font-medium">时间</th>
                  <th className="px-4 py-2.5 font-medium">推演服务</th>
                  <th className="px-4 py-2.5 font-medium">调用方</th>
                  <th className="px-4 py-2.5 font-medium">结果</th>
                  <th className="px-4 py-2.5 text-right font-medium">耗时</th>
                </tr>
              </thead>
              <tbody>
                {items.map((item, _index) => (
                  <tr
                    key={item.id}
                    onClick={() => void openDetail(item)}
                    className="cursor-pointer border-b border-border transition-colors hover:bg-muted"
                  >
                    <td className="px-4 py-2.5 tabular-nums text-muted-foreground">{formatDateTime(item.created_at, { fallback: '-' })}</td>
                    <td className="px-4 py-2.5 text-foreground">{item.service_name || '—'}</td>
                    <td className="px-4 py-2.5 text-muted-foreground">{item.caller || '—'}</td>
                    <td className="px-4 py-2.5">
                      {item.ok
                        ? <span className="inline-flex items-center gap-1 text-xs text-brand-ink"><CheckCircle2 size={13} /> 成功</span>
                        : <span className="inline-flex items-center gap-1 text-xs text-destructive"><XCircle size={13} /> 失败</span>}
                    </td>
                    <td className="px-4 py-2.5 text-right tabular-nums text-muted-foreground">{item.duration_ms} ms</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <div className="flex items-center justify-between border-t border-border px-4 py-2.5 text-xs text-muted-foreground">
              <span>共 {total} 条</span>
              <div className="flex items-center gap-2">
                <button
                  type="button"
                  onClick={() => setPage(p => Math.max(1, p - 1))}
                  disabled={page <= 1}
                  aria-label="上一页"
                  className="inline-flex h-7 w-7 items-center justify-center rounded-md border border-border disabled:opacity-40"
                >
                  <ChevronLeft size={14} />
                </button>
                <span className="tabular-nums">{page} / {totalPages}</span>
                <button
                  type="button"
                  onClick={() => setPage(p => Math.min(totalPages, p + 1))}
                  disabled={page >= totalPages}
                  aria-label="下一页"
                  className="inline-flex h-7 w-7 items-center justify-center rounded-md border border-border disabled:opacity-40"
                >
                  <ChevronRight size={14} />
                </button>
              </div>
            </div>
          </>
        )}
      </section>

      {/* 详情抽屉 */}
      {selected && (
        <div className="fixed inset-0 z-40 flex justify-end" role="dialog" aria-label="调用详情">
          <div className="absolute inset-0 bg-[var(--color-bg-overlay)]" onClick={() => setSelected(null)} />
          <aside className="relative z-10 flex h-full w-[min(560px,100%)] flex-col bg-card shadow-2xl">
            <header className="flex items-center justify-between border-b border-border px-5 py-3.5">
              <div>
                <h2 className="text-sm font-semibold text-foreground">调用详情</h2>
                <p className="mt-0.5 text-[11px] text-muted-foreground">
                  {selected.service_name || '—'} · {formatDateTime(selected.created_at)}
                </p>
              </div>
              <button
                type="button"
                onClick={() => setSelected(null)}
                aria-label="关闭详情"
                className="inline-flex h-8 w-8 items-center justify-center rounded-lg text-muted-foreground hover:bg-muted hover:text-muted-foreground"
              >
                <X size={16} />
              </button>
            </header>

            <div className="flex items-center gap-4 border-b border-border px-5 py-3 text-xs text-muted-foreground">
              <span className="inline-flex items-center gap-1">
                {selected.ok
                  ? <><CheckCircle2 size={13} className="text-brand-ink" /> 成功</>
                  : <><XCircle size={13} className="text-destructive" /> 失败</>}
              </span>
              <span className="inline-flex items-center gap-1"><Clock3 size={13} /> {selected.duration_ms} ms</span>
              <span className="ml-auto">调用方：{selected.caller || '—'}</span>
            </div>

            <nav className="flex gap-1 border-b border-border px-5 pt-2" aria-label="详情内容">
              {(['request', 'response'] as DetailTab[]).map(tabKey => (
                <button
                  key={tabKey}
                  type="button"
                  onClick={() => setDetailTab(tabKey)}
                  className={`-mb-px border-b-2 px-3 pb-2 text-xs transition-colors ${
                    detailTab === tabKey
                      ? 'border-brand font-medium text-brand-ink'
                      : 'border-transparent text-muted-foreground hover:text-muted-foreground'
                  }`}
                >
                  {tabKey === 'request' ? '请求' : '响应'}
                </button>
              ))}
            </nav>

            <div className="min-h-0 flex-1 overflow-auto px-5 py-4">
              {detailLoading ? (
                <p className="py-10 text-center text-xs text-muted-foreground">加载详情…</p>
              ) : (
                <pre className="overflow-auto rounded-lg bg-muted p-3 text-xs leading-5 text-foreground">
                  {JSON.stringify(
                    detailTab === 'request' ? detail?.request_payload : (detail?.response_payload ?? { error: detail?.error ?? selected.error }),
                    null,
                    2,
                  ) ?? '—'}
                </pre>
              )}
            </div>
          </aside>
        </div>
      )}
    </div>
  )
}
