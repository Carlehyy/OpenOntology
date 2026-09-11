import { useCallback, useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  X, CheckCircle2, XCircle, Loader2, Clock, ChevronDown, ChevronUp, Table2,
  ArrowRight, ShieldCheck, GitBranch, Settings2, Plus, Pencil, Minus, Database,
  ChevronLeft, ChevronRight, FilterX,
} from 'lucide-react'
import {
  pipelineTasksApi, WRITE_MODE_META,
  type PipelineTask, type PipelineTaskRun, type WriteMode, type RunAudit,
  type RunAuditOutput, type LakeImpactDetail,
} from '@/api/v2/pipeline-tasks'

const TRIGGER_LABEL: Record<string, string> = { manual: '手动', scheduled: '定时' }

const STATUS_META: Record<string, { icon: React.ReactNode; color: string; label: string }> = {
  pending: { icon: <Clock size={13} />, color: 'text-muted-foreground bg-muted', label: '排队中' },
  running: { icon: <Loader2 size={13} className="animate-spin" />, color: 'text-[var(--color-info)] bg-[var(--color-info-bg)]', label: '执行中' },
  success: { icon: <CheckCircle2 size={13} />, color: 'text-[var(--color-success)] bg-[var(--color-success-bg)]', label: '成功' },
  failed:  { icon: <XCircle size={13} />, color: 'text-viz-rose bg-viz-rose-soft', label: '失败' },
  cancelled: { icon: <XCircle size={13} />, color: 'text-[var(--color-warning)] bg-[var(--color-warning-bg)]', label: '已取消' },
}

type StatusFilter = '' | 'pending' | 'running' | 'success' | 'failed' | 'cancelled'
type TriggerFilter = '' | 'manual' | 'scheduled'
const PAGE_SIZE_OPTIONS = [10, 20, 50]

function formatDate(iso: string | null): string {
  if (!iso) return '-'
  try { return new Date(iso).toLocaleString('zh-CN') } catch { return iso }
}

function formatDuration(start: string | null, end: string | null): string {
  if (!start || !end) return '-'
  try {
    const ms = new Date(end).getTime() - new Date(start).getTime()
    if (ms < 1000) return `${ms}ms`
    if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`
    return `${Math.floor(ms / 60000)}m${Math.floor((ms % 60000) / 1000)}s`
  } catch { return '-' }
}

export default function HistoryDrawer({
  task,
  initialRunId,
  onClose,
}: {
  task: PipelineTask
  initialRunId?: string | null
  onClose: () => void
}) {
  const [items, setItems] = useState<PipelineTaskRun[]>([])
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState('')
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(10)
  const [statusFilter, setStatusFilter] = useState<StatusFilter>('')
  const [triggerFilter, setTriggerFilter] = useState<TriggerFilter>('')
  const [dateFrom, setDateFrom] = useState('')
  const [dateTo, setDateTo] = useState('')
  const [expanded, setExpanded] = useState<Set<string>>(new Set())
  const [audits, setAudits] = useState<Record<string, RunAudit | 'loading' | 'error'>>({})
  const [linkedRun, setLinkedRun] = useState<PipelineTaskRun | null>(null)

  const load = useCallback(() => {
    setLoading(true)
    setLoadError('')
    const createdFrom = dateFrom ? new Date(`${dateFrom}T00:00:00+08:00`).toISOString() : undefined
    const createdTo = dateTo ? new Date(`${dateTo}T23:59:59.999+08:00`).toISOString() : undefined
    pipelineTasksApi.histories(task.id, {
      page,
      page_size: pageSize,
      status: statusFilter || undefined,
      trigger_type: triggerFilter || undefined,
      created_from: createdFrom,
      created_to: createdTo,
    })
      .then(res => {
        setItems(res.items)
        setTotal(res.total)
        const pages = Math.max(1, Math.ceil(res.total / pageSize))
        if (page > pages) setPage(pages)
      })
      .catch(err => {
        setItems([])
        setTotal(0)
        setLoadError(err?.detail || err?.message || '执行记录加载失败')
      })
      .finally(() => setLoading(false))
  }, [dateFrom, dateTo, page, pageSize, statusFilter, task.id, triggerFilter])

  useEffect(() => {
    setExpanded(new Set())
    void load()
  }, [load])

  useEffect(() => {
    setLinkedRun(null)
    if (!initialRunId) return
    let active = true
    setAudits(current => ({ ...current, [initialRunId]: 'loading' }))
    pipelineTasksApi.runAudit(task.id, initialRunId)
      .then(audit => {
        if (!active) return
        setAudits(current => ({ ...current, [initialRunId]: audit }))
        setLinkedRun({
          id: audit.id,
          status: audit.status,
          trigger_type: audit.trigger_type,
          started_at: audit.started_at,
          finished_at: audit.finished_at,
          rows_in: audit.rows_in,
          rows_out: audit.rows_out,
          lake_rows: audit.lake_rows,
          write_mode: audit.write_mode,
          skipped_outputs: audit.outputs.filter(output => output.skipped).map(output => ({
            curated_dataset_id: output.curated_dataset_id,
            reason: output.skipped,
          })),
          curated_dataset_ids: audit.outputs.flatMap(output => output.curated_dataset_id ? [output.curated_dataset_id] : []),
          lake_impact: audit.lake_impact,
          config_snapshot: audit.config_snapshot,
          error_message: audit.error_message,
        })
        setExpanded(current => new Set(current).add(initialRunId))
      })
      .catch(() => {
        if (!active) return
        setAudits(current => ({ ...current, [initialRunId]: 'error' }))
        setExpanded(current => new Set(current).add(initialRunId))
      })
    return () => { active = false }
  }, [initialRunId, task.id])

  const resetFilters = () => {
    setStatusFilter('')
    setTriggerFilter('')
    setDateFrom('')
    setDateTo('')
    setPage(1)
  }

  const totalPages = Math.max(1, Math.ceil(total / pageSize))
  const rangeStart = total === 0 ? 0 : (page - 1) * pageSize + 1
  const rangeEnd = Math.min(total, page * pageSize)
  const hasFilters = Boolean(statusFilter || triggerFilter || dateFrom || dateTo)
  const visibleItems = linkedRun && !items.some(item => item.id === linkedRun.id)
    ? [linkedRun, ...items]
    : items

  const toggle = (runId: string) => {
    setExpanded(prev => {
      const n = new Set(prev)
      if (n.has(runId)) { n.delete(runId); return n }
      n.add(runId)
      if (!audits[runId]) {
        setAudits(a => ({ ...a, [runId]: 'loading' }))
        pipelineTasksApi.runAudit(task.id, runId)
          .then(res => setAudits(a => ({ ...a, [runId]: res })))
          .catch(() => setAudits(a => ({ ...a, [runId]: 'error' })))
      }
      return n
    })
  }

  return (
    <div className="fixed inset-0 bg-accent backdrop-blur-sm z-50 flex justify-end" onClick={onClose}>
      <div data-testid="execution-history-drawer" className="w-full max-w-3xl bg-card h-full flex flex-col shadow-2xl" onClick={e => e.stopPropagation()}>
        <div className="px-5 py-4 border-b border-border shrink-0">
          <div className="flex items-center justify-between">
            <h3 className="text-base font-semibold text-foreground">执行记录</h3>
            <button type="button" onClick={onClose} aria-label="关闭执行记录" className="grid h-8 w-8 place-items-center rounded-lg text-[var(--color-text-tertiary)] transition hover:bg-muted hover:text-muted-foreground"><X size={18} /></button>
          </div>
          <p className="text-xs text-[var(--color-text-tertiary)] mt-0.5">
            {task.name} · 流水线「{task.pipeline_name}」 · 每次执行可逐条追溯配置、产物与原始入湖影响（相对上一原始快照）
          </p>
        </div>

        <div className="shrink-0 border-b border-border bg-muted px-5 py-3">
          <div className="flex flex-wrap items-end gap-2.5">
            <label className="space-y-1 text-[11px] text-muted-foreground">
              <span className="block">执行状态</span>
              <select aria-label="执行状态筛选" value={statusFilter} onChange={event => { setStatusFilter(event.target.value as StatusFilter); setPage(1) }}
                className="h-8 min-w-28 rounded-lg border border-border bg-card px-2 text-xs text-foreground outline-none focus-visible:border-ring">
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
              <select aria-label="触发方式筛选" value={triggerFilter} onChange={event => { setTriggerFilter(event.target.value as TriggerFilter); setPage(1) }}
                className="h-8 min-w-24 rounded-lg border border-border bg-card px-2 text-xs text-foreground outline-none focus-visible:border-ring">
                <option value="">全部方式</option>
                <option value="manual">手动</option>
                <option value="scheduled">定时</option>
              </select>
            </label>
            <label className="space-y-1 text-[11px] text-muted-foreground">
              <span className="block">开始日期</span>
              <input aria-label="执行记录开始日期" type="date" value={dateFrom} max={dateTo || undefined} onChange={event => { setDateFrom(event.target.value); setPage(1) }}
                className="h-8 rounded-lg border border-border bg-card px-2 text-xs text-foreground outline-none focus-visible:border-ring" />
            </label>
            <label className="space-y-1 text-[11px] text-muted-foreground">
              <span className="block">结束日期</span>
              <input aria-label="执行记录结束日期" type="date" value={dateTo} min={dateFrom || undefined} onChange={event => { setDateTo(event.target.value); setPage(1) }}
                className="h-8 rounded-lg border border-border bg-card px-2 text-xs text-foreground outline-none focus-visible:border-ring" />
            </label>
            {hasFilters && (
              <button type="button" onClick={resetFilters} className="inline-flex h-8 items-center gap-1 rounded-lg px-2.5 text-xs text-muted-foreground transition hover:bg-card hover:text-viz-rose">
                <FilterX size={13} /> 清除筛选
              </button>
            )}
            <span className="ml-auto pb-1 text-[11px] tabular-nums text-[var(--color-text-tertiary)]">显示 {rangeStart}–{rangeEnd} / {total}</span>
          </div>
        </div>

        {loadError && (
          <div className="mx-5 mt-3 flex shrink-0 items-center gap-2 rounded-lg border border-viz-rose-soft bg-viz-rose-soft px-3 py-2 text-xs text-viz-rose">
            <XCircle size={13} /><span className="flex-1">{loadError}</span>
            <button type="button" onClick={() => void load()} className="font-medium hover:underline">重试</button>
          </div>
        )}

        <div className="min-h-0 flex-1 overflow-y-auto">
          {loading ? (
            <div className="py-20 text-center text-[var(--color-text-tertiary)] text-sm flex items-center justify-center gap-1">
              <Loader2 size={14} className="animate-spin" />加载中...
            </div>
          ) : visibleItems.length === 0 ? (
            <div className="py-20 text-center text-[var(--color-text-tertiary)] text-sm">{hasFilters ? '当前筛选条件下暂无执行记录' : '暂无执行记录'}</div>
          ) : (
            <div className="divide-y border-border">
              {visibleItems.map(h => {
                const sm = STATUS_META[h.status] || STATUS_META.running
                const isOpen = expanded.has(h.id)
                const skipped = (h.skipped_outputs?.length ?? 0) > 0
                const imp = h.lake_impact
                return (
                  <div key={h.id} className="px-5 py-3">
                    <button type="button" data-testid={`execution-record-${h.id}`} aria-expanded={isOpen} className="flex w-full items-start justify-between gap-2 text-left" onClick={() => toggle(h.id)}>
                      <div className="flex-1 min-w-0">
                        <div className="flex items-center gap-2 flex-wrap">
                          <span className={`inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-xs ${sm.color}`}>
                            {sm.icon}{sm.label}
                          </span>
                          <span className="text-xs text-[var(--color-text-tertiary)]">{TRIGGER_LABEL[h.trigger_type] || h.trigger_type}</span>
                          {skipped && (
                            <span className="inline-flex items-center gap-0.5 text-[10px] text-[var(--color-warning)] bg-[var(--color-warning-bg)] px-1.5 py-0.5 rounded" title="流水线输出 0 行，空输出保护已跳过入库">
                              <ShieldCheck size={9} /> 已跳过入库
                            </span>
                          )}
                          {imp && (
                            <span className="inline-flex items-center gap-1.5 text-[10.5px]">
                              {imp.added > 0 && <span className="text-[var(--color-success)]">+{imp.added}</span>}
                              {imp.updated > 0 && <span className="text-[var(--color-warning)]">~{imp.updated}</span>}
                              {imp.deleted > 0 && <span className="text-viz-rose">-{imp.deleted}</span>}
                            </span>
                          )}
                        </div>
                        <div className="text-xs text-muted-foreground mt-1 flex items-center gap-1">
                          <Clock size={11} />{formatDate(h.started_at)}
                          {h.status === 'success' && !skipped && (
                            <span className="text-[var(--color-text-tertiary)]">· 输出 {h.rows_out} 行{h.lake_rows != null ? ` · 湖内共 ${h.lake_rows} 行` : ''}</span>
                          )}
                        </div>
                      </div>
                      {isOpen ? <ChevronUp size={14} className="text-[var(--color-text-tertiary)] mt-1" /> : <ChevronDown size={14} className="text-[var(--color-text-tertiary)] mt-1" />}
                    </button>

                    {isOpen && (
                      <div className="mt-3">
                        {audits[h.id] === 'loading' || audits[h.id] === undefined ? (
                          <div className="py-6 text-center text-[var(--color-text-tertiary)] text-xs flex items-center justify-center gap-1.5">
                            <Loader2 size={13} className="animate-spin" /> 加载审计明细...
                          </div>
                        ) : audits[h.id] === 'error' ? (
                          <div className="py-6 text-center text-viz-rose text-xs">审计明细加载失败</div>
                        ) : (
                          <RunAuditView audit={audits[h.id] as RunAudit} run={h} />
                        )}
                      </div>
                    )}
                  </div>
                )
              })}
            </div>
          )}
        </div>

        <div className="flex shrink-0 flex-wrap items-center justify-end gap-3 border-t border-border bg-muted px-5 py-3">
          <span className="mr-auto text-[11px] text-[var(--color-text-tertiary)]">点击任一记录可查看执行详情</span>
          <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
            每页
            <select aria-label="执行记录每页条数" value={pageSize} onChange={event => { setPageSize(Number(event.target.value)); setPage(1) }}
              className="h-8 rounded-lg border border-border bg-card px-2 text-xs outline-none focus-visible:border-ring">
              {PAGE_SIZE_OPTIONS.map(size => <option key={size} value={size}>{size}</option>)}
            </select>
            条
          </label>
          <span className="min-w-20 text-center text-xs tabular-nums text-muted-foreground">第 {page} / {totalPages} 页</span>
          <div className="flex items-center gap-1">
            <button type="button" aria-label="执行记录上一页" onClick={() => setPage(current => Math.max(1, current - 1))} disabled={page <= 1 || loading}
              className="grid h-8 w-8 place-items-center rounded-lg border border-border bg-card text-muted-foreground transition hover:border-[color-mix(in_srgb,var(--color-success)_35%,transparent)] hover:text-[var(--color-success)] disabled:opacity-35"><ChevronLeft size={13} /></button>
            <button type="button" aria-label="执行记录下一页" onClick={() => setPage(current => Math.min(totalPages, current + 1))} disabled={page >= totalPages || loading}
              className="grid h-8 w-8 place-items-center rounded-lg border border-border bg-card text-muted-foreground transition hover:border-[color-mix(in_srgb,var(--color-success)_35%,transparent)] hover:text-[var(--color-success)] disabled:opacity-35"><ChevronRight size={13} /></button>
          </div>
        </div>
      </div>
    </div>
  )
}

function RunAuditView({ audit, run }: { audit: RunAudit; run: PipelineTaskRun }) {
  const navigate = useNavigate()
  const cfg = audit.config_snapshot
  const wmLabel = (m?: string) => (m ? WRITE_MODE_META[m as WriteMode]?.label || m : '-')
  return (
    <div className="space-y-3 text-xs bg-muted rounded-xl p-3">
      {/* 执行信息 */}
      <Section title="执行信息" icon={<Clock size={12} />}>
        <KV label="开始时间" value={formatDate(audit.started_at)} />
        {audit.finished_at && <KV label="结束时间" value={formatDate(audit.finished_at)} />}
        <KV label="执行耗时" value={formatDuration(audit.started_at, audit.finished_at)} />
        <KV label="触发方式" value={TRIGGER_LABEL[audit.trigger_type] || audit.trigger_type} />
        <KV label="流水线输入" value={`${audit.rows_in} 行`} />
        <KV label="流水线产物" value={`${audit.rows_out} 行`} />
        {audit.run_params?.cursor_column && (
          <KV label="增量水位" mono
            value={`${audit.run_params.full_refresh ? '（全量回填）' : (audit.run_params.cursor_since || '（首次全量）')} → ${audit.watermark_after || '未推进'}`} />
        )}
      </Section>

      {/* 调用的流水线 */}
      <Section title="调用的流水线" icon={<GitBranch size={12} />}>
        <div className="flex items-center justify-between">
          <div className="text-foreground font-medium">
            {audit.pipeline.name}{audit.pipeline.version ? ` (v${audit.pipeline.version})` : ''}
          </div>
          <span className="text-[10.5px] text-[var(--color-text-tertiary)]">{audit.pipeline.domain || '通用'} · {audit.pipeline.status}</span>
        </div>
      </Section>

      {/* 配置快照 */}
      {cfg && (
        <Section title="执行时配置快照" icon={<Settings2 size={12} />}>
          <KV label="入库方式" value={wmLabel(cfg.write_mode)} />
          {cfg.primary_key ? <KV label="主键" value={cfg.primary_key} mono /> : null}
          {cfg.soft_delete_column ? <KV label="软删除列" value={cfg.soft_delete_column} mono /> : null}
          {cfg.cursor_column ? <KV label="增量游标" value={cfg.cursor_column + (cfg.full_refresh ? '（当次全量回填）' : '')} mono /> : null}
          <KV label="空输出保护" value={cfg.skip_empty ? '开启' : '关闭'} />
          <KV label="调度方式" value={
            cfg.schedule_type === 'MANUAL' ? '手动触发'
              : cfg.schedule_type === 'CRON' ? `Cron: ${cfg.cron_expression || '-'}`
              : `每 ${cfg.interval_seconds || 0} 秒`
          } />
        </Section>
      )}

      {/* 每个成品数据集：产物 + 原始入湖影响 */}
      {audit.outputs.map((o, i) => (
        <OutputAudit key={o.curated_dataset_id || i} out={o}
          onOpenLake={() => navigate('/data/structured?tab=curated')} />
      ))}

      {audit.error_message && (
        <div>
          <div className="text-muted-foreground mb-0.5 font-medium">错误信息</div>
          <div className="text-viz-rose bg-viz-rose-soft rounded-lg p-2 font-mono text-[11px] whitespace-pre-wrap break-all">
            {audit.error_message}
          </div>
        </div>
      )}
      {run.status === 'success' && audit.outputs.length === 0 && !audit.error_message && (
        <div className="text-[var(--color-text-tertiary)] text-center py-2">本次未产生入湖产物</div>
      )}
    </div>
  )
}

function OutputAudit({ out, onOpenLake }: { out: RunAuditOutput; onOpenLake: () => void }) {
  const [tab, setTab] = useState<'' | 'output' | 'added' | 'updated' | 'deleted'>('')
  const im = out.lake_impact
  const toggleTab = (t: typeof tab) => setTab(cur => (cur === t ? '' : t))

  return (
    <div className="rounded-lg border border-border bg-card overflow-hidden">
      <div className="px-3 py-2 border-b border-border flex items-center justify-between gap-2 bg-muted">
        <div className="flex items-center gap-1.5 min-w-0">
          <Database size={12} className="text-[var(--color-text-tertiary)] shrink-0" />
          <span className="text-[12px] font-medium text-foreground truncate">{out.curated_dataset_name || '成品数据集'}</span>
          {out.table_name && <span className="text-[10px] text-[var(--color-text-tertiary)]">/{out.table_name}</span>}
        </div>
        <button onClick={onOpenLake} className="text-[10.5px] text-[var(--color-info)] hover:underline flex items-center gap-0.5 shrink-0">
          资产湖 <ArrowRight size={10} />
        </button>
      </div>

      <div className="px-3 py-2.5 space-y-2.5">
        {/* 流水线输出 */}
        <div>
          <button onClick={() => toggleTab('output')}
            className="w-full flex items-center justify-between text-[11.5px] text-muted-foreground hover:text-foreground">
            <span className="flex items-center gap-1.5"><Table2 size={11} className="text-[var(--color-text-tertiary)]" /> 流水线输出 <b className="text-foreground">{out.rows_out ?? 0}</b> 行</span>
            {out.output_sample.length > 0 && (tab === 'output' ? <ChevronUp size={12} /> : <ChevronDown size={12} />)}
          </button>
          {tab === 'output' && <RowTable rows={out.output_sample} columns={out.output_columns} />}
        </div>

        {/* 原始入湖影响 */}
        {im ? (
          <div className="border-t border-border pt-2 space-y-1.5">
            <div className="flex flex-wrap items-center gap-1.5 text-[11px] text-muted-foreground">
              <span className="font-medium text-muted-foreground">原始入湖影响（相对上一原始快照）</span>
              {im.keyed_by ? <span className="text-[10px] text-[var(--color-text-tertiary)]">（按主键 {im.keyed_by.join(',')} 识别）</span>
                : <span className="text-[10px] text-[var(--color-text-tertiary)]">（按整行内容比对）</span>}
            </div>
            <div className="grid grid-cols-3 gap-1.5">
              <ImpactChip tone="emerald" icon={<Plus size={11} />} label="新增" count={im.added_count}
                active={tab === 'added'} onClick={() => im.added_count && toggleTab('added')} />
              <ImpactChip tone="amber" icon={<Pencil size={10} />} label="更新" count={im.updated_count}
                active={tab === 'updated'} onClick={() => im.updated_count && toggleTab('updated')} />
              <ImpactChip tone="rose" icon={<Minus size={11} />} label="删除" count={im.deleted_count}
                active={tab === 'deleted'} onClick={() => im.deleted_count && toggleTab('deleted')} />
            </div>
            <div className="text-[10px] text-[var(--color-text-tertiary)]">
              入库前 {im.total_before} 行 → 入库后 {im.total_after} 行（未变 {im.unchanged_count} 行）
            </div>
            {tab === 'added' && <RowTable rows={im.added_sample} columns={sampleCols(im.added_sample, out.output_columns)} />}
            {tab === 'deleted' && <RowTable rows={im.deleted_sample} columns={sampleCols(im.deleted_sample, out.output_columns)} />}
            {tab === 'updated' && <UpdatedTable pairs={im.updated_sample} pk={im.keyed_by} />}
            {im.sample_truncated && tab && (
              <div className="text-[10px] text-[var(--color-text-tertiary)] italic">仅展示前若干条样本，完整明细以资产湖版本为准</div>
            )}
          </div>
        ) : (
          <div className="border-t border-border pt-2 text-[10.5px] text-[var(--color-text-tertiary)]">本次为手动运行，无原始入湖影响记录</div>
        )}
      </div>
    </div>
  )
}

function sampleCols(rows: Array<Record<string, unknown>>, fallback: string[]): string[] {
  if (fallback && fallback.length) return fallback
  const seen: string[] = []
  for (const r of rows) for (const k of Object.keys(r)) if (!seen.includes(k)) seen.push(k)
  return seen
}

function fmt(v: unknown): string {
  if (v === null || v === undefined) return ''
  if (typeof v === 'object') { try { return JSON.stringify(v) } catch { return String(v) } }
  return String(v)
}

function RowTable({ rows, columns }: { rows: Array<Record<string, unknown>>; columns: string[] }) {
  if (!rows.length) return <div className="text-[10.5px] text-[var(--color-text-tertiary)] py-2 text-center">无数据样本</div>
  const cols = columns.length ? columns : sampleCols(rows, [])
  return (
    <div className="mt-1.5 overflow-x-auto rounded-lg border border-border">
      <table className="w-full text-[10.5px] border-collapse">
        <thead className="bg-muted">
          <tr>{cols.map(c => <th key={c} className="px-2 py-1 text-left font-medium text-muted-foreground font-mono whitespace-nowrap">{c}</th>)}</tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i} className="border-t border-border">
              {cols.map(c => <td key={c} className="px-2 py-1 text-muted-foreground whitespace-nowrap max-w-[180px] truncate" title={fmt(r[c])}>{fmt(r[c])}</td>)}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function UpdatedTable({ pairs, pk }: { pairs: LakeImpactDetail['updated_sample']; pk: string[] | null }) {
  if (!pairs.length) return <div className="text-[10.5px] text-[var(--color-text-tertiary)] py-2 text-center">无数据样本</div>
  // 只展示发生变化的列（+ 主键列作为定位）
  return (
    <div className="mt-1.5 space-y-1.5">
      {pairs.map((p, i) => {
        const keys = Array.from(new Set([...Object.keys(p.before), ...Object.keys(p.after)]))
        const changed = keys.filter(k => fmt(p.before[k]) !== fmt(p.after[k]))
        const pkStr = (pk || []).map(k => `${k}=${fmt(p.after[k] ?? p.before[k])}`).join(', ')
        return (
          <div key={i} className="rounded-lg border border-[color-mix(in_srgb,var(--color-warning)_35%,transparent)] bg-[var(--color-warning-bg)] p-2">
            {pkStr && <div className="text-[10px] text-muted-foreground mb-1 font-mono">{pkStr}</div>}
            <div className="space-y-0.5">
              {changed.map(k => (
                <div key={k} className="flex items-center gap-1.5 text-[10.5px]">
                  <span className="text-muted-foreground font-mono shrink-0">{k}:</span>
                  <span className="text-viz-rose line-through truncate max-w-[130px]" title={fmt(p.before[k])}>{fmt(p.before[k]) || '∅'}</span>
                  <ArrowRight size={9} className="text-[var(--color-text-tertiary)] shrink-0" />
                  <span className="text-[var(--color-success)] truncate max-w-[130px]" title={fmt(p.after[k])}>{fmt(p.after[k]) || '∅'}</span>
                </div>
              ))}
            </div>
          </div>
        )
      })}
    </div>
  )
}

function ImpactChip({ tone, icon, label, count, active, onClick }: {
  tone: 'emerald' | 'amber' | 'rose'; icon: React.ReactNode; label: string
  count: number; active: boolean; onClick: () => void
}) {
  const map = {
    emerald: 'text-[var(--color-success)] bg-[var(--color-success-bg)] border-[color-mix(in_srgb,var(--color-success)_35%,transparent)]',
    amber:   'text-[var(--color-warning)] bg-[var(--color-warning-bg)] border-[color-mix(in_srgb,var(--color-warning)_35%,transparent)]',
    rose:    'text-viz-rose bg-viz-rose-soft border-viz-rose-soft',
  }[tone]
  const disabled = count === 0
  return (
    <button onClick={onClick} disabled={disabled}
      className={`flex items-center justify-center gap-1 px-1.5 py-1 rounded-lg border text-[11px] transition
        ${disabled ? 'text-[var(--color-text-tertiary)] bg-muted border-border cursor-default' : map}
        ${active ? 'ring-2 ring-offset-1 ring-current/30' : ''}`}>
      {icon}<span>{label}</span><b className="tabular-nums">{count}</b>
    </button>
  )
}

function Section({ title, icon, children }: { title: string; icon: React.ReactNode; children: React.ReactNode }) {
  return (
    <div className="rounded-lg border border-border bg-card p-2.5">
      <div className="flex items-center gap-1.5 text-[11px] font-medium text-muted-foreground mb-1.5">
        <span className="text-[var(--color-text-tertiary)]">{icon}</span>{title}
      </div>
      <div className="space-y-1">{children}</div>
    </div>
  )
}

function KV({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="flex justify-between gap-2">
      <span className="text-muted-foreground shrink-0">{label}</span>
      <span className={`text-foreground text-right break-all ${mono ? 'font-mono' : ''}`}>{value}</span>
    </div>
  )
}
