import { useCallback, useEffect, useRef, useState } from 'react'
import { ArrowLeft, ArrowRight, ListChecks, RefreshCw } from 'lucide-react'
import { v4 as uuidv4 } from 'uuid'
import { autoloopApi, type DecisionItem, type DecisionSummary } from '@/api/autoloop'
import { Alert } from '@/components/ui/Alert'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { formatDateTime } from '@/utils/datetime'
import { DialogShell } from './AssistantConfiguration'
import { errorText } from './assistantPanelUtils'
import { acceptDecision, canMarkHandled, DECISION_RECORD_FILTERS, DECISION_STATUS_LABEL, DECISION_VIEWS, isRecommendedLabel, isUnfinished, PRIORITY_LABEL, sortDecisions, SOURCE_LABEL, type DecisionRecordFilter, type DecisionRequest, type DecisionSession, type DecisionView } from './autoloopLogic'
import { ActionSteps, IssueReference, textAreaClass, UntrustedOriginal } from './AutoLoopDetails'
import { AssistantMarkdown } from './AssistantConversation'

export function useDecisionSummary(enabled = true) {
  const [summary, setSummary] = useState<DecisionSummary | null>(null)
  const generation = useRef(0)
  const refresh = useCallback(async () => {
    if (!enabled) return
    const request = ++generation.current
    try {
      const value = await autoloopApi.decisionSummary()
      if (request === generation.current) setSummary(value)
    } catch { /* 保留最后一次已确认计数；详情中可刷新看到服务错误。 */ }
  }, [enabled])
  useEffect(() => {
    if (!enabled) return
    void refresh()
    const timer = window.setInterval(() => { if (document.visibilityState === 'visible') void refresh() }, 15000)
    return () => { window.clearInterval(timer); generation.current++ }
  }, [refresh, enabled])
  return { summary, refresh }
}

export default function DecisionItemsDialog({ initialId, summary, session, onSelect, onOpenEvent, onRefreshSummary, onClose }: {
  initialId?: string | null
  summary: DecisionSummary | null
  session: DecisionSession
  onSelect: (id: string | null) => void
  onOpenEvent: (id: string) => void
  onRefreshSummary: () => Promise<void>
  onClose: () => void
}) {
  const [view, setView] = useState<DecisionView>('pending')
  const [recordFilter, setRecordFilter] = useState<DecisionRecordFilter>('all')
  const [confirmHandled, setConfirmHandled] = useState(false)
  const [items, setItems] = useState<DecisionItem[]>([])
  const [selected, setSelected] = useState<string | null>(initialId || null)
  const [detail, setDetail] = useState<DecisionItem | null>(null)
  const [cursor, setCursor] = useState<string | null>(null)
  const [previous, setPrevious] = useState<(string | null)[]>([])
  const [next, setNext] = useState<string | null>(null)
  const [revision, setRevision] = useState(0)
  const [listLoading, setListLoading] = useState(true)
  const [detailLoading, setDetailLoading] = useState(false)
  const [error, setError] = useState('')
  const [detailError, setDetailError] = useState('')
  const [operationError, setOperationError] = useState('')
  const [notice, setNotice] = useState('')
  const [optionId, setOptionId] = useState('')
  const [freeText, setFreeText] = useState('')
  const [busy, setBusy] = useState<'decide' | 'handled' | null>(null)
  const [uncertain, setUncertain] = useState<DecisionRequest | null>(initialId ? session.requests.get(initialId) ?? null : null)
  const active = useRef(true)
  const listRequest = useRef(0)
  const detailRequest = useRef(0)
  const mutationLock = useRef(false)
  const handledTrigger = useRef<HTMLButtonElement>(null)
  const detailRegion = useRef<HTMLDivElement>(null)
  const listKey = useRef('')
  const locatedSelection = useRef<string | null>(null)
  const statusFilter = view === 'pending' ? DECISION_VIEWS.pending.status : DECISION_RECORD_FILTERS[recordFilter].status
  const selectedRef = useRef(selected)
  const detailRef = useRef(detail)
  selectedRef.current = selected
  detailRef.current = detail
  const merge = useCallback((value: DecisionItem) => {
    const result = acceptDecision(session.items.get(value.id) ?? null, value)
    session.items.set(value.id, result)
    if (!isUnfinished(result.status)) session.requests.delete(result.id)
    return result
  }, [session])
  function select(id: string | null) { setSelected(id); onSelect(id) }
  function changeView(nextView: DecisionView, nextFilter: DecisionRecordFilter = 'all') {
    setView(nextView); setRecordFilter(nextFilter); setCursor(null); setPrevious([]); select(null)
  }
  useEffect(() => { active.current = true; return () => { active.current = false; listRequest.current++; detailRequest.current++ } }, [])
  useEffect(() => { setSelected(initialId || null) }, [initialId])
  useEffect(() => {
    const generation = ++listRequest.current
    const key = `${statusFilter}:${cursor ?? ''}`
    setListLoading(listKey.current !== key)
    if (listKey.current !== key) setError('')
    listKey.current = key
    void autoloopApi.listDecisions(statusFilter, cursor).then(result => {
      if (generation !== listRequest.current) return
      const statuses = statusFilter.split(',')
      setItems(sortDecisions(result.items.map(merge).filter(item => statuses.includes(item.status))))
      setNext(result.hasMore ? result.nextCursor : null)
      setError('')
    }).catch(err => { if (generation === listRequest.current) setError(errorText(err, '决策事项加载失败')) })
      .finally(() => { if (generation === listRequest.current) setListLoading(false) })
    return () => { listRequest.current++ }
  }, [statusFilter, cursor, revision, merge])

  const loadDetail = useCallback(async (id: string, background = false) => {
    const generation = ++detailRequest.current
    if (!background) { setDetailLoading(true); setDetailError('') }
    try {
      const value = await autoloopApi.getDecision(id)
      if (!active.current || generation !== detailRequest.current || selectedRef.current !== id) return
      const previous = detailRef.current?.id === id ? detailRef.current : null
      const merged = merge(value)
      if (background && previous && merged.contentVersion > previous.contentVersion && previous.status === 'open') {
        setNotice(merged.status === 'open' ? '事项内容已更新。请重新阅读问题并确认答复。' : '事项状态已更新，请查看最新处理结果。')
      }
      setDetail(merged)
      setDetailError('')
      setUncertain(session.requests.get(id) ?? null)
    } catch (err) {
      if (active.current && generation === detailRequest.current) setDetailError(errorText(err, '决策详情加载失败'))
    } finally {
      if (active.current && generation === detailRequest.current) setDetailLoading(false)
    }
  }, [merge, session])
  useEffect(() => {
    const sync = () => {
      const id = selectedRef.current
      if (id) {
        setDetail(session.items.get(id) ?? null)
        setUncertain(session.requests.get(id) ?? null)
        if (!session.inFlight.has(id)) setBusy(null)
        void loadDetail(id, true)
      }
      setRevision(value => value + 1)
      void onRefreshSummary()
    }
    session.listeners.add(sync)
    return () => { session.listeners.delete(sync) }
  }, [session, onRefreshSummary, loadDetail])
  useEffect(() => {
    const request = selected ? session.requests.get(selected) ?? null : null
    detailRegion.current?.scrollTo({ top: 0 })
    setDetail(selected ? session.items.get(selected) ?? null : null); setDetailError(''); setOperationError(''); setNotice(''); setOptionId(''); setFreeText(''); setUncertain(request)
    setBusy(selected && session.inFlight.has(selected) ? request?.kind ?? null : null)
    if (selected) void loadDetail(selected)
    return () => { detailRequest.current++ }
  }, [selected, loadDetail, session])
  useEffect(() => { setOptionId(''); setFreeText(''); setConfirmHandled(false) }, [detail?.id, detail?.contentVersion])
  useEffect(() => {
    if (!selected) { locatedSelection.current = null; return }
    if (!detail || detail.id !== selected || locatedSelection.current === selected) return
    locatedSelection.current = selected
    if (detail.status !== 'open' && view === 'pending') {
      setView('records'); setRecordFilter('all'); setCursor(null); setPrevious([])
    }
  }, [selected, detail, view])
  useEffect(() => {
    const timer = window.setInterval(() => {
      if (!mutationLock.current && !confirmHandled && document.visibilityState === 'visible') {
        if (selected) void loadDetail(selected, true)
        void onRefreshSummary()
        setRevision(value => value + 1)
      }
    }, 10000)
    return () => window.clearInterval(timer)
  }, [selected, loadDetail, onRefreshSummary, confirmHandled])

  useEffect(() => {
    if (notice || operationError) detailRegion.current?.scrollTo({ top: 0 })
  }, [notice, operationError])

  async function submit(kind: 'decide' | 'handled', retry?: DecisionRequest) {
    if (!detail || mutationLock.current || session.inFlight.has(detail.id) || (!retry && session.requests.has(detail.id))) return
    setConfirmHandled(false)
    const request: DecisionRequest = retry ?? {
      id: detail.id, kind,
      body: { expectedContentVersion: detail.contentVersion, idempotencyKey: uuidv4(), ...(kind === 'decide' ? { optionId: optionId || undefined, freeText: freeText.trim() || undefined } : {}) },
    }
    // 父页面持有原请求与已确认事实；选择切换、关闭重开不会产生新键或旧事实复活。
    session.requests.set(request.id, request)
    session.inFlight.add(request.id)
    mutationLock.current = true
    detailRequest.current++; listRequest.current++
    setDetailLoading(false); setBusy(kind); setUncertain(request); setDetailError(''); setOperationError(''); setNotice('')
    try {
      const value = request.kind === 'decide' ? await autoloopApi.decide(request.id, request.body) : await autoloopApi.markHandled(request.id, request.body)
      const merged = merge(value)
      session.requests.delete(request.id)
      if (!active.current) return
      if (selectedRef.current === request.id) {
        setDetail(merged)
        setUncertain(null)
        setOptionId('')
        setFreeText('')
        if (value.alreadyProcessed) setNotice('该请求已处理，已显示已有结果。')
        else if (request.kind === 'handled') setNotice('本地事项已关闭，已移入处理记录。关联任务请在原系统查看。')
        else if (!isUnfinished(merged.status)) setNotice('决定已完成。')
        else if (merged.status === 'writeback_failed' || merged.status === 'writeback_unknown') setNotice('决定已受理，处理结果见下方步骤。')
        else if (merged.source === 'watchdog') setNotice('决定已受理，等待写回 Multica 评论。')
        else if (merged.issueRef) setNotice('决定已受理，等待重新匹配规则和写回 Multica 评论。')
        else setNotice('决定已受理，等待重新匹配规则。')
      }
      setRevision(value => value + 1)
      void onRefreshSummary()
    } catch (err) {
      const status = (err as { status?: number })?.status
      const definitive = !!status && status >= 400 && status < 500
      if (definitive) session.requests.delete(request.id)
      if (!active.current || selectedRef.current !== request.id) return
      if (status === 409 || status === 412) {
        setUncertain(null); setOptionId(''); setFreeText('')
        setNotice(status === 412 ? '事项版本已变化。已刷新当前内容与步骤，请重新阅读后确认本次操作。' : '当前操作未被接受。已刷新状态与步骤，请结合返回原因确认后续处理。')
        setOperationError(errorText(err, '服务端拒绝了当前状态的操作'))
        await loadDetail(request.id)
        setRevision(value => value + 1)
        void onRefreshSummary()
      } else if (definitive) {
        setUncertain(null)
        setOperationError(errorText(err, '请求未被接受'))
      } else {
        setUncertain(request)
        setOperationError('请求结果尚未确认。请刷新查看处理结果，或重试同一次提交。')
        await loadDetail(request.id)
      }
    } finally {
      session.inFlight.delete(request.id)
      mutationLock.current = false
      if (active.current) setBusy(null)
      for (const listener of session.listeners) listener()
    }
  }
  const actionable = detail?.status === 'open'
  const handledAllowed = !!detail && canMarkHandled(detail)
  const attentionCount = summary ? summary.unknownCount + summary.failedCount : null
  const submitHint = detail?.source === 'watchdog'
    ? '提交后，答复将作为评论写回关联任务。'
    : detail?.issueRef
      ? '提交后会记录决定、重新匹配已启用的规则，并将答复作为评论写回关联任务。'
      : '提交后会保存你的决定，并按已启用的规则确定下一步。'

  return <>
    <DialogShell
      title="决策事项"
      description="处理需要你决定的事项，查看后续执行结果。"
      size="xwide"
      icon={<ListChecks size={18} />}
      onClose={onClose}
      onEscapeKeyDown={event => { if (confirmHandled) { event.preventDefault(); setConfirmHandled(false) } }}
      // 遮罩空白点击与右上关闭等效，但二次确认打开时只由确认层处理。
      onInteractOutside={event => { if (confirmHandled) event.preventDefault() }}
      contentClassName="h-[90dvh] sm:h-[88dvh]"
    >
      <div className="flex shrink-0 items-center gap-2 border-b border-border px-3 py-2 sm:px-5">
        <div role="tablist" aria-label="决策分区" className="flex items-center gap-1" onKeyDown={event => {
          if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key) || busy) return
          event.preventDefault()
          const nextView = event.key === 'Home' ? 'pending' : event.key === 'End' ? 'records' : view === 'pending' ? 'records' : 'pending'
          changeView(nextView)
          event.currentTarget.querySelector<HTMLButtonElement>(`#decision-tab-${nextView}`)?.focus()
        }}>
          {(Object.keys(DECISION_VIEWS) as DecisionView[]).map(key => <Button key={key} id={`decision-tab-${key}`} role="tab" aria-selected={view === key} aria-controls={`decision-panel-${key}`} tabIndex={view === key ? 0 : -1} disabled={!!busy} variant="ghost" className={`h-10 rounded-lg px-3 ${view === key ? 'bg-brand-soft text-brand-ink hover:bg-brand-soft hover:text-brand-ink' : ''}`} onClick={() => changeView(key)}>
            {DECISION_VIEWS[key].label}{key === 'pending' && summary && <span className="font-mono text-xs">{summary.openCount}</span>}
          </Button>)}
        </div>
        <Button size="sm" variant="ghost" className="ml-auto h-10 px-2 text-xs" disabled={!!busy} onClick={() => changeView('records', 'attention')}>需关注 {attentionCount ?? '—'}</Button>
        <Button size="icon-sm" variant="ghost" className="h-10 w-10 shrink-0" aria-label="刷新决策事项" disabled={!!busy} onClick={() => { setRevision(value => value + 1); if (selected) void loadDetail(selected); void onRefreshSummary() }}><RefreshCw size={16} /></Button>
      </div>
      {error && <Alert role="alert" variant="danger" className="m-3">{error}</Alert>}
      <div id={`decision-panel-${view}`} role="tabpanel" aria-labelledby={`decision-tab-${view}`} className="flex min-h-0 flex-1">
        <div className={`${selected ? 'hidden md:flex' : 'flex'} min-h-0 w-full shrink-0 flex-col border-r border-border bg-background md:w-72 lg:w-80`}>
          {view === 'records' && <div role="group" aria-label="处理记录筛选" className="grid shrink-0 grid-cols-4 gap-1 border-b border-border p-3">
            {(Object.keys(DECISION_RECORD_FILTERS) as DecisionRecordFilter[]).map(key => <Button key={key} size="sm" variant="ghost" aria-pressed={recordFilter === key} disabled={!!busy} className={`px-1 ${recordFilter === key ? 'bg-muted text-foreground' : ''}`} onClick={() => changeView('records', key)}>{DECISION_RECORD_FILTERS[key].label}</Button>)}
          </div>}
          <section aria-label="事项列表" className="min-h-0 flex-1 overflow-y-auto p-3">
            {listLoading ? <p role="status" className="p-3 text-sm text-muted-foreground">加载事项…</p> : items.length === 0 ? <p className="px-3 py-12 text-center text-sm leading-6 text-muted-foreground">{view === 'pending' ? '当前没有需要你处理的事项。' : '暂无处理记录。'}</p> : items.map(item => <button key={item.id} type="button" disabled={!!busy} onClick={() => select(item.id)} aria-pressed={selected === item.id} className={`mb-2 w-full rounded-xl border p-3 text-left outline-none transition-colors focus-visible:ring-2 focus-visible:ring-ring ${selected === item.id ? 'border-brand bg-brand-soft' : 'border-border bg-card hover:bg-muted'}`}>
              <span className="flex flex-wrap gap-2"><Badge variant={item.priority === 'high' ? 'warning' : 'secondary'}>{PRIORITY_LABEL[item.priority]}优先</Badge>{view === 'records' && <Badge variant={['writeback_unknown', 'writeback_failed'].includes(item.status) ? 'warning' : 'secondary'}>{DECISION_STATUS_LABEL[item.status]}</Badge>}</span>
              <span className="mt-2 block break-words text-sm font-medium leading-6">{item.title}</span>
              <span className="mt-2 block text-xs leading-5 text-muted-foreground">{SOURCE_LABEL[item.source]} · {item.status === 'open' ? '等待自' : '建立于'} {formatDateTime(item.createdAt)}</span>
            </button>)}
          </section>
          <div className="flex shrink-0 justify-between border-t border-border p-2"><Button size="sm" variant="ghost" disabled={!!busy || listLoading || !previous.length} onClick={() => { setCursor(previous[previous.length - 1]); setPrevious(value => value.slice(0, -1)) }}><ArrowLeft size={14} />上一页</Button><Button size="sm" variant="ghost" disabled={!!busy || listLoading || !next} onClick={() => { setPrevious(value => [...value, cursor]); setCursor(next) }}>下一页<ArrowRight size={14} /></Button></div>
        </div>
        <div className={`${selected ? 'flex' : 'hidden md:flex'} min-h-0 min-w-0 flex-1 flex-col`}>
          {selected && <div className="shrink-0 border-b border-border px-3 py-1 md:hidden"><Button size="sm" variant="ghost" disabled={!!busy} className="h-10" onClick={() => select(null)}><ArrowLeft size={16} />返回事项列表</Button></div>}
          <div ref={detailRegion} tabIndex={-1} data-testid="decision-detail-scroll" className="min-h-0 flex-1 overflow-y-auto p-4 outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring sm:p-6">
            {notice && <Alert role="status" className="mb-4">{notice}</Alert>}
            {operationError && <Alert role="alert" variant="danger" className="mb-4">{operationError}</Alert>}
            {detailError && <Alert role="alert" variant="danger" className="mb-4">{detailError}</Alert>}
            {detailLoading && !detail ? <p role="status" className="text-sm text-muted-foreground">加载决策详情…</p> : !detail ? <p className="py-12 text-center text-sm text-muted-foreground">选择事项查看问题与处理结果。</p> : <div key={detail.id} className="space-y-5">
              <header className="space-y-3">
                <div className="flex flex-wrap items-center gap-2"><Badge variant={['writeback_unknown', 'writeback_failed'].includes(detail.status) ? 'warning' : detail.status === 'completed' ? 'success' : 'secondary'}>{DECISION_STATUS_LABEL[detail.status]}</Badge><Badge variant="outline">{SOURCE_LABEL[detail.source]}</Badge><span className="ml-auto text-xs text-muted-foreground">内容版本 {detail.contentVersion}</span></div>
                <h2 className="break-words text-base font-semibold leading-7">{detail.title}</h2>
              </header>
              <section aria-label="发生了什么" className="space-y-2"><h3 className="text-xs font-medium text-muted-foreground">发生了什么</h3><AssistantMarkdown content={detail.factsSummary} /></section>
              <section aria-label="待决定问题" className="space-y-3 border-t border-border pt-4">
                <div className="space-y-2"><h3 className="text-xs font-medium text-muted-foreground">{actionable ? '待你决定' : '当时的问题'}</h3><p className="whitespace-pre-wrap break-words text-sm font-medium leading-6">{detail.question}</p></div>
                {actionable && <fieldset disabled={!!busy || !!uncertain || detailLoading} className="space-y-3">
                  <legend className="sr-only">{detail.options.length ? '选择决定' : '填写答复'}</legend>
                  {detail.options.map(option => <label key={option.id} className={`flex min-h-12 cursor-pointer items-start gap-3 rounded-lg border p-3 text-sm transition-colors ${optionId === option.id ? 'border-brand bg-brand-soft' : 'border-border hover:bg-muted'}`}><input type="radio" name={`decision-${detail.id}`} value={option.id} checked={optionId === option.id} onChange={() => setOptionId(option.id)} className="mt-1 accent-[var(--color-primary)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring" /><span className="min-w-0 whitespace-pre-wrap break-words leading-6">{option.label}{detail.recommendedOptionId === option.id && !isRecommendedLabel(option.label) && <Badge variant="secondary" className="ml-2">推荐</Badge>}</span></label>)}
                  {detail.freeTextAllowed && <div className="space-y-2">
                    <label htmlFor="decision-reply" className="block text-sm font-medium">{detail.options.length ? '补充答复' : '你的答复'}</label>
                    <textarea id="decision-reply" aria-describedby="decision-reply-help" rows={3} value={freeText} onChange={event => setFreeText(event.target.value)} className={textAreaClass} />
                    <p id="decision-reply-help" className="text-xs leading-5 text-muted-foreground">{detail.options.length ? '可直接选择一个方案，也可填写答复。填写后将以这段文字作为答复，所选方案仍会保留。' : '请补充继续处理所需的信息，提交后按当前规则确定下一步。'}</p>
                  </div>}
                </fieldset>}
              </section>
              {detail.status === 'writeback_unknown' && <Alert variant="warning">外部结果待核实。该动作可能已经生效，系统正在核对处理记录。</Alert>}
              {detail.status === 'writeback_failed' && <Alert variant="warning">处理未完成，请查看动作步骤中的失败原因；已确认的步骤不会重复执行。</Alert>}
              {detail.status === 'withdrawn' && <Alert>事项已撤回。请结合关联事件和原系统中的最新进展确认后续安排。</Alert>}
              {detail.resolution && <section aria-label="处理历史" className="space-y-2 rounded-lg border border-border p-4 text-sm"><h3 className="font-medium">处理历史 · {DECISION_STATUS_LABEL[detail.resolution.kind]}</h3>{detail.resolution.optionLabel && <p className="whitespace-pre-wrap break-words">提交选项：{detail.resolution.optionLabel}</p>}{detail.resolution.optionId && <p className="break-all text-xs text-muted-foreground">选项标识：{detail.resolution.optionId}</p>}{detail.resolution.freeText && <p className="whitespace-pre-wrap break-words">答复内容：{detail.resolution.freeText}</p>}{detail.resolution.reason && <p className="whitespace-pre-wrap break-words">原因：{detail.resolution.reason}</p>}<p className="break-words text-xs text-muted-foreground">{detail.resolution.actor} · {formatDateTime(detail.resolution.at)}</p></section>}
              {detail.issueRef && <p className="text-xs leading-6 text-muted-foreground">关联任务：<IssueReference issueRef={detail.issueRef} issueUrl={detail.issueUrl} /></p>}
              {detail.eventIds.length > 0 && <section aria-label="关联事件" className="flex flex-wrap items-center gap-2 text-xs"><span className="text-muted-foreground">关联事件</span>{detail.eventIds.map(id => <Button key={id} variant="link" size="sm" className="h-auto whitespace-normal break-all py-1 text-left" onClick={() => onOpenEvent(id)}>查看事件 {id}</Button>)}</section>}
              {(!actionable || detail.writebackSteps.length > 0) && <ActionSteps steps={detail.writebackSteps} />}
              {detail.untrustedOriginal != null && <details className="rounded-lg border border-border">
                <summary className="cursor-pointer rounded-lg px-3 py-3 text-xs font-medium text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">参考资料 / 技术详情</summary>
                <div className="px-3 pb-3"><UntrustedOriginal value={detail.untrustedOriginal} /></div>
              </details>}
              <p className="text-xs leading-5 text-muted-foreground">建立于 {formatDateTime(detail.createdAt)} · 更新于 {formatDateTime(detail.updatedAt)}</p>
            </div>}
          </div>
          {detail && <section aria-label="决策操作" className="shrink-0 space-y-3 border-t border-border bg-popover px-4 py-3 sm:px-6 sm:py-4">
            {uncertain && isUnfinished(detail.status) ? <Alert variant="warning"><p>{busy ? '正在提交，请稍候。' : '上次请求结果未确认，期间暂停新操作。重试会核实同一次提交。'}</p><Button variant="outline" size="sm" className="mt-2" loading={!!busy} disabled={session.inFlight.has(detail.id)} onClick={() => void submit(uncertain.kind, uncertain)}>重试原请求</Button></Alert> : <p className="text-xs leading-5 text-muted-foreground">{actionable ? submitHint : ['pending_dispatch', 'sending'].includes(detail.status) ? '你的决定已保存，系统正在继续处理。' : detail.status === 'handled' ? '本地事项已关闭。关联任务请在原系统查看。' : '问题、答复和处理结果会保留在处理记录中。'}</p>}
            {isUnfinished(detail.status) && <div className="flex items-center justify-between gap-3">
              <Button ref={handledTrigger} variant="ghost" className="h-11 px-2" loading={busy === 'handled'} disabled={!!busy || !!uncertain || detailLoading || !handledAllowed} onClick={() => setConfirmHandled(true)}>关闭本地事项</Button>
              {actionable ? <Button className="h-11 min-w-28" loading={busy === 'decide'} disabled={!!busy || !!uncertain || detailLoading || (!optionId && !freeText.trim())} onClick={() => void submit('decide')}>提交决定</Button> : <Button variant="outline" className="h-11" disabled={!!busy} onClick={() => changeView('records')}>查看处理记录</Button>}
            </div>}
          </section>}
        </div>
      </div>
    </DialogShell>
    <ConfirmDialog open={confirmHandled} layer="nested" onCloseAutoFocus={event => { event.preventDefault(); const trigger = handledTrigger.current; (trigger && !trigger.disabled ? trigger : detailRegion.current)?.focus() }} title="关闭本地事项？" variant="warning" description={<div className="space-y-2"><p>关闭后，此事项将移入处理记录。尚未发送的步骤会取消；已经开始处理的事项可能无法关闭。</p><p>此操作不代表关联任务已完成，关联任务仍需在原系统查看。</p></div>} confirmText="确认关闭" onClose={() => setConfirmHandled(false)} onConfirm={() => void submit('handled')} />
  </>
}
