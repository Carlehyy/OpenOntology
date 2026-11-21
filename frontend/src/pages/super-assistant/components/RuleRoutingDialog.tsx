import { useEffect, useEffectEvent, useId, useRef, useState } from 'react'
import type { KeyboardEvent as ReactKeyboardEvent } from 'react'
import { BarChart3, Brain, Route, ScrollText } from 'lucide-react'
import { toast } from 'sonner'
import { autoloopApi, type AutoLoopPreview, type AutoLoopRoutingStats, type AutoLoopRule, type AutoLoopRuleCreate } from '@/api/autoloop'
import { Alert } from '@/components/ui/Alert'
import { Button } from '@/components/ui/Button'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { DialogShell } from './AssistantConfiguration'
import { errorText } from './assistantPanelUtils'
import { newRule, parseRuleDraft } from './autoloopLogic'
import { DEFAULT_SAMPLE, draftFromTrace, parsePreviewSample, sampleFromTrace, validateRuleFormRows, type RuleFieldEqualsRow, type RuleFormErrors, type RuleFormField } from './ruleRoutingForm'
import { textAreaClass } from './AutoLoopDetails'
import AutoLoopEventHistory, { type TraceDrilldown } from './AutoLoopEventHistory'
import AutoLoopAssistantRouting from './AutoLoopAssistantRouting'
// 组件默认导入与 api 层的 AutoLoopRoutingStats 数据类型重名，视图组件取 Panel 后缀。
import AutoLoopRoutingStatsPanel from './AutoLoopRoutingStats'
import RuleRoutingEditor from './RuleRoutingEditor'
import RulesList from './RulesList'

type Tab = 'events' | 'rules' | 'assistant' | 'stats'

/** 消息通知式左栏分区：事件流水（默认）→ 规则模板 → 助手路由 → 事件统计。 */
const NAV_ITEMS: { key: Tab; label: string; icon: typeof Route }[] = [
  { key: 'events', label: '事件流水', icon: ScrollText },
  { key: 'rules', label: '规则模板', icon: Route },
  { key: 'assistant', label: '助手路由', icon: Brain },
  { key: 'stats', label: '事件统计', icon: BarChart3 },
]
const TAB_KEYS = NAV_ITEMS.map(item => item.key)

export default function RuleRoutingDialog({ initialDraft, initialEventId, scheduledTaskId, scheduledRunId, onClose, onOpenDecision }: {
  initialDraft?: AutoLoopRuleCreate | null
  initialEventId?: string | null
  scheduledTaskId?: string | null
  scheduledRunId?: string | null
  onClose: () => void
  onOpenDecision: (id: string) => void
}) {
  const id = useId()
  // 默认事件流水；聊天草稿直进规则编辑；已有事件/日程深链优先于草稿。
  const deepLinked = !!(initialEventId || scheduledTaskId || scheduledRunId)
  const [tab, setTab] = useState<Tab>(initialDraft && !deepLinked ? 'rules' : 'events')
  const [rules, setRules] = useState<AutoLoopRule[]>([])
  const [loading, setLoading] = useState(true)
  const [listLoadError, setListLoadError] = useState('')
  const [listActionError, setListActionError] = useState('')
  const [importError, setImportError] = useState('')
  const [saveError, setSaveError] = useState('')
  const [previewError, setPreviewError] = useState('')
  const [fieldErrors, setFieldErrors] = useState<RuleFormErrors>({})
  const [editing, setEditing] = useState<AutoLoopRule | 'new' | null>(initialDraft ? 'new' : null)
  const [form, setForm] = useState<AutoLoopRuleCreate>(initialDraft ?? newRule())
  const [position, setPosition] = useState(String(initialDraft?.position ?? 0))
  const [fieldEquals, setFieldEquals] = useState<RuleFieldEqualsRow[]>(initialDraft?.match.fieldEquals ?? [])
  const [baseline, setBaseline] = useState('')
  const [sample, setSample] = useState(DEFAULT_SAMPLE)
  const [sampleFromEvent, setSampleFromEvent] = useState(false)
  const [sampleOpen, setSampleOpen] = useState(false)
  const [draftText, setDraftText] = useState('')
  const [showImport, setShowImport] = useState(false)
  const [preview, setPreview] = useState<AutoLoopPreview | null>(null)
  const [saving, setSaving] = useState(false)
  const [previewing, setPreviewing] = useState(false)
  const [sorting, setSorting] = useState(false)
  const [mutating, setMutating] = useState(false)
  const [deleting, setDeleting] = useState<AutoLoopRule | null>(null)
  const [exit, setExit] = useState<{ run: () => void; destination?: Tab } | null>(null)
  // 流水列表与助手研判共用选中事件，切换分区时保留流水筛选和分页。
  const [selectedEventId, setSelectedEventId] = useState<string | null>(initialEventId ?? null)
  const [drilldown, setDrilldown] = useState<TraceDrilldown | null>(null)
  // 统计由本弹窗统一持有：规则列表与事件统计共用同一份窗口摘要，失败不当作 0。
  const [days, setDays] = useState<7 | 30>(7)
  const [summary, setSummary] = useState<AutoLoopRoutingStats | null>(null)
  const [statisticsLoading, setStatisticsLoading] = useState(true)
  const [statisticsError, setStatisticsError] = useState('')
  const request = useRef(0)
  const previewRequest = useRef(0)
  const statsRequest = useRef(0)
  const previousDraft = useRef(initialDraft)
  const editorRef = useRef<HTMLDivElement>(null)
  const previewHeading = useRef<HTMLHeadingElement>(null)
  const navRefs = useRef<(HTMLButtonElement | null)[]>([])
  const confirmedDestination = useRef<Tab | undefined>(undefined)
  const snapshot = JSON.stringify({ form, fieldEquals, position })
  const currentDraft = useRef(snapshot)
  useEffect(() => { currentDraft.current = snapshot }, [snapshot])
  const dirty = !!editing && baseline !== snapshot
  const busy = saving || previewing || sorting || mutating
  const statsReady = !!summary && !statisticsError

  async function load() {
    const generation = ++request.current
    setLoading(true); setListLoadError('')
    try {
      const result = await autoloopApi.listRules()
      if (generation !== request.current) return
      setRules([...result].sort((a, b) => a.position - b.position || a.id.localeCompare(b.id)))
    } catch (err) { if (generation === request.current) setListLoadError(errorText(err, '规则加载失败')) }
    finally { if (generation === request.current) setLoading(false) }
  }
  useEffect(() => { void load(); return () => { request.current++; previewRequest.current++ } }, [])

  async function loadStatistics() {
    const generation = ++statsRequest.current
    setStatisticsLoading(true); setStatisticsError('')
    try {
      const result = await autoloopApi.routingStats(days)
      if (generation === statsRequest.current) setSummary(result)
    } catch (err) {
      // 失败清空摘要：列表与统计区呈现错误/未知，而不是把失败渲染成 0。
      if (generation === statsRequest.current) { setSummary(null); setStatisticsError(errorText(err, '统计加载失败')) }
    } finally { if (generation === statsRequest.current) setStatisticsLoading(false) }
  }
  useEffect(() => { void loadStatistics(); return () => { statsRequest.current++ } }, [days])

  useEffect(() => {
    if (preview) { previewHeading.current?.scrollIntoView({ block: 'start' }); previewHeading.current?.focus({ preventScroll: true }) }
  }, [preview])

  const syncLinkedEvent = useEffectEvent(() => {
    // 已在事件流水（含首次打开的深链）时不再打扰；仅在其它分区时带守卫跳回。
    if (tab === 'events') return
    guarded(() => { setTab('events') }, 'events')
  })
  useEffect(() => {
    if (initialEventId) setSelectedEventId(initialEventId)
    // 仅在存在事件/日程深链时才跳转；挂载时（如聊天草稿直进规则编辑）不得打扰。
    if (!initialEventId && !scheduledTaskId && !scheduledRunId) return
    syncLinkedEvent()
  }, [initialEventId, scheduledTaskId, scheduledRunId])

  const fillIncomingDraft = useEffectEvent((draft: AutoLoopRuleCreate) => { guarded(() => { start(undefined, draft); setTab('rules') }, 'rules') })
  useEffect(() => {
    if (initialDraft && initialDraft !== previousDraft.current) fillIncomingDraft(initialDraft)
    previousDraft.current = initialDraft
  }, [initialDraft])

  function clearFormFeedback() { setSaveError(''); setPreviewError(''); setFieldErrors({}); setPreview(null); previewRequest.current++ }
  function discard() {
    setEditing(null); setBaseline(''); setShowImport(false); setDraftText(''); setImportError('')
    setSampleFromEvent(false); setSampleOpen(false)
    clearFormFeedback()
  }
  function focusTab(key: Tab) {
    requestAnimationFrame(() => navRefs.current[TAB_KEYS.indexOf(key)]?.focus())
  }
  function guarded(action: () => void, destination?: Tab) {
    if (busy) return
    if (dirty || (showImport && draftText.trim())) {
      setExit({ run: () => { discard(); action() }, destination })
      return
    }
    action()
    if (destination) focusTab(destination)
  }
  function confirmExit() {
    if (!exit) return
    confirmedDestination.current = exit.destination
    setExit(null)
    exit.run()
  }
  function restoreExitFocus(event: Event) {
    const destination = confirmedDestination.current
    confirmedDestination.current = undefined
    if (!destination) return
    // 确认层开始恢复焦点后再聚焦目标标签，避免被尚未释放的焦点陷阱拦截。
    event.preventDefault()
    focusTab(destination)
  }
  function selectTab(key: Tab) {
    if (key === tab) return
    guarded(() => { discard(); setTab(key) }, key)
  }
  function showEvents(next: TraceDrilldown) {
    guarded(() => {
      setSelectedEventId(null)
      setDrilldown({ ...next })
      setTab('events')
    }, 'events')
  }
  function openEvent(eventId: string) {
    guarded(() => { setSelectedEventId(eventId); setDrilldown(null); setTab('events') }, 'events')
  }
  function createFromTrace(trace?: {
    title?: string | null
    source?: { kind?: string | null; system?: string | null; ref?: string | null } | null
    untrustedOriginal?: unknown
  }) {
    guarded(() => {
      const next = Math.max(-1, ...rules.map(item => item.position)) + 1
      if (trace) {
        // 事件预填：白名单字段进条件、原始载荷进样例；默认停用、不自动保存。
        start(undefined, draftFromTrace(trace, next))
        setSample(sampleFromTrace(trace)); setSampleFromEvent(true); setSampleOpen(true)
      } else start()
      setTab('rules')
    }, 'rules')
  }
  function start(rule?: AutoLoopRule, imported?: AutoLoopRuleCreate) {
    const value = imported ?? (rule ? {
      name: rule.name, enabled: rule.enabled, position: rule.position, match: rule.match,
      titleTemplate: rule.titleTemplate, descriptionTemplate: rule.descriptionTemplate,
      priority: rule.priority, assignee: rule.assignee, binding: rule.binding,
    } : newRule(Math.max(-1, ...rules.map(item => item.position)) + 1))
    const rows = value.match.fieldEquals ?? []
    setForm(value); setPosition(String(value.position)); setFieldEquals(rows)
    setBaseline(imported ? '' : JSON.stringify({ form: value, fieldEquals: rows, position: String(value.position) }))
    setEditing(rule ?? 'new'); setShowImport(false); setDraftText(''); setImportError('')
    setSampleFromEvent(false); setSampleOpen(false)
    clearFormFeedback()
  }
  function invalidatePreview() { setPreview(null); setPreviewError(''); previewRequest.current++ }
  function clearField(field?: RuleFormField) { if (field) setFieldErrors(previous => ({ ...previous, [field]: undefined })) }
  function change(next: AutoLoopRuleCreate, field?: RuleFormField) { setForm(next); invalidatePreview(); clearField(field) }
  function changeFieldEquals(rows: RuleFieldEqualsRow[]) { setFieldEquals(rows); invalidatePreview(); clearField('fieldEquals') }
  function changePosition(value: string) { setPosition(value); change({ ...form, position: Number(value) }, 'position') }
  function changeSample(text: string) { setSample(text); setSampleFromEvent(false); invalidatePreview(); clearField('sample') }
  function focusInvalid(errors: RuleFormErrors) {
    setFieldErrors(errors)
    // 等待内联错误挂载后，按 DOM 顺序聚焦并滚动到首个错误字段。
    requestAnimationFrame(() => editorRef.current?.querySelector<HTMLElement>('[aria-invalid="true"]')?.focus())
  }
  function validated() {
    const result = validateRuleFormRows(form, fieldEquals, position, !!editing && editing !== 'new')
    if (!result.value) focusInvalid(result.errors)
    else setFieldErrors({})
    return result.value
  }
  async function save() {
    if (busy) return
    setSaveError('')
    const value = validated()
    if (!value) return
    const submitted = snapshot
    const target = editing
    setSaving(true)
    try {
      const saved = target && target !== 'new' ? await autoloopApi.updateRule(target.id, value) : await autoloopApi.createRule(value)
      if (currentDraft.current === submitted) discard()
      else { setEditing(saved); setBaseline(submitted); invalidatePreview() }
      toast.success('规则已保存', { description: currentDraft.current === submitted ? '仅影响后续事件' : '请求期间的新增修改仍在表单中，尚未保存' })
      await load()
    } catch (err) { setSaveError(`保存失败，表单已保留：${errorText(err, '请稍后重试')}`) }
    finally { setSaving(false) }
  }
  async function renderPreview() {
    if (busy) return
    invalidatePreview()
    const generation = previewRequest.current
    const value = validated()
    if (!value) return
    let sampleEvent: Record<string, unknown>
    try { sampleEvent = parsePreviewSample(sample); clearField('sample') }
    catch (err) { focusInvalid({ sample: (err as Error).message }); return }
    setPreviewing(true)
    try {
      const result = editing && editing !== 'new' && !dirty
        ? await autoloopApi.previewStoredRule(editing.id, sampleEvent)
        : await autoloopApi.previewRule(value, sampleEvent)
      if (generation === previewRequest.current) setPreview(result)
    } catch (err) { if (generation === previewRequest.current) setPreviewError(errorText(err, '预览失败')) }
    finally { setPreviewing(false) }
  }
  async function mutate(rule: AutoLoopRule, patch?: Partial<AutoLoopRuleCreate>) {
    setMutating(true); setListActionError('')
    try {
      if (patch) await autoloopApi.updateRule(rule.id, patch)
      else { await autoloopApi.deleteRule(rule.id); setDeleting(null) }
      toast.success(patch ? '规则已更新' : '规则已删除')
      await load()
    } catch (err) { setListActionError(errorText(err, '规则更新失败')) }
    finally { setMutating(false) }
  }
  async function move(index: number, direction: -1 | 1) {
    const order = [...rules]
    const destination = index + direction
    if (busy || destination < 0 || destination >= order.length) return
    ;[order[index], order[destination]] = [order[destination], order[index]]
    setSorting(true); setListActionError('')
    try {
      // 每条 PATCH 独立提交；失败后重读服务器事实，不宣称已回滚。
      for (const [position, rule] of order.entries()) if (rule.position !== position) await autoloopApi.updateRule(rule.id, { position })
      toast.success('规则顺序已更新')
    } catch (err) { setListActionError(`部分顺序已更新，请检查后重试：${errorText(err, '排序未全部保存')}`) }
    finally { await load(); setSorting(false) }
  }
  function handleNavKeyDown(event: ReactKeyboardEvent<HTMLButtonElement>, index: number) {
    let next: Tab | null = null
    if (event.key === 'ArrowDown' || event.key === 'ArrowRight') next = TAB_KEYS[(index + 1) % TAB_KEYS.length]
    else if (event.key === 'ArrowUp' || event.key === 'ArrowLeft') next = TAB_KEYS[(index - 1 + TAB_KEYS.length) % TAB_KEYS.length]
    else if (event.key === 'Home') next = TAB_KEYS[0]
    else if (event.key === 'End') next = TAB_KEYS[TAB_KEYS.length - 1]
    if (!next) return
    event.preventDefault()
    selectTab(next)
  }

  // 命中数：统计成功后缺失条目按 0 计（真 0）；统计失败/未就绪返回 null 显示「—」。
  function hitFor(ruleId: string): number | null {
    if (!statsReady || !summary) return null
    return summary.rules.find(item => item.ruleId === ruleId)?.hits ?? 0
  }
  const hitsLabel = statsReady && summary ? `近 ${summary.days} 天命中` : null
  const hitsRange = summary ? `${summary.since} ~ ${summary.until}` : undefined

  return <>
    <DialogShell title="规则路由" description="我的规则：处理发送给当前用户的后续事件。按匹配顺序首条命中，未命中交由助手研判。" size="xwide" icon={<Route size={18} />} onClose={() => guarded(onClose)} onInteractOutside={event => event.preventDefault()} contentClassName="h-[88dvh]">
      <div className="grid min-h-0 flex-1 grid-cols-1 grid-rows-[auto_minmax(0,1fr)] md:grid-cols-[9.5rem_minmax(0,1fr)] md:grid-rows-[minmax(0,1fr)]">
        {/* 窄屏导航保持单行的自然高度，剩余空间留给可滚动的面板。 */}
        <nav role="tablist" aria-label="规则路由分区" className="scrollbar-none flex min-h-0 shrink-0 flex-row gap-1 overflow-x-auto border-b border-[var(--color-border)] p-2 md:flex-col md:overflow-x-visible md:border-b-0 md:border-r">
          {NAV_ITEMS.map(({ key, label, icon: Icon }, index) => (
            <button
              key={key}
              type="button"
              role="tab"
              id={`${id}-tab-${key}`}
              aria-selected={tab === key}
              aria-controls={`${id}-panel-${key}`}
              tabIndex={tab === key ? 0 : -1}
              onClick={() => selectTab(key)}
              onKeyDown={event => handleNavKeyDown(event, index)}
              ref={element => { navRefs.current[index] = element }}
              data-rule-routing-nav={key}
              className={`flex min-h-10 shrink-0 items-center gap-1.5 rounded-lg px-1.5 text-left text-xs transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring md:px-2.5 ${
                tab === key
                  ? 'bg-brand-soft font-medium text-brand-ink'
                  : 'text-[var(--color-text-secondary)] hover:bg-[var(--color-bg-hover)] hover:text-[var(--color-text-primary)]'
              }`}
            >
              <Icon size={15} className="shrink-0" aria-hidden="true" />
              <span className="min-w-0 flex-1 whitespace-nowrap md:truncate">{label}</span>
            </button>
          ))}
        </nav>
        {/* 事件流水与规则模板常挂载（hidden 时不产生 grid item）：切换分区保留流水筛选、
            选中事件与编辑器滚动位置；深链/显式打开才通过 props 重定位。 */}
        <div id={`${id}-panel-events`} role="tabpanel" aria-labelledby={`${id}-tab-events`} hidden={tab !== 'events'} className={tab === 'events' ? 'flex min-h-0 flex-1 flex-col' : 'hidden'}>
          <AutoLoopEventHistory
            active={tab === 'events'}
            selectedId={selectedEventId}
            onSelectEvent={setSelectedEventId}
            scheduledTaskId={scheduledTaskId}
            scheduledRunId={scheduledRunId}
            rules={rules}
            drilldown={drilldown}
            onCreateRule={trace => createFromTrace(trace)}
            onOpenDecision={identity => guarded(() => onOpenDecision(identity))}
          />
        </div>
        <div id={`${id}-panel-rules`} role="tabpanel" aria-labelledby={`${id}-tab-rules`} hidden={tab !== 'rules'} className={tab === 'rules' ? 'flex min-h-0 flex-1 flex-col' : 'hidden'}>
          {editing ? <RuleRoutingEditor
            idPrefix={id}
            editorRef={editorRef}
            previewHeadingRef={previewHeading}
            form={form}
            fieldEquals={fieldEquals}
            position={position}
            sample={sample}
            sampleFromEvent={sampleFromEvent}
            sampleOpen={sampleOpen}
            onSampleOpenChange={setSampleOpen}
            editing={editing}
            dirty={dirty}
            busy={busy}
            saving={saving}
            previewing={previewing}
            fieldErrors={fieldErrors}
            saveError={saveError}
            previewError={previewError}
            preview={preview}
            onBack={() => guarded(discard)}
            onFieldChange={change}
            onPositionChange={changePosition}
            onFieldEqualsChange={changeFieldEquals}
            onSampleChange={changeSample}
            onSave={() => void save()}
            onPreview={() => void renderPreview()}
          /> : <div className="min-h-0 flex-1 overflow-y-auto p-4 sm:p-5">
            {showImport ? <div className="space-y-4">
              <Alert>粘贴助手在会话中给出的规则 JSON。导入会填入编辑表单，可先修改和试渲染。规则保存并启用后才参与匹配。</Alert>
              <div className="min-w-0 space-y-1.5 text-xs font-medium"><label htmlFor={`${id}-import`}>助手规则草稿 JSON</label><textarea id={`${id}-import`} data-rule-routing-import-text aria-invalid={!!importError} aria-describedby={importError ? `${id}-import-error` : undefined} value={draftText} onChange={event => { setDraftText(event.target.value); setImportError('') }} className={textAreaClass} rows={10} /></div>
              {importError && <Alert id={`${id}-import-error`} variant="danger" role="alert">{importError}</Alert>}
              <div className="flex flex-wrap gap-2"><Button onClick={() => { try { start(undefined, parseRuleDraft(draftText)) } catch (err) { setImportError(errorText(err, '草稿校验失败')); document.getElementById(`${id}-import`)?.focus() } }} data-rule-routing-import-confirm>导入并审阅</Button><Button variant="ghost" onClick={() => guarded(discard)} data-rule-routing-import-cancel>取消</Button></div>
            </div> : <RulesList
              rules={rules}
              loading={loading}
              busy={busy}
              sorting={sorting}
              listLoadError={listLoadError}
              listActionError={listActionError}
              hitsLabel={hitsLabel}
              hitsRange={hitsRange}
              hitFor={hitFor}
              onCreate={() => start()}
              onImport={() => { setShowImport(true); setImportError('') }}
              onRefresh={() => { setListActionError(''); void load() }}
              onEdit={rule => start(rule)}
              onToggle={rule => void mutate(rule, { enabled: !rule.enabled })}
              onMove={(index, direction) => void move(index, direction)}
              onDelete={setDeleting}
              onViewHits={rule => { if (summary) showEvents({ ruleId: rule.id, since: summary.since, until: summary.until, ruleName: rule.name }) }}
            />}
          </div>}
        </div>
        {/* 助手路由/事件统计为自带头部与滚动的 flex section，面板容器仅做透传定位。 */}
        {tab === 'assistant' && <div id={`${id}-panel-assistant`} role="tabpanel" aria-labelledby={`${id}-tab-assistant`} className="flex min-h-0 flex-1 flex-col">
          <AutoLoopAssistantRouting onOpenEvent={openEvent} onOpenDecision={identity => guarded(() => onOpenDecision(identity))} onViewAll={() => showEvents({ route: 'assistant' })} />
        </div>}
        {tab === 'stats' && <div id={`${id}-panel-stats`} role="tabpanel" aria-labelledby={`${id}-tab-stats`} className="flex min-h-0 flex-1 flex-col">
          <AutoLoopRoutingStatsPanel summary={summary} loading={statisticsLoading} error={statisticsError} days={days} onDaysChange={setDays} onRefresh={() => void loadStatistics()} onDrilldown={showEvents} />
        </div>}
      </div>
    </DialogShell>
    <ConfirmDialog open={!!deleting} onClose={() => setDeleting(null)} onConfirm={() => deleting && void mutate(deleting)} loading={mutating} title="删除规则？" description={`删除“${deleting?.name ?? ''}”后，仅后续事件停止匹配，在途事件保留原版本。`} confirmText="删除" variant="danger" />
    <ConfirmDialog open={!!exit} onClose={() => setExit(null)} onConfirm={confirmExit} onCloseAutoFocus={restoreExitFocus} title="放弃未保存的规则草稿？" description="表单中的修改尚未保存。" confirmText="放弃草稿" cancelText="继续编辑" variant="warning" />
  </>
}
