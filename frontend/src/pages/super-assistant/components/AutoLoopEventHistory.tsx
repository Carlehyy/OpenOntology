import { useEffect, useRef, useState } from 'react'
import { ArrowDown, ArrowLeft, ArrowRight, ChevronDown, List, Plus, RefreshCw } from 'lucide-react'
import {
  autoloopApi, type AutoLoopEventDetail, type AutoLoopEventTrace, type AutoLoopOutcome,
  type AutoLoopPage, type AutoLoopRoute, type AutoLoopRule, type AutoLoopTraceDetail, type AutoLoopTraceFilters,
} from '@/api/autoloop'
import { PageSizeSelect } from '@/components/PageSizeSelect'
import { Alert } from '@/components/ui/Alert'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { formatDateTime } from '@/utils/datetime'
import { errorText } from './assistantPanelUtils'
import { ActionSteps, IssueReference, UntrustedOriginal } from './AutoLoopDetails'
import { EVENT_ACTION_LABEL, EVENT_ERROR_LABEL, EVENT_OUTCOME, EVENT_ROUTE_LABEL, EVENT_SOURCE_LABEL } from './autoloopDisplay'

export type TraceDrilldown = AutoLoopTraceFilters & { ruleName?: string }

const OUTCOME_OPTIONS = Object.fromEntries(Object.entries(EVENT_OUTCOME).map(([key, { label }]) => [key, label]))
const FILTER_ALL = '__all__'
const PAGE_SIZES = [10, 20, 50] as const
const TIME_FORMAT = { seconds: true, fallback: '时间未知' }

function FilterSelect<T extends string>({ label, all, value, options, onChange }: {
  label: string
  all: string
  value?: T
  options: Record<T, string>
  onChange: (value: T | undefined) => void
}) {
  return (
    <Select value={value ?? FILTER_ALL} onValueChange={next => onChange(next === FILTER_ALL ? undefined : next as T)}>
      <SelectTrigger className="min-w-0 shadow-none" aria-label={label}><SelectValue /></SelectTrigger>
      <SelectContent>
        <SelectItem value={FILTER_ALL}>{all}</SelectItem>
        {Object.entries<string>(options).map(([key, text]) => <SelectItem key={key} value={key}>{text}</SelectItem>)}
      </SelectContent>
    </Select>
  )
}

function OutcomeBadge({ outcome }: { outcome: AutoLoopOutcome }) {
  const { label, variant } = EVENT_OUTCOME[outcome]
  return <Badge className="whitespace-nowrap" variant={variant}>{label}</Badge>
}

function TraceDetail({ detail, onCreateRule, onOpenDecision }: {
  detail: AutoLoopTraceDetail
  onCreateRule: (trace: AutoLoopTraceDetail) => void
  onOpenDecision: (id: string) => void
}) {
  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 space-y-1">
          <h3 className="break-words text-base font-semibold">{detail.title?.trim() || '未命名事件'}</h3>
          <p className="break-words text-xs leading-5 text-muted-foreground">
            {EVENT_SOURCE_LABEL[detail.source.kind] ?? detail.source.kind} · {detail.source.system} · {formatDateTime(detail.receivedAt, TIME_FORMAT)}
          </p>
        </div>
        <OutcomeBadge outcome={detail.outcome} />
      </div>

      <dl className="grid gap-4 rounded-xl border border-border p-4 text-sm sm:grid-cols-2">
        <div>
          <dt className="mb-1 text-xs text-muted-foreground">路由结果</dt>
          <dd className="break-words font-medium">
            {detail.rule ? <>{detail.rule.name || '未命名规则'} {detail.rule.version !== null && <span className="font-normal text-muted-foreground">v{detail.rule.version}</span>}</> : EVENT_ROUTE_LABEL[detail.route]}
          </dd>
        </div>
        <div>
          <dt className="mb-1 text-xs text-muted-foreground">处理动作</dt>
          <dd>{detail.action ? EVENT_ACTION_LABEL[detail.action] ?? detail.action : '尚未生成动作'}</dd>
        </div>
        {detail.issueRef && <div><dt className="mb-1 text-xs text-muted-foreground">关联 Multica 任务</dt><dd><IssueReference issueRef={detail.issueRef} issueUrl={detail.issueUrl} /></dd></div>}
        {detail.scheduledTaskId && <div><dt className="mb-1 text-xs text-muted-foreground">定时任务</dt><dd><ScheduleLink taskId={detail.scheduledTaskId} runId={detail.scheduledRunId} /></dd></div>}
        {detail.reason && detail.reason !== detail.error && <div className="sm:col-span-2"><dt className="mb-1 text-xs text-muted-foreground">处理说明</dt><dd className="whitespace-pre-wrap break-words">{detail.reason === 'first_matching_rule' ? '按匹配顺序选中第一条满足条件的启用规则。' : EVENT_ERROR_LABEL[detail.reason] ?? detail.reason}</dd></div>}
      </dl>

      {detail.error && <Alert variant={detail.outcome === 'unknown' ? 'warning' : 'danger'}>{EVENT_ERROR_LABEL[detail.error] ?? detail.error}</Alert>}
      <div className="flex flex-wrap gap-2">
        <Button size="sm" variant="outline" type="button" onClick={() => onCreateRule(detail)}><Plus size={14} aria-hidden="true" />按此事件建规则</Button>
        {detail.decisionItemId && <Button size="sm" type="button" onClick={() => onOpenDecision(detail.decisionItemId!)}>查看决策事项</Button>}
      </div>
      {detail.actionSteps.length > 0 && <ActionSteps steps={detail.actionSteps} />}
      <details className="group rounded-lg border border-border">
        <summary className="flex cursor-pointer list-none items-center justify-between gap-2 rounded-lg px-3 py-2.5 text-sm font-medium outline-none focus-visible:ring-2 focus-visible:ring-ring [&::-webkit-details-marker]:hidden">
          原始记录 <ChevronDown size={15} className="text-muted-foreground group-open:rotate-180" aria-hidden="true" />
        </summary>
        <div className="space-y-3 border-t border-border p-3">
          <p className="break-all font-mono text-xs text-muted-foreground">事件 ID {detail.id}</p>
          <UntrustedOriginal value={detail.untrustedOriginal} />
          {detail.auditEvents.length > 0 && <section className="space-y-2" aria-label="处理记录">
            <h4 className="text-xs font-medium">最近处理记录</h4>
            <ol className="max-h-48 divide-y divide-border overflow-auto rounded-lg border border-border">
              {detail.auditEvents.map(record => <li key={record.id} className="flex flex-wrap justify-between gap-x-3 gap-y-1 px-3 py-2 text-xs">
                <span className="break-all font-mono">{record.type}</span>
                <time className="text-muted-foreground" dateTime={record.happenedAt}>{formatDateTime(record.happenedAt, TIME_FORMAT)}</time>
                {record.error && <span className="w-full break-words text-muted-foreground">{EVENT_ERROR_LABEL[record.error] ?? record.error}</span>}
              </li>)}
            </ol>
          </section>}
        </div>
      </details>
    </div>
  )
}

function ScheduleLink({ taskId, runId }: { taskId: string; runId?: string | null }) {
  return (
    <a className="break-words text-brand-ink underline-offset-4 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring" href={`#/super-assistant?schedule=${encodeURIComponent(taskId)}${runId ? `&scheduleRun=${encodeURIComponent(runId)}` : ''}`}>
      查看日程执行
    </a>
  )
}

function AuditEventDetail({ event, onOpenEvent, onOpenDecision }: {
  event: AutoLoopEventDetail
  onOpenEvent: (id: string) => void
  onOpenDecision: (id: string) => void
}) {
  return (
    <div className="space-y-4">
      <div>
        <h3 className="break-words text-base font-semibold">{event.type}</h3>
        <p className="mt-1 text-sm text-muted-foreground">{formatDateTime(event.happenedAt, TIME_FORMAT)}</p>
      </div>
      <dl className="space-y-1.5 break-all rounded-lg border border-border p-3 text-xs leading-5">
        <div><dt className="font-medium">事件 ID / 序号</dt><dd>{event.id} / {event.seq}</dd></div>
        <div><dt className="font-medium">来源</dt><dd>{EVENT_SOURCE_LABEL[event.source.kind] ?? event.source.kind} · {event.source.system} · {event.source.ref}</dd></div>
        <div><dt className="font-medium">去重标识</dt><dd>{event.dedup.key}</dd></div>
        {event.issueRef && <div><dt className="font-medium">关联 Multica 任务</dt><dd><IssueReference issueRef={event.issueRef} issueUrl={event.issueUrl} /></dd></div>}
      </dl>
      <div className="flex flex-wrap items-center gap-2">
        {event.causality.parentEventId && <Button size="sm" variant="outline" type="button" onClick={() => onOpenEvent(event.causality.parentEventId!)}>查看上游事件</Button>}
        {event.decisionItemId && <Button size="sm" variant="outline" type="button" onClick={() => onOpenDecision(event.decisionItemId!)}>查看决策事项</Button>}
        {event.scheduledTaskId && <ScheduleLink taskId={event.scheduledTaskId} runId={event.scheduledRunId} />}
      </div>
      {event.error && <Alert variant="danger">{EVENT_ERROR_LABEL[event.error] ?? event.error}</Alert>}
      {event.actionSteps.length > 0 && <ActionSteps steps={event.actionSteps} />}
      <UntrustedOriginal value={event.untrustedOriginal} />
    </div>
  )
}

type SelectedEvent = { trace: AutoLoopTraceDetail } | { audit: AutoLoopEventDetail }

function initialFilters(scheduledTaskId?: string | null, scheduledRunId?: string | null, drilldown?: TraceDrilldown | null): AutoLoopTraceFilters {
  const { ruleName: _ruleName, ...filters } = drilldown ?? {}
  return { scheduledTaskId: scheduledTaskId || undefined, scheduledRunId: scheduledRunId || undefined, ...filters }
}

export default function AutoLoopEventHistory({ active, selectedId, onSelectEvent, scheduledTaskId, scheduledRunId, rules, drilldown, onCreateRule, onOpenDecision }: {
  active: boolean
  selectedId: string | null
  onSelectEvent: (id: string | null) => void
  scheduledTaskId?: string | null
  scheduledRunId?: string | null
  rules: AutoLoopRule[]
  drilldown?: TraceDrilldown | null
  onCreateRule: (trace?: AutoLoopTraceDetail) => void
  onOpenDecision: (id: string) => void
}) {
  const [applied, setApplied] = useState(() => initialFilters(scheduledTaskId, scheduledRunId, drilldown))
  const [filters, setFilters] = useState(applied)
  const [pageSize, setPageSize] = useState(20)
  const [cursors, setCursors] = useState<(string | null)[]>([null])
  const [result, setResult] = useState<AutoLoopPage<AutoLoopEventTrace> | null>(null)
  const [detail, setDetail] = useState<SelectedEvent | null>(null)
  const [error, setError] = useState('')
  const [detailError, setDetailError] = useState('')
  const [loading, setLoading] = useState(true)
  const [detailLoading, setDetailLoading] = useState(Boolean(selectedId))
  const [revision, setRevision] = useState(0)
  const tableScroll = useRef<HTMLDivElement>(null)
  const detailHeader = useRef<HTMLHeadingElement>(null)
  const openedBy = useRef<HTMLButtonElement>(null)
  const cursor = cursors[cursors.length - 1]
  const hasFilters = Object.values(applied).some(Boolean)

  useEffect(() => {
    const value = initialFilters(scheduledTaskId, scheduledRunId, drilldown)
    setApplied(value)
    setFilters(value)
    setCursors([null])
    tableScroll.current?.scrollTo({ top: 0 })
  }, [scheduledTaskId, scheduledRunId, drilldown])

  useEffect(() => {
    if (!active) return
    let current = true
    setLoading(true)
    setError('')
    void autoloopApi.listEventTraces(applied, cursor, pageSize).then(data => {
      if (current) setResult(data)
    }).catch(err => {
      if (current) setError(errorText(err, '事件流水加载失败，请刷新重试。'))
    }).finally(() => { if (current) setLoading(false) })
    return () => { current = false }
  }, [active, applied, cursor, pageSize, revision])

  useEffect(() => {
    if (!active || !selectedId) return
    let current = true
    setDetail(null)
    setDetailError('')
    setDetailLoading(true)
    void autoloopApi.getEventTrace(selectedId).then(async trace => {
      if (!current) return
      if (trace) setDetail({ trace })
      else {
        const audit = await autoloopApi.getEvent(selectedId)
        if (current) setDetail({ audit })
      }
    }).catch(err => {
      if (current) setDetailError(errorText(err, '事件详情加载失败，请重试。'))
    }).finally(() => { if (current) setDetailLoading(false) })
    return () => { current = false }
  }, [active, selectedId, revision])

  function apply(value: AutoLoopTraceFilters) {
    setApplied(value)
    setCursors([null])
    tableScroll.current?.scrollTo({ top: 0 })
  }
  function clear() { setFilters({}); apply({}) }
  function closeDetail() {
    onSelectEvent(null)
    requestAnimationFrame(() => {
      if (openedBy.current?.isConnected) openedBy.current.focus()
      else tableScroll.current?.focus()
    })
  }
  const ruleOptions = Object.fromEntries(rules.map(rule => [rule.id, rule.name]))
  if (filters.ruleId && !ruleOptions[filters.ruleId]) ruleOptions[filters.ruleId] = drilldown?.ruleName ?? '历史规则'
  const items = result?.items ?? []

  return (
    <div className="flex min-h-0 min-w-0 flex-1 flex-col">
      <div hidden={Boolean(selectedId)} className={!selectedId ? 'flex min-h-0 flex-1 flex-col' : undefined}>
        <div className="shrink-0 space-y-2 border-b border-border px-3 py-3 sm:px-5">
          <div className="flex flex-col gap-2 sm:flex-row sm:flex-wrap sm:items-center" role="group" aria-label="筛选事件流水">
            <div className="grid min-w-0 grid-cols-2 gap-2 sm:flex sm:flex-1 sm:flex-wrap">
              <div className="min-w-0 sm:w-32"><FilterSelect label="来源类型" all="全部来源" options={EVENT_SOURCE_LABEL} value={applied.source} onChange={source => { const next = { ...applied, source }; setFilters(next); apply(next) }} /></div>
              <div className="min-w-0 sm:w-32"><FilterSelect<AutoLoopRoute> label="路由结果" all="全部路由" options={EVENT_ROUTE_LABEL} value={applied.route} onChange={route => { const next = { ...applied, route }; setFilters(next); apply(next) }} /></div>
              <div className="min-w-0 sm:w-36"><FilterSelect<AutoLoopOutcome> label="处理状态" all="全部状态" options={OUTCOME_OPTIONS as Record<AutoLoopOutcome, string>} value={applied.outcome} onChange={outcome => { const next = { ...applied, outcome }; setFilters(next); apply(next) }} /></div>
              <div className="min-w-0 sm:w-36"><FilterSelect label="流水规则" all="全部规则" options={ruleOptions} value={applied.ruleId} onChange={ruleId => { const next = { ...applied, ruleId }; setFilters(next); apply(next) }} /></div>
            </div>
            <div className="ml-auto flex items-center gap-2 self-start">
              <Button size="icon" variant="ghost" type="button" aria-label="刷新流水" title="刷新流水" disabled={loading} onClick={() => setRevision(value => value + 1)}><RefreshCw size={15} aria-hidden="true" /></Button>
              <Button type="button" onClick={() => onCreateRule()}><Plus size={15} aria-hidden="true" />新建规则</Button>
            </div>
          </div>
          <div className="flex min-w-0 flex-wrap items-start justify-between gap-x-3 gap-y-1">
            <details className="group min-w-0 grow">
              <summary className="inline-flex cursor-pointer list-none items-center gap-1.5 rounded-md px-1 py-1.5 text-xs font-medium text-muted-foreground outline-none hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring [&::-webkit-details-marker]:hidden">
                更多筛选 <ChevronDown size={13} className="group-open:rotate-180" aria-hidden="true" />
              </summary>
              <form className="mt-2 grid max-h-[26dvh] grid-cols-1 items-end gap-2 overflow-y-auto px-1 pb-1 sm:grid-cols-2 lg:grid-cols-3" aria-label="高级筛选" onSubmit={event => { event.preventDefault(); apply({ ...filters, sourceSystem: filters.sourceSystem?.trim() || undefined, scheduledTaskId: filters.scheduledTaskId?.trim() || undefined, scheduledRunId: filters.scheduledRunId?.trim() || undefined }) }}>
                <Input label="来源系统" value={filters.sourceSystem ?? ''} onChange={event => setFilters({ ...filters, sourceSystem: event.target.value })} />
                <Input label="日程任务 ID" value={filters.scheduledTaskId ?? ''} onChange={event => setFilters({ ...filters, scheduledTaskId: event.target.value })} />
                <Input label="日程执行 ID" value={filters.scheduledRunId ?? ''} onChange={event => setFilters({ ...filters, scheduledRunId: event.target.value })} />
                <Button type="submit" size="sm" variant="outline" className="w-fit">筛选流水</Button>
              </form>
            </details>
            {hasFilters && <Button type="button" variant="ghost" size="sm" className="h-7" onClick={clear}>清除筛选</Button>}
          </div>
          {(applied.since || applied.until || applied.scheduledTaskId || applied.scheduledRunId || applied.sourceSystem) && (
            <div className="flex flex-wrap gap-1.5" aria-label="已应用筛选">
              {(applied.since || applied.until) && <Badge variant="secondary" className="max-w-full whitespace-normal">接收时间：{formatDateTime(applied.since, TIME_FORMAT)} 至 {formatDateTime(applied.until, TIME_FORMAT)}</Badge>}
              {applied.sourceSystem && <Badge variant="secondary" className="max-w-full break-all">来源系统：{applied.sourceSystem}</Badge>}
              {(applied.scheduledTaskId || applied.scheduledRunId) && <Badge className="max-w-full break-all" variant="secondary" title={applied.scheduledRunId || applied.scheduledTaskId}>限定{applied.scheduledRunId ? '日程执行' : '定时任务'}</Badge>}
            </div>
          )}
        </div>

        <div ref={tableScroll} tabIndex={-1} className="min-h-0 flex-1 overflow-auto outline-none" aria-busy={loading}>
          {error ? <Alert role="alert" variant="danger" className="m-4">{error}</Alert> : loading ? (
            <div className="p-4 sm:p-5" role="status"><span className="sr-only">加载事件流水…</span><div aria-hidden="true" className="space-y-3">{[0, 1, 2, 3].map(row => <div key={row} className="h-11 rounded-md bg-muted" />)}</div></div>
          ) : items.length === 0 ? (
            <div className="flex min-h-full flex-col items-center justify-center gap-2 px-4 py-10 text-center">
              <span className="rounded-xl bg-muted p-2.5 text-muted-foreground"><List size={18} aria-hidden="true" /></span>
              <p className="text-sm font-medium">{hasFilters ? '没有符合条件的事件' : '暂无事件'}</p>
              <p className="text-xs text-muted-foreground">{hasFilters ? '调整筛选条件后重试。' : '外部通知或定时任务发来的事件会出现在这里。'}</p>
            </div>
          ) : (
            <table className="w-full table-fixed border-collapse text-left text-sm" aria-label="事件流水">
              <thead className="sticky top-0 z-[1] bg-card text-xs text-muted-foreground">
                <tr className="border-b border-border">
                  <th scope="col" className="px-3 py-3 font-medium sm:pl-5">事件</th>
                  <th scope="col" className="hidden w-32 px-3 py-3 font-medium lg:table-cell">来源</th>
                  <th scope="col" className="hidden w-40 px-3 py-3 font-medium lg:table-cell">路由结果</th>
                  <th scope="col" className="hidden w-32 px-3 py-3 font-medium xl:table-cell">处理状态</th>
                  <th scope="col" aria-sort="descending" className="hidden w-44 px-3 py-3 font-medium xl:table-cell"><span className="inline-flex items-center gap-1">接收时间 <ArrowDown size={12} aria-hidden="true" /></span></th>
                  <th scope="col" className="w-20 px-2 py-3 text-right font-medium sm:w-24 sm:pr-5">操作</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {items.map(item => (
                  <tr key={item.id} className="transition-colors hover:bg-muted">
                    <td className="px-3 py-3 align-middle sm:pl-5">
                      <p className="truncate font-medium" title={item.title ?? undefined}>{item.title || '未命名事件'}</p>
                      <p className="mt-1 truncate text-xs text-muted-foreground lg:hidden">{EVENT_SOURCE_LABEL[item.source.kind] ?? item.source.kind} · {item.rule?.name || EVENT_ROUTE_LABEL[item.route]}</p>
                      <div className="mt-1.5 flex flex-wrap items-center gap-1.5 xl:hidden"><OutcomeBadge outcome={item.outcome} /><time dateTime={item.receivedAt} className="font-mono text-xs text-muted-foreground">{formatDateTime(item.receivedAt, TIME_FORMAT)}</time></div>
                    </td>
                    <td className="hidden px-3 py-3 align-middle lg:table-cell"><p className="truncate text-xs font-medium">{EVENT_SOURCE_LABEL[item.source.kind] ?? item.source.kind}</p><p title={item.source.system} className="mt-1 truncate text-xs text-muted-foreground">{item.source.system}</p></td>
                    <td className="hidden px-3 py-3 align-middle text-xs lg:table-cell"><p className="truncate font-medium" title={item.rule?.name ?? undefined}>{item.rule?.name || EVENT_ROUTE_LABEL[item.route]}</p>{item.rule?.version != null && <p className="mt-1 text-muted-foreground">规则命中 · v{item.rule.version}</p>}</td>
                    <td className="hidden px-3 py-3 align-middle xl:table-cell"><OutcomeBadge outcome={item.outcome} /></td>
                    <td className="hidden whitespace-nowrap px-3 py-3 align-middle font-mono text-xs text-muted-foreground xl:table-cell"><time dateTime={item.receivedAt}>{formatDateTime(item.receivedAt, TIME_FORMAT)}</time></td>
                    <td className="px-2 py-3 text-right align-middle sm:pr-5">
                      <Button type="button" size="sm" variant="ghost" aria-label={`查看详情 ${item.title || '未命名事件'}`} className="px-1.5 text-brand-ink sm:px-2" onClick={event => { openedBy.current = event.currentTarget; onSelectEvent(item.id); requestAnimationFrame(() => detailHeader.current?.focus()) }}>查看详情</Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
        <div className="flex shrink-0 flex-wrap items-center justify-between gap-2 border-t border-border px-3 py-2 text-xs text-muted-foreground sm:px-5" aria-label="事件分页">
          <div className="flex items-center gap-2"><span>每页</span><PageSizeSelect ariaLabel="每页事件数" value={pageSize} sizes={PAGE_SIZES} disabled={loading} onChange={value => { setPageSize(value); setCursors([null]); tableScroll.current?.scrollTo({ top: 0 }) }} /></div>
          <div className="flex flex-wrap items-center gap-1 sm:gap-2">
            <span className="mr-1 tabular-nums" aria-live="polite">第 {cursors.length} 页{!loading && !error ? ` · 本页 ${items.length} 条` : ''}</span>
            <Button type="button" size="sm" variant="ghost" className="px-2" disabled={loading || cursors.length === 1} onClick={() => { setCursors(value => value.slice(0, -1)); tableScroll.current?.scrollTo({ top: 0 }) }}><ArrowLeft size={14} aria-hidden="true" />上一页</Button>
            <Button type="button" size="sm" variant="ghost" className="px-2" disabled={loading || Boolean(error) || !result?.hasMore || !result.nextCursor} onClick={() => { setCursors(value => [...value, result!.nextCursor]); tableScroll.current?.scrollTo({ top: 0 }) }}>下一页<ArrowRight size={14} aria-hidden="true" /></Button>
          </div>
        </div>
      </div>

      {selectedId && <div className="flex min-h-0 flex-1 flex-col" aria-label="事件详情" role="region">
        <div className="flex shrink-0 items-center justify-between gap-2 border-b border-border px-3 py-2 sm:px-5">
          <Button type="button" size="sm" variant="ghost" onClick={closeDetail}><ArrowLeft size={14} aria-hidden="true" />返回流水</Button>
          <h2 ref={detailHeader} tabIndex={-1} className="rounded text-sm font-semibold outline-none focus-visible:ring-2 focus-visible:ring-ring">事件详情</h2>
          <Button type="button" variant="ghost" size="icon-sm" aria-label="刷新事件详情" title="刷新事件详情" disabled={detailLoading} onClick={() => setRevision(value => value + 1)}><RefreshCw size={15} aria-hidden="true" /></Button>
        </div>
        <div className="min-h-0 flex-1 overflow-auto p-4 sm:p-5" aria-busy={detailLoading}>
          {detailLoading ? <p className="text-sm text-muted-foreground" role="status">加载事件详情…</p>
            : detailError ? <Alert role="alert" variant="danger">{detailError}</Alert>
              : detail && ('trace' in detail ? <TraceDetail detail={detail.trace} onCreateRule={onCreateRule} onOpenDecision={onOpenDecision} /> : <AuditEventDetail event={detail.audit} onOpenEvent={onSelectEvent} onOpenDecision={onOpenDecision} />)}
        </div>
      </div>}
    </div>
  )
}
