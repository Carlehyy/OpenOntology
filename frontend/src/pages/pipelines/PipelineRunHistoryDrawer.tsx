/**
 * PipelineRunHistoryDrawer — 单条流水线的历史执行记录抽屉。
 *
 * 数据全部复用现有只读接口：`pipelinesApi.runs(id)`（最近 50 条，含状态与
 * 起止时间）；失败记录的错误日志在展开该行时才按需调 `pipelinesApi.getRun`
 * 获取，避免打开抽屉就发 N 个详情请求。容器复用 components/ui/sheet.tsx。
 */
import { formatDateTime } from '@/utils/datetime'
import { useCallback, useEffect, useState } from 'react'
import {
  CheckCircle2, XCircle, Loader2, Clock, ChevronDown, ChevronUp, AlertCircle,
} from 'lucide-react'
import pipelinesApi from '@/api/v2/pipelines'
import type { Pipeline, PipelineRunItem } from '@/api/v2/pipelines'
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from '@/components/ui/sheet'

const STATUS_META: Record<string, { icon: React.ReactNode; color: string; label: string }> = {
  pending: { icon: <Clock size={13} />, color: 'text-muted-foreground bg-muted', label: '排队中' },
  running: { icon: <Loader2 size={13} className="animate-spin" />, color: 'text-[var(--color-info)] bg-[var(--color-info-bg)]', label: '执行中' },
  success: { icon: <CheckCircle2 size={13} />, color: 'text-[var(--color-success)] bg-[var(--color-success-bg)]', label: '成功' },
  failed:  { icon: <XCircle size={13} />, color: 'text-viz-rose bg-viz-rose-soft', label: '失败' },
}

const statusMeta = (status: string) => STATUS_META[status] ?? {
  icon: <Clock size={13} />, color: 'text-muted-foreground bg-muted', label: status || '未知',
}

function formatDate(iso: string | null): string {
  if (!iso) return '-'
  return formatDateTime(iso, { fallback: iso })
}

function formatDuration(start: string | null, end: string | null): string {
  if (!start || !end) return '-'
  try {
    const ms = new Date(end).getTime() - new Date(start).getTime()
    if (ms < 0) return '-'
    if (ms < 1000) return `${ms}ms`
    if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`
    return `${Math.floor(ms / 60000)}m${Math.floor((ms % 60000) / 1000)}s`
  } catch { return '-' }
}

type ErrorState = { state: 'loading' } | { state: 'error' } | { state: 'ready'; text: string }

export default function PipelineRunHistoryDrawer({
  pipeline,
  onClose,
}: {
  pipeline: Pipeline
  onClose: () => void
}) {
  const [runs, setRuns] = useState<PipelineRunItem[]>([])
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState('')
  const [expanded, setExpanded] = useState<Set<string>>(new Set())
  const [errors, setErrors] = useState<Record<string, ErrorState>>({})

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    pipelinesApi.runs(pipeline.id)
      .then(items => { if (!cancelled) setRuns(Array.isArray(items) ? items : []) })
      .catch(() => { if (!cancelled) setLoadError('历史执行记录加载失败，请稍后重试。') })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [pipeline.id])

  const toggleError = useCallback((run: PipelineRunItem) => {
    setExpanded(prev => {
      const next = new Set(prev)
      if (next.has(run.id)) next.delete(run.id)
      else next.add(run.id)
      return next
    })
    // 展开失败行时按需拉取错误日志（每条仅请求一次）
    if (!errors[run.id]) {
      setErrors(prev => ({ ...prev, [run.id]: { state: 'loading' } }))
      pipelinesApi.getRun(run.id)
        .then(detail => setErrors(prev => ({
          ...prev,
          [run.id]: { state: 'ready', text: detail.error_log || '该次运行未记录错误日志。' },
        })))
        .catch(() => setErrors(prev => ({ ...prev, [run.id]: { state: 'error' } })))
    }
  }, [errors])

  return (
    <Sheet open onOpenChange={open => { if (!open) onClose() }}>
      <SheetContent>
        <SheetHeader>
          <SheetTitle>执行历史</SheetTitle>
          <SheetDescription>「{pipeline.name}」最近 50 次运行记录</SheetDescription>
        </SheetHeader>
        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">
          {loading ? (
            <div className="flex h-40 flex-col items-center justify-center gap-2 text-[var(--color-text-tertiary)]">
              <Loader2 size={18} className="animate-spin text-brand-ink" />
              <span className="text-xs">加载执行历史...</span>
            </div>
          ) : loadError ? (
            <div className="flex items-center gap-2 rounded-lg border border-viz-rose-soft bg-viz-rose-soft px-3 py-2 text-xs text-viz-rose">
              <AlertCircle size={13} className="shrink-0" />
              <span className="flex-1">{loadError}</span>
            </div>
          ) : runs.length === 0 ? (
            <div className="flex h-40 flex-col items-center justify-center gap-1.5 text-[var(--color-text-tertiary)]">
              <Clock size={22} className="opacity-40" />
              <p className="text-sm">暂无运行记录</p>
              <p className="text-xs">该流水线还未执行过，可在编辑向导或任务池中触发</p>
            </div>
          ) : (
            <ul className="space-y-2">
              {runs.map(run => {
                const meta = statusMeta(run.status)
                const isFailed = run.status === 'failed'
                const isOpen = expanded.has(run.id)
                const errorState = errors[run.id]
                return (
                  <li key={run.id} className="rounded-xl border border-border bg-card">
                    <button
                      type="button"
                      disabled={!isFailed}
                      onClick={() => isFailed && toggleError(run)}
                      className={`flex w-full items-center gap-3 px-3.5 py-2.5 text-left ${isFailed ? 'cursor-pointer' : 'cursor-default'}`}
                      title={isFailed ? '点击展开/收起错误日志' : undefined}
                    >
                      <span className={`inline-flex h-6 w-6 shrink-0 items-center justify-center rounded-md ${meta.color}`}>
                        {meta.icon}
                      </span>
                      <span className={`w-12 shrink-0 text-xs font-medium ${meta.color.split(' ')[0]}`}>{meta.label}</span>
                      <span className="min-w-0 flex-1 truncate text-xs text-muted-foreground">
                        {formatDate(run.started_at)}
                      </span>
                      <span className="shrink-0 text-xs tabular-nums text-[var(--color-text-tertiary)]">
                        耗时 {formatDuration(run.started_at, run.finished_at)}
                      </span>
                      {isFailed && (
                        <span className="shrink-0 text-[var(--color-text-tertiary)]">
                          {isOpen ? <ChevronUp size={13} /> : <ChevronDown size={13} />}
                        </span>
                      )}
                    </button>
                    {isFailed && isOpen && (
                      <div className="border-t border-border px-3.5 py-2.5">
                        {!errorState || errorState.state === 'loading' ? (
                          <span className="inline-flex items-center gap-1.5 text-xs text-[var(--color-text-tertiary)]">
                            <Loader2 size={12} className="animate-spin" /> 加载错误日志...
                          </span>
                        ) : errorState.state === 'error' ? (
                          <span className="text-xs text-viz-rose">错误日志加载失败，请稍后重试。</span>
                        ) : (
                          <pre className="max-h-48 overflow-auto whitespace-pre-wrap break-all rounded-lg bg-accent px-3 py-2 text-xs leading-relaxed text-muted-foreground">{errorState.text}</pre>
                        )}
                      </div>
                    )}
                  </li>
                )
              })}
            </ul>
          )}
        </div>
      </SheetContent>
    </Sheet>
  )
}
