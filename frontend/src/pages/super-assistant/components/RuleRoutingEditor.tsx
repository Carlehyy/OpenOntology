import { ArrowLeft, ChevronDown, Plus, Trash2 } from 'lucide-react'
import { useState, type ReactNode, type RefObject } from 'react'
import type { AutoLoopPreview, AutoLoopPriority, AutoLoopRule, AutoLoopRuleCreate } from '@/api/autoloop'
import { Alert } from '@/components/ui/Alert'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { textAreaClass, UntrustedOriginal } from './AutoLoopDetails'
import { PRIORITY_LABEL } from './autoloopLogic'
import { activeMatchConditions, MATCH_LABEL, matchesAllEvents, type MatchKey, type RuleFieldEqualsRow, type RuleFormErrors, type RuleFormField } from './ruleRoutingForm'

const previewBoundary = '仅检查任务内容与关联提取，不创建任务。匹配顺序和目标权限在实际执行时校验。'
const rowInputClass = 'h-9 w-full min-w-0 rounded-md border bg-[var(--color-bg-elevated)] px-3 py-2 text-sm shadow-sm outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50'
/** 触屏（粗指针）把控件的可点区域抬到 44px；桌面保持 36px 密度。 */
const touchTarget = '[@media(pointer:coarse)]:min-h-11 [@media(pointer:coarse)]:min-w-11'
const labelClass = 'mb-1.5 block text-sm font-medium text-[var(--color-text-primary)]'
const summaryClass = `flex cursor-pointer list-none flex-wrap items-center gap-x-2 gap-y-0.5 rounded-md px-1 py-1.5 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring [&::-webkit-details-marker]:hidden ${touchTarget}`

/** 高级条件里的匹配键（来源身份 / 入口字段）；顺序即展示顺序。 */
const ADVANCED_KEYS: readonly MatchKey[] = ['ingestKeyId', 'sourceSystem', 'channel']

type DisclosureName = 'advanced' | 'equals' | 'binding'
/** 收进折叠区的字段：出现错误时所在折叠区自动展开，错误才可见、可聚焦。 */
const DISCLOSED_FIELDS: Partial<Record<RuleFormField, DisclosureName>> = {
  ingestKeyId: 'advanced', sourceSystem: 'advanced', channel: 'advanced',
  fieldEquals: 'equals', pattern: 'binding', targetIssue: 'binding',
}
function erroredDisclosures(errors: RuleFormErrors): DisclosureName[] {
  const names = new Set<DisclosureName>()
  for (const [field, name] of Object.entries(DISCLOSED_FIELDS) as [RuleFormField, DisclosureName][]) if (errors[field]) names.add(name)
  return [...names]
}

function Field({ id, label, error, children }: { id: string; label: string; error?: string; children: ReactNode }) {
  return <div className="min-w-0">
    <label htmlFor={id} className={labelClass}>{label}</label>
    {children}
    {error && <p id={`${id}-error`} aria-live="polite" className="mt-1 text-xs text-[var(--color-danger)]">{error}</p>}
  </div>
}

/** 原生 details 折叠区：标题 + 提示 + 状态；键盘与触屏都经 summary 展开。 */
function Disclosure({ name, title, hint, status, open, onOpenChange, children }: {
  name: DisclosureName
  title: string
  hint?: string
  status: string
  open: boolean
  onOpenChange: (open: boolean) => void
  children: ReactNode
}) {
  return <details open={open} onToggle={event => onOpenChange(event.currentTarget.open)} data-rule-routing-disclosure={name} className="group rounded-lg border border-border">
    <summary className={summaryClass}>
      <span className="font-medium">{title}</span>
      {hint && <span className="text-xs text-muted-foreground">{hint}</span>}
      <span className="ml-auto flex items-center gap-1.5 text-xs text-muted-foreground">
        {status}
        <ChevronDown size={16} className="transition-transform group-open:rotate-180" aria-hidden="true" />
      </span>
    </summary>
    <div className="space-y-4 border-t border-border p-3">{children}</div>
  </details>
}

/** 动作摘要由关联设置推导：未开启关联即新建任务，开启后向关联任务追加评论。 */
function actionSummary(form: AutoLoopRuleCreate): string {
  if (!form.binding) return '命中后创建 Multica 任务。'
  if (form.binding.targetIssue?.trim()) return '命中后向指定任务追加评论，不新建任务。'
  if (form.binding.pattern?.trim()) return '命中后从事件正文提取任务编号，验证后追加评论；提取不到时这条规则执行失败。'
  return '已开启关联提取，但还没有填写提取模式或目标任务，命中后这条规则会执行失败。'
}

/**
 * 规则编辑器（展示型）：表单状态、校验、保存/试渲染竞态与脏草稿守卫都由 RuleRoutingDialog
 * 统一持有，本组件只负责渲染与回调。结构按「何时匹配 → 命中后做什么」组织：
 * 常用条件直接展示，入口字段 / 字段等值 / 关联提取收进折叠区（出错时自动展开）。
 * 匹配顺序只读展示，调整在规则列表完成；启用同样在规则列表完成，编辑器不提供启用开关。
 */
export default function RuleRoutingEditor({
  idPrefix, editorRef, previewHeadingRef,
  form, fieldEquals, position, positionLabel, sample, sampleFromEvent, sampleOpen, onSampleOpenChange,
  editing, dirty, busy, saving, previewing,
  fieldErrors, saveError, previewError, preview,
  onBack, onFieldChange, onFieldEqualsChange, onSampleChange, onSave, onPreview,
}: {
  idPrefix: string
  editorRef: RefObject<HTMLDivElement | null>
  previewHeadingRef: RefObject<HTMLHeadingElement | null>
  form: AutoLoopRuleCreate
  fieldEquals: RuleFieldEqualsRow[]
  /** 服务端顺序值（从 0 起），只读展示。 */
  position: string
  /** 顺序值的可读说明（如「第 3 位」）；缺省时原样展示顺序值。 */
  positionLabel?: string
  sample: string
  sampleFromEvent: boolean
  sampleOpen: boolean
  onSampleOpenChange: (open: boolean) => void
  editing: AutoLoopRule | 'new'
  dirty: boolean
  busy: boolean
  saving: boolean
  previewing: boolean
  fieldErrors: RuleFormErrors
  saveError: string
  previewError: string
  preview: AutoLoopPreview | null
  onBack: () => void
  onFieldChange: (next: AutoLoopRuleCreate, field?: RuleFormField) => void
  onFieldEqualsChange: (rows: RuleFieldEqualsRow[]) => void
  onSampleChange: (text: string) => void
  onSave: () => void
  onPreview: () => void
}) {
  const isNew = editing === 'new'
  const binding = form.binding
  // 折叠开合是纯视图状态；带错误的折叠区自动展开，保证错误可见、可聚焦，之后用户仍可手动收起。
  const [open, setOpen] = useState<Record<DisclosureName, boolean>>({ advanced: false, equals: false, binding: false })
  // 样例开合由父级持有；样例出错而折叠着时，用本地标记补一次展开。
  const [sampleForced, setSampleForced] = useState(false)
  const [last, setLast] = useState<{ errors: RuleFormErrors | null; form: AutoLoopRuleCreate; rows: RuleFieldEqualsRow[]; sample: string } | null>(null)
  if (last?.errors !== fieldErrors || last?.form !== form || last?.rows !== fieldEquals || last?.sample !== sample) {
    // 父级跑校验时只替换错误对象、不动表单内容；据此区分「刚校验出错误」与「用户正在修改」，
    // 每次校验都重新展开，而用户收起后不会因为改别的字段又被弹开。
    const validated = last?.form === form && last?.rows === fieldEquals && last?.sample === sample
    setLast({ errors: fieldErrors, form, rows: fieldEquals, sample })
    if (!last || validated) revealErrors(fieldErrors)
  }
  const sampleShown = sampleOpen || sampleForced
  function revealErrors(errors: RuleFormErrors) {
    const errored = erroredDisclosures(errors)
    if (errored.length) setOpen(current => ({ ...current, ...Object.fromEntries(errored.map(name => [name, true])) }))
    if (errors.sample) setSampleForced(true)
  }
  function setDisclosure(name: DisclosureName, value: boolean) {
    setOpen(current => current[name] === value ? current : { ...current, [name]: value })
  }

  const equalsInvalid = !!fieldErrors.fieldEquals
  const equalsAria = { 'aria-invalid': equalsInvalid, 'aria-describedby': equalsInvalid ? `${idPrefix}-fieldEquals-error` : undefined }
  const equalsInputClass = `${rowInputClass} ${touchTarget} ${equalsInvalid ? 'border-[var(--color-danger)]' : 'border-[var(--color-border)]'}`
  const advancedCount = activeMatchConditions(form.match).filter(condition => (ADVANCED_KEYS as readonly string[]).includes(condition.key)).length
  const allEvents = matchesAllEvents({ ...form.match, fieldEquals })
  const enableNote = form.enabled
    ? (isNew ? '保存后立即启用' : '已启用，保存后保持启用')
    : (isNew ? '保存后不启用，需在规则列表中启用后才参与匹配' : '已停用，保存后保持停用')

  function textareaProps(field: RuleFormField) {
    return { id: `${idPrefix}-${field}`, 'aria-invalid': !!fieldErrors[field], 'aria-describedby': fieldErrors[field] ? `${idPrefix}-${field}-error` : undefined }
  }
  function fieldError(field: RuleFormField) {
    return fieldErrors[field] && <p id={`${idPrefix}-${field}-error`} aria-live="polite" className="text-xs text-[var(--color-danger)]">{fieldErrors[field]}</p>
  }
  function updateRow(index: number, patch: Partial<RuleFieldEqualsRow>) {
    onFieldEqualsChange(fieldEquals.map((row, current) => current === index ? { ...row, ...patch } : row))
  }

  return <>
    <div ref={editorRef} className="min-h-0 flex-1 space-y-6 overflow-y-auto p-4 sm:p-5">
      <div className="flex flex-wrap items-center gap-2">
        <Button size="sm" variant="ghost" className={touchTarget} onClick={onBack} data-rule-routing-back><ArrowLeft size={14} aria-hidden="true" />返回规则列表</Button>
        <h2 className="text-sm font-semibold">{isNew ? '新建规则' : '编辑规则'}</h2>
        {dirty && <Badge variant="secondary">未保存草稿</Badge>}
      </div>
      <section aria-labelledby={`${idPrefix}-basic`} className="space-y-4">
        <h3 id={`${idPrefix}-basic`} className="text-sm font-semibold">基本信息</h3>
        <Input label="规则名称" value={form.name} error={fieldErrors.name} aria-invalid={!!fieldErrors.name} className={touchTarget} onChange={event => onFieldChange({ ...form, name: event.target.value }, 'name')} />
        <dl className="grid gap-x-4 gap-y-1 text-xs leading-5 sm:grid-cols-[5.5rem_minmax(0,1fr)]">
          <dt className="text-muted-foreground">匹配位置</dt>
          <dd className="min-w-0 break-words">{positionLabel ?? `顺序值 ${position}`}。靠前的启用规则优先，顺序在规则列表中调整。{fieldError('position')}</dd>
          <dt className="text-muted-foreground">启用状态</dt>
          <dd>{enableNote}。</dd>
        </dl>
      </section>
      <section aria-labelledby={`${idPrefix}-match`} className="space-y-4">
        <h3 id={`${idPrefix}-match`} className="text-sm font-semibold">何时匹配</h3>
        <p className="text-xs leading-5 text-muted-foreground">已填写的条件须同时满足；全部留空时匹配所有路由事件。</p>
        <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
          <div className="min-w-0 space-y-1.5">
            <label htmlFor={`${idPrefix}-sourceKind`} className={labelClass}>{MATCH_LABEL.sourceKind}</label>
            <Select value={form.match.sourceKind || 'all'} onValueChange={value => onFieldChange({ ...form, match: { ...form.match, sourceKind: value === 'all' ? undefined : value } }, 'sourceKind')}>
              <SelectTrigger aria-label="规则来源类型" {...textareaProps('sourceKind')} className={touchTarget}><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="all">全部来源</SelectItem>
                <SelectItem value="notification">外部通知 (notification)</SelectItem>
                <SelectItem value="schedule">日程 (schedule)</SelectItem>
                {form.match.sourceKind && !['notification', 'schedule'].includes(form.match.sourceKind) && <SelectItem value={form.match.sourceKind}>{form.match.sourceKind}（历史值）</SelectItem>}
              </SelectContent>
            </Select>
            {fieldError('sourceKind')}
          </div>
          {(['titleContains', 'bodyContains'] as const).map(key => <Input key={key} label={MATCH_LABEL[key]} value={form.match[key] ?? ''} error={fieldErrors[key]} aria-invalid={!!fieldErrors[key]} className={touchTarget} onChange={event => onFieldChange({ ...form, match: { ...form.match, [key]: event.target.value || undefined } }, key)} />)}
        </div>
        <Disclosure name="advanced" title="高级条件" hint="来源 Key、来源系统、渠道" status={advancedCount ? `已设置 ${advancedCount} 项` : '未设置'} open={open.advanced} onOpenChange={value => setDisclosure('advanced', value)}>
          <div className="grid gap-4 sm:grid-cols-2">
            {ADVANCED_KEYS.map(key => <Input key={key} label={MATCH_LABEL[key]} value={form.match[key] ?? ''} error={fieldErrors[key]} aria-invalid={!!fieldErrors[key]} className={touchTarget} onChange={event => onFieldChange({ ...form, match: { ...form.match, [key]: event.target.value || undefined } }, key)} />)}
          </div>
          <p className="text-xs leading-5 text-muted-foreground">来源 Key 来自已验证的接入身份；来源系统名称仅作内容筛选，不代表可信身份。</p>
        </Disclosure>
        <Disclosure name="equals" title="字段等值条件" status={fieldEquals.length ? `已设置 ${fieldEquals.length} 项` : '未设置'} open={open.equals} onOpenChange={value => setDisclosure('equals', value)}>
          <div className="flex flex-wrap items-center justify-between gap-2">
            <p className="min-w-0 flex-1 basis-48 text-xs leading-5 text-muted-foreground">事件字段取值等于给定字符串时命中，field 支持点路径。</p>
            <Button size="sm" variant="outline" className={touchTarget} disabled={fieldEquals.length >= 50} onClick={() => onFieldEqualsChange([...fieldEquals, { field: '', value: '' }])}><Plus size={14} aria-hidden="true" />添加等值条件</Button>
          </div>
          {fieldEquals.length > 0 && <div className="space-y-2">
            {fieldEquals.map((row, index) => <div key={index} className="flex flex-wrap items-center gap-2">
              <input {...equalsAria} aria-label={`字段等值第 ${index + 1} 项 field`} value={row.field} placeholder="field（如 payload.severity）" onChange={event => updateRow(index, { field: event.target.value })} className={`${equalsInputClass} min-w-40 flex-[2]`} />
              <input {...equalsAria} aria-label={`字段等值第 ${index + 1} 项 value`} value={row.value} placeholder="value（字符串）" onChange={event => updateRow(index, { value: event.target.value })} className={`${equalsInputClass} min-w-40 flex-[3]`} />
              <Button size="icon-sm" variant="ghost" className={touchTarget} aria-label={`移除字段等值第 ${index + 1} 项`} onClick={() => onFieldEqualsChange(fieldEquals.filter((_, current) => current !== index))}><Trash2 size={14} aria-hidden="true" /></Button>
            </div>)}
          </div>}
          {fieldError('fieldEquals')}
          <p className="text-xs leading-5 text-muted-foreground">值按字符串精确相等比较，最多 50 项。</p>
        </Disclosure>
        {allEvents && (form.enabled
          ? <Alert variant="warning">这条规则没有匹配条件，会处理前序启用规则未匹配的所有入站事件。</Alert>
          : <Alert>没有填写任何条件时，这条规则会匹配所有路由事件，前序启用规则优先命中。</Alert>)}
      </section>
      <section aria-labelledby={`${idPrefix}-task`} className="space-y-4">
        <h3 id={`${idPrefix}-task`} className="text-sm font-semibold">命中后做什么</h3>
        <p className="text-sm leading-6">{actionSummary(form)}</p>
        <Field id={`${idPrefix}-titleTemplate`} label="任务标题" error={fieldErrors.titleTemplate}>
          <textarea {...textareaProps('titleTemplate')} className={textAreaClass} rows={2} value={form.titleTemplate} onChange={event => onFieldChange({ ...form, titleTemplate: event.target.value }, 'titleTemplate')} />
        </Field>
        <Field id={`${idPrefix}-descriptionTemplate`} label="任务描述" error={fieldErrors.descriptionTemplate}>
          <textarea {...textareaProps('descriptionTemplate')} className={textAreaClass} rows={3} value={form.descriptionTemplate} onChange={event => onFieldChange({ ...form, descriptionTemplate: event.target.value }, 'descriptionTemplate')} />
        </Field>
        <div className="grid gap-4 sm:grid-cols-2">
          <div className="min-w-0 space-y-1.5">
            <label htmlFor={`${idPrefix}-priority`} className={labelClass}>任务优先级</label>
            <Select value={form.priority || 'default'} onValueChange={value => onFieldChange({ ...form, priority: value === 'default' ? null : value as AutoLoopPriority })}>
              <SelectTrigger id={`${idPrefix}-priority`} aria-label="任务优先级" className={touchTarget}><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="default">默认</SelectItem>
                {Object.entries(PRIORITY_LABEL).map(([key, value]) => <SelectItem key={key} value={key}>{value}</SelectItem>)}
              </SelectContent>
            </Select>
          </div>
          <Input label="指派对象" value={form.assignee ?? ''} error={fieldErrors.assignee} aria-invalid={!!fieldErrors.assignee} className={touchTarget} onChange={event => onFieldChange({ ...form, assignee: event.target.value }, 'assignee')} />
        </div>
        <p className="text-xs leading-5 text-muted-foreground">任务优先级影响命中后新建任务的优先级，不影响规则匹配顺序；指派对象留空表示不指定。标题与描述可引用事件字段，如 {'{{ title }}'}、{'{{ body }}'}。描述可留空，任务描述或评论会附上原始事件。</p>
        <Disclosure name="binding" title="关联已有任务" hint="从事件中提取任务编号" status={binding ? '已开启' : '未开启'} open={open.binding} onOpenChange={value => setDisclosure('binding', value)}>
          <p className="text-xs leading-5 text-muted-foreground">开启后，命中时向关联的任务追加评论，而不是新建任务；目标任务的权限在实际执行时校验。</p>
          <div>
            <Button size="sm" variant="outline" className={touchTarget} aria-pressed={!!binding} data-rule-routing-binding-toggle onClick={() => onFieldChange({ ...form, binding: binding ? null : { mode: 'extract' } })}>{binding ? '关闭关联提取' : '开启关联提取'}</Button>
          </div>
          {binding && <>
            <div className="grid gap-4 sm:grid-cols-2">
              <Input label="关联提取模式（可选）" value={binding.pattern ?? ''} error={fieldErrors.pattern} aria-invalid={!!fieldErrors.pattern} className={touchTarget} onChange={event => onFieldChange({ ...form, binding: { ...binding, pattern: event.target.value } }, 'pattern')} />
              <Input label="关联目标 Issue（可选）" value={binding.targetIssue ?? ''} error={fieldErrors.targetIssue} aria-invalid={!!fieldErrors.targetIssue} className={touchTarget} onChange={event => onFieldChange({ ...form, binding: { ...binding, targetIssue: event.target.value } }, 'targetIssue')} />
            </div>
            <p className="text-xs leading-5 text-muted-foreground">关联提取可使用有界模式，例如 INC-[0-9]{'{1,8}'}；不支持无界重复、分支、引用或环视。</p>
          </>}
        </Disclosure>
      </section>
      <section aria-labelledby={`${idPrefix}-preview`} className="space-y-3">
        <h3 id={`${idPrefix}-preview`} className="text-sm font-semibold">预览任务内容</h3>
        <p className="text-xs leading-5 text-muted-foreground">{previewBoundary}</p>
        <details open={sampleShown} onToggle={event => { if (!event.currentTarget.open) setSampleForced(false); onSampleOpenChange(event.currentTarget.open) }}>
          <summary className={summaryClass}>样例事件 JSON{sampleFromEvent ? '（已从事件预填，请检查后再试渲染）' : '（高级）'}</summary>
          <div className="mt-2 space-y-1.5">
            <textarea {...textareaProps('sample')} className={textAreaClass} rows={4} value={sample} onChange={event => onSampleChange(event.target.value)} />
            {fieldError('sample')}
            <p className="text-xs text-muted-foreground">样例事件 JSON 仅供本次试渲染使用，不会被保存。</p>
          </div>
        </details>
        {previewError && <Alert variant="danger" role="alert">{previewError}</Alert>}
        {preview && <section className="space-y-3 rounded-xl border border-border p-4" aria-label="模板预览结果">
          <h4 ref={previewHeadingRef} tabIndex={-1} className="text-sm font-medium outline-none focus-visible:ring-2 focus-visible:ring-ring">{preview.ruleVersion === 0 ? '模板预览结果（未保存草稿）' : `模板预览结果（已存规则 v${preview.ruleVersion}）`}</h4>
          <p className="text-xs leading-5 text-muted-foreground">{previewBoundary}</p>
          <p className="whitespace-pre-wrap break-words font-medium">{preview.renderedTitle}</p><pre className="whitespace-pre-wrap break-words text-sm">{preview.renderedDescription}</pre>
          <p className="text-xs text-muted-foreground">任务优先级：{preview.priority ? PRIORITY_LABEL[preview.priority] : '默认'} · 指派：{preview.assignee || '未指定'} · 绑定：{preview.bindingTarget || '未绑定'}</p>
          {preview.warnings.map((warning, index) => <Alert key={index} variant="warning">{warning}</Alert>)}
          <UntrustedOriginal value={preview.untrustedOriginal} />
        </section>}
      </section>
    </div>
    <div className="shrink-0 space-y-2 border-t border-border bg-background px-4 py-3 sm:px-5">
      {saveError && <Alert variant="danger" role="alert">{saveError}</Alert>}
      <div role="group" aria-label="规则操作" className="flex flex-wrap items-center justify-end gap-2">
        <span className="sr-only" role="status">{previewing ? '正在试渲染模板…' : preview ? '模板预览已更新' : saving ? '正在保存规则…' : ''}</span>
        <Button variant="outline" className={touchTarget} loading={previewing} disabled={busy} onClick={onPreview} data-rule-routing-preview>试渲染模板（不创建任务）</Button>
        <Button className={touchTarget} loading={saving} disabled={busy} onClick={onSave} data-rule-routing-save>保存规则</Button>
      </div>
    </div>
  </>
}
