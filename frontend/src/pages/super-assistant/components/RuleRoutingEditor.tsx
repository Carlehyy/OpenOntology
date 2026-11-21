import { ArrowLeft, Plus, Trash2 } from 'lucide-react'
import type { RefObject } from 'react'
import type { AutoLoopPreview, AutoLoopRule, AutoLoopRuleCreate } from '@/api/autoloop'
import { Alert } from '@/components/ui/Alert'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { textAreaClass, UntrustedOriginal } from './AutoLoopDetails'
import { PRIORITY_LABEL } from './autoloopLogic'
import { matchFields } from './RulesList'
import type { RuleFieldEqualsRow, RuleFormErrors, RuleFormField } from './ruleRoutingForm'

const previewBoundary = '仅渲染模板与关联提取，不校验匹配条件与规则顺序，也不校验 Issue 权限，不创建任务。'
const rowInputClass = 'h-9 w-full min-w-0 rounded-md border bg-[var(--color-bg-elevated)] px-3 py-2 text-sm shadow-sm outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50'

function Field({ label, htmlFor, children }: { label: string; htmlFor: string; children: React.ReactNode }) {
  return <div className="min-w-0 space-y-1.5 text-xs font-medium"><label htmlFor={htmlFor}>{label}</label>{children}</div>
}

/**
 * 规则编辑器（展示型）：表单状态、校验、保存/试渲染竞态与脏草稿守卫都由 RuleRoutingDialog
 * 统一持有，本组件只负责渲染与回调。字段等值为结构化行输入（后端仍收到
 * {field,value} 数组，字符串语义不变）；样例事件 JSON 收纳为高级入口。
 */
export default function RuleRoutingEditor({
  idPrefix, editorRef, previewHeadingRef,
  form, fieldEquals, position, sample, sampleFromEvent, sampleOpen, onSampleOpenChange,
  editing, dirty, busy, saving, previewing,
  fieldErrors, saveError, previewError, preview,
  onBack, onFieldChange, onPositionChange, onFieldEqualsChange, onSampleChange, onSave, onPreview,
}: {
  idPrefix: string
  editorRef: RefObject<HTMLDivElement | null>
  previewHeadingRef: RefObject<HTMLHeadingElement | null>
  form: AutoLoopRuleCreate
  fieldEquals: RuleFieldEqualsRow[]
  position: string
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
  onPositionChange: (value: string) => void
  onFieldEqualsChange: (rows: RuleFieldEqualsRow[]) => void
  onSampleChange: (text: string) => void
  onSave: () => void
  onPreview: () => void
}) {
  const equalsInvalid = !!fieldErrors.fieldEquals
  const equalsAria = { 'aria-invalid': equalsInvalid, 'aria-describedby': equalsInvalid ? `${idPrefix}-fieldEquals-error` : undefined }
  const equalsInputClass = `${rowInputClass} ${equalsInvalid ? 'border-[var(--color-danger)]' : 'border-[var(--color-border)]'}`
  const emptyMatch = !Object.entries(form.match).some(([key, value]) => key !== 'fieldEquals' && !!value) && fieldEquals.length === 0

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
      <div className="flex flex-wrap items-center gap-2"><Button size="sm" variant="ghost" onClick={onBack} data-rule-routing-back><ArrowLeft size={14} />返回规则列表</Button>{dirty && <Badge variant="secondary">未保存草稿</Badge>}</div>
      <section aria-labelledby={`${idPrefix}-basic`} className="space-y-3">
        <h3 id={`${idPrefix}-basic`} className="text-sm font-semibold">基本设置</h3>
        <Input label="规则名称" value={form.name} error={fieldErrors.name} aria-invalid={!!fieldErrors.name} onChange={event => onFieldChange({ ...form, name: event.target.value }, 'name')} />
        <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={form.enabled} onChange={event => onFieldChange({ ...form, enabled: event.target.checked })} />保存后启用</label>
        {form.enabled && emptyMatch && <Alert variant="warning">空条件会匹配当前用户的全部路由事件；前序启用规则优先命中。</Alert>}
      </section>
      <section aria-labelledby={`${idPrefix}-match`} className="space-y-3">
        <h3 id={`${idPrefix}-match`} className="text-sm font-semibold">事件匹配</h3>
        <p className="text-xs leading-5 text-muted-foreground">已填写条件须同时满足。匹配顺序的小值优先，仅启用规则参与匹配。</p>
        <div className="grid gap-4 sm:grid-cols-2">
          <Input label="匹配顺序" type="number" min="0" step="1" value={position} error={fieldErrors.position} aria-invalid={!!fieldErrors.position} onChange={event => onPositionChange(event.target.value)} />
          <div className="space-y-1.5"><span className="text-xs font-medium">来源类型</span><Select value={form.match.sourceKind || 'all'} onValueChange={value => onFieldChange({ ...form, match: { ...form.match, sourceKind: value === 'all' ? undefined : value } }, 'sourceKind')}><SelectTrigger aria-label="规则来源类型" {...textareaProps('sourceKind')}><SelectValue /></SelectTrigger><SelectContent><SelectItem value="all">全部来源</SelectItem><SelectItem value="notification">外部通知 (notification)</SelectItem><SelectItem value="schedule">日程 (schedule)</SelectItem>{form.match.sourceKind && !['notification', 'schedule'].includes(form.match.sourceKind) && <SelectItem value={form.match.sourceKind}>{form.match.sourceKind}（历史值）</SelectItem>}</SelectContent></Select>{fieldError('sourceKind')}</div>
          {matchFields.map(([key, label]) => <Input key={key} label={label} value={form.match[key] ?? ''} error={fieldErrors[key]} aria-invalid={!!fieldErrors[key]} onChange={event => onFieldChange({ ...form, match: { ...form.match, [key]: event.target.value || undefined } }, key)} />)}
        </div>
        <p className="text-xs leading-5 text-muted-foreground">来源 Key 来自已验证的接入身份；来源系统只是事件内容的弱过滤，不代表可信身份。包含条件区分大小写。</p>
        <div className="space-y-2">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span className="text-xs font-medium">字段等值条件</span>
            <Button size="sm" variant="ghost" disabled={fieldEquals.length >= 50} onClick={() => onFieldEqualsChange([...fieldEquals, { field: '', value: '' }])}><Plus size={14} />添加等值条件</Button>
          </div>
          {fieldEquals.length === 0 && <p className="text-xs text-muted-foreground">未添加。事件字段取值等于给定字符串时命中，field 支持点路径。</p>}
          {fieldEquals.map((row, index) => <div key={index} className="flex flex-wrap items-center gap-2">
            <input {...equalsAria} aria-label={`字段等值第 ${index + 1} 项 field`} value={row.field} placeholder="field（如 payload.severity）" onChange={event => updateRow(index, { field: event.target.value })} className={`${equalsInputClass} min-w-40 flex-[2]`} />
            <input {...equalsAria} aria-label={`字段等值第 ${index + 1} 项 value`} value={row.value} placeholder="value（字符串）" onChange={event => updateRow(index, { value: event.target.value })} className={`${equalsInputClass} min-w-40 flex-[3]`} />
            <Button size="icon-sm" variant="ghost" aria-label={`移除字段等值第 ${index + 1} 项`} onClick={() => onFieldEqualsChange(fieldEquals.filter((_, current) => current !== index))}><Trash2 size={14} /></Button>
          </div>)}
          {fieldError('fieldEquals')}
          <p className="text-xs text-muted-foreground">值按字符串精确相等比较；最多 50 项。导入助手草稿中的等值数组同样以行编辑呈现。</p>
        </div>
      </section>
      <section aria-labelledby={`${idPrefix}-task`} className="space-y-3">
        <h3 id={`${idPrefix}-task`} className="text-sm font-semibold">任务内容与关联</h3>
        <div className="grid gap-4 sm:grid-cols-2">
          <div className="space-y-1.5"><span className="text-xs font-medium">任务优先级</span><Select value={form.priority || 'default'} onValueChange={value => onFieldChange({ ...form, priority: value === 'default' ? null : value as 'low' | 'medium' | 'high' })}><SelectTrigger aria-label="任务优先级"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="default">默认</SelectItem>{Object.entries(PRIORITY_LABEL).map(([key, value]) => <SelectItem key={key} value={key}>{value}</SelectItem>)}</SelectContent></Select></div>
          <Input label="指派对象" value={form.assignee ?? ''} error={fieldErrors.assignee} aria-invalid={!!fieldErrors.assignee} onChange={event => onFieldChange({ ...form, assignee: event.target.value }, 'assignee')} />
        </div>
        <p className="text-xs leading-5 text-muted-foreground">任务优先级决定命中后新建任务的优先级，不影响规则匹配顺序。指派对象留空表示不指定。</p>
        <Field label="标题模板" htmlFor={`${idPrefix}-titleTemplate`}><textarea {...textareaProps('titleTemplate')} className={textAreaClass} rows={2} value={form.titleTemplate} onChange={event => onFieldChange({ ...form, titleTemplate: event.target.value }, 'titleTemplate')} />{fieldError('titleTemplate')}</Field>
        <Field label="描述模板" htmlFor={`${idPrefix}-descriptionTemplate`}><textarea {...textareaProps('descriptionTemplate')} className={textAreaClass} rows={3} value={form.descriptionTemplate} onChange={event => onFieldChange({ ...form, descriptionTemplate: event.target.value }, 'descriptionTemplate')} />{fieldError('descriptionTemplate')}</Field>
        <p className="text-xs text-muted-foreground">描述可留空。命中后，任务描述或评论会附上原始事件。</p>
        <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={!!form.binding} onChange={event => onFieldChange({ ...form, binding: event.target.checked ? { mode: 'extract' } : null })} />显式关联提取</label>
        {form.binding && <div className="space-y-3 rounded-lg border border-border p-3"><div className="grid gap-4 sm:grid-cols-2"><Input label="关联提取模式（可选）" value={form.binding.pattern ?? ''} error={fieldErrors.pattern} aria-invalid={!!fieldErrors.pattern} onChange={event => onFieldChange({ ...form, binding: { ...form.binding!, pattern: event.target.value } }, 'pattern')} /><Input label="关联目标 Issue（可选）" value={form.binding.targetIssue ?? ''} error={fieldErrors.targetIssue} aria-invalid={!!fieldErrors.targetIssue} onChange={event => onFieldChange({ ...form, binding: { ...form.binding!, targetIssue: event.target.value } }, 'targetIssue')} /></div><p className="text-xs text-muted-foreground">关联提取可使用有界模式，例如 INC-[0-9]{'{1,8}'}。不支持无界重复、分支、引用或环视；填写目标 Issue 仍须在执行时校验权限。</p></div>}
      </section>
      <section aria-labelledby={`${idPrefix}-preview`} className="space-y-3">
        <h3 id={`${idPrefix}-preview`} className="text-sm font-semibold">预览</h3>
        <p className="text-xs leading-5 text-muted-foreground">{previewBoundary}样例事件 JSON 仅供本次试渲染使用。</p>
        <details open={sampleOpen} onToggle={event => onSampleOpenChange(event.currentTarget.open)}>
          <summary className="cursor-pointer text-xs font-medium outline-none focus-visible:ring-2 focus-visible:ring-ring">样例事件 JSON{sampleFromEvent ? '（已从事件预填，请检查后再试渲染）' : '（高级）'}</summary>
          <div className="mt-2 space-y-1.5">
            <textarea {...textareaProps('sample')} className={textAreaClass} rows={4} value={sample} onChange={event => onSampleChange(event.target.value)} />
            {fieldError('sample')}
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
      <div className="flex flex-wrap items-center justify-end gap-2" aria-label="规则操作"><span className="sr-only" role="status">{previewing ? '正在试渲染模板…' : preview ? '模板预览已更新' : saving ? '正在保存规则…' : ''}</span><Button variant="outline" loading={previewing} disabled={busy} onClick={onPreview} data-rule-routing-preview>试渲染模板（不创建任务）</Button><Button loading={saving} disabled={busy} onClick={onSave} data-rule-routing-save>{editing === 'new' ? '确认创建规则' : '保存规则'}</Button></div>
    </div>
  </>
}
