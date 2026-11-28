import { useEffect, useId, useRef, useState } from 'react'
import { ArrowLeft, FlaskConical, RotateCcw } from 'lucide-react'
import { toast } from 'sonner'
import { autoloopApi, type AutoLoopRoutingPolicy, type AutoLoopRoutingPolicyPreview, type AutoLoopRoutingPolicyPreviewInput } from '@/api/autoloop'
import { Alert } from '@/components/ui/Alert'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { textAreaClass } from './AutoLoopDetails'

const PROMPT_LIMIT = 8_000
const SAMPLE_LIMIT = 32_768
const SAMPLE = JSON.stringify({
  title: '库存核对需要确认',
  body: '两个来源的库存数不一致。请人工选择以盘点数为准，或暂缓发布。',
}, null, 2)

export function routingPromptLabel(version: number, promptText: string | null) {
  if (promptText != null) return `自定义 v${version}`
  return version ? `平台默认（v${version}）` : '平台默认'
}

function conflictPolicy(error: unknown) {
  const detail = (error as { detail?: { code?: string; current?: AutoLoopRoutingPolicy } } | undefined)?.detail
  return detail?.code === 'routing_policy_version_conflict' ? detail.current : undefined
}

function previewErrorMessage(error: unknown) {
  const response = error as { status?: number; detail?: { reason?: string } } | undefined
  const reason = response?.detail?.reason
  if (reason === 'daily_limit_reached') return '今日模型研判额度已用尽，请联系管理员。'
  if (response?.status === 429) return '模型调用过于频繁，请稍后重试。'
  if (response?.status === 504 || reason === 'model_timeout') return '模型研判超时，请重试。'
  if (reason === 'model_unavailable' || reason === 'explicit_model_unavailable') return '当前没有可用的研判模型，请联系管理员。'
  if (response?.status === 503) return '模型调用失败，请稍后重试。'
  if (response?.status === 502) return '模型未返回有效研判结果，请重试。'
  if (response?.status === 413 || response?.status === 422) return '提示词或样例不符合试运行要求，请检查格式和大小。'
  return '试运行失败，请稍后重试。'
}

export default function RoutingPolicyEditor({ policy, onPolicyChange, onDirtyChange, onSavingChange, onBack, onSaved }: {
  policy: AutoLoopRoutingPolicy
  onPolicyChange: (policy: AutoLoopRoutingPolicy) => void
  onDirtyChange: (dirty: boolean) => void
  onSavingChange: (saving: boolean) => void
  onBack: () => void
  onSaved: () => void
}) {
  const id = useId()
  const [expectedVersion, setExpectedVersion] = useState(policy.version)
  const [text, setText] = useState(policy.promptText ?? '')
  const [sample, setSample] = useState(SAMPLE)
  const [eventType, setEventType] = useState<AutoLoopRoutingPolicyPreviewInput['eventType']>('notification')
  const [result, setResult] = useState<AutoLoopRoutingPolicyPreview | null>(null)
  const [sampleError, setSampleError] = useState('')
  const [previewError, setPreviewError] = useState('')
  const [saveError, setSaveError] = useState('')
  const [saving, setSaving] = useState(false)
  const [previewing, setPreviewing] = useState(false)
  const [restoreOpen, setRestoreOpen] = useState(false)
  const promptRef = useRef<HTMLTextAreaElement>(null)
  const sampleRef = useRef<HTMLTextAreaElement>(null)
  const previewRequest = useRef<AbortController | null>(null)
  const busy = useRef(false)
  const promptText = text.trim() || null
  const dirty = promptText !== policy.promptText
  const conflict = expectedVersion !== policy.version

  useEffect(() => { promptRef.current?.focus() }, [])
  useEffect(() => { onDirtyChange(dirty) }, [dirty, onDirtyChange])
  useEffect(() => () => { previewRequest.current?.abort() }, [])

  function invalidatePreview() {
    previewRequest.current?.abort()
    previewRequest.current = null
    setPreviewing(false)
    setSampleError('')
    setPreviewError('')
    setResult(null)
  }

  function applyConflict(error: unknown) {
    const current = conflictPolicy(error)
    if (!current) return false
    onPolicyChange(current)
    return true
  }

  async function preview() {
    if (previewRequest.current || busy.current || conflict) return
    setSampleError(''); setPreviewError(''); setResult(null)
    let sampleEvent: Record<string, unknown>
    try {
      const value: unknown = JSON.parse(sample)
      if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('样例必须是 JSON 对象。')
      if (new TextEncoder().encode(JSON.stringify(value)).length > SAMPLE_LIMIT) throw new Error('样例不能超过 32 KB。')
      sampleEvent = value as Record<string, unknown>
    } catch (error) {
      setSampleError(error instanceof SyntaxError ? '请输入有效的 JSON 样例。' : (error as Error).message)
      sampleRef.current?.focus()
      return
    }
    const controller = new AbortController()
    previewRequest.current = controller
    setPreviewing(true)
    try {
      const value = await autoloopApi.previewRoutingPolicy({ expectedVersion, promptText, eventType, sampleEvent }, controller.signal)
      if (!controller.signal.aborted) setResult(value)
    } catch (error) {
      if (!controller.signal.aborted && !applyConflict(error)) setPreviewError(previewErrorMessage(error))
    } finally {
      if (previewRequest.current === controller) {
        previewRequest.current = null
        setPreviewing(false)
      }
    }
  }

  async function save(value: string | null) {
    if (busy.current || conflict) return
    busy.current = true
    invalidatePreview()
    setSaving(true); onSavingChange(true); setSaveError('')
    try {
      const current = await autoloopApi.updateRoutingPolicy({ expectedVersion, promptText: value })
      onPolicyChange(current)
      onDirtyChange(false)
      toast.success(value == null ? '已恢复平台默认提示词' : '研判提示词已更新', { description: '应用于此后开始的研判。' })
      onSaved()
    } catch (error) {
      if (!applyConflict(error)) setSaveError('保存失败，输入已保留。请重试。')
    } finally {
      busy.current = false
      setRestoreOpen(false); setSaving(false); onSavingChange(false)
    }
  }

  return <>
    <div className="min-h-0 flex-1 space-y-5 overflow-y-auto p-4 sm:p-5">
      <div className="flex flex-wrap items-center gap-2">
        <Button type="button" variant="ghost" className="max-md:h-11" disabled={saving} onClick={onBack}><ArrowLeft size={16} aria-hidden="true" />返回研判</Button>
        {dirty && <Badge variant="secondary">未保存更改</Badge>}
      </div>
      <div className="space-y-2">
        <h2 className="text-base font-semibold">编辑研判提示词</h2>
        <Alert>{policy.policySummary}</Alert>
      </div>
      {conflict && <Alert variant="warning" role="alert">
        <div className="space-y-2">
          <p>生效版本已变为{routingPromptLabel(policy.version, policy.promptText)}。你的输入未被覆盖。</p>
          <div className="flex flex-wrap gap-2">
            <Button type="button" variant="outline" size="sm" onClick={() => { setExpectedVersion(policy.version); setText(policy.promptText ?? ''); invalidatePreview() }}>载入生效文本</Button>
            <Button type="button" variant="outline" size="sm" onClick={() => { setExpectedVersion(policy.version); invalidatePreview() }}>保留草稿继续编辑</Button>
          </div>
        </div>
      </Alert>}
      <div className="grid min-w-0 gap-5 lg:grid-cols-2">
        <section className="min-w-0 space-y-2">
          <label htmlFor={`${id}-prompt`} className="block text-sm font-medium">我的研判要求</label>
          <textarea ref={promptRef} id={`${id}-prompt`} className={`${textAreaClass} font-mono`} rows={8} maxLength={PROMPT_LIMIT} value={text} disabled={saving} onChange={event => { setText(event.target.value); setSaveError(''); invalidatePreview() }} placeholder="例如：事件明确要求人工确认时，给出待决定的问题和选项。" />
          <p className="flex flex-wrap justify-between gap-2 text-xs text-muted-foreground"><span>{routingPromptLabel(policy.version, policy.promptText)}</span><span className="tabular-nums">{text.length} / {PROMPT_LIMIT}</span></p>
        </section>
        <section className="min-w-0 space-y-2">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <label htmlFor={`${id}-sample`} className="text-sm font-medium">脱敏样例 JSON</label>
            <Select value={eventType} onValueChange={(type: AutoLoopRoutingPolicyPreviewInput['eventType']) => { setEventType(type); invalidatePreview() }} disabled={saving}>
              <SelectTrigger aria-label="样例来源" className="w-32 max-md:h-11"><SelectValue /></SelectTrigger>
              <SelectContent><SelectItem value="notification">外部通知</SelectItem><SelectItem value="schedule">定时任务</SelectItem></SelectContent>
            </Select>
          </div>
          <textarea ref={sampleRef} id={`${id}-sample`} className={`${textAreaClass} font-mono`} rows={7} value={sample} maxLength={SAMPLE_LIMIT} aria-invalid={!!sampleError} aria-describedby={sampleError ? `${id}-sample-error` : undefined} disabled={saving} onChange={event => { setSample(event.target.value); invalidatePreview() }} />
          {sampleError && <Alert id={`${id}-sample-error`} variant="danger" role="alert">{sampleError}</Alert>}
          {previewError && <Alert variant="danger" role="alert">{previewError}</Alert>}
          {result && <div className="space-y-2 rounded-lg border border-border p-3" role="status" aria-label="试运行结果">
            <Badge variant={result.action === 'create_decision' ? 'warning' : 'secondary'}>{result.action === 'create_decision' ? '建议转人工' : '建议忽略'}</Badge>
            <p className="whitespace-pre-wrap break-words text-sm">{result.reason}</p>
            <p className="break-words text-xs text-muted-foreground">{result.isDraft ? '未保存草稿，基于' : '生效版本：'}v{result.routingPromptVersion}；模型：{result.modelConfigId}</p>
          </div>}
        </section>
      </div>
    </div>
    <div className="shrink-0 space-y-2 border-t border-border bg-popover px-4 pt-3 pb-[max(.75rem,env(safe-area-inset-bottom))] sm:px-5">
      {saveError && <Alert variant="danger" role="alert">{saveError}</Alert>}
      <div className="flex flex-wrap items-center justify-end gap-2">
        {policy.promptText != null && <Button type="button" variant="ghost" className="mr-auto max-md:h-11" disabled={saving || conflict} onClick={() => setRestoreOpen(true)}><RotateCcw size={16} aria-hidden="true" />恢复默认</Button>}
        <Button type="button" variant="ghost" className="max-md:h-11" disabled={saving} onClick={onBack}>取消</Button>
        <Button type="button" variant="outline" className="max-md:h-11" disabled={saving || conflict} loading={previewing} onClick={() => void preview()}><FlaskConical size={16} aria-hidden="true" />试运行</Button>
        <Button type="button" className="max-md:h-11" disabled={!dirty || !promptText || conflict} loading={saving} onClick={() => void save(promptText)}>保存并启用</Button>
      </div>
    </div>
    <ConfirmDialog layer="nested" open={restoreOpen} onClose={() => { if (!busy.current) setRestoreOpen(false) }} onConfirm={() => void save(null)} loading={saving} title="恢复平台默认提示词？" description="自定义要求将不再用于此后开始的研判。已开始的研判和历史记录不变。" confirmText="恢复默认" variant="warning" />
  </>
}
