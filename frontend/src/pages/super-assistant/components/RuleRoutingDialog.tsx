import { useEffect, useEffectEvent, useId, useRef, useState, type ReactNode } from 'react'
import { ArrowLeft, ArrowDown, ArrowUp, Plus, RefreshCw, Route, Trash2 } from 'lucide-react'
import { toast } from 'sonner'
import { autoloopApi, type AutoLoopPreview, type AutoLoopRule, type AutoLoopRuleCreate } from '@/api/autoloop'
import { Alert } from '@/components/ui/Alert'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { Input } from '@/components/ui/Input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { DialogShell } from './AssistantConfiguration'
import { errorText } from './assistantPanelUtils'
import { newRule, parseRuleDraft, PRIORITY_LABEL } from './autoloopLogic'
import { parsePreviewSample, validateRuleForm, type RuleFormErrors, type RuleFormField } from './ruleRoutingForm'
import { textAreaClass, UntrustedOriginal } from './AutoLoopDetails'
import AutoLoopEventHistory from './AutoLoopEventHistory'

function Field({ label, htmlFor, children }: { label: string; htmlFor: string; children: ReactNode }) {
  return <div className="min-w-0 space-y-1.5 text-xs font-medium"><label htmlFor={htmlFor}>{label}</label>{children}</div>
}
const matchFields = [['ingestKeyId', '来源 Key 标识（已验证）'], ['sourceSystem', '来源系统（弱过滤）'], ['channel', '渠道'], ['titleContains', '标题包含'], ['bodyContains', '正文包含']] as const
const matchLabels: Record<string, string> = { sourceKind: '来源类型', ...Object.fromEntries(matchFields), fieldEquals: '字段等值' }
function matchSummary(match: AutoLoopRuleCreate['match']) {
  return Object.entries(match).filter(([, value]) => value && (!Array.isArray(value) || value.length))
    .map(([key, value]) => `${matchLabels[key] ?? key}：${Array.isArray(value) ? value.map(item => `${item.field} = ${item.value}`).join('、') : value}`).join(' · ') || '全部路由事件（前序启用规则优先命中）'
}
const previewBoundary = '仅渲染模板与关联提取，不校验匹配条件与规则顺序，也不校验 Issue 权限，不创建任务。'

export default function RuleRoutingDialog({ initialDraft, initialEventId, scheduledTaskId, scheduledRunId, onClose, onOpenDecision }: {
  initialDraft?: AutoLoopRuleCreate | null
  initialEventId?: string | null
  scheduledTaskId?: string | null
  scheduledRunId?: string | null
  onClose: () => void
  onOpenDecision: (id: string) => void
}) {
  const id = useId()
  const [tab, setTab] = useState<'rules' | 'events'>(initialEventId || scheduledTaskId || scheduledRunId ? 'events' : 'rules')
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
  const [baseline, setBaseline] = useState('')
  const [fieldEquals, setFieldEquals] = useState(JSON.stringify(initialDraft?.match.fieldEquals ?? [], null, 2))
  const [sample, setSample] = useState('{"title":"示例标题","body":"示例正文"}')
  const [draftText, setDraftText] = useState('')
  const [showImport, setShowImport] = useState(false)
  const [preview, setPreview] = useState<AutoLoopPreview | null>(null)
  const [saving, setSaving] = useState(false)
  const [previewing, setPreviewing] = useState(false)
  const [sorting, setSorting] = useState(false)
  const [mutating, setMutating] = useState(false)
  const [deleting, setDeleting] = useState<AutoLoopRule | null>(null)
  const [exit, setExit] = useState<{ run: () => void } | null>(null)
  const request = useRef(0)
  const previewRequest = useRef(0)
  const previousDraft = useRef(initialDraft)
  const editorRef = useRef<HTMLDivElement>(null)
  const previewHeading = useRef<HTMLHeadingElement>(null)
  const rulesTab = useRef<HTMLButtonElement>(null)
  const eventsTab = useRef<HTMLButtonElement>(null)
  const snapshot = JSON.stringify({ form, fieldEquals, position })
  const currentDraft = useRef(snapshot)
  useEffect(() => { currentDraft.current = snapshot }, [snapshot])
  const dirty = !!editing && baseline !== snapshot
  const busy = saving || previewing || sorting || mutating
  const emptyMatch = !Object.entries(form.match).some(([key, value]) => key !== 'fieldEquals' && !!value) && /^\s*\[\s*\]\s*$/.test(fieldEquals)

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
  useEffect(() => {
    if (preview) { previewHeading.current?.scrollIntoView({ block: 'start' }); previewHeading.current?.focus({ preventScroll: true }) }
  }, [preview])
  const openLinkedEvent = useEffectEvent(() => { guarded(() => setTab('events')) })
  useEffect(() => { if (initialEventId || scheduledTaskId || scheduledRunId) openLinkedEvent() }, [initialEventId, scheduledTaskId, scheduledRunId])
  const fillIncomingDraft = useEffectEvent((draft: AutoLoopRuleCreate) => { guarded(() => { start(undefined, draft); setTab('rules') }) })
  useEffect(() => {
    if (initialDraft && initialDraft !== previousDraft.current) fillIncomingDraft(initialDraft)
    previousDraft.current = initialDraft
  }, [initialDraft])

  function clearFormFeedback() { setSaveError(''); setPreviewError(''); setFieldErrors({}); setPreview(null); previewRequest.current++ }
  function discard() {
    setEditing(null); setBaseline(''); setShowImport(false); setDraftText(''); setImportError(''); clearFormFeedback()
  }
  function guarded(action: () => void) {
    if (busy) return
    if (dirty || (showImport && draftText.trim())) setExit({ run: () => { discard(); action() } })
    else action()
  }
  function selectTab(key: 'rules' | 'events') {
    if (key === tab) return
    guarded(() => {
      discard(); setTab(key)
      // 等确认弹窗卸载并完成焦点恢复后，再聚焦目标 Tab。
      requestAnimationFrame(() => (key === 'rules' ? rulesTab : eventsTab).current?.focus())
    })
  }
  function start(rule?: AutoLoopRule, imported?: AutoLoopRuleCreate) {
    const value = imported ?? (rule ? {
      name: rule.name, enabled: rule.enabled, position: rule.position, match: rule.match,
      titleTemplate: rule.titleTemplate, descriptionTemplate: rule.descriptionTemplate,
      priority: rule.priority, assignee: rule.assignee, binding: rule.binding,
    } : newRule(Math.max(-1, ...rules.map(item => item.position)) + 1))
    const fields = JSON.stringify(value.match.fieldEquals ?? [], null, 2)
    setForm(value); setPosition(String(value.position)); setFieldEquals(fields)
    setBaseline(imported ? '' : JSON.stringify({ form: value, fieldEquals: fields, position: String(value.position) }))
    setEditing(rule ?? 'new'); setShowImport(false); setDraftText(''); setImportError(''); clearFormFeedback()
  }
  function invalidatePreview() { setPreview(null); setPreviewError(''); previewRequest.current++ }
  function clearField(field?: RuleFormField) { if (field) setFieldErrors(previous => ({ ...previous, [field]: undefined })) }
  function change(next: AutoLoopRuleCreate, field?: RuleFormField) { setForm(next); invalidatePreview(); clearField(field) }
  function focusInvalid(errors: RuleFormErrors) {
    setFieldErrors(errors)
    // 等待内联错误挂载后，按 DOM 顺序聚焦并滚动到首个错误字段。
    requestAnimationFrame(() => editorRef.current?.querySelector<HTMLElement>('[aria-invalid="true"]')?.focus())
  }
  function validated() {
    const result = validateRuleForm(form, fieldEquals, position, !!editing && editing !== 'new')
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
  function textareaProps(field: RuleFormField) { return { id: `${id}-${field}`, 'aria-invalid': !!fieldErrors[field], 'aria-describedby': fieldErrors[field] ? `${id}-${field}-error` : undefined } }
  function fieldError(field: RuleFormField) { return fieldErrors[field] && <p id={`${id}-${field}-error`} aria-live="polite" className="text-xs text-[var(--color-danger)]">{fieldErrors[field]}</p> }

  return <>
    <DialogShell title="规则路由" description="我的规则：处理发送给当前用户的后续事件。按匹配顺序首条命中，未命中交由助手研判。" size="xwide" icon={<Route size={18} />} onClose={() => guarded(onClose)} onInteractOutside={event => event.preventDefault()} contentClassName="h-[88dvh]">
      <div className="flex shrink-0 gap-1 border-b border-border px-4 py-2" role="tablist" aria-label="规则路由分区">
        {(['rules', 'events'] as const).map(key => <Button key={key} ref={key === 'rules' ? rulesTab : eventsTab} id={`${id}-${key}-tab`} role="tab" aria-selected={tab === key} aria-controls={`${id}-${key}-panel`} tabIndex={tab === key ? 0 : -1} variant={tab === key ? 'default' : 'ghost'} size="sm" onClick={() => selectTab(key)} onKeyDown={event => {
          if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return
          event.preventDefault()
          selectTab(event.key === 'Home' ? 'rules' : event.key === 'End' ? 'events' : key === 'rules' ? 'events' : 'rules')
        }}>{key === 'rules' ? '规则与模板' : '事件流水'}</Button>)}
      </div>
      <div id={`${id}-events-panel`} role="tabpanel" aria-labelledby={`${id}-events-tab`} hidden={tab !== 'events'} className={tab === 'events' ? 'flex min-h-0 flex-1 flex-col' : 'hidden'}>
        {tab === 'events' && <AutoLoopEventHistory initialEventId={initialEventId} scheduledTaskId={scheduledTaskId} scheduledRunId={scheduledRunId} rules={rules} onOpenDecision={onOpenDecision} />}
      </div>
      <div id={`${id}-rules-panel`} role="tabpanel" aria-labelledby={`${id}-rules-tab`} hidden={tab !== 'rules'} className={tab === 'rules' ? 'flex min-h-0 flex-1 flex-col' : 'hidden'}>
        <div ref={editorRef} className="min-h-0 flex-1 overflow-y-auto p-4 sm:p-5">
          {!editing && !showImport && <>
            <div className="mb-4 flex flex-wrap items-center gap-2">
              <Button size="sm" disabled={busy} onClick={() => start()}><Plus size={14} />新建规则</Button>
              <Button size="sm" variant="outline" disabled={busy} onClick={() => { setShowImport(true); setImportError('') }}>导入助手草稿</Button>
              <Button size="sm" variant="ghost" onClick={() => { setListActionError(''); void load() }} disabled={busy || loading}><RefreshCw size={14} />刷新</Button>
            </div>
            {listLoadError && <Alert variant="danger" role="alert" className="mb-3">{listLoadError}</Alert>}
            {listActionError && <Alert variant="danger" role="alert" className="mb-3">{listActionError}</Alert>}
            {sorting && <p role="status" className="mb-3 text-sm text-muted-foreground">正在保存规则顺序…</p>}
            {loading ? <p role="status" className="text-sm text-muted-foreground">加载规则…</p> : rules.length === 0 ? <p className="py-12 text-center text-sm text-muted-foreground">尚无规则。未命中事件交由助手研判。</p> :
              <div className="space-y-3">{rules.map((rule, index) => <article key={rule.id} className="rounded-xl border border-border p-4">
                <div className="flex flex-wrap items-center gap-2"><span className="text-xs text-muted-foreground">第 {index + 1} 条</span><h3 className="min-w-0 break-words text-sm font-medium">{rule.name}</h3><Badge variant={rule.enabled ? 'success' : 'secondary'}>{rule.enabled ? '已启用' : '已停用'}</Badge><span className="text-xs text-muted-foreground">顺序 {rule.position} · v{rule.version}</span></div>
                <p className="mt-2 break-words text-xs text-muted-foreground">{matchSummary(rule.match)}</p>
                <div className="mt-3 flex flex-wrap gap-2">
                  <Button size="sm" variant="outline" disabled={busy} onClick={() => start(rule)}>编辑 {rule.name}</Button>
                  <Button size="sm" variant="ghost" disabled={busy} onClick={() => void mutate(rule, { enabled: !rule.enabled })}>{rule.enabled ? '停用' : '启用'}</Button>
                  <Button size="icon-sm" variant="ghost" aria-label={`上移 ${rule.name}`} disabled={busy || index === 0} onClick={() => void move(index, -1)}><ArrowUp size={14} /></Button>
                  <Button size="icon-sm" variant="ghost" aria-label={`下移 ${rule.name}`} disabled={busy || index === rules.length - 1} onClick={() => void move(index, 1)}><ArrowDown size={14} /></Button>
                  <Button size="icon-sm" variant="ghost" aria-label={`删除 ${rule.name}`} disabled={busy} onClick={() => setDeleting(rule)}><Trash2 size={14} /></Button>
                </div>
              </article>)}</div>}
          </>}
          {showImport && <div className="space-y-4">
            <Alert>粘贴助手在会话中给出的规则 JSON。导入仅填入表单，审阅后点击“确认创建规则”才会生效。</Alert>
            <Field label="助手规则草稿 JSON" htmlFor={`${id}-import`}><textarea id={`${id}-import`} aria-invalid={!!importError} aria-describedby={importError ? `${id}-import-error` : undefined} value={draftText} onChange={event => { setDraftText(event.target.value); setImportError('') }} className={textAreaClass} rows={10} /></Field>
            {importError && <Alert id={`${id}-import-error`} variant="danger" role="alert">{importError}</Alert>}
            <div className="flex flex-wrap gap-2"><Button onClick={() => { try { start(undefined, parseRuleDraft(draftText)) } catch (err) { setImportError(errorText(err, '草稿校验失败')); document.getElementById(`${id}-import`)?.focus() } }}>导入并审阅</Button><Button variant="ghost" onClick={() => guarded(discard)}>取消</Button></div>
          </div>}
          {editing && <div className="space-y-6">
            <div className="flex flex-wrap items-center gap-2"><Button size="sm" variant="ghost" onClick={() => guarded(discard)}><ArrowLeft size={14} />返回规则列表</Button>{dirty && <Badge variant="secondary">未保存草稿</Badge>}</div>
            <section aria-labelledby={`${id}-basic`} className="space-y-3">
              <h3 id={`${id}-basic`} className="text-sm font-semibold">基本设置</h3>
              <Input label="规则名称" value={form.name} error={fieldErrors.name} aria-invalid={!!fieldErrors.name} onChange={event => change({ ...form, name: event.target.value }, 'name')} />
              <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={form.enabled} onChange={event => change({ ...form, enabled: event.target.checked })} />保存后启用</label>
              {form.enabled && emptyMatch && <Alert variant="warning">空条件会匹配当前用户的全部路由事件；前序启用规则优先命中。</Alert>}
            </section>
            <section aria-labelledby={`${id}-match`} className="space-y-3">
              <h3 id={`${id}-match`} className="text-sm font-semibold">事件匹配</h3>
              <p className="text-xs leading-5 text-muted-foreground">已填写条件须同时满足。匹配顺序的小值优先，仅启用规则参与匹配。</p>
              <div className="grid gap-4 sm:grid-cols-2">
                <Input label="匹配顺序" type="number" min="0" step="1" value={position} error={fieldErrors.position} aria-invalid={!!fieldErrors.position} onChange={event => { setPosition(event.target.value); change({ ...form, position: Number(event.target.value) }, 'position') }} />
                <div className="space-y-1.5"><span className="text-xs font-medium">来源类型</span><Select value={form.match.sourceKind || 'all'} onValueChange={value => change({ ...form, match: { ...form.match, sourceKind: value === 'all' ? undefined : value } }, 'sourceKind')}><SelectTrigger aria-label="规则来源类型" {...textareaProps('sourceKind')}><SelectValue /></SelectTrigger><SelectContent><SelectItem value="all">全部来源</SelectItem><SelectItem value="notification">外部通知 (notification)</SelectItem><SelectItem value="schedule">日程 (schedule)</SelectItem>{form.match.sourceKind && !['notification', 'schedule'].includes(form.match.sourceKind) && <SelectItem value={form.match.sourceKind}>{form.match.sourceKind}（历史值）</SelectItem>}</SelectContent></Select>{fieldError('sourceKind')}</div>
                {matchFields.map(([key, label]) => <Input key={key} label={label} value={form.match[key] ?? ''} error={fieldErrors[key]} aria-invalid={!!fieldErrors[key]} onChange={event => change({ ...form, match: { ...form.match, [key]: event.target.value || undefined } }, key)} />)}
              </div>
              <p className="text-xs leading-5 text-muted-foreground">来源 Key 来自已验证的接入身份；来源系统只是事件内容的弱过滤，不代表可信身份。包含条件区分大小写。</p>
              <Field label="字段等值 JSON" htmlFor={`${id}-fieldEquals`}><textarea {...textareaProps('fieldEquals')} className={textAreaClass} rows={3} value={fieldEquals} onChange={event => { setFieldEquals(event.target.value); invalidatePreview(); clearField('fieldEquals') }} />{fieldError('fieldEquals')}</Field>
              <p className="text-xs text-muted-foreground">例如 [{'{"field":"severity","value":"high"}'}]；field 可用点路径，value 必须是字符串。</p>
            </section>
            <section aria-labelledby={`${id}-task`} className="space-y-3">
              <h3 id={`${id}-task`} className="text-sm font-semibold">任务内容与关联</h3>
              <div className="grid gap-4 sm:grid-cols-2">
                <div className="space-y-1.5"><span className="text-xs font-medium">任务优先级</span><Select value={form.priority || 'default'} onValueChange={value => change({ ...form, priority: value === 'default' ? null : value as 'low' | 'medium' | 'high' })}><SelectTrigger aria-label="任务优先级"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="default">默认</SelectItem>{Object.entries(PRIORITY_LABEL).map(([key, value]) => <SelectItem key={key} value={key}>{value}</SelectItem>)}</SelectContent></Select></div>
                <Input label="指派对象" value={form.assignee ?? ''} error={fieldErrors.assignee} aria-invalid={!!fieldErrors.assignee} onChange={event => change({ ...form, assignee: event.target.value }, 'assignee')} />
              </div>
              <p className="text-xs text-muted-foreground">任务优先级决定命中后新建任务的优先级，不影响规则匹配顺序。指派对象留空表示不指定。</p>
              <Field label="标题模板" htmlFor={`${id}-titleTemplate`}><textarea {...textareaProps('titleTemplate')} className={textAreaClass} rows={2} value={form.titleTemplate} onChange={event => change({ ...form, titleTemplate: event.target.value }, 'titleTemplate')} />{fieldError('titleTemplate')}</Field>
              <Field label="描述模板" htmlFor={`${id}-descriptionTemplate`}><textarea {...textareaProps('descriptionTemplate')} className={textAreaClass} rows={3} value={form.descriptionTemplate} onChange={event => change({ ...form, descriptionTemplate: event.target.value }, 'descriptionTemplate')} />{fieldError('descriptionTemplate')}</Field>
              <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={!!form.binding} onChange={event => change({ ...form, binding: event.target.checked ? { mode: 'extract' } : null })} />显式关联提取</label>
              {form.binding && <div className="space-y-3 rounded-lg border border-border p-3"><div className="grid gap-4 sm:grid-cols-2"><Input label="关联提取模式（可选）" value={form.binding.pattern ?? ''} error={fieldErrors.pattern} aria-invalid={!!fieldErrors.pattern} onChange={event => change({ ...form, binding: { ...form.binding!, pattern: event.target.value } }, 'pattern')} /><Input label="关联目标 Issue（可选）" value={form.binding.targetIssue ?? ''} error={fieldErrors.targetIssue} aria-invalid={!!fieldErrors.targetIssue} onChange={event => change({ ...form, binding: { ...form.binding!, targetIssue: event.target.value } }, 'targetIssue')} /></div><p className="text-xs text-muted-foreground">关联提取可使用有界模式，例如 INC-[0-9]{'{1,8}'}。不支持无界重复、分支、引用或环视；填写目标 Issue 仍须在执行时校验权限。</p></div>}
            </section>
            <section aria-labelledby={`${id}-preview`} className="space-y-3">
              <h3 id={`${id}-preview`} className="text-sm font-semibold">预览</h3>
              <p className="text-xs leading-5 text-muted-foreground">{previewBoundary}样例事件 JSON 仅供本次试渲染使用。</p>
              <Field label="样例事件 JSON" htmlFor={`${id}-sample`}><textarea {...textareaProps('sample')} className={textAreaClass} rows={4} value={sample} onChange={event => { setSample(event.target.value); invalidatePreview(); clearField('sample') }} />{fieldError('sample')}</Field>
              {previewError && <Alert variant="danger" role="alert">{previewError}</Alert>}
              {preview && <section className="space-y-3 rounded-xl border border-border p-4" aria-label="模板预览结果">
                <h4 ref={previewHeading} tabIndex={-1} className="text-sm font-medium outline-none focus-visible:ring-2 focus-visible:ring-ring">{preview.ruleVersion === 0 ? '模板预览结果（未保存草稿）' : `模板预览结果（已存规则 v${preview.ruleVersion}）`}</h4>
                <p className="text-xs leading-5 text-muted-foreground">{previewBoundary}</p>
                <p className="whitespace-pre-wrap break-words font-medium">{preview.renderedTitle}</p><pre className="whitespace-pre-wrap break-words text-sm">{preview.renderedDescription}</pre>
                <p className="text-xs text-muted-foreground">任务优先级：{preview.priority ? PRIORITY_LABEL[preview.priority] : '默认'} · 指派：{preview.assignee || '未指定'} · 绑定：{preview.bindingTarget || '未绑定'}</p>
                {preview.warnings.map((warning, index) => <Alert key={index} variant="warning">{warning}</Alert>)}
                <UntrustedOriginal value={preview.untrustedOriginal} />
              </section>}
            </section>
          </div>}
        </div>
        {editing && <div className="shrink-0 space-y-2 border-t border-border bg-background px-4 py-3 sm:px-5">
          {saveError && <Alert variant="danger" role="alert">{saveError}</Alert>}
          <div className="flex flex-wrap items-center justify-end gap-2" aria-label="规则操作"><span className="sr-only" role="status">{previewing ? '正在试渲染模板…' : preview ? '模板预览已更新' : saving ? '正在保存规则…' : ''}</span><Button variant="outline" loading={previewing} disabled={busy} onClick={() => void renderPreview()}>试渲染模板（不创建任务）</Button><Button loading={saving} disabled={busy} onClick={() => void save()}>{editing === 'new' ? '确认创建规则' : '保存规则'}</Button></div>
        </div>}
      </div>
    </DialogShell>
    <ConfirmDialog open={!!deleting} onClose={() => setDeleting(null)} onConfirm={() => deleting && void mutate(deleting)} loading={mutating} title="删除规则？" description={`删除“${deleting?.name ?? ''}”后，仅后续事件停止匹配，在途事件保留原版本。`} confirmText="删除" variant="danger" />
    <ConfirmDialog open={!!exit} onClose={() => setExit(null)} onConfirm={() => { const action = exit; setExit(null); action?.run() }} title="放弃未保存的规则草稿？" description="表单中的修改尚未保存。" confirmText="放弃草稿" cancelText="继续编辑" variant="warning" />
  </>
}
