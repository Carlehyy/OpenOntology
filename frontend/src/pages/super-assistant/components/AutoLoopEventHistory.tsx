import { useEffect, useId, useRef, useState } from 'react'
import type { FormEvent, ReactNode } from 'react'
import { ArrowDown, ArrowLeft, ArrowRight, ChevronDown, Plus, RefreshCw, X } from 'lucide-react'
import {
  autoloopApi, type AutoLoopEventDetail, type AutoLoopEventTrace, type AutoLoopOutcome,
  type AutoLoopPage, type AutoLoopRoute, type AutoLoopRule, type AutoLoopTraceDetail, type AutoLoopTraceFilters,
} from '@/api/autoloop'
import { PageSizeSelect } from '@/components/PageSizeSelect'
import { Alert } from '@/components/ui/Alert'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { EmptyState } from '@/components/ui/LoadingState'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip'
import { errorText } from './assistantPanelUtils'
import { ActionSteps, IssueReference, UntrustedOriginal } from './AutoLoopDetails'
import {
  EVENT_ACTION_LABEL, EVENT_ERROR_LABEL, EVENT_OUTCOME, EVENT_OUTCOME_NOTE, EVENT_PENDING_ROUTE_NOTE, EVENT_ROUTE_LABEL,
  EVENT_SOURCE_LABEL, advancedConditionCount, appliedConditions, conditionReset, formatEventTime, formatEventTimeFull,
  promptProvenance, type AppliedConditionKey,
} from './autoloopDisplay'

export type TraceDrilldown = AutoLoopTraceFilters & { ruleName?: string }

const OUTCOME_OPTIONS = Object.fromEntries(Object.entries(EVENT_OUTCOME).map(([key, { label }]) => [key, label]))
const FILTER_ALL = '__all__'
const PAGE_SIZES = [10, 20, 50] as const
/** 「更多筛选」里按回车或「筛选流水」才生效的文本条件；下拉项即时生效。 */
const TEXT_FIELDS = ['sourceSystem', 'scheduledTaskId', 'scheduledRunId'] as const

function FilterSelect<T extends string>({ label, all, value, options, onChange, className }: {
  label: string
  all: string
  value?: T
  options: Record<T, string>
  onChange: (value: T | undefined) => void
  className?: string
}) {
  return (
    <Select value={value ?? FILTER_ALL} onValueChange={next => onChange(next === FILTER_ALL ? undefined : next as T)}>
      <SelectTrigger className={`min-w-0 shadow-none ${className ?? ''}`} aria-label={label}><SelectValue /></SelectTrigger>
      <SelectContent>
        <SelectItem value={FILTER_ALL}>{all}</SelectItem>
        {Object.entries<string>(options).map(([key, text]) => <SelectItem key={key} value={key}>{text}</SelectItem>)}
      </SelectContent>
    </Select>
  )
}

/** 「更多筛选」里的下拉项：触发器已有同名 aria-label，可见标签只负责与文本项视觉对齐。 */
function AdvancedField({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="min-w-0">
      <p aria-hidden="true" className="mb-1.5 flex text-sm font-medium text-[var(--color-text-primary)]">{label}</p>
      {children}
    </div>
  )
}

function OutcomeBadge({ outcome }: { outcome: AutoLoopOutcome }) {
  const { label, variant } = EVENT_OUTCOME[outcome]
  return <Badge className="whitespace-nowrap" variant={variant}>{label}</Badge>
}

/** 首次读取失败：标题、原因与重试同处一块，重试期间保留提示（不回落成骨架屏）。 */
function LoadFailure({ title, message, busy, retryLabel, onRetry }: {
  title: string
  message: string
  busy: boolean
  retryLabel: string
  onRetry: () => void
}) {
  return (
    <Alert role="alert" variant="danger">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
        <div className="min-w-0 flex-1">
          <p className="font-medium">{title}</p>
          <p className="mt-0.5 break-words text-xs leading-5">{message}</p>
        </div>
        <Button type="button" size="sm" variant="outline" className="shrink-0 max-md:h-11" loading={busy} onClick={onRetry}>{retryLabel}</Button>
      </div>
    </Alert>
  )
}

function DetailField({ label, wide, children }: { label: string; wide?: boolean; children: ReactNode }) {
  return (
    <div className={wide ? 'sm:col-span-2' : undefined}>
      <dt className="text-xs leading-5 text-muted-foreground">{label}</dt>
      <dd className="mt-0.5 min-w-0 break-words text-sm leading-6">{children}</dd>
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

function TraceDetail({ detail, onCreateRule, onOpenDecision }: {
  detail: AutoLoopTraceDetail
  onCreateRule: (trace: AutoLoopTraceDetail) => void
  onOpenDecision: (id: string) => void
}) {
  // 结论先行：路由结果、处理状态、当前可用动作；过程记录在其后分段展开。
  const note = detail.outcome === 'pending' ? EVENT_PENDING_ROUTE_NOTE[detail.route] : EVENT_OUTCOME_NOTE[detail.outcome]
  return (
    <div className="space-y-5">
      <div className="min-w-0 space-y-1">
        <h3 className="break-words text-base font-semibold leading-6">{detail.title?.trim() || '未命名事件'}</h3>
        <p className="break-all text-xs leading-5 text-muted-foreground">
          {EVENT_SOURCE_LABEL[detail.source.kind] ?? detail.source.kind} · {detail.source.system} · <time dateTime={detail.receivedAt}>{formatEventTimeFull(detail.receivedAt)}</time>
        </p>
      </div>

      <section aria-label="处理结论" className="space-y-3 rounded-xl border border-border p-4">
        {/* 路由结果与处理状态始终并排：小屏上把可用操作留在首屏内。 */}
        <dl className="grid grid-cols-2 gap-x-6 gap-y-3">
          <div>
            <dt className="text-xs leading-5 text-muted-foreground">路由结果</dt>
            <dd className="mt-0.5 break-words text-sm font-medium leading-6">
              {detail.rule
                ? <>{detail.rule.name || '未命名规则'}{' '}{detail.rule.version != null && <span className="font-normal text-muted-foreground">v{detail.rule.version}</span>}</>
                : EVENT_ROUTE_LABEL[detail.route]}
            </dd>
          </div>
          <div>
            <dt className="text-xs leading-5 text-muted-foreground">处理状态</dt>
            <dd className="mt-0.5 leading-6"><OutcomeBadge outcome={detail.outcome} /></dd>
          </div>
          {/* 溯源只到版本号：研判提示词正文不在事件详情展示。 */}
          {detail.route === 'assistant' && detail.outcome !== 'pending' && (
            <div className="col-span-2">
              <dt className="text-xs leading-5 text-muted-foreground">研判依据</dt>
              <dd className="mt-0.5 text-sm leading-6">{promptProvenance(detail.routingPromptVersion)}</dd>
            </div>
          )}
        </dl>
        <p className="text-sm leading-6">{note}</p>
        {detail.error && <Alert variant={detail.outcome === 'unknown' ? 'warning' : 'danger'}>{EVENT_ERROR_LABEL[detail.error] ?? detail.error}</Alert>}
        <div className="space-y-2 border-t border-border pt-3">
          {/* 小屏只留给读屏：分隔线已标出操作区，省下的一行让主操作留在首屏内。 */}
          <p className="sr-only text-xs font-medium leading-5 text-muted-foreground md:not-sr-only">可用操作</p>
          <div className="flex flex-wrap items-center gap-2">
            {detail.decisionItemId && <Button type="button" className="max-md:h-11" onClick={() => onOpenDecision(detail.decisionItemId!)}>查看决策事项</Button>}
            {/* 小屏收起装饰性图标：两个操作才排得进同一行，320px 宽的首屏内都完整可见。 */}
            <Button type="button" variant="outline" className="max-md:h-11" onClick={() => onCreateRule(detail)}><Plus size={14} className="max-md:hidden" aria-hidden="true" />按此事件建规则</Button>
          </div>
        </div>
      </section>

      <section aria-label="收到的事件" className="space-y-2">
        <h4 className="text-sm font-semibold leading-5">收到的事件</h4>
        <dl className="grid gap-x-6 gap-y-3 sm:grid-cols-2">
          <DetailField label="来源">
            {EVENT_SOURCE_LABEL[detail.source.kind] ?? detail.source.kind} · {detail.source.system}
            {detail.source.ref && <span className="block break-all font-mono text-xs leading-5 text-muted-foreground">{detail.source.ref}</span>}
          </DetailField>
          <DetailField label="接收时间"><time dateTime={detail.receivedAt}>{formatEventTimeFull(detail.receivedAt)}</time></DetailField>
          {detail.scheduledTaskId && <DetailField label="定时任务"><ScheduleLink taskId={detail.scheduledTaskId} runId={detail.scheduledRunId} /></DetailField>}
        </dl>
      </section>

      <section aria-label="处理结果" className="space-y-3">
        <h4 className="text-sm font-semibold leading-5">处理结果</h4>
        <dl className="grid gap-x-6 gap-y-3 sm:grid-cols-2">
          <DetailField label="处理动作">{detail.action ? EVENT_ACTION_LABEL[detail.action] ?? detail.action : '尚未生成动作'}</DetailField>
          {detail.issueRef && <DetailField label="关联 Multica 任务"><IssueReference issueRef={detail.issueRef} issueUrl={detail.issueUrl} /></DetailField>}
          {detail.reason && detail.reason !== detail.error && (
            <DetailField label="处理说明" wide>
              <p className="whitespace-pre-wrap">{detail.reason === 'first_matching_rule' ? '按匹配顺序选中第一条满足条件的启用规则。' : EVENT_ERROR_LABEL[detail.reason] ?? detail.reason}</p>
            </DetailField>
          )}
        </dl>
        {detail.actionSteps.length > 0 && <ActionSteps steps={detail.actionSteps} />}
      </section>

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
                <time className="text-muted-foreground" dateTime={record.happenedAt}>{formatEventTimeFull(record.happenedAt)}</time>
                {record.error && <span className="w-full break-words text-muted-foreground">{EVENT_ERROR_LABEL[record.error] ?? record.error}</span>}
              </li>)}
            </ol>
          </section>}
        </div>
      </details>
    </div>
  )
}

/** 无入站回执时的审计回落视图：保留去重标识与序号，原始内容不折叠。 */
function AuditEventDetail({ event, onOpenEvent, onOpenDecision }: {
  event: AutoLoopEventDetail
  onOpenEvent: (id: string) => void
  onOpenDecision: (id: string) => void
}) {
  return (
    <div className="space-y-5">
      <div className="min-w-0 space-y-1">
        <h3 className="break-words text-base font-semibold leading-6">{event.type}</h3>
        <p className="text-xs leading-5 text-muted-foreground">发生于 <time dateTime={event.happenedAt}>{formatEventTimeFull(event.happenedAt)}</time></p>
      </div>
      <dl className="grid gap-x-6 gap-y-3 sm:grid-cols-2">
        <DetailField label="事件 ID / 序号">{event.id} / {event.seq}</DetailField>
        <DetailField label="来源">{EVENT_SOURCE_LABEL[event.source.kind] ?? event.source.kind} · {event.source.system} · {event.source.ref}</DetailField>
        <DetailField label="去重标识">{event.dedup.key}</DetailField>
        {event.issueRef && <DetailField label="关联 Multica 任务"><IssueReference issueRef={event.issueRef} issueUrl={event.issueUrl} /></DetailField>}
      </dl>
      <div className="flex flex-wrap items-center gap-2">
        {event.causality.parentEventId && <Button size="sm" variant="outline" type="button" className="max-md:h-11" onClick={() => onOpenEvent(event.causality.parentEventId!)}>查看上游事件</Button>}
        {event.decisionItemId && <Button size="sm" variant="outline" type="button" className="max-md:h-11" onClick={() => onOpenDecision(event.decisionItemId!)}>查看决策事项</Button>}
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

/** 窄屏行内路由结果：命中时给规则名与版本。版本写成「（v3）」而非「v3」——
 *  详情里的路由结果是连续文本「规则甲 v3」，列表行（详情打开时仍留在 DOM 里）不得出现同一串文本。 */
function routeName(item: AutoLoopEventTrace) {
  return item.rule ? `${item.rule.name || '未命名规则'}${item.rule.version != null ? `（v${item.rule.version}）` : ''}` : EVENT_ROUTE_LABEL[item.route]
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
  const advancedId = useId()
  const staleId = useId()
  // applied 驱动请求；filters 是「更多筛选」文本项的草稿，未应用前不影响列表。
  const [applied, setApplied] = useState(() => initialFilters(scheduledTaskId, scheduledRunId, drilldown))
  const [filters, setFilters] = useState(applied)
  const [advancedOpen, setAdvancedOpen] = useState(false)
  const [pageSize, setPageSize] = useState(20)
  const [cursors, setCursors] = useState<(string | null)[]>([null])
  const [result, setResult] = useState<AutoLoopPage<AutoLoopEventTrace> | null>(null)
  const [updatedAt, setUpdatedAt] = useState<Date | null>(null)
  const [detail, setDetail] = useState<SelectedEvent | null>(null)
  const [error, setError] = useState('')
  const [detailError, setDetailError] = useState('')
  const [loading, setLoading] = useState(true)
  const [detailLoading, setDetailLoading] = useState(Boolean(selectedId))
  const [revision, setRevision] = useState(0)
  const tableScroll = useRef<HTMLDivElement>(null)
  const toolbar = useRef<HTMLDivElement>(null)
  const chips = useRef<HTMLUListElement>(null)
  const detailHeader = useRef<HTMLHeadingElement>(null)
  const openedBy = useRef<HTMLButtonElement>(null)
  // 打开详情那一刻的列表滚动位置；返回时原样还原（null = 详情不是从列表行打开的）。
  const savedScroll = useRef<number | null>(null)
  const cursor = cursors[cursors.length - 1]

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
    // 刷新与翻页保留已显示的行；失败时连同「上次成功加载」的时间一起提示。
    void autoloopApi.listEventTraces(applied, cursor, pageSize).then(data => {
      if (!current) return
      setResult(data)
      setUpdatedAt(new Date())
      setError('')
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

  const items = result?.items ?? []
  // 还没有任何结果（首次、条件变化后）才显示骨架屏；同一条件下的刷新与翻页保留已显示的行。
  // 不以 loading 判断：刷新时 loading 为真，但行必须留在原位，滚动位置才不会丢。
  const initialLoading = !result && !error
  // 刷新或翻页失败且已有行：保留上次结果并常驻提示，不渲染成空列表。
  const stale = Boolean(error) && items.length > 0
  const ruleOptions: Record<string, string> = Object.fromEntries(rules.map(rule => [rule.id, rule.name]))
  const selectedRuleId = applied.ruleId ?? filters.ruleId
  if (selectedRuleId && !ruleOptions[selectedRuleId]) ruleOptions[selectedRuleId] = drilldown?.ruleName ?? '历史规则'
  const conditions = appliedConditions(applied, ruleOptions)
  const advancedCount = advancedConditionCount(applied)
  const draftDirty = TEXT_FIELDS.some(field => (filters[field] ?? '').trim() !== (applied[field] ?? ''))

  function apply(value: AutoLoopTraceFilters) {
    setApplied(value)
    setCursors([null])
    setResult(null)
    setError('')
    tableScroll.current?.scrollTo({ top: 0 })
  }
  const reload = () => setRevision(value => value + 1)
  function focusToolbar() { requestAnimationFrame(() => toolbar.current?.focus()) }
  /** 下拉项即时生效：只改动对应字段，文本草稿原样保留。 */
  function setCondition(patch: Partial<AutoLoopTraceFilters>) {
    setFilters(previous => ({ ...previous, ...patch }))
    apply({ ...applied, ...patch })
  }
  function applyDraft(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const next = {
      ...filters,
      sourceSystem: filters.sourceSystem?.trim() || undefined,
      scheduledTaskId: filters.scheduledTaskId?.trim() || undefined,
      scheduledRunId: filters.scheduledRunId?.trim() || undefined,
    }
    setFilters(next)
    apply(next)
  }
  function clear() {
    setFilters({})
    apply({})
    focusToolbar()
  }
  /** 移除的是已应用条件；输入框里尚未应用的新草稿不被连带清掉。 */
  function removeCondition(key: AppliedConditionKey) {
    const index = conditions.findIndex(condition => condition.key === key)
    const reset = conditionReset(key)
    const edited = (Object.keys(reset) as (keyof AutoLoopTraceFilters)[]).some(field => filters[field] !== applied[field])
    if (!edited) setFilters(previous => ({ ...previous, ...reset }))
    apply({ ...applied, ...reset })
    // 键盘移除后焦点不丢：同位置的下一个条件 → 没有则上一个条件 → 条件清空则回到筛选行。
    // 刻意不落到「清除筛选」：刚移除一个条件就停在会清空全部条件的按钮上，回车即误触。
    const remaining = conditions.filter(condition => condition.key !== key)
    const target = remaining.length ? remaining[Math.min(Math.max(index, 0), remaining.length - 1)] : null
    requestAnimationFrame(() => {
      const next = target && chips.current?.querySelector<HTMLButtonElement>(`button[aria-label="移除筛选：${target.label}"]`)
      if (next) next.focus()
      else focusToolbar()
    })
  }
  function closeDetail() {
    onSelectEvent(null)
    requestAnimationFrame(() => {
      const scroller = tableScroll.current
      const opener = openedBy.current?.isConnected ? openedBy.current : null
      // 详情打开期间列表是 display:none，浏览器会丢弃它的滚动位置：回到列表先原样还原，再把焦点还给出发行。
      if (scroller && savedScroll.current !== null) { scroller.scrollTo({ top: savedScroll.current }); savedScroll.current = null }
      if (opener && scroller) {
        // 出发行还在可视区内就不让聚焦动作再滚动；不在（例如行数变少被夹住）才交给浏览器滚入可视区。
        const box = scroller.getBoundingClientRect(), row = opener.getBoundingClientRect()
        opener.focus({ preventScroll: row.top >= box.top && row.bottom <= box.bottom })
      } else (opener ?? scroller)?.focus()
    })
  }

  return (
    <TooltipProvider delayDuration={300}>
      <div className="flex min-h-0 min-w-0 flex-1 flex-col">
        <div hidden={Boolean(selectedId)} className={!selectedId ? 'flex min-h-0 flex-1 flex-col' : undefined}>
          {/* 筛选头整体封顶为面板高度减 7rem：小屏 / 小高度下展开高级筛选也不把表头和分页挤出弹窗，
              超出部分在筛选头内部滚动（唯一滚动区，不与表单嵌套）。常规尺寸下内容放得下，不会出现滚动。 */}
          <div className="max-h-[calc(100%-7rem)] shrink-0 overflow-x-hidden overflow-y-auto border-b border-border">
            {/* ≥768：单行 48px，筛选组不拉伸不换行，刷新固定靠右；窄屏：2×2 触控栅格。
                又窄又矮（宽 ≥480 且高 ≤480，即 1280×720 窗口 200% 缩放一类的视口）：收成一行，
                把竖向空间让给列表——此时两行工具栏会占掉面板一半以上高度。 */}
            <div ref={toolbar} role="group" aria-label="筛选事件流水" tabIndex={-1}
              className="grid grid-cols-2 items-center gap-2 rounded-lg px-4 py-2 outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring sm:px-5 [@media(min-width:480px)_and_(max-height:480px)]:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_minmax(0,1fr)_auto] md:flex md:h-12 md:flex-nowrap md:py-0">
              <div className="min-w-0 md:w-32 md:flex-none">
                <FilterSelect label="来源类型" all="全部来源" options={EVENT_SOURCE_LABEL} value={applied.source} className="max-md:h-11" onChange={source => setCondition({ source })} />
              </div>
              <div className="min-w-0 md:w-36 md:flex-none">
                {/* 「全部」项写明字段名：首行没有可见标签，占位文字同时说明这个下拉筛的是什么。 */}
                <FilterSelect<AutoLoopOutcome> label="处理状态" all="全部处理状态" options={OUTCOME_OPTIONS as Record<AutoLoopOutcome, string>} value={applied.outcome} className="max-md:h-11" onChange={outcome => setCondition({ outcome })} />
              </div>
              <Button type="button" variant="outline" aria-expanded={advancedOpen} aria-controls={advancedOpen ? advancedId : undefined}
                onClick={() => setAdvancedOpen(open => !open)}
                className={`justify-between gap-2 font-normal max-md:h-11 md:flex-none md:justify-center ${advancedOpen ? 'bg-muted hover:bg-muted' : ''}`}>
                <span>更多筛选</span>
                <span className="flex items-center gap-1.5">
                  {advancedCount > 0 && <><span aria-hidden="true" className="tabular-nums">· {advancedCount}</span><span className="sr-only">，已应用 {advancedCount} 项高级条件</span></>}
                  <ChevronDown size={14} className={`transition-transform ${advancedOpen ? 'rotate-180' : ''}`} aria-hidden="true" />
                </span>
              </Button>
              <Tooltip>
                <TooltipTrigger asChild>
                  <Button size="icon" variant="ghost" type="button" aria-label="刷新流水" disabled={loading} className="ml-auto max-md:size-11" onClick={reload}>
                    <RefreshCw size={15} className={loading ? 'animate-spin' : undefined} aria-hidden="true" />
                  </Button>
                </TooltipTrigger>
                <TooltipContent align="end">刷新流水</TooltipContent>
              </Tooltip>
            </div>

            {advancedOpen && (
              <form id={advancedId} aria-label="高级筛选" onSubmit={applyDraft}
                className="border-t border-border px-4 py-3 sm:px-5">
                <div className="grid grid-cols-1 items-end gap-x-4 gap-y-3 sm:grid-cols-2 lg:grid-cols-3">
                  <AdvancedField label="路由结果">
                    <FilterSelect<AutoLoopRoute> label="路由结果" all="全部路由" options={EVENT_ROUTE_LABEL} value={applied.route} className="max-md:h-11" onChange={route => setCondition({ route })} />
                  </AdvancedField>
                  <AdvancedField label="流水规则">
                    <FilterSelect label="流水规则" all="全部规则" options={ruleOptions} value={applied.ruleId} className="max-md:h-11" onChange={ruleId => setCondition({ ruleId })} />
                  </AdvancedField>
                  <div className="min-w-0"><Input label="来源系统" value={filters.sourceSystem ?? ''} onChange={event => setFilters({ ...filters, sourceSystem: event.target.value })} className="max-md:h-11" /></div>
                  <div className="min-w-0"><Input label="日程任务 ID" value={filters.scheduledTaskId ?? ''} onChange={event => setFilters({ ...filters, scheduledTaskId: event.target.value })} className="max-md:h-11" /></div>
                  <div className="min-w-0"><Input label="日程执行 ID" value={filters.scheduledRunId ?? ''} onChange={event => setFilters({ ...filters, scheduledRunId: event.target.value })} className="max-md:h-11" /></div>
                  <div className="flex items-center">
                    {/* 有未应用的文本修改时用主按钮提示；无修改时提交等同按当前草稿重新筛选。 */}
                    <Button type="submit" size="sm" variant={draftDirty ? 'default' : 'outline'} className="max-md:h-11 max-md:px-3">筛选流水</Button>
                  </div>
                </div>
              </form>
            )}

            {conditions.length > 0 && (
              <ul ref={chips} aria-label="已应用条件" className="flex flex-wrap items-center gap-1.5 px-4 pb-3 sm:px-5">
                {conditions.map(condition => (
                  <li key={condition.key} className="min-w-0 max-w-full">
                    <Badge variant="outline" className="max-w-full gap-0.5 py-0.5 pr-0.5 font-normal">
                      <span className="min-w-0 break-all leading-5" title={condition.full}>
                        <span className="text-muted-foreground">{condition.label}：</span>{condition.text}
                        {condition.full && <span className="sr-only">（{condition.full}）</span>}
                      </span>
                      <button type="button" aria-label={`移除筛选：${condition.label}`} onClick={() => removeCondition(condition.key)}
                        className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full outline-none transition-colors hover:bg-muted hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring">
                        <X size={12} aria-hidden="true" />
                      </button>
                    </Badge>
                  </li>
                ))}
                <li><Button type="button" variant="ghost" size="sm" className="h-7 max-md:h-9" onClick={clear}>清除筛选</Button></li>
              </ul>
            )}

            {stale && (
              <Alert id={staleId} role="alert" variant="warning" className="mx-4 mb-3 sm:mx-5">
                <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
                  <p className="min-w-0 flex-1">
                    事件流水加载失败，以下为上次成功加载的结果{updatedAt && <>（<time dateTime={updatedAt.toISOString()}>{formatEventTimeFull(updatedAt)}</time>）</>}，可能与当前条件不一致。
                  </p>
                  <Button type="button" size="sm" variant="outline" className="shrink-0 max-md:h-11" loading={loading} onClick={reload}>重试加载事件流水</Button>
                </div>
                <p className="mt-1 break-words text-xs leading-5">{error}</p>
              </Alert>
            )}
          </div>

          <div ref={tableScroll} tabIndex={-1} className="min-h-0 flex-1 overflow-auto outline-none" aria-busy={loading} aria-describedby={stale ? staleId : undefined}>
            {initialLoading ? (
              <div className="p-4 sm:p-5" role="status"><span className="sr-only">加载事件流水…</span><div aria-hidden="true" className="space-y-3">{[0, 1, 2, 3].map(row => <div key={row} className="h-11 rounded-md bg-muted" />)}</div></div>
            ) : error && !stale ? (
              <div className="p-4 sm:p-5"><LoadFailure title="事件流水加载失败" message={error} busy={loading} retryLabel="重试加载事件流水" onRetry={reload} /></div>
            ) : result && items.length === 0 ? (
              <EmptyState className="min-h-full justify-center py-10" title={conditions.length ? '没有符合条件的事件' : '暂无事件'}
                description={conditions.length ? '逐个移除上方已应用条件，或清除筛选后再试。' : '只显示进入当前账号路由流程的事件。外部通知或定时任务发来的事件会出现在这里。'} />
            ) : result ? (
              <>
                {loading && <p role="status" className="sr-only">正在刷新事件流水…</p>}
                <table className="w-full table-fixed border-collapse text-left text-sm" aria-label="事件流水">
                  <caption className="sr-only">按接收时间从新到旧排列</caption>
                  <thead className="sticky top-0 z-[1] bg-card text-xs text-muted-foreground">
                    <tr className="border-b border-border">
                      <th scope="col" className="px-3 py-2.5 font-medium leading-4 sm:pl-5">事件与来源</th>
                      <th scope="col" className="hidden w-32 px-3 py-2.5 font-medium leading-4 md:table-cell">路由结果</th>
                      <th scope="col" className="hidden w-36 px-3 py-2.5 font-medium leading-4 md:table-cell">处理状态</th>
                      <th scope="col" aria-sort="descending" className="hidden w-44 px-3 py-2.5 font-medium leading-4 lg:table-cell"><span className="inline-flex items-center gap-1">接收时间 <ArrowDown size={12} aria-hidden="true" /></span></th>
                      <th scope="col" className="w-20 px-3 py-2.5 text-right font-medium leading-4 sm:w-24 sm:pr-5">操作</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-border">
                    {items.map(item => (
                      <tr key={item.id} className="transition-colors hover:bg-muted">
                        {/* 标题与来源合并；接收时间在 <lg 时并入来源行，路由结果与处理状态在 ≥md 各自成列。
                            窄屏把「路由结果 + 处理状态」紧放在标题下、来源与时间之后：行高被压缩时（如 200% 缩放的矮视口）
                            最先被裁掉的是次要的来源信息，而不是结论。 */}
                        <td className="px-3 py-3 align-middle sm:pl-5">
                          <p className="line-clamp-2 break-words text-sm font-medium leading-5" title={item.title ?? undefined}>{item.title || '未命名事件'}</p>
                          <div className="mt-1 flex flex-wrap items-center gap-1.5 md:hidden">
                            <span className="max-w-full truncate text-xs font-medium leading-5">{routeName(item)}</span>
                            <OutcomeBadge outcome={item.outcome} />
                          </div>
                          <div className="mt-0.5 flex flex-wrap items-center justify-between gap-x-3 gap-y-0.5">
                            <p className="min-w-0 max-w-full truncate text-xs leading-[18px] text-muted-foreground">
                              {EVENT_SOURCE_LABEL[item.source.kind] ?? item.source.kind}{item.source.system && <> · {item.source.system}</>}
                            </p>
                            <time dateTime={item.receivedAt} className="shrink-0 whitespace-nowrap font-mono text-xs leading-[18px] text-muted-foreground lg:hidden">{formatEventTime(item.receivedAt)}</time>
                          </div>
                        </td>
                        <td className="hidden px-3 py-3 align-middle md:table-cell">
                          <p className="truncate text-sm font-medium leading-5" title={item.rule?.name ?? undefined}>{item.rule ? item.rule.name || '未命名规则' : EVENT_ROUTE_LABEL[item.route]}</p>
                          {item.rule?.version != null && <p className="mt-0.5 text-xs leading-[18px] text-muted-foreground">规则命中 · v{item.rule.version}</p>}
                        </td>
                        <td className="hidden px-3 py-3 align-middle md:table-cell"><OutcomeBadge outcome={item.outcome} /></td>
                        <td className="hidden whitespace-nowrap px-3 py-3 align-middle font-mono text-xs text-muted-foreground lg:table-cell"><time dateTime={item.receivedAt}>{formatEventTime(item.receivedAt)}</time></td>
                        <td className="px-3 py-3 text-right align-middle sm:pr-5">
                          <Button type="button" size="sm" variant="ghost" aria-label={`查看详情 ${item.title || '未命名事件'}`} className="px-1.5 text-brand-ink max-md:h-11 sm:px-2"
                            onClick={event => { openedBy.current = event.currentTarget; savedScroll.current = tableScroll.current?.scrollTop ?? 0; onSelectEvent(item.id); requestAnimationFrame(() => detailHeader.current?.focus()) }}>查看详情</Button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </>
            ) : null}
          </div>
          <div className="flex shrink-0 flex-wrap items-center justify-between gap-2 border-t border-border px-4 py-2 text-xs text-muted-foreground sm:px-5" aria-label="事件分页">
            <div className="flex items-center gap-2"><span>每页</span><PageSizeSelect ariaLabel="每页事件数" value={pageSize} sizes={PAGE_SIZES} disabled={loading} className="h-8 w-20 rounded-lg bg-card px-2 text-xs max-md:h-11" onChange={value => { setPageSize(value); setCursors([null]); tableScroll.current?.scrollTo({ top: 0 }) }} /></div>
            <div className="flex flex-wrap items-center gap-1 sm:gap-2">
              <span className="mr-1 tabular-nums" aria-live="polite">第 {cursors.length} 页{!loading && !error ? ` · 本页 ${items.length} 条` : ''}</span>
              <Button type="button" size="sm" variant="ghost" className="px-2 max-md:h-11" disabled={loading || cursors.length === 1} onClick={() => { setCursors(value => value.slice(0, -1)); tableScroll.current?.scrollTo({ top: 0 }) }}><ArrowLeft size={14} aria-hidden="true" />上一页</Button>
              <Button type="button" size="sm" variant="ghost" className="px-2 max-md:h-11" disabled={loading || Boolean(error) || !result?.hasMore || !result.nextCursor} onClick={() => { setCursors(value => [...value, result!.nextCursor]); tableScroll.current?.scrollTo({ top: 0 }) }}>下一页<ArrowRight size={14} aria-hidden="true" /></Button>
            </div>
          </div>
        </div>

        {selectedId && <div className="flex min-h-0 flex-1 flex-col" aria-label="事件详情" role="region">
          <div className="flex min-h-12 shrink-0 items-center justify-between gap-2 border-b border-border px-4 py-2 sm:px-5">
            <Button type="button" size="sm" variant="ghost" className="max-md:h-11" onClick={closeDetail}><ArrowLeft size={14} aria-hidden="true" />返回流水</Button>
            <h2 ref={detailHeader} tabIndex={-1} className="rounded text-sm font-semibold outline-none focus-visible:ring-2 focus-visible:ring-ring">事件详情</h2>
            <Tooltip>
              <TooltipTrigger asChild>
                <Button type="button" variant="ghost" size="icon" aria-label="刷新事件详情" disabled={detailLoading} className="max-md:size-11" onClick={reload}>
                  <RefreshCw size={15} className={detailLoading ? 'animate-spin' : undefined} aria-hidden="true" />
                </Button>
              </TooltipTrigger>
              <TooltipContent align="end">刷新事件详情</TooltipContent>
            </Tooltip>
          </div>
          <div className="min-h-0 flex-1 overflow-auto p-4 sm:p-5" aria-busy={detailLoading}>
            {detailLoading ? <p className="text-sm text-muted-foreground" role="status">加载事件详情…</p>
              : detailError ? <LoadFailure title="事件详情加载失败" message={detailError} busy={false} retryLabel="重试加载事件详情" onRetry={reload} />
                : detail && ('trace' in detail ? <TraceDetail detail={detail.trace} onCreateRule={onCreateRule} onOpenDecision={onOpenDecision} /> : <AuditEventDetail event={detail.audit} onOpenEvent={onSelectEvent} onOpenDecision={onOpenDecision} />)}
          </div>
        </div>}
      </div>
    </TooltipProvider>
  )
}
