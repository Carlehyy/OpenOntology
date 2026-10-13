import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  ArrowLeft, CheckCircle2, Clock, Loader2, Pause, Play, Plus, Trash2, XCircle,
} from 'lucide-react'

import {
  superAssistantApi,
  type ScheduledKind,
  type ScheduledRun,
  type ScheduledRunStatus,
  type ScheduledTask,
} from '@/api/superAssistant'
import { Alert } from '@/components/ui/Alert'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { LoadingState } from '@/components/ui/LoadingState'
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from '@/components/ui/select'
import { formatDateTime } from '@/utils/datetime'
import { toast } from 'sonner'

import { DialogShell } from './AssistantConfiguration'
import { errorText } from './assistantPanelUtils'
import {
  STATUS_LABEL, WEEKDAYS, freshCreateForm, isCreateFormDirty, scheduleLabel,
  shanghaiDatetimeLocal, shanghaiIsoFromLocal, validateCreateForm,
  type CreateFormState, type CreateFormValidation,
} from './scheduledTaskLogic'

export default function ScheduledTasksDialog({
  open,
  initialTaskId,
  initialRunId,
  onClose,
  onOpenConversation,
}: {
  open: boolean
  initialTaskId?: string | null
  initialRunId?: string | null
  onClose: () => void
  onOpenConversation: (conversationId: string) => void
}) {
  const [tasks, setTasks] = useState<ScheduledTask[]>([])
  const [runs, setRuns] = useState<ScheduledRun[]>([])
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [creating, setCreating] = useState(false)
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(initialTaskId ?? null)
  const [selectedRunId, setSelectedRunId] = useState<string | null>(initialRunId ?? null)
  const [runDetail, setRunDetail] = useState<ScheduledRun | null>(null)
  const [deleting, setDeleting] = useState<ScheduledTask | null>(null)
  const [deletePending, setDeletePending] = useState(false)
  const [discardOpen, setDiscardOpen] = useState(false)
  const [formBaseline, setFormBaseline] = useState<CreateFormState>(() => freshCreateForm())
  const [validation, setValidation] = useState<CreateFormValidation>({})

  const [title, setTitle] = useState('')
  const [instruction, setInstruction] = useState('')
  const [kind, setKind] = useState<ScheduledKind>('once')
  const [onceAt, setOnceAt] = useState(() => freshCreateForm().onceAt)
  const [timeOfDay, setTimeOfDay] = useState('09:00')
  const [weekday, setWeekday] = useState('0')

  const selectedTask = useMemo(
    () => tasks.find(item => item.id === selectedTaskId) || null,
    [tasks, selectedTaskId],
  )

  const resetForm = () => {
    const fresh = freshCreateForm()
    setTitle(fresh.title)
    setInstruction(fresh.instruction)
    setKind(fresh.kind)
    setOnceAt(fresh.onceAt)
    setTimeOfDay(fresh.timeOfDay)
    setWeekday(fresh.weekday)
    setFormBaseline(fresh)
    setValidation({})
    setError('')
  }

  const refreshTasks = useCallback(async () => {
    const data = await superAssistantApi.listScheduledTasks()
    setTasks(data)
    return data
  }, [])

  useEffect(() => {
    if (!open) return
    let alive = true
    setLoading(true)
    setError('')
    void refreshTasks()
      .then(data => {
        if (!alive) return
        if (initialTaskId && data.some(item => item.id === initialTaskId)) {
          setSelectedTaskId(initialTaskId)
          setCreating(false)
        }
        if (initialRunId) setSelectedRunId(initialRunId)
      })
      .catch(err => { if (alive) setError(errorText(err, '定时任务加载失败')) })
      .finally(() => { if (alive) setLoading(false) })
    return () => { alive = false }
  }, [open, initialTaskId, initialRunId, refreshTasks])

  useEffect(() => {
    if (!open || !selectedTaskId || creating) { setRuns([]); return }
    let alive = true
    void superAssistantApi.listScheduledRuns(selectedTaskId)
      .then(data => { if (alive) setRuns(data) })
      .catch(() => { if (alive) setRuns([]) })
    return () => { alive = false }
  }, [open, selectedTaskId, creating])

  useEffect(() => {
    if (!open || !selectedTaskId || !selectedRunId) { setRunDetail(null); return }
    let alive = true
    void superAssistantApi.getScheduledRun(selectedTaskId, selectedRunId)
      .then(data => { if (alive) setRunDetail(data) })
      .catch(err => { if (alive) setError(errorText(err, '执行记录加载失败')) })
    return () => { alive = false }
  }, [open, selectedTaskId, selectedRunId])

  useEffect(() => {
    if (!open) return
    let alive = true
    const timer = window.setInterval(() => {
      void refreshTasks().catch(() => undefined)
      if (selectedTaskId && !creating) {
        void superAssistantApi.listScheduledRuns(selectedTaskId)
          .then(data => { if (alive) setRuns(data) })
          .catch(() => undefined)
      }
      if (selectedTaskId && selectedRunId) {
        void superAssistantApi.getScheduledRun(selectedTaskId, selectedRunId)
          .then(data => { if (alive) setRunDetail(data) })
          .catch(() => undefined)
      }
    }, 8000)
    return () => { alive = false; window.clearInterval(timer) }
  }, [open, selectedTaskId, selectedRunId, creating, refreshTasks])

  if (!open) return null

  const payloadFromForm = () => {
    const [hour, minute] = timeOfDay.split(':').map(part => Number(part))
    return {
      title: title.trim(),
      instruction: instruction.trim(),
      schedule_kind: kind,
      run_at: kind === 'once' ? shanghaiIsoFromLocal(onceAt) : null,
      hour: kind === 'once' ? null : hour,
      minute: kind === 'once' ? null : minute,
      weekday: kind === 'weekly' ? Number(weekday) : null,
      enabled: true,
    }
  }

  const create = async () => {
    const errors = validateCreateForm({ title, instruction, kind, onceAt, timeOfDay, weekday })
    setValidation(errors)
    if (errors.instruction || errors.onceAt) return
    setSaving(true); setError('')
    try {
      const created = await superAssistantApi.createScheduledTask(payloadFromForm())
      await refreshTasks()
      setCreating(false)
      setSelectedTaskId(created.id)
      setSelectedRunId(null)
      resetForm()
      toast.success('定时任务已创建', { description: '到点后会在后台执行，结果会出现在本弹窗的执行记录里。' })
    } catch (err) {
      setError(errorText(err, '创建失败'))
    } finally {
      setSaving(false)
    }
  }

  const toggleEnabled = async (task: ScheduledTask) => {
    try {
      const updated = await superAssistantApi.updateScheduledTask(task.id, { enabled: !task.enabled })
      setTasks(current => current.map(item => item.id === updated.id ? updated : item))
    } catch (err) {
      toast.error(errorText(err, '更新失败'))
    }
  }

  const confirmDelete = async () => {
    if (!deleting || deletePending) return
    setDeletePending(true)
    try {
      await superAssistantApi.deleteScheduledTask(deleting.id)
      setTasks(current => current.filter(item => item.id !== deleting.id))
      if (selectedTaskId === deleting.id) {
        setSelectedTaskId(null)
        setSelectedRunId(null)
        setRunDetail(null)
      }
      setDeleting(null)
      toast.success('定时任务已删除')
    } catch (err) {
      toast.error(errorText(err, '删除失败'))
    } finally {
      setDeletePending(false)
    }
  }

  const openCreate = () => {
    resetForm()
    setCreating(true)
    setSelectedRunId(null)
    setRunDetail(null)
  }

  /** P0-2（UX 评审）：脏表单下 Esc/遮罩/关闭符统一走二次确认，防误触丢失已填内容 */
  const requestClose = () => {
    if (saving) return
    if (creating && isCreateFormDirty({ title, instruction, kind, onceAt, timeOfDay, weekday }, formBaseline)) {
      setDiscardOpen(true)
      return
    }
    onClose()
  }

  const discardCreate = () => {
    setDiscardOpen(false)
    resetForm()
    setCreating(false)
    onClose()
  }

  /** P0-4：错误锚定字段后，输入即清除对应错误，避免红色提示挂到下次提交 */
  const changeInstruction = (value: string) => {
    setInstruction(value)
    if (validation.instruction) setValidation(current => ({ ...current, instruction: undefined }))
  }
  const changeOnceAt = (value: string) => {
    setOnceAt(value)
    if (validation.onceAt) setValidation(current => ({ ...current, onceAt: undefined }))
  }
  const changeKind = (value: ScheduledKind) => {
    setKind(value)
    setValidation({})
  }

  return (
    <>
      <DialogShell
        size="wide"
        title="定时任务"
        description="到点后自动执行。需要确认的操作会跳过并记录，不会代替你确认。"
        onClose={requestClose}
        icon={<Clock size={18} />}
        contentClassName="h-[min(82dvh,44rem)]"
      >
        <div className="flex min-h-0 flex-1" data-testid="scheduled-tasks-dialog">
          <nav aria-label="定时任务列表" className="flex w-56 shrink-0 flex-col border-r border-[var(--color-border)]">
            <div className="p-2">
              <button
                type="button"
                onClick={openCreate}
                className="flex h-9 w-full items-center justify-center gap-1.5 rounded-lg bg-brand text-xs font-medium text-white transition-colors hover:bg-brand-deep focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                <Plus size={14} /> 创建定时任务
              </button>
            </div>
            <div className="min-h-0 flex-1 overflow-y-auto px-2 pb-2">
              {loading && <LoadingState className="py-6" message="加载中…" />}
              {!loading && tasks.length === 0 && (
                <p className="px-2 py-6 text-center text-xs leading-5 text-[var(--color-text-tertiary)]">
                  还没有定时任务
                </p>
              )}
              {tasks.map(task => {
                const current = !creating && task.id === selectedTaskId
                return (
                  <button
                    key={task.id}
                    type="button"
                    onClick={() => { setCreating(false); setSelectedTaskId(task.id); setSelectedRunId(null); setRunDetail(null) }}
                    className={`mb-1 w-full rounded-lg px-2.5 py-2 text-left transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${
                      current ? 'bg-brand-soft' : 'hover:bg-[var(--color-bg-hover)]'
                    }`}
                  >
                    <div className={`truncate text-xs ${current ? 'font-medium text-brand-ink' : 'text-[var(--color-text-primary)]'}`}>
                      {task.title}
                    </div>
                    <div className="mt-0.5 truncate text-xs text-[var(--color-text-tertiary)]">
                      {task.enabled ? scheduleLabel(task) : '已暂停'}
                      {task.last_run_status ? ` · ${STATUS_LABEL[task.last_run_status]}` : ''}
                    </div>
                  </button>
                )
              })}
            </div>
          </nav>

          <div className="flex min-h-0 min-w-0 flex-1 flex-col">
            {error && (
              <div className="shrink-0 border-b border-[var(--color-border)] px-5 py-3">
                <Alert variant="danger" role="alert">{error}</Alert>
              </div>
            )}
            {creating ? (
              <CreateForm
                title={title} setTitle={setTitle}
                instruction={instruction} setInstruction={changeInstruction}
                kind={kind} setKind={changeKind}
                onceAt={onceAt} setOnceAt={changeOnceAt}
                timeOfDay={timeOfDay} setTimeOfDay={setTimeOfDay}
                weekday={weekday} setWeekday={setWeekday}
                validation={validation}
                saving={saving}
                onSubmit={() => void create()}
              />
            ) : selectedRunId && selectedTask ? (
              <RunDetail
                task={selectedTask}
                run={runDetail}
                onBack={() => { setSelectedRunId(null); setRunDetail(null) }}
                onOpenConversation={id => { onOpenConversation(id); onClose() }}
              />
            ) : selectedTask ? (
              <TaskDetail
                task={selectedTask}
                runs={runs}
                onToggle={() => void toggleEnabled(selectedTask)}
                onDelete={() => setDeleting(selectedTask)}
                onOpenRun={id => setSelectedRunId(id)}
              />
            ) : (
              <div className="flex flex-1 flex-col items-center justify-center gap-2 px-8 text-center">
                <Clock size={22} className="text-[var(--color-text-tertiary)]" />
                <p className="text-sm text-[var(--color-text-secondary)]">让助手定时替你做事，例如「每天 9 点汇总昨天的待办并给出建议」。</p>
              </div>
            )}
          </div>
        </div>
      </DialogShell>

      <ConfirmDialog
        open={deleting !== null}
        onClose={() => setDeleting(null)}
        onConfirm={() => void confirmDelete()}
        loading={deletePending}
        title="删除定时任务？"
        description="计划会停止，已经生成的会话还在近期会话里。"
        confirmText="删除"
        variant="danger"
      />

      <ConfirmDialog
        open={discardOpen}
        onClose={() => setDiscardOpen(false)}
        onConfirm={discardCreate}
        title="放弃创建？"
        description="已填写的内容将丢失。"
        confirmText="放弃"
        cancelText="继续编辑"
        variant="warning"
      />
    </>
  )
}

function CreateForm({
  title, setTitle, instruction, setInstruction, kind, setKind,
  onceAt, setOnceAt, timeOfDay, setTimeOfDay, weekday, setWeekday, validation, saving, onSubmit,
}: {
  title: string
  setTitle: (value: string) => void
  instruction: string
  setInstruction: (value: string) => void
  kind: ScheduledKind
  setKind: (value: ScheduledKind) => void
  onceAt: string
  setOnceAt: (value: string) => void
  timeOfDay: string
  setTimeOfDay: (value: string) => void
  weekday: string
  setWeekday: (value: string) => void
  validation: CreateFormValidation
  saving: boolean
  onSubmit: () => void
}) {
  const inputClass = (invalid?: string) =>
    `mt-1.5 h-9 w-full rounded-md border bg-[var(--color-bg-elevated)] px-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring ${invalid ? 'border-[var(--color-danger)]' : 'border-[var(--color-border)]'}`
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="min-h-0 flex-1 space-y-4 overflow-y-auto p-5">
        <label className="block text-xs font-medium text-[var(--color-text-primary)]">
          任务标题
          <input
            value={title}
            onChange={event => setTitle(event.target.value)}
            placeholder="可留空，将用指令前几字作为标题"
            className={inputClass()}
          />
        </label>
        <div className="space-y-1.5">
          <span className="text-xs font-medium text-[var(--color-text-primary)]">触发方式</span>
          <Select value={kind} onValueChange={value => setKind(value as ScheduledKind)}>
            <SelectTrigger className="h-9 w-full text-sm"><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="once">执行一次</SelectItem>
              <SelectItem value="daily">每天</SelectItem>
              <SelectItem value="weekly">每周</SelectItem>
            </SelectContent>
          </Select>
        </div>
        {kind === 'once' && (
          <div>
            <label htmlFor="scheduled-once-at" className="block text-xs font-medium text-[var(--color-text-primary)]">
              执行时间（北京时间）
            </label>
            <input
              id="scheduled-once-at"
              type="datetime-local"
              value={onceAt}
              min={shanghaiDatetimeLocal()}
              onChange={event => setOnceAt(event.target.value)}
              aria-describedby={validation.onceAt ? 'scheduled-once-at-error' : undefined}
              className={inputClass(validation.onceAt)}
            />
            {validation.onceAt && (
              <p id="scheduled-once-at-error" role="alert" className="mt-1.5 text-xs leading-5 text-[var(--color-danger)]">
                {validation.onceAt}
              </p>
            )}
          </div>
        )}
        {kind !== 'once' && (
          <div className="flex gap-3">
            {kind === 'weekly' && (
              <div className="min-w-0 flex-1 space-y-1.5">
                <span className="text-xs font-medium text-[var(--color-text-primary)]">星期</span>
                <Select value={weekday} onValueChange={setWeekday}>
                  <SelectTrigger className="h-9 w-full text-sm"><SelectValue /></SelectTrigger>
                  <SelectContent>
                    {WEEKDAYS.map((label, index) => (
                      <SelectItem key={label} value={String(index)}>{label}</SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
            )}
            <label className="block min-w-0 flex-1 text-xs font-medium text-[var(--color-text-primary)]">
              执行时间（北京时间）
              <input
                type="time"
                value={timeOfDay}
                onChange={event => setTimeOfDay(event.target.value)}
                className={inputClass()}
              />
            </label>
          </div>
        )}
        <div>
          <label htmlFor="scheduled-instruction" className="block text-xs font-medium text-[var(--color-text-primary)]">
            指令内容
          </label>
          <textarea
            id="scheduled-instruction"
            value={instruction}
            onChange={event => setInstruction(event.target.value)}
            rows={8}
            placeholder="到点后助手会按这段指令在后台执行，例如：汇总昨天的待办并给出建议。"
            aria-describedby={validation.instruction ? 'scheduled-instruction-error' : undefined}
            className={`mt-1.5 w-full rounded-md border bg-[var(--color-bg-elevated)] px-3 py-2 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring ${validation.instruction ? 'border-[var(--color-danger)]' : 'border-[var(--color-border)]'}`}
          />
          {validation.instruction && (
            <p id="scheduled-instruction-error" role="alert" className="mt-1.5 text-xs leading-5 text-[var(--color-danger)]">
              {validation.instruction}
            </p>
          )}
          <p className="mt-1.5 text-xs leading-5 text-[var(--color-text-tertiary)]">
            浏览器、委派等平台写操作到点不会自动执行；外部工具是否直接执行，取决于其「需要确认」设置。
          </p>
        </div>
      </div>
      <footer className="flex shrink-0 justify-end border-t border-[var(--color-border)] px-5 py-3.5">
        <button
          type="button"
          disabled={saving}
          onClick={onSubmit}
          className="inline-flex h-9 min-w-28 items-center justify-center gap-2 rounded-lg bg-brand px-4 text-xs font-medium text-white hover:bg-brand-deep focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50"
        >
          {saving ? <Loader2 size={13} className="animate-spin" /> : <Plus size={13} />} 创建
        </button>
      </footer>
    </div>
  )
}

function TaskDetail({
  task, runs, onToggle, onDelete, onOpenRun,
}: {
  task: ScheduledTask
  runs: ScheduledRun[]
  onToggle: () => void
  onDelete: () => void
  onOpenRun: (id: string) => void
}) {
  // P2-6：详情挂载（创建成功/深链打开）后焦点落到标题，键盘与读屏用户免于重新 Tab 定位
  const titleRef = useRef<HTMLHeadingElement>(null)
  useEffect(() => { titleRef.current?.focus() }, [])
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="shrink-0 border-b border-[var(--color-border)] px-5 py-4">
        <div className="flex items-start gap-3">
          <div className="min-w-0 flex-1">
            <h3 ref={titleRef} tabIndex={-1} className="truncate text-sm font-semibold text-[var(--color-text-primary)] outline-none">{task.title}</h3>
            <p className="mt-1 text-xs text-[var(--color-text-secondary)]">
              {scheduleLabel(task)}
              {task.next_run_at && task.enabled && task.schedule_kind !== 'once' ? ` · 下次 ${formatDateTime(task.next_run_at)}` : ''}
            </p>
          </div>
          <button
            type="button"
            onClick={onToggle}
            className="inline-flex h-8 items-center gap-1 rounded-md border border-[var(--color-border)] px-2.5 text-xs text-[var(--color-text-secondary)] hover:bg-[var(--color-bg-hover)]"
          >
            {task.enabled ? <Pause size={12} /> : <Play size={12} />}
            {task.enabled ? '暂停' : '启用'}
          </button>
          <button
            type="button"
            onClick={onDelete}
            className="inline-flex h-8 items-center gap-1 rounded-md border border-[var(--color-border)] px-2.5 text-xs text-[var(--color-danger)] hover:bg-[var(--color-danger-bg)]"
          >
            <Trash2 size={12} /> 删除
          </button>
        </div>
        <p className="mt-3 max-h-24 overflow-y-auto whitespace-pre-wrap border-l-2 border-[var(--color-border-active)] pl-3 text-[13px] leading-6 text-[var(--color-text-secondary)]">{task.instruction}</p>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto p-3">
        <p className="px-2 pb-2 text-xs text-[var(--color-text-tertiary)]">执行记录</p>
        {runs.length === 0 && (
          <p className="px-2 py-8 text-center text-xs text-[var(--color-text-tertiary)]">还没有执行过。到点后会出现在这里。</p>
        )}
        {runs.map(run => (
          <button
            key={run.id}
            type="button"
            onClick={() => onOpenRun(run.id)}
            className="mb-1 flex w-full items-center gap-2 rounded-lg px-2.5 py-2 text-left hover:bg-[var(--color-bg-hover)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            <StatusIcon status={run.status} />
            <span className="min-w-0 flex-1 truncate text-xs text-[var(--color-text-primary)]">
              {formatDateTime(run.scheduled_for)}
            </span>
            <span className="shrink-0 text-xs text-[var(--color-text-tertiary)]">{STATUS_LABEL[run.status]}</span>
          </button>
        ))}
      </div>
    </div>
  )
}

function RunDetail({
  task, run, onBack, onOpenConversation,
}: {
  task: ScheduledTask
  run: ScheduledRun | null
  onBack: () => void
  onOpenConversation: (id: string) => void
}) {
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex shrink-0 items-center gap-2 border-b border-[var(--color-border)] px-4 py-3">
        <button
          type="button"
          onClick={onBack}
          className="inline-flex h-8 items-center gap-1 rounded-md px-2 text-xs text-[var(--color-text-secondary)] hover:bg-[var(--color-bg-hover)]"
        >
          <ArrowLeft size={13} /> {task.title}
        </button>
      </div>
      {!run ? (
        <LoadingState className="min-h-0 flex-1" message="加载执行详情…" />
      ) : (
        <div className="min-h-0 flex-1 overflow-y-auto p-5">
          <div className="flex items-center gap-2">
            <StatusIcon status={run.status} />
            <span className="text-sm font-medium text-[var(--color-text-primary)]">{STATUS_LABEL[run.status]}</span>
            <span className="text-xs text-[var(--color-text-tertiary)]">{formatDateTime(run.scheduled_for)}</span>
          </div>
          {run.started_at && (
            <p className="mt-2 text-xs text-[var(--color-text-tertiary)]">
              开始 {formatDateTime(run.started_at, { seconds: true })}
              {run.finished_at ? ` · 结束 ${formatDateTime(run.finished_at, { seconds: true })}` : ''}
            </p>
          )}
          {run.error && (
            <p className="mt-3 rounded-lg border border-[var(--color-danger)] bg-[var(--color-danger-bg)] px-3 py-2 text-xs leading-5 text-[var(--color-danger)]">{run.error}</p>
          )}
          {run.result_summary && (
            <pre className="mt-3 whitespace-pre-wrap rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-elevated)] p-3 text-xs leading-5 text-[var(--color-text-primary)]">{run.result_summary}</pre>
          )}
          {!run.result_summary && !run.error && run.status === 'running' && (
            <p className="mt-6 text-center text-xs text-[var(--color-text-tertiary)]">正在后台执行…</p>
          )}
          {run.conversation_id && (
            <button
              type="button"
              onClick={() => onOpenConversation(run.conversation_id!)}
              className="mt-4 inline-flex h-9 items-center rounded-lg border border-[var(--color-border)] px-3 text-xs text-[var(--color-text-secondary)] hover:bg-[var(--color-bg-hover)]"
            >
              在会话中继续
            </button>
          )}
        </div>
      )}
    </div>
  )
}

function StatusIcon({ status }: { status: ScheduledRunStatus }) {
  if (status === 'completed') return <CheckCircle2 size={14} className="shrink-0 text-[var(--color-success)]" />
  if (status === 'failed') return <XCircle size={14} className="shrink-0 text-[var(--color-danger)]" />
  if (status === 'running' || status === 'queued') return <Loader2 size={14} className="shrink-0 animate-spin text-brand-ink" />
  return <Clock size={14} className="shrink-0 text-[var(--color-text-tertiary)]" />
}
