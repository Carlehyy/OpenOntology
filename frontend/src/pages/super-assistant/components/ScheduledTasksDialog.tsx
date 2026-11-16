import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  ArrowLeft, CheckCircle2, Clock, Loader2, Pause, Pencil, Play, Plus, Trash2, XCircle,
} from 'lucide-react'

import {
  superAssistantApi,
  type ScheduledKind,
  type ScheduledRun,
  type ScheduledRunStatus,
  type ScheduledTargetKind,
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
  STATUS_LABEL, WEEKDAYS, freshCreateForm, isCreateFormDirty,
  scheduleLabel, targetLabel, shanghaiDatetimeLocal, createScheduledPayload, editScheduledPayload, validateCreateForm,
  type CreateFormState, type CreateFormValidation,
} from './scheduledTaskLogic'

export default function ScheduledTasksDialog({
  open,
  initialTaskId,
  initialRunId,
  onClose,
  onOpenConversation,
  onOpenTaskInstance,
  onOpenAutoLoopEvent,
}: {
  open: boolean
  initialTaskId?: string | null
  initialRunId?: string | null
  onClose: () => void
  onOpenConversation: (conversationId: string) => void
  onOpenTaskInstance: (instanceId: string) => void
  onOpenAutoLoopEvent: (eventId: string) => void
}) {
  const [tasks, setTasks] = useState<ScheduledTask[]>([])
  const [runs, setRuns] = useState<ScheduledRun[]>([])
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [creating, setCreating] = useState(false)
  const [editingTask, setEditingTask] = useState<ScheduledTask | null>(null)
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(initialTaskId ?? null)
  const [selectedRunId, setSelectedRunId] = useState<string | null>(initialRunId ?? null)
  const [runDetail, setRunDetail] = useState<ScheduledRun | null>(null)
  const [deleting, setDeleting] = useState<ScheduledTask | null>(null)
  const [deletePending, setDeletePending] = useState(false)
  const [discardOpen, setDiscardOpen] = useState(false)
  const [pendingExit, setPendingExit] = useState<{ run: () => void } | null>(null)
  const [formBaseline, setFormBaseline] = useState<CreateFormState>(() => freshCreateForm())
  const [validation, setValidation] = useState<CreateFormValidation>({})

  const [title, setTitle] = useState('')
  const [instruction, setInstruction] = useState('')
  const [target, setTarget] = useState<ScheduledTargetKind>('super_assistant')
  const [templateId, setTemplateId] = useState('')
  const [kind, setKind] = useState<ScheduledKind>('once')
  const [onceAt, setOnceAt] = useState(() => freshCreateForm().onceAt)
  const [timeOfDay, setTimeOfDay] = useState('09:00')
  const [weekday, setWeekday] = useState('0')

  const selectedTask = useMemo(
    () => tasks.find(item => item.id === selectedTaskId) || null,
    [tasks, selectedTaskId],
  )

  // 深链 effect 的 .then 闭包捕获的是 effect 运行那次渲染的快照；列表请求在途
  // 期间用户可能已进入创建态输入——判脏必须读最新草稿（对抗复审：在途窗口静默丢草稿）
  const draftDirtyRef = useRef(false)
  useEffect(() => {
    draftDirtyRef.current = creating && isCreateFormDirty(
      { title, instruction, target, templateId, kind, onceAt, timeOfDay, weekday }, formBaseline,
    )
  })

  const resetForm = useCallback(() => {
    const fresh = freshCreateForm()
    setTitle(fresh.title)
    setInstruction(fresh.instruction)
    setTarget(fresh.target)
    setTemplateId(fresh.templateId)
    setKind(fresh.kind)
    setOnceAt(fresh.onceAt)
    setTimeOfDay(fresh.timeOfDay)
    setWeekday(fresh.weekday)
    setFormBaseline(fresh)
    setValidation({})
    setError('')
    setEditingTask(null)
  }, [])

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
          // 脏草稿优先于深链自动选中：URL 参数变化（后退/前进）不得静默丢弃用户输入
          if (!draftDirtyRef.current) {
            setSelectedTaskId(initialTaskId)
            setCreating(false)
          }
        }
        if (initialRunId) setSelectedRunId(initialRunId)
      })
      .catch(err => { if (alive) setError(errorText(err, '定时任务加载失败')) })
      .finally(() => { if (alive) setLoading(false) })
    return () => { alive = false }
  }, [open, initialTaskId, initialRunId, refreshTasks])

  // 关闭即复位创建态：脏草稿不可能在未确认的情况下离开（exitCreate 守卫），
  // 因此这里重置的必然是干净基线——杜绝校验错误与陈旧默认时间跨开合残留
  useEffect(() => {
    if (open) return
    resetForm()
    setCreating(false)
    setDiscardOpen(false)
    setPendingExit(null)
  }, [open, resetForm])

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

  const create = async () => {
    if (saving) return
    const form = { title, instruction, target, templateId, kind, onceAt, timeOfDay, weekday }
    const errors = validateCreateForm(form, new Date(), !!editingTask)
    setValidation(errors)
    if (errors.instruction || errors.templateId || errors.onceAt || errors.timeOfDay) {
      // 清掉上一轮后端报错，避免顶部横幅与新字段错误同屏矛盾
      setError('')
      return
    }
    setSaving(true); setError('')
    try {
      const created = editingTask
        ? await superAssistantApi.updateScheduledTask(editingTask.id, editScheduledPayload(form, formBaseline))
        : await superAssistantApi.createScheduledTask(createScheduledPayload(form))
      await refreshTasks()
      setCreating(false)
      setSelectedTaskId(created.id)
      setSelectedRunId(null)
      resetForm()
      toast.success(editingTask ? '定时任务已保存' : '定时任务已创建', {
        description: '到点后产生事件，由规则或助手处理。可在执行记录中查看关联事件。',
      })
    } catch (err) {
      // axios 超时（ECONNABORTED）无 response，errorText 会透出英文技术串；
      // 客户端超时不回滚服务端，须提示用户先查列表避免重复创建
      setError((err as { code?: string })?.code === 'ECONNABORTED'
        ? editingTask ? '保存请求超时。请稍后刷新列表确认保存结果。' : '创建请求超时。请稍后重开列表确认是否已创建，避免重复创建。'
        : errorText(err, editingTask ? '保存失败' : '创建失败'))
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

  const openEdit = (task: ScheduledTask) => {
    const value: CreateFormState = {
      title: task.title, instruction: task.instruction, target: task.target_kind,
      templateId: task.target_template_id ?? '', kind: task.schedule_kind,
      onceAt: task.run_at ? shanghaiDatetimeLocal(new Date(task.run_at)) : freshCreateForm().onceAt,
      timeOfDay: `${String(task.hour ?? 9).padStart(2, '0')}:${String(task.minute ?? 0).padStart(2, '0')}`,
      weekday: String(task.weekday ?? 0),
    }
    setTitle(value.title); setInstruction(value.instruction); setTarget(value.target); setTemplateId(value.templateId)
    setKind(value.kind); setOnceAt(value.onceAt); setTimeOfDay(value.timeOfDay); setWeekday(value.weekday)
    setFormBaseline(value); setValidation({}); setError(''); setEditingTask(task); setCreating(true)
    setSelectedRunId(null); setRunDetail(null)
  }

  /** 离开创建态的统一守卫（P0-2，对抗审查收敛为单点）：关闭弹窗、左栏切换任务、
   *  重新开始创建，脏草稿一律先弹「放弃创建？」确认，放弃后执行原本要做的去向；
   *  saving 中所有离开动作改为 toast 反馈（请求已有 30s 超时，不会永久锁死） */
  const exitCreate = (after: () => void) => {
    if (saving) {
      toast.info(editingTask ? '正在保存定时任务，请稍候…' : '正在创建定时任务，请稍候…')
      return
    }
    if (creating && isCreateFormDirty(
      { title, instruction, target, templateId, kind, onceAt, timeOfDay, weekday }, formBaseline,
    )) {
      setPendingExit({ run: after })
      setDiscardOpen(true)
      return
    }
    after()
  }

  const requestClose = () => exitCreate(onClose)

  const discardCreate = () => {
    const exit = pendingExit
    setDiscardOpen(false)
    setPendingExit(null)
    resetForm()
    setCreating(false)
    if (exit) exit.run()
    else onClose()
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
  const changeTimeOfDay = (value: string) => {
    setTimeOfDay(value)
    if (validation.timeOfDay) setValidation(current => ({ ...current, timeOfDay: undefined }))
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
        description="到点后产生事件，交给规则或助手处理。执行记录保留关联事件与处理结果。"
        onClose={requestClose}
        icon={<Clock size={18} />}
        contentClassName="h-[min(82dvh,44rem)]"
      >
        <div className="flex min-h-0 flex-1" data-testid="scheduled-tasks-dialog">
          <nav aria-label="定时任务列表" className="flex w-56 shrink-0 flex-col border-r border-[var(--color-border)]">
            <div className="p-2">
              <button
                type="button"
                disabled={saving}
                onClick={() => exitCreate(openCreate)}
                className="flex h-9 w-full items-center justify-center gap-1.5 rounded-lg bg-brand text-xs font-medium text-white transition-colors hover:bg-brand-deep focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50"
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
                    disabled={saving}
                    onClick={() => exitCreate(() => {
                      setCreating(false)
                      setSelectedTaskId(task.id)
                      setSelectedRunId(null)
                      setRunDetail(null)
                    })}
                    className={`mb-1 w-full rounded-lg px-2.5 py-2 text-left transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50 ${
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
                editingTask={editingTask}
                kind={kind} setKind={changeKind}
                onceAt={onceAt} setOnceAt={changeOnceAt}
                timeOfDay={timeOfDay} setTimeOfDay={changeTimeOfDay}
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
                onOpenTaskInstance={id => { onOpenTaskInstance(id); onClose() }}
                onOpenAutoLoopEvent={onOpenAutoLoopEvent}
              />
            ) : selectedTask ? (
              <TaskDetail
                task={selectedTask}
                runs={runs}
                onToggle={() => void toggleEnabled(selectedTask)}
                onEdit={() => openEdit(selectedTask)}
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
        description="计划会停止，已生成的运行记录和事件仍会保留。"
        confirmText="删除"
        variant="danger"
      />

      <ConfirmDialog
        open={discardOpen}
        onClose={() => { setDiscardOpen(false); setPendingExit(null) }}
        onConfirm={discardCreate}
        title={editingTask ? '放弃修改？' : '放弃创建？'}
        description="已填写的内容将丢失。"
        confirmText="放弃"
        cancelText="继续编辑"
        variant="warning"
      />
    </>
  )
}

function CreateForm({
  title, setTitle, instruction, setInstruction, editingTask,
  kind, setKind,
  onceAt, setOnceAt, timeOfDay, setTimeOfDay, weekday, setWeekday, validation, saving, onSubmit,
}: {
  title: string
  setTitle: (value: string) => void
  instruction: string
  setInstruction: (value: string) => void
  editingTask: ScheduledTask | null
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
    // 与 SelectTrigger 同款令牌（rounded-lg/border-input/bg-background/shadow-sm），
    // 原生输入与下拉并排时视觉一致；label 一律 block 盒（16px 行高）保证同排控件基线对齐
    `mt-1.5 h-9 w-full rounded-lg border bg-background px-3 text-sm shadow-sm outline-none transition-colors focus-visible:ring-2 focus-visible:ring-ring ${invalid ? 'border-[var(--color-danger)]' : 'border-input'}`
  return (
    <div className="min-h-0 flex-1 space-y-4 overflow-y-auto p-5">
      <label className="block text-xs font-medium text-[var(--color-text-primary)]">
        任务标题
        <input
          value={title}
          onChange={event => setTitle(event.target.value)}
          placeholder="可留空，将用任务内容前几字作为标题"
          className={inputClass()}
        />
      </label>
      <div>
        <label htmlFor="scheduled-instruction" className="block text-xs font-medium text-[var(--color-text-primary)]">
          任务内容
        </label>
        <textarea
          id="scheduled-instruction"
          value={instruction}
          onChange={event => setInstruction(event.target.value)}
          rows={8}
          placeholder="作为到期事件的内容，例如：汇总昨天的待办并给出建议。"
          aria-describedby={validation.instruction ? 'scheduled-instruction-error' : undefined}
          className={`mt-1.5 w-full rounded-lg border bg-background px-3 py-2 text-sm shadow-sm outline-none transition-colors focus-visible:ring-2 focus-visible:ring-ring ${validation.instruction ? 'border-[var(--color-danger)]' : 'border-input'}`}
        />
        {validation.instruction && (
          <p id="scheduled-instruction-error" role="alert" className="mt-1.5 text-xs leading-5 text-[var(--color-danger)]">
            {validation.instruction}
          </p>
        )}
        <p className="mt-1.5 text-xs leading-5 text-[var(--color-text-tertiary)]">规则路由统一处理到期事件，需要你决定的内容会进入决策事项。</p>
        {editingTask && <p className="mt-1.5 text-xs text-[var(--color-text-tertiary)]">原始目标：{targetLabel(editingTask)} · 时区：{editingTask.timezone}。保存保留该记录。</p>}
      </div>
      {/* 触发设置：短下拉并排成网格，标题与内容两行核心输入之后的紧凑区 */}
      <div className="grid grid-cols-2 gap-3">
        <div className="min-w-0 space-y-1.5">
          <span className="block text-xs font-medium text-[var(--color-text-primary)]">触发方式</span>
          <Select value={kind} onValueChange={value => setKind(value as ScheduledKind)}>
            <SelectTrigger aria-label="触发方式" className="h-9 w-full text-sm"><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="once">执行一次</SelectItem>
              <SelectItem value="daily">每天</SelectItem>
              <SelectItem value="weekly">每周</SelectItem>
            </SelectContent>
          </Select>
        </div>
        {kind === 'once' && (
          <div className="col-span-2">
            <label htmlFor="scheduled-once-at" className="block text-xs font-medium text-[var(--color-text-primary)]">
              执行时间（北京时间）
            </label>
            <input
              id="scheduled-once-at"
              type="datetime-local"
              value={onceAt}
              min={editingTask ? undefined : shanghaiDatetimeLocal()}
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
          <>
            {kind === 'weekly' && (
              <div className="min-w-0 space-y-1.5">
                <span className="block text-xs font-medium text-[var(--color-text-primary)]">星期</span>
                <Select value={weekday} onValueChange={setWeekday}>
                  <SelectTrigger aria-label="星期" className="h-9 w-full text-sm"><SelectValue /></SelectTrigger>
                  <SelectContent>
                    {WEEKDAYS.map((label, index) => (
                      <SelectItem key={label} value={String(index)}>{label}</SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
            )}
            <div className={kind === 'weekly' ? 'min-w-0' : 'col-span-2'}>
              <label htmlFor="scheduled-time-of-day" className="block text-xs font-medium text-[var(--color-text-primary)]">
                执行时间
              </label>
              <input
                id="scheduled-time-of-day"
                type="time"
                value={timeOfDay}
                onChange={event => setTimeOfDay(event.target.value)}
                aria-describedby={validation.timeOfDay ? 'scheduled-time-of-day-error' : undefined}
                className={inputClass(validation.timeOfDay)}
              />
              {validation.timeOfDay && (
                <p id="scheduled-time-of-day-error" role="alert" className="mt-1.5 text-xs leading-5 text-[var(--color-danger)]">
                  {validation.timeOfDay}
                </p>
              )}
            </div>
          </>
        )}
      </div>
      <div className="flex justify-end">
        <button
          type="button"
          disabled={saving}
          onClick={onSubmit}
          className="inline-flex h-9 min-w-28 items-center justify-center gap-2 rounded-lg bg-brand px-4 text-xs font-medium text-white hover:bg-brand-deep focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50"
        >
          {saving ? <Loader2 size={13} className="animate-spin" /> : editingTask ? <Pencil size={13} /> : <Plus size={13} />} {editingTask ? '保存修改' : '创建'}
        </button>
      </div>
    </div>
  )
}

function TaskDetail({
  task, runs, onToggle, onEdit, onDelete, onOpenRun,
}: {
  task: ScheduledTask
  runs: ScheduledRun[]
  onToggle: () => void
  onEdit: () => void
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
              {scheduleLabel(task)} · 到期事件 · {task.timezone} · 原始目标：{targetLabel(task)}
              {task.next_run_at && task.enabled && task.schedule_kind !== 'once' ? ` · 下次 ${formatDateTime(task.next_run_at)}` : ''}
            </p>
          </div>
          <button type="button" onClick={onEdit} className="inline-flex h-8 items-center gap-1 rounded-md border border-[var(--color-border)] px-2.5 text-xs hover:bg-[var(--color-bg-hover)]"><Pencil size={12} />编辑</button>
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
        <p className="mt-3 max-h-24 overflow-y-auto whitespace-pre-wrap border-l-2 border-[var(--color-border)] pl-3 text-[13px] leading-6 text-[var(--color-text-secondary)]">{task.instruction}</p>
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
  task, run, onBack, onOpenConversation, onOpenTaskInstance, onOpenAutoLoopEvent,
}: {
  task: ScheduledTask
  run: ScheduledRun | null
  onBack: () => void
  onOpenConversation: (id: string) => void
  onOpenTaskInstance: (id: string) => void
  onOpenAutoLoopEvent: (id: string) => void
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
          {!!run.autoloopEventIds?.length && <div className="mt-4 space-y-2"><p className="text-xs text-[var(--color-text-tertiary)]">关联事件</p>{run.autoloopEventIds.map(id => <button type="button" key={id} onClick={() => onOpenAutoLoopEvent(id)} className="block max-w-full break-all rounded-lg border border-[var(--color-border)] px-3 py-2 text-left text-xs hover:bg-[var(--color-bg-hover)]">查看事件 {id}</button>)}</div>}
          {run.conversation_id && (
            <button
              type="button"
              onClick={() => onOpenConversation(run.conversation_id!)}
              className="mt-4 inline-flex h-9 items-center rounded-lg border border-[var(--color-border)] px-3 text-xs text-[var(--color-text-secondary)] hover:bg-[var(--color-bg-hover)]"
            >
              在会话中继续
            </button>
          )}
          {run.instance_id && (
            <button
              type="button"
              onClick={() => onOpenTaskInstance(run.instance_id!)}
              className="mt-4 inline-flex h-9 items-center rounded-lg border border-[var(--color-border)] px-3 text-xs text-[var(--color-text-secondary)] hover:bg-[var(--color-bg-hover)]"
            >
              查看实例
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
