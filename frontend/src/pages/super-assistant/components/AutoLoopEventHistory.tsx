import { useEffect, useRef, useState } from 'react'
import { ArrowLeft, ArrowRight, RefreshCw } from 'lucide-react'
import { autoloopApi, type AutoLoopEvent, type AutoLoopEventDetail, type AutoLoopEventFilters, type AutoLoopRule } from '@/api/autoloop'
import { Alert } from '@/components/ui/Alert'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { formatDateTime } from '@/utils/datetime'
import { errorText } from './assistantPanelUtils'
import { ActionSteps, IssueReference, UntrustedOriginal } from './AutoLoopDetails'

export default function AutoLoopEventHistory({ initialEventId, scheduledTaskId, scheduledRunId, rules, onOpenDecision }: {
  initialEventId?: string | null
  scheduledTaskId?: string | null
  scheduledRunId?: string | null
  rules: AutoLoopRule[]
  onOpenDecision: (id: string) => void
}) {
  const [filters, setFilters] = useState<AutoLoopEventFilters>({ scheduledTaskId: scheduledTaskId || undefined, scheduledRunId: scheduledRunId || undefined })
  const [applied, setApplied] = useState<AutoLoopEventFilters>(filters)
  const [items, setItems] = useState<AutoLoopEvent[]>([])
  const [cursor, setCursor] = useState<string | null>(null)
  const [previous, setPrevious] = useState<(string | null)[]>([])
  const [next, setNext] = useState<string | null>(null)
  const [selectedId, setSelectedId] = useState<string | null>(initialEventId || null)
  const [detail, setDetail] = useState<AutoLoopEventDetail | null>(null)
  const [error, setError] = useState('')
  const [detailError, setDetailError] = useState('')
  const [loading, setLoading] = useState(true)
  const [detailLoading, setDetailLoading] = useState(false)
  const [revision, setRevision] = useState(0)
  const listRequest = useRef(0)
  const detailRequest = useRef(0)

  useEffect(() => {
    setSelectedId(initialEventId || null)
  }, [initialEventId])
  useEffect(() => {
    const updated = { scheduledTaskId: scheduledTaskId || undefined, scheduledRunId: scheduledRunId || undefined }
    setFilters(updated); setApplied(updated); setCursor(null); setPrevious([])
  }, [scheduledTaskId, scheduledRunId])
  useEffect(() => {
    const generation = ++listRequest.current
    setLoading(true); setError('')
    void autoloopApi.listEvents(applied, cursor).then(result => {
      if (generation !== listRequest.current) return
      setItems(result.items); setNext(result.hasMore ? result.nextCursor : null)
    }).catch(err => { if (generation === listRequest.current) setError(errorText(err, '事件流水加载失败')) })
      .finally(() => { if (generation === listRequest.current) setLoading(false) })
    return () => { listRequest.current++ }
  }, [applied, cursor, revision])
  useEffect(() => {
    const generation = ++detailRequest.current
    setDetail(null); setDetailError('')
    if (!selectedId) { setDetailLoading(false); return }
    setDetailLoading(true)
    void autoloopApi.getEvent(selectedId).then(result => {
      if (generation === detailRequest.current) setDetail(result)
    }).catch(err => { if (generation === detailRequest.current) setDetailError(errorText(err, '事件详情加载失败')) })
      .finally(() => { if (generation === detailRequest.current) setDetailLoading(false) })
    return () => { detailRequest.current++ }
  }, [selectedId, revision])

  return <div className="flex min-h-0 flex-1 flex-col">
    <form className="grid shrink-0 grid-cols-2 gap-2 border-b border-border p-3 sm:grid-cols-4 lg:grid-cols-5" onSubmit={event => { event.preventDefault(); setApplied({ ...filters }); setCursor(null); setPrevious([]); setSelectedId(null) }} aria-label="流水过滤">
      <Input label="来源类型" value={filters.source ?? ''} placeholder="notification / scheduled" onChange={event => setFilters({ ...filters, source: event.target.value || undefined })} />
      <Input label="来源系统" value={filters.sourceSystem ?? ''} onChange={event => setFilters({ ...filters, sourceSystem: event.target.value || undefined })} />
      <Input label="动作" value={filters.action ?? ''} placeholder="create_task / append_task" onChange={event => setFilters({ ...filters, action: event.target.value || undefined })} />
      <Input label="动作状态" value={filters.status ?? ''} placeholder="failed,unknown" onChange={event => setFilters({ ...filters, status: event.target.value || undefined })} />
      <div className="space-y-1.5"><span className="text-sm font-medium">规则</span><Select value={filters.ruleId || 'all'} onValueChange={value => setFilters({ ...filters, ruleId: value === 'all' ? undefined : value })}><SelectTrigger aria-label="流水规则"><SelectValue placeholder="全部规则" /></SelectTrigger><SelectContent><SelectItem value="all">全部规则</SelectItem>{rules.map(rule => <SelectItem key={rule.id} value={rule.id}>{rule.name}</SelectItem>)}</SelectContent></Select></div>
      <Input label="日程任务 ID" value={filters.scheduledTaskId ?? ''} onChange={event => setFilters({ ...filters, scheduledTaskId: event.target.value || undefined })} />
      <Input label="日程执行 ID" value={filters.scheduledRunId ?? ''} onChange={event => setFilters({ ...filters, scheduledRunId: event.target.value || undefined })} />
      <div className="flex items-end gap-1"><Button size="sm" type="submit" disabled={loading}>筛选流水</Button><Button type="button" size="icon-sm" variant="ghost" aria-label="刷新流水" onClick={() => setRevision(value => value + 1)}><RefreshCw size={14} /></Button></div>
    </form>
    {error && <Alert role="alert" variant="danger" className="m-3">{error}</Alert>}
    <div className="flex min-h-0 flex-1">
      <div className={`${selectedId ? 'hidden md:flex' : 'flex'} min-h-0 w-full flex-col border-r border-border md:w-2/5`}>
        <div className="min-h-0 flex-1 overflow-y-auto p-3">
          {loading ? <p role="status" className="text-sm text-muted-foreground">加载流水…</p> : items.length === 0 ? <p className="py-10 text-center text-sm text-muted-foreground">没有符合条件的事件。</p> : items.map(item => <button key={item.id} type="button" onClick={() => setSelectedId(item.id)} aria-pressed={selectedId === item.id} className={`mb-2 w-full rounded-lg border p-3 text-left outline-none focus-visible:ring-2 focus-visible:ring-ring ${selectedId === item.id ? 'border-brand bg-brand-soft' : 'border-border hover:bg-muted'}`}>
            <span className="flex flex-wrap items-center gap-2 text-xs"><Badge variant="secondary">{item.source.kind}</Badge><span className="break-all">{item.source.system}</span><span className="text-muted-foreground">#{item.seq}</span></span>
            <span className="mt-2 block break-all text-sm font-medium">{item.type}</span><span className="mt-1 block break-all text-xs">{item.action || '尚未执行'}</span><span className="mt-1 block text-xs text-muted-foreground">{formatDateTime(item.happenedAt)}</span>
            {item.error && <span className="mt-2 block break-words text-xs text-[var(--color-danger)]">{item.error}</span>}
          </button>)}
        </div>
        <div className="flex shrink-0 justify-between border-t border-border p-2">
          <Button size="sm" variant="ghost" disabled={loading || previous.length === 0} onClick={() => { setCursor(previous[previous.length - 1]); setPrevious(value => value.slice(0, -1)) }}><ArrowLeft size={13} />上一页</Button>
          <Button size="sm" variant="ghost" disabled={loading || !next} onClick={() => { setPrevious(value => [...value, cursor]); setCursor(next) }}>下一页<ArrowRight size={13} /></Button>
        </div>
      </div>
      <div className={`${selectedId ? 'flex' : 'hidden md:flex'} min-h-0 min-w-0 flex-1 flex-col overflow-y-auto p-4`}>
        {selectedId && <Button size="sm" variant="ghost" className="mb-3 self-start md:hidden" onClick={() => setSelectedId(null)}><ArrowLeft size={14} />返回流水</Button>}
        {detailLoading ? <p role="status" className="text-sm text-muted-foreground">加载事件详情…</p> : detailError ? <Alert role="alert" variant="danger">{detailError}</Alert> : !detail ? <p className="py-12 text-center text-sm text-muted-foreground">选择事件查看来源、因果与动作步骤。</p> : <div className="space-y-4">
          <h2 className="break-words text-base font-semibold">{detail.type}</h2>
          <dl className="space-y-2 break-all text-xs leading-5">
            <div><dt className="text-muted-foreground">事件 ID / 序号</dt><dd>{detail.id} / {detail.seq}</dd></div>
            <div><dt className="text-muted-foreground">来源</dt><dd>{detail.source.kind} · {detail.source.system} · {detail.source.ref}</dd></div>
            <div><dt className="text-muted-foreground">因果</dt><dd>{detail.causality.parentEventId ? <Button variant="link" size="sm" onClick={() => setSelectedId(detail.causality.parentEventId!)}>{detail.causality.parentEventId}</Button> : '无父事件'} · hops {detail.causality.hops}</dd></div>
            <div><dt className="text-muted-foreground">去重</dt><dd>{detail.dedup.key}{detail.dedup.duplicateOf && <> · 重复于 <Button variant="link" size="sm" onClick={() => setSelectedId(detail.dedup.duplicateOf!)}>{detail.dedup.duplicateOf}</Button></>}</dd></div>
            <div><dt className="text-muted-foreground">规则版本 / 动作</dt><dd>{detail.ruleVersion ?? '无'} / {detail.action || '尚未执行'}</dd></div>
            {detail.issueRef && <div><dt className="text-muted-foreground">Issue</dt><dd><IssueReference issueRef={detail.issueRef} issueUrl={detail.issueUrl} /></dd></div>}
            {detail.scheduledTaskId && <div><dt className="text-muted-foreground">日程执行</dt><dd><a className="text-brand-ink underline" href={`#/super-assistant?schedule=${encodeURIComponent(detail.scheduledTaskId)}${detail.scheduledRunId ? `&scheduleRun=${encodeURIComponent(detail.scheduledRunId)}` : ''}`}>查看日程执行 {detail.scheduledRunId || detail.scheduledTaskId}</a></dd></div>}
          </dl>
          {detail.decisionItemId && <Button size="sm" variant="outline" onClick={() => onOpenDecision(detail.decisionItemId!)}>查看决策事项</Button>}
          {detail.error && <Alert variant="danger">{detail.error}</Alert>}
          <ActionSteps steps={detail.actionSteps} /><UntrustedOriginal value={detail.untrustedOriginal} />
        </div>}
      </div>
    </div>
  </div>
}
