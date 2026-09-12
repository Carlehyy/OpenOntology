import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import {
  AlertCircle,
  CalendarDays,
  ChevronLeft,
  ChevronRight,
  Clock3,
  FileClock,
  RefreshCw,
  Search,
  X,
} from 'lucide-react'
import { modelApi } from '@/api/ontologies'
import type { ModelCallLog, ModelCallStatus, ModelConfig } from '@/types/ontology'

interface ModelDetailDrawerProps {
  model: ModelConfig | null
  isOpen: boolean
  onClose: () => void
}

interface LogFilters {
  status: '' | ModelCallStatus
  start: string
  end: string
}

const PAGE_SIZE = 20
const EMPTY_FILTERS: LogFilters = { status: '', start: '', end: '' }

const STATUS_META: Record<ModelCallStatus, { label: string; dot: string; badge: string }> = {
  success: { label: '成功', dot: 'bg-[var(--color-success)]', badge: 'bg-[var(--color-success-bg)] text-[var(--color-success)]' },
  error: { label: '失败', dot: 'bg-[var(--color-danger)]', badge: 'bg-[var(--color-danger-bg)] text-[var(--color-danger)]' },
  timeout: { label: '超时', dot: 'bg-[var(--color-warning)]', badge: 'bg-[var(--color-warning-bg)] text-[var(--color-warning)]' },
}

function toIso(value: string): string | undefined {
  if (!value) return undefined
  const parsed = new Date(value)
  return Number.isNaN(parsed.getTime()) ? undefined : parsed.toISOString()
}

function formatCallTime(value: string): { date: string; time: string } {
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return { date: '—', time: '' }
  const pad = (part: number) => String(part).padStart(2, '0')
  return {
    date: `${parsed.getFullYear()}-${pad(parsed.getMonth() + 1)}-${pad(parsed.getDate())}`,
    time: `${pad(parsed.getHours())}:${pad(parsed.getMinutes())}:${pad(parsed.getSeconds())}`,
  }
}

function formatLatency(value: number): string {
  if (!Number.isFinite(value)) return '—'
  if (value < 1000) return `${value} ms`
  return `${(value / 1000).toFixed(value < 10000 ? 2 : 1)} s`
}

function errorText(error: unknown): string {
  if (error && typeof error === 'object') {
    const detail = (error as { detail?: unknown; message?: unknown }).detail
      ?? (error as { message?: unknown }).message
    if (detail) return String(detail)
  }
  return '调用日志加载失败'
}

export default function ModelDetailDrawer({ model, isOpen, onClose }: ModelDetailDrawerProps) {
  const [items, setItems] = useState<ModelCallLog[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [draftFilters, setDraftFilters] = useState<LogFilters>(EMPTY_FILTERS)
  const [filters, setFilters] = useState<LogFilters>(EMPTY_FILTERS)
  const [loading, setLoading] = useState(false)
  const [loadError, setLoadError] = useState('')
  const [filterError, setFilterError] = useState('')
  const [reloadKey, setReloadKey] = useState(0)
  const requestIdRef = useRef(0)

  useEffect(() => {
    if (!isOpen || !model) {
      requestIdRef.current += 1
      return
    }
    setItems([])
    setTotal(0)
    setPage(1)
    setDraftFilters(EMPTY_FILTERS)
    setFilters(EMPTY_FILTERS)
    setFilterError('')
    setLoadError('')
  }, [isOpen, model?.id])

  useEffect(() => {
    if (!isOpen) return
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', handleKeyDown)
    return () => window.removeEventListener('keydown', handleKeyDown)
  }, [isOpen, onClose])

  const loadLogs = useCallback(async () => {
    if (!isOpen || !model) return
    const requestId = ++requestIdRef.current
    setLoading(true)
    setLoadError('')
    try {
      const result = await modelApi.calls(model.id, {
        page,
        page_size: PAGE_SIZE,
        status: filters.status || undefined,
        start: toIso(filters.start),
        end: toIso(filters.end),
      })
      if (requestId !== requestIdRef.current) return
      setItems(Array.isArray(result.items) ? result.items : [])
      setTotal(Number(result.total) || 0)
    } catch (error) {
      if (requestId !== requestIdRef.current) return
      setItems([])
      setTotal(0)
      setLoadError(errorText(error))
    } finally {
      if (requestId === requestIdRef.current) setLoading(false)
    }
  }, [filters.end, filters.start, filters.status, isOpen, model, page])

  useEffect(() => {
    void loadLogs()
  }, [loadLogs, reloadKey])

  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE))
  const rangeStart = total ? (page - 1) * PAGE_SIZE + 1 : 0
  const rangeEnd = Math.min(page * PAGE_SIZE, total)
  const filtered = Boolean(filters.status || filters.start || filters.end)
  const modelName = useMemo(() => model?.models?.[0] || '未指定模型', [model])

  const applyFilters = () => {
    const start = draftFilters.start ? new Date(draftFilters.start) : null
    const end = draftFilters.end ? new Date(draftFilters.end) : null
    if (start && end && start > end) {
      setFilterError('开始时间不能晚于结束时间')
      return
    }
    setFilterError('')
    setPage(1)
    setFilters({ ...draftFilters })
    setReloadKey(value => value + 1)
  }

  const clearFilters = () => {
    setDraftFilters(EMPTY_FILTERS)
    setFilters(EMPTY_FILTERS)
    setFilterError('')
    setPage(1)
    setReloadKey(value => value + 1)
  }

  if (!isOpen || !model) return null

  return (
    <div className="fixed inset-0 z-50 flex justify-end" role="dialog" aria-modal="true" aria-label={`${model.name} 调用日志`}>
      <button
        type="button"
        className="absolute inset-0 cursor-default bg-[var(--color-bg-overlay)] backdrop-blur-sm"
        onClick={onClose}
        aria-label="关闭调用日志"
      />

      <section className="relative flex h-full w-full max-w-3xl flex-col bg-muted shadow-2xl animate-slide-in-right">
        <header className="shrink-0 border-b border-border bg-card px-6 py-4">
          <div className="flex items-start justify-between gap-4">
            <div className="min-w-0">
              <div className="flex items-center gap-2 text-[11px] font-medium text-brand-ink">
                <FileClock size={13} />
                模型调用日志
              </div>
              <h2 className="mt-1 truncate text-lg font-semibold tracking-[-0.02em] text-foreground">{model.name}</h2>
              <p className="mt-1 truncate text-xs text-muted-foreground">{model.provider} · {modelName}</p>
            </div>
            <button
              type="button"
              onClick={onClose}
              aria-label="关闭"
              className="shrink-0 rounded-lg p-2 text-[var(--color-text-tertiary)] transition hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              <X size={18} />
            </button>
          </div>

          <form
            data-testid="model-log-filters"
            className="mt-4"
            onSubmit={event => {
              event.preventDefault()
              applyFilters()
            }}
          >
            <div className="flex flex-nowrap items-center gap-2">
              <div
                role="group"
                aria-label="调用时间范围"
                className="flex h-9 min-w-0 flex-1 items-center rounded-lg border border-border bg-muted transition focus-within:bg-card focus-within:ring-2 focus-within:ring-ring"
              >
                <CalendarDays size={13} className="ml-3 shrink-0 text-[var(--color-text-tertiary)]" />
                <label className="min-w-0 flex-1">
                  <span className="sr-only">开始时间</span>
                  <input
                    type="datetime-local"
                    aria-label="开始时间"
                    value={draftFilters.start}
                    onChange={event => setDraftFilters(current => ({ ...current, start: event.target.value }))}
                    className="w-full min-w-0 bg-transparent px-2 text-[11px] text-muted-foreground outline-none"
                  />
                </label>
                <span className="flex h-full shrink-0 items-center border-x border-border px-2 text-[11px] text-[var(--color-text-tertiary)]">至</span>
                <label className="min-w-0 flex-1">
                  <span className="sr-only">结束时间</span>
                  <input
                    type="datetime-local"
                    aria-label="结束时间"
                    value={draftFilters.end}
                    onChange={event => setDraftFilters(current => ({ ...current, end: event.target.value }))}
                    className="w-full min-w-0 bg-transparent px-2 text-[11px] text-muted-foreground outline-none"
                  />
                </label>
              </div>
              <Select
                value={draftFilters.status || '__all__'}
                onValueChange={value => setDraftFilters(current => ({ ...current, status: (value === '__all__' ? '' : value) as LogFilters['status'] }))}
              >
                <SelectTrigger aria-label="调用状态" className="h-9 w-24 shrink-0 rounded-lg bg-card px-3 text-xs">
                  <SelectValue placeholder="全部状态" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="__all__">全部状态</SelectItem>
                  <SelectItem value="success">成功</SelectItem>
                  <SelectItem value="error">失败</SelectItem>
                  <SelectItem value="timeout">超时</SelectItem>
                </SelectContent>
              </Select>
              <button
                type="submit"
                disabled={loading}
                className="inline-flex h-9 items-center gap-1.5 rounded-lg bg-brand px-3.5 text-xs font-medium text-[var(--color-text-inverse)] transition hover:bg-brand-deep active:translate-y-px disabled:opacity-50"
              >
                <Search size={13} /> 查询
              </button>
              <button
                type="button"
                onClick={() => setReloadKey(value => value + 1)}
                disabled={loading}
                aria-label="刷新调用日志"
                title="刷新"
                className="inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border border-border bg-card text-[var(--color-text-tertiary)] transition hover:border-brand-line hover:bg-brand-soft hover:text-brand-ink disabled:opacity-50"
              >
                <RefreshCw size={14} className={loading ? 'animate-spin' : ''} />
              </button>
            </div>
            {filterError && <p className="mt-2 text-[11px] text-[var(--color-danger)]">{filterError}</p>}
          </form>
        </header>

        {loadError && (
          <div className="mx-5 mt-4 flex shrink-0 items-center gap-2 rounded-lg border border-[color-mix(in_srgb,var(--color-danger)_35%,transparent)] bg-[var(--color-danger-bg)] px-3 py-2 text-xs text-[var(--color-danger)]">
            <AlertCircle size={14} className="shrink-0" />
            <span className="flex-1">{loadError}</span>
            <button type="button" onClick={() => void loadLogs()} className="font-medium hover:underline">重试</button>
          </div>
        )}

        <div className="min-h-0 flex-1 overflow-auto bg-card">
          <table className="w-full min-w-[720px] border-collapse text-left text-xs">
            <thead className="sticky top-0 z-10 bg-muted text-muted-foreground backdrop-blur [&_th]:align-middle">
              <tr>
                <th className="w-52 border-b border-border px-6 py-3 text-center font-medium">调用时间</th>
                <th className="w-28 border-b border-border px-4 py-3 text-center font-medium">状态</th>
                <th className="w-32 border-b border-border px-4 py-3 text-center font-medium">耗时</th>
                <th className="border-b border-border px-4 py-3 text-left font-medium">错误摘要</th>
              </tr>
            </thead>
            <tbody className="divide-y border-border [&_td]:align-middle">
              {loading
                ? Array.from({ length: 8 }).map((_, index) => <LogSkeleton key={index} />)
                : items.map(item => <LogRow key={item.id} item={item} />)}
            </tbody>
          </table>

          {!loading && !items.length && !loadError && (
            <div className="flex min-h-72 flex-col items-center justify-center px-6 text-center">
              <span className="flex h-12 w-12 items-center justify-center rounded-xl bg-muted text-[var(--color-text-tertiary)]">
                <Clock3 size={21} />
              </span>
              <p className="mt-4 text-sm font-medium text-foreground">{filtered ? '没有匹配的调用日志' : '还没有调用日志'}</p>
              <p className="mt-1 text-xs text-[var(--color-text-tertiary)]">{filtered ? '请调整时间范围或状态后重新查询' : '真实业务调用产生后会记录在这里'}</p>
              {filtered && (
                <button type="button" onClick={clearFilters} className="mt-3 text-xs font-medium text-brand-ink hover:underline">清除筛选</button>
              )}
            </div>
          )}
        </div>

        <footer className="flex h-12 shrink-0 items-center justify-between border-t border-border bg-muted px-6 py-2.5">
          <span className="text-[11px] tabular-nums text-[var(--color-text-tertiary)]">
            {total ? `显示 ${rangeStart}–${rangeEnd} / ${total} 条` : '暂无记录'}
          </span>
          <div className="flex items-center gap-2">
            <button
              type="button"
              aria-label="上一页"
              disabled={page <= 1 || loading}
              onClick={() => setPage(value => Math.max(1, value - 1))}
              className="flex h-8 w-8 items-center justify-center rounded-lg border border-border bg-card text-muted-foreground transition hover:border-brand-line hover:bg-brand-soft hover:text-brand-ink disabled:cursor-not-allowed disabled:opacity-35"
            >
              <ChevronLeft size={14} />
            </button>
            <span className="min-w-16 text-center text-xs tabular-nums text-muted-foreground">{page} / {totalPages}</span>
            <button
              type="button"
              aria-label="下一页"
              disabled={page >= totalPages || loading}
              onClick={() => setPage(value => Math.min(totalPages, value + 1))}
              className="flex h-8 w-8 items-center justify-center rounded-lg border border-border bg-card text-muted-foreground transition hover:border-brand-line hover:bg-brand-soft hover:text-brand-ink disabled:cursor-not-allowed disabled:opacity-35"
            >
              <ChevronRight size={14} />
            </button>
          </div>
        </footer>
      </section>
    </div>
  )
}

function LogRow({ item }: { item: ModelCallLog }) {
  const time = formatCallTime(item.created_at)
  const status = STATUS_META[item.status] ?? STATUS_META.error
  const summary = item.error_summary || '—'
  return (
    <tr className="transition hover:bg-muted">
      <td className="px-6 py-3 text-center tabular-nums text-muted-foreground">
        <p>{time.date}</p>
        <p className="mt-0.5 text-[10px] text-[var(--color-text-tertiary)]">{time.time}</p>
      </td>
      <td className="px-4 py-3 text-center">
        <span className={`inline-flex items-center gap-1.5 rounded-md px-2 py-1 text-[11px] font-medium ${status.badge}`}>
          <span className={`h-1.5 w-1.5 rounded-full ${status.dot}`} />
          {status.label}
        </span>
      </td>
      <td className="px-4 py-3 text-center font-mono text-[11px] tabular-nums text-muted-foreground">{formatLatency(item.latency_ms)}</td>
      <td className="max-w-0 px-4 py-3">
        <p className={`truncate ${item.error_summary ? 'text-[var(--color-danger)]' : 'text-[var(--color-text-tertiary)]'}`} title={item.error_summary || undefined}>{summary}</p>
      </td>
    </tr>
  )
}

function LogSkeleton() {
  return (
    <tr className="animate-pulse">
      <td className="px-6 py-4"><div className="mx-auto h-3 w-28 rounded bg-muted" /><div className="mx-auto mt-2 h-2.5 w-20 rounded bg-muted" /></td>
      <td className="px-4 py-4"><div className="mx-auto h-6 w-14 rounded bg-muted" /></td>
      <td className="px-4 py-4"><div className="mx-auto h-3 w-16 rounded bg-muted" /></td>
      <td className="px-4 py-4"><div className="h-3 w-3/4 rounded bg-muted" /></td>
    </tr>
  )
}
