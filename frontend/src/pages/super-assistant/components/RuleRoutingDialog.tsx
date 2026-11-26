import { useEffect, useEffectEvent, useId, useLayoutEffect, useRef, useState, type KeyboardEvent as ReactKeyboardEvent, type RefObject } from 'react'
import { BarChart3, Brain, Route, ScrollText } from 'lucide-react'
import { toast } from 'sonner'
import { autoloopApi, type AutoLoopPreview, type AutoLoopRoutingPolicy, type AutoLoopRoutingStats, type AutoLoopRule, type AutoLoopRuleCreate } from '@/api/autoloop'
import { Alert } from '@/components/ui/Alert'
import { Button } from '@/components/ui/Button'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import type { GuardRoutingNavigation, RuleRoutingLocation, RuleRoutingTab as Tab } from '../ruleRoutingLocation'
import { DialogShell } from './AssistantConfiguration'
import { errorText } from './assistantPanelUtils'
import { newRule, parseRuleDraft } from './autoloopLogic'
import { DEFAULT_SAMPLE, draftFromTrace, matchesAllEvents, parsePreviewSample, sampleFromTrace, validateRuleFormRows, type RuleFieldEqualsRow, type RuleFormErrors, type RuleFormField } from './ruleRoutingForm'
import { textAreaClass } from './AutoLoopDetails'
import AutoLoopEventHistory, { type TraceDrilldown } from './AutoLoopEventHistory'
import AutoLoopAssistantRouting from './AutoLoopAssistantRouting'
import AutoLoopRoutingStatsPanel from './AutoLoopRoutingStats'
import RoutingPolicyEditor from './RoutingPolicyEditor'
import RuleRoutingEditor from './RuleRoutingEditor'
import RulesList from './RulesList'

const NAV_ITEMS: { key: Tab; label: string; icon: typeof Route }[] = [
  { key: 'events', label: '事件流水', icon: ScrollText },
  { key: 'rules', label: '路由规则', icon: Route },
  { key: 'assistant', label: '助手研判', icon: Brain },
  { key: 'stats', label: '事件统计', icon: BarChart3 },
]
const TAB_KEYS = NAV_ITEMS.map(item => item.key)
const DESKTOP = '(min-width: 768px)'
const ruleErrorCode = (error: unknown) => (error as { detail?: { code?: string } } | undefined)?.detail?.code

export default function RuleRoutingDialog({ initialDraft, location, navigationGuard, navigationPending, onLocationChange, onClose, onCloseAutoFocus, onOpenDecision }: {
  initialDraft?: AutoLoopRuleCreate | null
  location: RuleRoutingLocation
  navigationGuard: RefObject<GuardRoutingNavigation | null>
  navigationPending: boolean
  onLocationChange: (view: RuleRoutingLocation) => void
  onClose: () => void
  onCloseAutoFocus: (event: Event) => void
  onOpenDecision: (id: string) => void
}) {
  const id = useId()
  const { tab, eventId, scheduledTaskId, scheduledRunId } = location
  const [vertical, setVertical] = useState(() => window.matchMedia(DESKTOP).matches)
  const [rules, setRules] = useState<AutoLoopRule[]>([])
  const [loading, setLoading] = useState(true)
  const [listLoadError, setListLoadError] = useState('')
  const [listActionError, setListActionError] = useState('')
  const [importError, setImportError] = useState('')
  const [saveError, setSaveError] = useState('')
  const [previewError, setPreviewError] = useState('')
  const [fieldErrors, setFieldErrors] = useState<RuleFormErrors>({})
  const [editing, setEditing] = useState<AutoLoopRule | 'new' | null>(initialDraft ? 'new' : null)
  const [form, setForm] = useState<AutoLoopRuleCreate>(() => initialDraft ? { ...initialDraft, enabled: false } : newRule())
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
  const [activation, setActivation] = useState<{ rule: AutoLoopRule; value: AutoLoopRuleCreate; run: () => void } | null>(null)
  const [undo, setUndo] = useState<AutoLoopRule | null>(null)
  const [highlightedId, setHighlightedId] = useState<string | null>(null)
  const [exit, setExit] = useState<{ run: () => void; stay?: () => void; focus?: () => void; restoring?: boolean } | null>(null)
  const [drilldown, setDrilldown] = useState<TraceDrilldown | null>(null)
  const [days, setDays] = useState<7 | 30>(7)
  const [summary, setSummary] = useState<AutoLoopRoutingStats | null>(null)
  const [statisticsLoading, setStatisticsLoading] = useState(true)
  const [statisticsError, setStatisticsError] = useState('')
  const [policy, setPolicy] = useState<AutoLoopRoutingPolicy | null>(null)
  const [policyLoading, setPolicyLoading] = useState(true)
  const [policyError, setPolicyError] = useState('')
  const [policyEditing, setPolicyEditing] = useState(false)
  const [policyDirty, setPolicyDirty] = useState(false)
  const [policySaving, setPolicySaving] = useState(false)
  const request = useRef(0)
  const previewRequest = useRef(0)
  const statsRequest = useRef(0)
  const policyRequest = useRef(0)
  const previousDraft = useRef(initialDraft)
  const editorRef = useRef<HTMLDivElement>(null)
  const previewHeading = useRef<HTMLHeadingElement>(null)
  const rulesPanel = useRef<HTMLDivElement>(null)
  const assistantPanel = useRef<HTMLDivElement>(null)
  const navRefs = useRef<(HTMLButtonElement | null)[]>([])
  const exitFocus = useRef<(() => void) | undefined>(undefined)
  const mutationInFlight = useRef(false)
  const position = String(form.position)
  const snapshot = JSON.stringify({ form, fieldEquals })
  const currentDraft = useRef(snapshot)
  useLayoutEffect(() => { currentDraft.current = snapshot }, [snapshot])
  const dirty = !!editing && baseline !== snapshot
  const hasDraft = dirty || (showImport && Boolean(draftText.trim())) || policyDirty
  const busy = saving || previewing || sorting || mutating || policySaving
  const statsReady = !!summary && !statisticsError

  useEffect(() => {
    const query = window.matchMedia(DESKTOP)
    const update = () => setVertical(query.matches)
    query.addEventListener('change', update)
    return () => query.removeEventListener('change', update)
  }, [])
  useEffect(() => {
    if (!hasDraft) return
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = '' }
    window.addEventListener('beforeunload', warn)
    return () => window.removeEventListener('beforeunload', warn)
  }, [hasDraft])
  useEffect(() => {
    if (!vertical) navRefs.current[TAB_KEYS.indexOf(tab)]?.scrollIntoView({ block: 'nearest', inline: 'nearest' })
  }, [tab, vertical])

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
      if (generation === statsRequest.current) { setSummary(null); setStatisticsError(errorText(err, '统计加载失败')) }
    } finally { if (generation === statsRequest.current) setStatisticsLoading(false) }
  }
  useEffect(() => { void loadStatistics(); return () => { statsRequest.current++ } }, [days])

  async function loadPolicy() {
    const generation = ++policyRequest.current
    setPolicyLoading(true); setPolicyError('')
    try {
      const result = await autoloopApi.getRoutingPolicy()
      if (generation === policyRequest.current) setPolicy(result)
    } catch {
      if (generation === policyRequest.current) setPolicyError('研判提示词加载失败。')
    } finally { if (generation === policyRequest.current) setPolicyLoading(false) }
  }
  useEffect(() => {
    if (tab === 'assistant') void loadPolicy()
    return () => { policyRequest.current++ }
  }, [tab])

  useEffect(() => {
    if (preview) { previewHeading.current?.scrollIntoView({ block: 'start' }); previewHeading.current?.focus({ preventScroll: true }) }
  }, [preview])

  const fillIncomingDraft = useEffectEvent((draft: AutoLoopRuleCreate) => { guarded(() => start(undefined, draft)) })
  useEffect(() => {
    if (initialDraft && initialDraft !== previousDraft.current) fillIncomingDraft(initialDraft)
    previousDraft.current = initialDraft
  }, [initialDraft])

  function clearFormFeedback() { setSaveError(''); setPreviewError(''); setFieldErrors({}); setPreview(null); previewRequest.current++ }
  function discard() {
    setEditing(null); setBaseline(''); setShowImport(false); setDraftText(''); setImportError('')
    setSampleFromEvent(false); setSampleOpen(false); clearFormFeedback()
    setPolicyEditing(false); setPolicyDirty(false)
  }
  function guarded(action: () => void, focus?: () => void, stay?: () => void) {
    if (busy || mutationInFlight.current || activation || deleting) { stay?.(); return }
    const opener = document.activeElement
    if (hasDraft) {
      setExit({ run: () => { discard(); action() }, stay, focus })
      if (!exit) exitFocus.current = () => { if (opener instanceof HTMLElement && opener.isConnected) opener.focus() }
      return
    }
    discard(); action()
    if (focus) requestAnimationFrame(focus)
  }
  useLayoutEffect(() => {
    navigationGuard.current = (proceed, stay) => guarded(proceed, undefined, stay)
    return () => { navigationGuard.current = null }
  })
  // The browser URL changes before HashRouter commits it. Keep the modal in place
  // until the router accepts the restored view, so a repeat link is a new navigation.
  useEffect(() => {
    if (!navigationPending) setExit(current => current?.stay ? null : current)
  }, [navigationPending])
  function confirmExit() {
    if (!exit || exit.restoring) return
    exitFocus.current = exit.focus
    setExit(null)
    exit.run()
  }
  function cancelExit() {
    if (!exit || exit.restoring) return
    if (exit.stay && navigationPending) {
      setExit({ ...exit, restoring: true })
      exit.stay()
    } else setExit(null)
  }
  function restoreExitFocus(event: Event) {
    const focus = exitFocus.current
    exitFocus.current = undefined
    if (!focus) return
    event.preventDefault()
    requestAnimationFrame(focus)
  }
  function focusTab(key: Tab) { navRefs.current[TAB_KEYS.indexOf(key)]?.focus() }
  function goTo(key: Tab, selected: string | null = null) {
    onLocationChange({ ...location, tab: key, eventId: selected })
  }
  function selectTab(key: Tab) {
    if (key !== tab) guarded(() => goTo(key), () => focusTab(key))
  }
  function showEvents(next: TraceDrilldown) {
    guarded(() => { setDrilldown({ ...next }); goTo('events') }, () => focusTab('events'))
  }
  function openEvent(event: string) {
    guarded(() => { setDrilldown(null); goTo('events', event) })
  }
  function nextPosition() { return Math.max(-1, ...rules.map(item => item.position)) + 1 }
  function createFromTrace(trace?: Parameters<typeof draftFromTrace>[0]) {
    guarded(() => {
      if (trace) {
        start(undefined, draftFromTrace(trace, nextPosition()))
        setSample(sampleFromTrace(trace)); setSampleFromEvent(true); setSampleOpen(true)
      } else start()
      goTo('rules')
    })
  }
  function start(rule?: AutoLoopRule, imported?: AutoLoopRuleCreate) {
    const value = rule ? {
      name: rule.name, enabled: rule.enabled, position: rule.position, match: rule.match,
      titleTemplate: rule.titleTemplate, descriptionTemplate: rule.descriptionTemplate,
      priority: rule.priority, assignee: rule.assignee, binding: rule.binding,
    } : imported ? { ...imported, enabled: false, position: nextPosition() } : newRule(nextPosition())
    const rows = value.match.fieldEquals ?? []
    setForm(value); setFieldEquals(rows); setSample(DEFAULT_SAMPLE)
    setBaseline(imported ? '' : JSON.stringify({ form: value, fieldEquals: rows }))
    setEditing(rule ?? 'new'); setShowImport(false); setDraftText(''); setImportError(''); setHighlightedId(null)
    setSampleFromEvent(false); setSampleOpen(false)
    clearFormFeedback()
    requestAnimationFrame(() => editorRef.current?.querySelector<HTMLElement>('input')?.focus())
  }
  function invalidatePreview() { setPreview(null); setPreviewError(''); previewRequest.current++ }
  function clearField(field?: RuleFormField) { if (field) setFieldErrors(previous => ({ ...previous, [field]: undefined })) }
  function change(next: AutoLoopRuleCreate, field?: RuleFormField) { setForm(next); invalidatePreview(); clearField(field) }
  function changeFieldEquals(rows: RuleFieldEqualsRow[]) { setFieldEquals(rows); invalidatePreview(); clearField('fieldEquals') }
  function changeSample(text: string) { setSample(text); setSampleFromEvent(false); invalidatePreview(); clearField('sample') }
  function focusInvalid(errors: RuleFormErrors) {
    setFieldErrors(errors)
    requestAnimationFrame(() => editorRef.current?.querySelector<HTMLElement>('[aria-invalid="true"]')?.focus())
  }
  function validated() {
    const result = validateRuleFormRows(form, fieldEquals, position, !!editing && editing !== 'new')
    if (!result.value) focusInvalid(result.errors)
    else setFieldErrors({})
    return result.value
  }
  function focusRule(ruleId?: string) {
    const panel = rulesPanel.current
    const rule = ruleId ? panel?.querySelector<HTMLButtonElement>(`[data-rule-routing-rule-edit="${CSS.escape(ruleId)}"]`) : null
    ;(rule || panel?.querySelector<HTMLButtonElement>('[data-rule-routing-create]'))?.focus()
  }
  async function persistRule(value: AutoLoopRuleCreate, target: AutoLoopRule | 'new' | null, confirmed = false) {
    if (mutationInFlight.current) return
    mutationInFlight.current = true
    const submitted = snapshot
    setSaving(true)
    let focusId: string | undefined
    try {
      const saved = target && target !== 'new' ? await autoloopApi.updateRule(target.id, {
        ...value, expectedVersion: target.version, ...(confirmed ? { confirmAllMatch: true } : {}),
      }) : await autoloopApi.createRule({ ...value, enabled: false, position: nextPosition() })
      if (currentDraft.current === submitted) { discard(); focusId = saved.id; setHighlightedId(saved.id) }
      else { setEditing(saved); setBaseline(submitted); invalidatePreview() }
      toast.success(saved.enabled ? '规则已保存' : '规则已保存，未启用', { description: currentDraft.current === submitted ? undefined : '请求期间的新增修改仍在表单中，尚未保存。' })
      await load()
    } catch (err) {
      const stale = ruleErrorCode(err) === 'rule_version_conflict'
      setSaveError(stale ? '规则已被更新。本次修改未保存。请返回列表刷新后重新编辑。' : `保存失败，表单已保留：${errorText(err, '请稍后重试')}`)
      if (stale) await load()
    } finally {
      mutationInFlight.current = false
      setSaving(false); setActivation(null)
      if (focusId) requestAnimationFrame(() => focusRule(focusId))
    }
  }
  function save() {
    if (busy || mutationInFlight.current) return
    setSaveError('')
    const value = validated()
    if (!value) return
    if (editing && editing !== 'new' && value.enabled && matchesAllEvents(value.match) && !matchesAllEvents(editing.match)) {
      const target = editing
      setActivation({ rule: target, value, run: () => void persistRule(value, target, true) })
    } else void persistRule(value, editing)
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
  async function mutate(rule: AutoLoopRule, enabled?: boolean, confirmed = false) {
    if (busy || mutationInFlight.current) return
    mutationInFlight.current = true
    setMutating(true); setListActionError('')
    try {
      if (enabled === undefined) {
        await autoloopApi.deleteRule(rule.id)
        toast.success(`已删除「${rule.name}」`)
      } else {
        const saved = await autoloopApi.updateRule(rule.id, { enabled, expectedVersion: rule.version, ...(confirmed ? { confirmAllMatch: true } : {}) })
        if (!enabled) setUndo(saved)
        toast.success(`${enabled ? '已启用' : '已停用'}「${rule.name}」`)
      }
      if (enabled !== false && undo?.id === rule.id) setUndo(null)
      await load()
    } catch (err) {
      const stale = ruleErrorCode(err) === 'rule_version_conflict'
      setListActionError(stale ? '规则已变化。已刷新列表，请核对后再操作。' : errorText(err, '规则更新失败'))
      if (stale) { if (undo?.id === rule.id) setUndo(null); await load() }
    } finally {
      mutationInFlight.current = false
      setMutating(false); setDeleting(null); setActivation(null)
      requestAnimationFrame(() => focusRule(enabled === undefined ? undefined : rule.id))
    }
  }
  function toggle(rule: AutoLoopRule) {
    if (busy || mutationInFlight.current) return
    if (!rule.enabled && matchesAllEvents(rule.match)) {
      setActivation({ rule, value: { ...rule, enabled: true }, run: () => void mutate(rule, true, true) })
    } else void mutate(rule, !rule.enabled)
  }
  async function move(index: number, direction: -1 | 1) {
    const order = [...rules]
    const destination = index + direction
    if (busy || mutationInFlight.current || destination < 0 || destination >= order.length) return
    mutationInFlight.current = true
    ;[order[index], order[destination]] = [order[destination], order[index]]
    setSorting(true); setListActionError('')
    try {
      // 每条 PATCH 独立提交；失败后重读服务器事实，不宣称已回滚。
      for (const [position, rule] of order.entries()) if (rule.position !== position) await autoloopApi.updateRule(rule.id, { position, expectedVersion: rule.version })
      toast.success('规则顺序已更新', { description: '靠前的启用规则优先命中。' })
    } catch (err) { setListActionError(`顺序可能只保存了部分，请重新核对：${errorText(err, '排序未全部保存')}`) }
    finally { await load(); setSorting(false); mutationInFlight.current = false }
  }
  function handleNavKeyDown(event: ReactKeyboardEvent<HTMLButtonElement>, index: number) {
    let next: Tab | null = null
    if (event.key === (vertical ? 'ArrowDown' : 'ArrowRight')) next = TAB_KEYS[(index + 1) % TAB_KEYS.length]
    else if (event.key === (vertical ? 'ArrowUp' : 'ArrowLeft')) next = TAB_KEYS[(index - 1 + TAB_KEYS.length) % TAB_KEYS.length]
    else if (event.key === 'Home') next = TAB_KEYS[0]
    else if (event.key === 'End') next = TAB_KEYS[TAB_KEYS.length - 1]
    if (!next) return
    event.preventDefault()
    selectTab(next)
  }

  function hitFor(ruleId: string): number | null {
    if (!statsReady || !summary) return null
    return summary.rules.find(item => item.ruleId === ruleId)?.hits ?? 0
  }
  function focusPolicy() { assistantPanel.current?.querySelector<HTMLButtonElement>('[data-rule-routing-edit-policy]')?.focus() }
  function backFromPolicy() { guarded(() => {}, focusPolicy) }
  const hitsLabel = statsReady && summary ? `近 ${summary.days} 天命中` : null
  const hitsRange = summary ? `${summary.since} ~ ${summary.until}` : undefined
  const editIndex = editing && editing !== 'new' ? rules.findIndex(rule => rule.id === editing.id) : -1
  const activateIndex = activation ? rules.findIndex(rule => rule.id === activation.rule.id) : -1
  const shadowed = activateIndex > 0 && rules.slice(0, activateIndex).some(rule => rule.enabled && matchesAllEvents(rule.match))

  return <>
    <DialogShell
      title="规则路由"
      description="管理当前账号的入站事件、路由规则与助手研判。"
      size="xwide"
      icon={<Route size={18} />}
      onClose={() => guarded(onClose)}
      onOpenAutoFocus={event => { event.preventDefault(); focusTab(tab) }}
      onCloseAutoFocus={onCloseAutoFocus}
      contentClassName="h-[min(88dvh,52rem)] max-md:left-0 max-md:top-0 max-md:h-dvh max-md:max-h-none max-md:w-full max-md:translate-x-0 max-md:translate-y-0 max-md:rounded-none max-md:border-0 max-md:pb-[env(safe-area-inset-bottom)] max-md:pt-[env(safe-area-inset-top)] max-md:[&>button:last-child]:size-11"
    >
      <div className="grid min-h-0 flex-1 grid-cols-1 grid-rows-[auto_minmax(0,1fr)] md:grid-cols-[9.5rem_minmax(0,1fr)] md:grid-rows-[minmax(0,1fr)]">
        <nav role="tablist" aria-label="规则路由分区" aria-orientation={vertical ? 'vertical' : 'horizontal'} className="scrollbar-none flex min-h-0 shrink-0 gap-1 overflow-x-auto border-b border-border p-2 md:flex-col md:overflow-x-visible md:border-b-0 md:border-r md:py-3 md:pl-2.5">
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
              className={`flex h-11 shrink-0 items-center gap-2 rounded-lg px-2 text-left text-xs transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring md:h-10 md:px-2.5 md:text-sm ${tab === key ? 'bg-brand-soft font-medium text-brand-ink' : 'text-muted-foreground hover:bg-muted hover:text-foreground'}`}
            >
              <Icon size={16} className="shrink-0" aria-hidden="true" />
              <span className="whitespace-nowrap">{label}</span>
            </button>
          ))}
        </nav>
        {/* 流水保持挂载，退出详情或切换分区时不会丢失已筛选的列表。 */}
        <div id={`${id}-panel-events`} role="tabpanel" aria-labelledby={`${id}-tab-events`} hidden={tab !== 'events'} className={tab === 'events' ? 'flex min-h-0 min-w-0 flex-col' : 'hidden'}>
          <AutoLoopEventHistory
            active={tab === 'events'} selectedId={eventId} onSelectEvent={event => goTo('events', event)}
            scheduledTaskId={scheduledTaskId} scheduledRunId={scheduledRunId} rules={rules} drilldown={drilldown}
            onCreateRule={createFromTrace} onOpenDecision={identity => guarded(() => onOpenDecision(identity))}
          />
        </div>
        <div ref={rulesPanel} id={`${id}-panel-rules`} role="tabpanel" aria-labelledby={`${id}-tab-rules`} hidden={tab !== 'rules'} className={tab === 'rules' ? 'flex min-h-0 min-w-0 flex-col' : 'hidden'}>
          {editing ? <RuleRoutingEditor
            idPrefix={id} editorRef={editorRef} previewHeadingRef={previewHeading}
            form={form} fieldEquals={fieldEquals} position={position} positionLabel={editing === 'new' ? '保存后排到最后' : editIndex >= 0 ? `第 ${editIndex + 1} 位（共 ${rules.length} 条）` : undefined}
            sample={sample} sampleFromEvent={sampleFromEvent} sampleOpen={sampleOpen} onSampleOpenChange={setSampleOpen}
            editing={editing} dirty={dirty} busy={busy} saving={saving} previewing={previewing}
            fieldErrors={fieldErrors} saveError={saveError} previewError={previewError} preview={preview}
            onBack={() => guarded(() => {}, () => focusRule(editing === 'new' ? undefined : editing.id))}
            onFieldChange={change} onFieldEqualsChange={changeFieldEquals} onSampleChange={changeSample} onSave={save} onPreview={() => void renderPreview()}
          /> : showImport ? <div className="min-h-0 flex-1 space-y-4 overflow-y-auto p-4 sm:p-5">
            <Alert>导入 JSON 后可修改或试渲染。新规则保存为停用，不会自动参与匹配。</Alert>
            <div className="min-w-0 space-y-1.5 text-sm font-medium"><label htmlFor={`${id}-import`}>助手规则草稿 JSON</label><textarea id={`${id}-import`} data-rule-routing-import-text aria-invalid={!!importError} aria-describedby={importError ? `${id}-import-error` : undefined} value={draftText} onChange={event => { setDraftText(event.target.value); setImportError('') }} className={textAreaClass} rows={10} /></div>
            {importError && <Alert id={`${id}-import-error`} variant="danger" role="alert">{importError}</Alert>}
            <div className="flex flex-wrap gap-2"><Button onClick={() => { try { start(undefined, parseRuleDraft(draftText)) } catch (err) { setImportError(errorText(err, '草稿校验失败')); document.getElementById(`${id}-import`)?.focus() } }} data-rule-routing-import-confirm>导入并审阅</Button><Button variant="ghost" onClick={() => guarded(() => {}, () => rulesPanel.current?.querySelector<HTMLButtonElement>('[data-rule-routing-import]')?.focus())} data-rule-routing-import-cancel>取消</Button></div>
          </div> : <>
            {undo && <Alert variant="success" className="mx-4 mt-3 shrink-0 sm:mx-5">
              <div className="flex flex-wrap items-center justify-between gap-2"><span>已停用「{undo.name}」</span><Button type="button" variant="outline" size="sm" className="max-md:h-11" disabled={busy} onClick={() => toggle(undo)}>撤销</Button></div>
            </Alert>}
            <RulesList
              rules={rules} loading={loading} busy={busy} sorting={sorting} listLoadError={listLoadError} listActionError={listActionError}
              hitsLabel={hitsLabel} hitsRange={hitsRange} hitFor={hitFor} highlightedId={highlightedId}
              statisticsLoading={statisticsLoading} statisticsError={statisticsError} onRetryStatistics={() => void loadStatistics()}
              onCreate={() => start()}
              onImport={() => { setShowImport(true); setImportError(''); requestAnimationFrame(() => document.getElementById(`${id}-import`)?.focus()) }}
              onRefresh={() => { setListActionError(''); void load() }} onEdit={rule => start(rule)} onToggle={toggle}
              onMove={(index, direction) => void move(index, direction)} onDelete={setDeleting}
              onViewHits={rule => { if (summary) showEvents({ ruleId: rule.id, since: summary.since, until: summary.until, ruleName: rule.name }) }}
            />
          </>}
        </div>
        <div ref={assistantPanel} id={`${id}-panel-assistant`} role="tabpanel" aria-labelledby={`${id}-tab-assistant`} hidden={tab !== 'assistant'} className={tab === 'assistant' ? 'flex min-h-0 min-w-0 flex-col' : 'hidden'}>
          {tab === 'assistant' && (policyEditing && policy ? <RoutingPolicyEditor
            policy={policy} onPolicyChange={setPolicy} onDirtyChange={setPolicyDirty} onSavingChange={setPolicySaving}
            onBack={backFromPolicy} onSaved={() => { setPolicyEditing(false); requestAnimationFrame(focusPolicy) }}
          /> : <AutoLoopAssistantRouting
            onOpenEvent={openEvent} onOpenDecision={identity => guarded(() => onOpenDecision(identity))} onViewAll={() => showEvents({ route: 'assistant' })}
            policy={policy} policyLoading={policyLoading} policyError={policyError} onEditPolicy={() => setPolicyEditing(true)} onRetryPolicy={() => void loadPolicy()}
          />)}
        </div>
        <div id={`${id}-panel-stats`} role="tabpanel" aria-labelledby={`${id}-tab-stats`} hidden={tab !== 'stats'} className={tab === 'stats' ? 'flex min-h-0 min-w-0 flex-col' : 'hidden'}>
          {tab === 'stats' && <AutoLoopRoutingStatsPanel summary={summary} loading={statisticsLoading} error={statisticsError} days={days} onDaysChange={setDays} onRefresh={() => void loadStatistics()} onDrilldown={showEvents} />}
        </div>
      </div>
    </DialogShell>
    <ConfirmDialog layer="nested" open={!!deleting} onClose={() => { if (!mutating) { const key = deleting?.id; setDeleting(null); requestAnimationFrame(() => focusRule(key)) } }} onConfirm={() => deleting && void mutate(deleting)} loading={mutating} title="删除规则？" description={`删除「${deleting?.name ?? ''}」后，仅后续事件停止匹配，在途事件保留原版本。`} confirmText="删除" variant="danger" />
    <ConfirmDialog layer="nested" open={!!activation} onClose={() => { if (!saving && !mutating) { const key = activation?.rule.id; setActivation(null); requestAnimationFrame(() => { if (editing) editorRef.current?.querySelector<HTMLElement>('input')?.focus(); else focusRule(key) }) } }} onConfirm={() => activation?.run()} loading={saving || mutating} title="启用全量匹配规则？" variant="warning" confirmText="确认启用" description={activation && <div className="space-y-2 break-words">
      <p>「{activation.rule.name}」会处理前序规则未匹配的所有入站事件。</p>
      <p>{activateIndex >= 0 ? `匹配顺序：第 ${activateIndex + 1} 位。` : `顺序值：${activation.rule.position}。`}{activation.value.binding ? '找到并验证关联的 Multica 任务后追加评论，否则可能创建任务。' : '命中后按规则向 Multica 写入任务或评论。'}</p>
      {shadowed && <p>前面已有启用的全量规则。请先核对顺序。</p>}
    </div>} />
    <ConfirmDialog layer="nested" open={!!exit} onClose={cancelExit} onConfirm={confirmExit} onCloseAutoFocus={restoreExitFocus} title={policyDirty ? '放弃未保存的研判提示词？' : '放弃未保存的规则草稿？'} description="表单中的修改尚未保存。" confirmText="放弃草稿" cancelText="继续编辑" variant="warning" loading={exit?.restoring} />
  </>
}
