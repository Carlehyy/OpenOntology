import { useEffect, useEffectEvent, useRef, useState, type ReactNode } from 'react'
import { ArrowLeft, ArrowDown, ArrowUp, Plus, RefreshCw, Route, Trash2 } from 'lucide-react'
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
import { textAreaClass, UntrustedOriginal } from './AutoLoopDetails'
import AutoLoopEventHistory from './AutoLoopEventHistory'

function Field({ label, children }: { label: string; children: ReactNode }) {
  return <label className="block space-y-1.5 text-xs font-medium"><span>{label}</span>{children}</label>
}

export default function RuleRoutingDialog({ initialDraft, initialEventId, scheduledTaskId, scheduledRunId, onClose, onOpenDecision }: {
  initialDraft?: AutoLoopRuleCreate | null
  initialEventId?: string | null
  scheduledTaskId?: string | null
  scheduledRunId?: string | null
  onClose: () => void
  onOpenDecision: (id: string) => void
}) {
  const [tab, setTab] = useState<'rules' | 'events'>(initialEventId || scheduledTaskId || scheduledRunId ? 'events' : 'rules')
  const [rules, setRules] = useState<AutoLoopRule[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [editing, setEditing] = useState<AutoLoopRule | 'new' | null>(initialDraft ? 'new' : null)
  const [form, setForm] = useState<AutoLoopRuleCreate>(initialDraft ?? newRule())
  const [baseline, setBaseline] = useState('')
  const [fieldEquals, setFieldEquals] = useState(JSON.stringify(initialDraft?.match.fieldEquals ?? [], null, 2))
  const [sample, setSample] = useState('{"title":"示例标题","body":"示例正文"}')
  const [draftText, setDraftText] = useState('')
  const [showImport, setShowImport] = useState(false)
  const [preview, setPreview] = useState<AutoLoopPreview | null>(null)
  const [busy, setBusy] = useState(false)
  const [deleting, setDeleting] = useState<AutoLoopRule | null>(null)
  const [exit, setExit] = useState<{ run: () => void } | null>(null)
  const request = useRef(0)
  const previewRequest = useRef(0)
  const previousDraft = useRef(initialDraft)
  const dirty = !!editing && baseline !== JSON.stringify({ form, fieldEquals })

  async function load() {
    const generation = ++request.current
    setLoading(true)
    try {
      const result = await autoloopApi.listRules()
      if (generation !== request.current) return
      setRules([...result].sort((a, b) => a.position - b.position || a.id.localeCompare(b.id)))
      setError('')
    } catch (err) { if (generation === request.current) setError(errorText(err, '规则加载失败')) }
    finally { if (generation === request.current) setLoading(false) }
  }
  useEffect(() => { void load(); return () => { request.current++; previewRequest.current++ } }, [])
  const openLinkedEvent = useEffectEvent(() => { guarded(() => setTab('events')) })
  useEffect(() => { if (initialEventId || scheduledTaskId || scheduledRunId) openLinkedEvent() }, [initialEventId, scheduledTaskId, scheduledRunId])
  const fillIncomingDraft = useEffectEvent((draft: AutoLoopRuleCreate) => { guarded(() => { start(undefined, draft); setTab('rules') }) })
  useEffect(() => {
    if (initialDraft && initialDraft !== previousDraft.current) fillIncomingDraft(initialDraft)
    previousDraft.current = initialDraft
  }, [initialDraft])

  function discard() {
    setEditing(null); setBaseline(''); setShowImport(false); setDraftText(''); setPreview(null)
    previewRequest.current++
  }
  function guarded(action: () => void) {
    if (busy) return
    if (dirty || (showImport && draftText.trim())) setExit({ run: () => { discard(); action() } })
    else action()
  }
  function start(rule?: AutoLoopRule, imported?: AutoLoopRuleCreate) {
    const value = imported ?? (rule ? {
      name: rule.name, enabled: rule.enabled, position: rule.position, match: rule.match,
      titleTemplate: rule.titleTemplate, descriptionTemplate: rule.descriptionTemplate,
      priority: rule.priority, assignee: rule.assignee, binding: rule.binding,
    } : newRule(Math.max(-1, ...rules.map(item => item.position)) + 1))
    const fields = JSON.stringify(value.match.fieldEquals ?? [], null, 2)
    setForm(value)
    setFieldEquals(fields)
    setBaseline(imported ? '' : JSON.stringify({ form: value, fieldEquals: fields }))
    setEditing(rule ?? 'new')
    setPreview(null)
    setShowImport(false)
    setDraftText('')
    setError('')
    previewRequest.current++
  }
  function change(next: AutoLoopRuleCreate) { setForm(next); setPreview(null); previewRequest.current++ }
  function payload(): AutoLoopRuleCreate {
    const fields: unknown = JSON.parse(fieldEquals)
    return parseRuleDraft(JSON.stringify({ ...form, name: form.name.trim(), match: { ...form.match, fieldEquals: fields }, assignee: form.assignee?.trim() || undefined }))
  }
  async function save() {
    if (busy) return
    setBusy(true); setError('')
    try {
      const value = payload()
      if (editing && editing !== 'new') await autoloopApi.updateRule(editing.id, value)
      else await autoloopApi.createRule(value)
      setEditing(null); setBaseline(''); setPreview(null)
      await load()
    } catch (err) { setError(errorText(err, '保存失败，表单已保留')) }
    finally { setBusy(false) }
  }
  async function renderPreview() {
    const generation = ++previewRequest.current
    setBusy(true); setError('')
    try {
      const sampleEvent: unknown = JSON.parse(sample)
      const result = editing && editing !== 'new' && !dirty
        ? await autoloopApi.previewStoredRule(editing.id, sampleEvent)
        : await autoloopApi.previewRule(payload(), sampleEvent)
      if (generation === previewRequest.current) setPreview(result)
    } catch (err) { if (generation === previewRequest.current) setError(errorText(err, '预览失败')) }
    finally { setBusy(false) }
  }
  async function mutate(rule: AutoLoopRule, patch?: Partial<AutoLoopRuleCreate>) {
    setBusy(true); setError('')
    try {
      if (patch) await autoloopApi.updateRule(rule.id, patch)
      else { await autoloopApi.deleteRule(rule.id); setDeleting(null) }
      await load()
    } catch (err) { setError(errorText(err, '规则更新失败')) }
    finally { setBusy(false) }
  }
  async function move(index: number, direction: -1 | 1) {
    const order = [...rules]
    const destination = index + direction
    if (busy || destination < 0 || destination >= order.length) return
    ;[order[index], order[destination]] = [order[destination], order[index]]
    setBusy(true); setError('')
    try {
      // position 不要求唯一；最终写入连续值，避免首位为 0 或历史重复值使移动无效。
      for (const [position, rule] of order.entries()) if (rule.position !== position) await autoloopApi.updateRule(rule.id, { position })
    } catch (err) { setError(errorText(err, '排序未全部保存，请刷新后重试')) }
    finally { await load(); setBusy(false) }
  }
  const matchFields = [['sourceKind', '来源类型'], ['ingestKeyId', '已验证来源 Key 标识'], ['sourceSystem', '来源系统（弱过滤）'], ['channel', '渠道'], ['titleContains', '标题包含'], ['bodyContains', '正文包含']] as const

  return <>
    <DialogShell title="规则路由" description="按顺序首条命中生效；保存仅影响后续事件。可试渲染模板并查看事件与动作事实。" size="xwide" icon={<Route size={18} />} onClose={() => guarded(onClose)} onInteractOutside={event => event.preventDefault()} contentClassName="h-[88dvh]">
      <div className="flex shrink-0 gap-1 border-b border-border px-4 py-2" role="tablist" aria-label="规则路由分区">
        {(['rules', 'events'] as const).map(key => <Button key={key} role="tab" aria-selected={tab === key} variant={tab === key ? 'default' : 'ghost'} size="sm" onClick={() => guarded(() => setTab(key))}>{key === 'rules' ? '规则与模板' : '事件流水'}</Button>)}
      </div>
      {error && <Alert variant="danger" role="alert" className="mx-4 mt-3 shrink-0">{error}</Alert>}
      {tab === 'events' ? <AutoLoopEventHistory initialEventId={initialEventId} scheduledTaskId={scheduledTaskId} scheduledRunId={scheduledRunId} rules={rules} onOpenDecision={onOpenDecision} /> :
        <div className="min-h-0 flex-1 overflow-y-auto p-4 sm:p-5">
          {!editing && !showImport && <>
            <div className="mb-4 flex flex-wrap items-center gap-2">
              <Button size="sm" onClick={() => start()}><Plus size={14} />新建规则</Button>
              <Button size="sm" variant="outline" onClick={() => { setShowImport(true); setError('') }}>导入助手草稿</Button>
              <Button size="sm" variant="ghost" onClick={() => void load()} disabled={busy || loading}><RefreshCw size={14} />刷新</Button>
            </div>
            {loading ? <p role="status" className="text-sm text-muted-foreground">加载规则…</p> : rules.length === 0 ? <p className="py-12 text-center text-sm text-muted-foreground">尚无规则。未命中事件交由助手研判。</p> :
              <div className="space-y-3">{rules.map((rule, index) => <article key={rule.id} className="rounded-xl border border-border p-4">
                <div className="flex flex-wrap items-center gap-2"><h3 className="min-w-0 break-words text-sm font-medium">{rule.name}</h3><Badge variant={rule.enabled ? 'success' : 'secondary'}>{rule.enabled ? '已启用' : '已停用'}</Badge><span className="text-xs text-muted-foreground">顺序 {rule.position} · v{rule.version}</span></div>
                <p className="mt-2 break-words text-xs text-muted-foreground">{Object.entries(rule.match).filter(([, value]) => value && (!Array.isArray(value) || value.length)).map(([key, value]) => `${key}=${typeof value === 'string' ? value : JSON.stringify(value)}`).join(' · ') || '全部来源'}</p>
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
            <Field label="助手规则草稿 JSON"><textarea value={draftText} onChange={event => setDraftText(event.target.value)} className={textAreaClass} rows={10} /></Field>
            <div className="flex gap-2"><Button onClick={() => { try { start(undefined, parseRuleDraft(draftText)) } catch (err) { setError(errorText(err)) } }}>导入并审阅</Button><Button variant="outline" onClick={() => guarded(() => { setShowImport(false); setDraftText('') })}>取消导入</Button></div>
          </div>}
          {editing && <div className="space-y-4">
            <div className="flex flex-wrap items-center gap-2"><Button size="sm" variant="ghost" disabled={busy} onClick={() => guarded(() => { setEditing(null); setError('') })}><ArrowLeft size={14} />返回规则列表</Button><Badge variant="secondary">{editing === 'new' ? '未保存草稿' : `编辑 v${editing.version}`}</Badge></div>
            <div className="grid gap-4 sm:grid-cols-2">
              <Input label="规则名称" value={form.name} onChange={event => change({ ...form, name: event.target.value })} />
              <Input label="匹配顺序（小值优先）" type="number" min={0} value={form.position} onChange={event => change({ ...form, position: Number(event.target.value) })} />
              {matchFields.map(([key, label]) => <Input key={key} label={label} value={form.match[key] ?? ''} onChange={event => change({ ...form, match: { ...form.match, [key]: event.target.value || undefined } })} />)}
              <Field label="字段等值 JSON"><textarea rows={2} className={textAreaClass} value={fieldEquals} onChange={event => { setFieldEquals(event.target.value); setPreview(null); previewRequest.current++ }} /></Field>
              <Field label="标题模板"><textarea rows={3} className={textAreaClass} value={form.titleTemplate} onChange={event => change({ ...form, titleTemplate: event.target.value })} /></Field>
              <Field label="描述模板"><textarea rows={5} className={textAreaClass} value={form.descriptionTemplate} onChange={event => change({ ...form, descriptionTemplate: event.target.value })} /></Field>
              <div className="space-y-1.5"><span className="text-xs font-medium">优先级</span><Select value={form.priority ?? 'medium'} onValueChange={value => change({ ...form, priority: value as AutoLoopRuleCreate['priority'] })}><SelectTrigger aria-label="规则优先级"><SelectValue /></SelectTrigger><SelectContent>{(['low', 'medium', 'high'] as const).map(value => <SelectItem key={value} value={value}>{PRIORITY_LABEL[value]}</SelectItem>)}</SelectContent></Select></div>
              <Input label="指派对象" value={form.assignee ?? ''} onChange={event => change({ ...form, assignee: event.target.value })} />
              <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={form.enabled} onChange={event => change({ ...form, enabled: event.target.checked })} />保存后启用</label>
              <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={!!form.binding} onChange={event => change({ ...form, binding: event.target.checked ? { mode: 'extract' } : null })} />显式关联提取</label>
              {form.binding && <><Input label="关联提取模式" value={form.binding.pattern ?? ''} onChange={event => change({ ...form, binding: { ...form.binding!, pattern: event.target.value } })} /><Input label="关联目标 Issue" value={form.binding.targetIssue ?? ''} onChange={event => change({ ...form, binding: { ...form.binding!, targetIssue: event.target.value } })} /></>}
            </div>
            <Field label="预览样例事件 JSON"><textarea rows={4} className={textAreaClass} value={sample} onChange={event => { setSample(event.target.value); setPreview(null); previewRequest.current++ }} /></Field>
            <div className="flex flex-wrap gap-2"><Button variant="outline" loading={busy} onClick={() => void renderPreview()}>试渲染（无副作用）</Button><Button loading={busy} onClick={() => void save()}>{editing === 'new' ? '确认创建规则' : '保存规则'}</Button></div>
            {preview && <section className="space-y-3 rounded-xl border border-border p-4" aria-label="模板预览结果">
              <h3 className="text-sm font-medium">模板预览 · v{preview.ruleVersion}</h3>
              <p className="whitespace-pre-wrap break-words font-medium">{preview.renderedTitle}</p><pre className="whitespace-pre-wrap break-words text-sm">{preview.renderedDescription}</pre>
              <p className="text-xs text-muted-foreground">优先级：{preview.priority ? PRIORITY_LABEL[preview.priority] : '默认'} · 指派：{preview.assignee || '未指定'} · 绑定：{preview.bindingTarget || '未绑定'}</p>
              {preview.warnings.map((warning, index) => <Alert key={index} variant="warning">{warning}</Alert>)}
              <UntrustedOriginal value={preview.untrustedOriginal} />
            </section>}
          </div>}
        </div>}
    </DialogShell>
    <ConfirmDialog open={!!deleting} onClose={() => setDeleting(null)} onConfirm={() => deleting && void mutate(deleting)} loading={busy} title="删除规则？" description={`删除“${deleting?.name ?? ''}”后，仅后续事件停止匹配，在途事件保留原版本。`} confirmText="删除" variant="danger" />
    <ConfirmDialog open={!!exit} onClose={() => setExit(null)} onConfirm={() => { const action = exit; setExit(null); action?.run() }} title="放弃未保存的规则草稿？" description="表单中的修改尚未保存。" confirmText="放弃草稿" cancelText="继续编辑" variant="warning" />
  </>
}
