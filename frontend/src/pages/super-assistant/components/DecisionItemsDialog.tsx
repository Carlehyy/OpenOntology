import { useCallback, useEffect, useRef, useState } from 'react'
import { ArrowLeft, ArrowRight, ListChecks, RefreshCw } from 'lucide-react'
import { autoloopApi, type DecisionItem, type DecisionSummary } from '@/api/autoloop'
import { Alert } from '@/components/ui/Alert'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { formatDateTime } from '@/utils/datetime'
import { DialogShell } from './AssistantConfiguration'
import { errorText } from './assistantPanelUtils'
import { acceptDecision, canMarkHandled, DECISION_GROUPS, DECISION_STATUS_LABEL, isUnfinished, PRIORITY_LABEL, sortDecisions, SOURCE_LABEL, type DecisionGroup, type DecisionRequest, type DecisionSession } from './autoloopLogic'
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
  const [group, setGroup] = useState<DecisionGroup>('pending')
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
  const selectedRef = useRef(selected)
  selectedRef.current = selected
  const merge = useCallback((value: DecisionItem) => {
    const result = acceptDecision(session.items.get(value.id) ?? null, value)
    session.items.set(value.id, result)
    if (!isUnfinished(result.status)) session.requests.delete(result.id)
    return result
  }, [session])
  function select(id: string | null) { setSelected(id); onSelect(id) }
  useEffect(() => { active.current = true; return () => { active.current = false; listRequest.current++; detailRequest.current++ } }, [])
  useEffect(() => { setSelected(initialId || null) }, [initialId])
  useEffect(() => {
    const generation = ++listRequest.current
    setListLoading(true); setError('')
    void autoloopApi.listDecisions(DECISION_GROUPS[group].status, cursor).then(result => {
      if (generation !== listRequest.current) return
      const statuses = DECISION_GROUPS[group].status.split(',')
      setItems(sortDecisions(result.items.map(merge).filter(item => statuses.includes(item.status))))
      setNext(result.hasMore ? result.nextCursor : null)
    }).catch(err => { if (generation === listRequest.current) setError(errorText(err, '决策事项加载失败')) })
      .finally(() => { if (generation === listRequest.current) setListLoading(false) })
    return () => { listRequest.current++ }
  }, [group, cursor, revision, merge])

  const loadDetail = useCallback(async (id: string) => {
    const generation = ++detailRequest.current
    setDetailLoading(true); setDetailError('')
    try {
      const value = await autoloopApi.getDecision(id)
      if (!active.current || generation !== detailRequest.current || selectedRef.current !== id) return
      const merged = merge(value)
      setDetail(merged)
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
        void loadDetail(id)
      }
      setRevision(value => value + 1)
      void onRefreshSummary()
    }
    session.listeners.add(sync)
    return () => { session.listeners.delete(sync) }
  }, [session, onRefreshSummary, loadDetail])
  useEffect(() => {
    const request = selected ? session.requests.get(selected) ?? null : null
    setDetail(selected ? session.items.get(selected) ?? null : null); setDetailError(''); setOperationError(''); setNotice(''); setOptionId(''); setFreeText(''); setUncertain(request)
    setBusy(selected && session.inFlight.has(selected) ? request?.kind ?? null : null)
    if (selected) void loadDetail(selected)
    return () => { detailRequest.current++ }
  }, [selected, loadDetail, session])
  useEffect(() => { setOptionId(''); setFreeText('') }, [detail?.id, detail?.contentVersion])
  useEffect(() => {
    const timer = window.setInterval(() => {
      if (!mutationLock.current && selected && document.visibilityState === 'visible') {
        void loadDetail(selected)
        void onRefreshSummary()
        setRevision(value => value + 1)
      }
    }, 10000)
    return () => window.clearInterval(timer)
  }, [selected, loadDetail, onRefreshSummary])

  async function submit(kind: 'decide' | 'handled', retry?: DecisionRequest) {
    if (!detail || mutationLock.current || session.inFlight.has(detail.id) || (!retry && session.requests.has(detail.id))) return
    const request: DecisionRequest = retry ?? {
      id: detail.id, kind,
      body: { expectedContentVersion: detail.contentVersion, idempotencyKey: crypto.randomUUID(), ...(kind === 'decide' ? { optionId: optionId || undefined, freeText: freeText.trim() || undefined } : {}) },
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
        else if (request.kind === 'handled') setNotice('已人工处理。')
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
        setOperationError('请求结果尚未确认。可刷新查看服务端事实，或使用原幂等键重试原请求。')
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
  const counts = summary ? { pending: summary.openCount, processing: summary.pendingDispatchCount + summary.sendingCount, failed: summary.unknownCount + summary.failedCount } : null

  return <DialogShell title="决策事项" description="永久保留问题、选择与逐步骤结果。处理失败或结果未知的事项仍计入未完成。" size="xwide" icon={<ListChecks size={18} />} onClose={onClose} onInteractOutside={event => event.preventDefault()} contentClassName="h-[88dvh]">
    <div className="flex shrink-0 flex-wrap items-center gap-1 border-b border-border p-3" role="tablist" aria-label="决策分区">
      {(Object.keys(DECISION_GROUPS) as DecisionGroup[]).map(key => <Button key={key} size="sm" role="tab" aria-selected={group === key} disabled={!!busy} variant={group === key ? 'default' : 'ghost'} onClick={() => { setGroup(key); setCursor(null); setPrevious([]); select(null) }}>{DECISION_GROUPS[key].label}{counts && key !== 'history' ? ` (${counts[key]})` : ''}</Button>)}
      <Button size="icon-sm" variant="ghost" aria-label="刷新决策事项" disabled={!!busy} onClick={() => { setRevision(value => value + 1); if (selected) void loadDetail(selected); void onRefreshSummary() }}><RefreshCw size={14} /></Button>
      <span className="ml-auto text-xs text-muted-foreground">未完成 {summary?.unfinishedTotal ?? '—'}</span>
    </div>
    {error && <Alert role="alert" variant="danger" className="m-3">{error}</Alert>}
    <div className="flex min-h-0 flex-1">
      <div className={`${selected ? 'hidden md:flex' : 'flex'} min-h-0 w-full flex-col border-r border-border md:w-2/5`}>
        <div className="min-h-0 flex-1 overflow-y-auto p-3">
          {listLoading ? <p role="status" className="text-sm text-muted-foreground">加载事项…</p> : items.length === 0 ? <p className="py-12 text-center text-sm text-muted-foreground">此分区暂无事项。</p> : items.map(item => <button key={item.id} type="button" disabled={!!busy} onClick={() => select(item.id)} aria-pressed={selected === item.id} className={`mb-2 w-full rounded-xl border p-3 text-left outline-none focus-visible:ring-2 focus-visible:ring-ring ${selected === item.id ? 'border-brand bg-brand-soft' : 'border-border hover:bg-muted'}`}>
            <span className="flex flex-wrap gap-2"><Badge variant={item.priority === 'high' ? 'warning' : 'secondary'}>{PRIORITY_LABEL[item.priority]}优先</Badge><Badge variant="secondary">{DECISION_STATUS_LABEL[item.status]}</Badge></span>
            <span className="mt-2 block break-words text-sm font-medium">{item.title}</span>
            <span className="mt-2 block text-xs text-muted-foreground">{SOURCE_LABEL[item.source]} · 等待自 {formatDateTime(item.createdAt)}</span>
          </button>)}
        </div>
        <div className="flex shrink-0 justify-between border-t border-border p-2"><Button size="sm" variant="ghost" disabled={!!busy || listLoading || !previous.length} onClick={() => { setCursor(previous[previous.length - 1]); setPrevious(value => value.slice(0, -1)) }}><ArrowLeft size={13} />上一页</Button><Button size="sm" variant="ghost" disabled={!!busy || listLoading || !next} onClick={() => { setPrevious(value => [...value, cursor]); setCursor(next) }}>下一页<ArrowRight size={13} /></Button></div>
      </div>
      <div className={`${selected ? 'flex' : 'hidden md:flex'} min-h-0 min-w-0 flex-1 flex-col overflow-y-auto p-4 sm:p-5`}>
        {selected && <Button size="sm" variant="ghost" disabled={!!busy} className="mb-3 self-start md:hidden" onClick={() => select(null)}><ArrowLeft size={14} />返回事项列表</Button>}
        {notice && <Alert role="status" className="mb-4">{notice}</Alert>}
        {operationError && <Alert role="alert" variant="danger" className="mb-4">{operationError}</Alert>}
        {detailError && <Alert role="alert" variant="danger" className="mb-4">{detailError}</Alert>}
        {detailLoading && !detail ? <p role="status" className="text-sm text-muted-foreground">加载决策详情…</p> : !detail ? <p className="py-12 text-center text-sm text-muted-foreground">选择事项查看问题与处理结果。</p> : <div className="space-y-4">
          <div className="flex flex-wrap items-center gap-2"><Badge variant="secondary">{DECISION_STATUS_LABEL[detail.status]}</Badge><Badge variant="outline">{SOURCE_LABEL[detail.source]}</Badge><span className="text-xs text-muted-foreground">内容版本 {detail.contentVersion}</span></div>
          <h2 className="break-words text-base font-semibold">{detail.title}</h2>
          <section aria-label="发生了什么" className="space-y-2"><h3 className="text-sm font-medium">发生了什么</h3><AssistantMarkdown content={detail.factsSummary} /></section>
          <section aria-label="待决定问题" className="space-y-2"><h3 className="text-sm font-medium">待决定问题</h3><p className="whitespace-pre-wrap break-words text-sm leading-6">{detail.question}</p></section>
          {detail.eventIds.length > 0 && <section aria-label="关联事件" className="flex flex-wrap items-center gap-2 text-xs"><span>关联事件</span>{detail.eventIds.map(id => <Button key={id} variant="link" size="sm" onClick={() => onOpenEvent(id)}>查看事件 {id}</Button>)}</section>}
          {detail.untrustedOriginal != null && <UntrustedOriginal value={detail.untrustedOriginal} />}
          {detail.issueRef && <p className="text-xs">关联 Issue：<IssueReference issueRef={detail.issueRef} issueUrl={detail.issueUrl} /></p>}
          {actionable && <p className="text-xs leading-6 text-muted-foreground">
            {detail.source === 'watchdog'
              ? '提交后会把决定作为评论写回 Multica。'
              : detail.issueRef
                ? '提交后会记录决定、重新匹配已启用的规则，并将决定作为评论写回已关联的 Multica 任务。'
                : '提交后会记录决定、重新匹配已启用的规则。命中规则后才会派发给 Multica。'}
            “已人工处理”仅关闭本地事项。
          </p>}
          {actionable && <fieldset disabled={!!busy || !!uncertain || detailLoading} className="space-y-3">
            <legend className="mb-2 text-sm font-medium">选择决定</legend>
            {detail.options.map(option => <label key={option.id} className={`flex cursor-pointer items-start gap-2 rounded-lg border p-3 text-sm ${optionId === option.id ? 'border-brand bg-brand-soft' : 'border-border'}`}><input type="radio" name={`decision-${detail.id}`} value={option.id} checked={optionId === option.id} onChange={() => setOptionId(option.id)} className="mt-1" /><span className="min-w-0 whitespace-pre-wrap break-words">{option.label}{detail.recommendedOptionId === option.id && <Badge variant="info" className="ml-2">推荐</Badge>}</span></label>)}
            {detail.freeTextAllowed && <label className="block space-y-2 text-sm"><span>自由补充</span><textarea rows={4} value={freeText} onChange={event => setFreeText(event.target.value)} className={textAreaClass} /></label>}
          </fieldset>}
          {isUnfinished(detail.status) && <div className="flex flex-wrap gap-2">
            {actionable && <Button loading={busy === 'decide'} disabled={!!busy || !!uncertain || detailLoading || (!optionId && !freeText.trim())} onClick={() => void submit('decide')}>提交决定</Button>}
            <Button variant="outline" loading={busy === 'handled'} disabled={!!busy || !!uncertain || detailLoading || !handledAllowed} onClick={() => void submit('handled')}>已人工处理</Button>
          </div>}
          {isUnfinished(detail.status) && <p className="text-xs leading-6 text-muted-foreground">“已人工处理”由服务端核对发送与写回事实；若当前无法关闭，会显示原因与最新步骤。</p>}
          {uncertain && isUnfinished(detail.status) && <Alert variant="warning"><p>上次请求结果未确认，期间暂停新操作。</p><Button variant="outline" size="sm" className="mt-2" loading={!!busy} disabled={session.inFlight.has(detail.id)} onClick={() => void submit(uncertain.kind, uncertain)}>重试原请求</Button></Alert>}
          {detail.status === 'writeback_unknown' && <Alert variant="warning">远端结果尚未确认，系统将对账。此事项仍计入未完成。</Alert>}
          {detail.status === 'withdrawn' && <Alert>事项已撤回。外部跟进记录表示检测到新的事实，请结合事件和远端内容确认后续进展。</Alert>}
          {detail.resolution && <section aria-label="处理历史" className="space-y-2 rounded-lg border border-border p-3 text-sm"><h3 className="font-medium">处理历史 · {DECISION_STATUS_LABEL[detail.resolution.kind]}</h3>{detail.resolution.optionLabel && <p className="whitespace-pre-wrap break-words">提交选项：{detail.resolution.optionLabel}</p>}{detail.resolution.optionId && <p className="break-all text-xs text-muted-foreground">选项标识：{detail.resolution.optionId}</p>}{detail.resolution.freeText && <p className="whitespace-pre-wrap break-words">自由补充：{detail.resolution.freeText}</p>}{detail.resolution.reason && <p className="whitespace-pre-wrap break-words">原因：{detail.resolution.reason}</p>}<p className="break-words text-xs text-muted-foreground">{detail.resolution.actor} · {formatDateTime(detail.resolution.at)}</p></section>}
          <ActionSteps steps={detail.writebackSteps} />
          <p className="text-xs text-muted-foreground">建立于 {formatDateTime(detail.createdAt)} · 更新于 {formatDateTime(detail.updatedAt)}</p>
        </div>}
      </div>
    </div>
  </DialogShell>
}
