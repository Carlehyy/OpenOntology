import { useEffect, useState } from 'react'
import { Select as FormSelect, SelectContent as FormSelectContent, SelectItem as FormSelectItem, SelectTrigger as FormSelectTrigger, SelectValue as FormSelectValue } from '@/components/ui/select'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, CheckCircle2, FilePlus2, Loader2, Paperclip, RefreshCcw, Trash2, Undo2, Upload } from 'lucide-react'
import { Alert } from '@/components/ui/Alert'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { toast } from 'sonner'
import { ontologyApi } from '@/api/ontologies'
import { eventsApi, formatBytes, type Attachment, type EventCreateBody, type EventItem } from '@/api/events'

const SEVERITY_OPTIONS = [
  { value: 'info', label: '信息' },
  { value: 'low', label: '低级' },
  { value: 'medium', label: '中级' },
  { value: 'high', label: '高级' },
  { value: 'critical', label: '严重' },
]

const EVENT_TYPE_SUGGESTIONS = [
  '设备异常', '系统告警', '业务异常', '业务变更', '客户反馈', '客户投诉',
  '数据质量', '流程事件', '交付事件', '供应链事件', '安全事件', '合规事件', '观察记录', '其它',
]

const MAX_ATTACHMENT_MB = 200
const MAX_ATTACHMENT_BYTES = MAX_ATTACHMENT_MB * 1024 * 1024

function fileIdentity(file: File): string {
  return `${file.name}:${file.size}:${file.lastModified}`
}

function errorDetail(cause: any): string {
  if (typeof cause === 'string') return cause
  return cause?.detail || cause?.message || '请稍后重试'
}

function isoToLocalInput(iso: string | null): string {
  if (!iso) return ''
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return ''
  const pad = (value: number) => String(value).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`
}

export default function EventFormModal({
  open,
  onClose,
  editing,
}: {
  open: boolean
  onClose: () => void
  editing?: EventItem | null
}) {
  const queryClient = useQueryClient()
  const isEdit = Boolean(editing)
  const [title, setTitle] = useState('')
  const [eventType, setEventType] = useState('')
  const [severity, setSeverity] = useState('info')
  const [occurredAt, setOccurredAt] = useState('')
  const [ontologyId, setOntologyId] = useState('')
  const [description, setDescription] = useState('')
  const [files, setFiles] = useState<File[]>([])
  const [existingAttachments, setExistingAttachments] = useState<Attachment[]>([])
  const [removedAttachmentIds, setRemovedAttachmentIds] = useState<Set<string>>(new Set())
  const [createdEventId, setCreatedEventId] = useState<string | null>(null)
  const [uploadedFileKeys, setUploadedFileKeys] = useState<Set<string>>(new Set())
  const [error, setError] = useState('')

  const { data: ontologyList } = useQuery({
    queryKey: ['events-ontology-options'],
    queryFn: () => ontologyApi.list({ page_size: 100 }),
    enabled: open,
  })
  const eventDetailQuery = useQuery({
    queryKey: ['events', 'detail', editing?.id],
    queryFn: () => eventsApi.get(editing!.id),
    enabled: open && Boolean(editing?.id),
  })
  const ontologyOptions = [
    { value: '', label: '（不关联本体）' },
    ...(ontologyList?.items || []).map(ontology => ({ value: ontology.id, label: ontology.name })),
  ]

  useEffect(() => {
    if (!open) return
    setError('')
    setFiles([])
    setExistingAttachments(editing?.attachments || [])
    setRemovedAttachmentIds(new Set())
    setCreatedEventId(null)
    setUploadedFileKeys(new Set())
    if (editing) {
      setTitle(editing.title)
      setEventType(editing.eventType || '')
      setSeverity(editing.severity || 'info')
      setOccurredAt(isoToLocalInput(editing.occurredAt))
      setOntologyId(editing.ontologyId || '')
      setDescription(editing.description || '')
      return
    }
    setTitle('')
    setEventType('')
    setSeverity('info')
    setOccurredAt('')
    setOntologyId('')
    setDescription('')
  }, [editing, open])

  useEffect(() => {
    if (!open || !editing || !eventDetailQuery.data || eventDetailQuery.data.id !== editing.id) return
    setExistingAttachments(eventDetailQuery.data.attachments || [])
  }, [editing, eventDetailQuery.data, open])

  const mutation = useMutation({
    mutationFn: async () => {
      // 一次报齐全部缺失必填项，避免用户多次试错（MYW-42 优化点4）
      const missing: string[] = []
      if (!title.trim()) missing.push('事件标题')
      if (!eventType.trim()) missing.push('事件类型')
      if (!severity.trim()) missing.push('严重程度')
      if (!description.trim()) missing.push('详细描述')
      if (missing.length) throw new Error(`请完善必填项：${missing.join('、')}`)
      const oversized = files.find(file => file.size > MAX_ATTACHMENT_BYTES)
      if (oversized) throw new Error(`附件“${oversized.name}”超过单文件 ${MAX_ATTACHMENT_MB}MB 限制`)
      const body: EventCreateBody = {
        title: title.trim(),
        eventType: eventType.trim(),
        severity,
        description: description.trim(),
        occurredAt: occurredAt ? new Date(occurredAt).toISOString() : null,
        ontologyId: ontologyId || null,
      }
      let event: EventItem
      if (isEdit) {
        event = await eventsApi.update(editing!.id, body)
      } else if (createdEventId) {
        event = await eventsApi.update(createdEventId, body)
      } else {
        event = await eventsApi.create(body)
        setCreatedEventId(event.id)
      }
      for (const attachment of existingAttachments) {
        if (!removedAttachmentIds.has(attachment.id)) continue
        try {
          await eventsApi.deleteAttachment(event.id, attachment.id)
          setExistingAttachments(current => current.filter(item => item.id !== attachment.id))
          setRemovedAttachmentIds(current => {
            const next = new Set(current)
            next.delete(attachment.id)
            return next
          })
        } catch (cause) {
          throw new Error(
            `事件信息已保存，但已有附件“${attachment.filename}”删除失败：${errorDetail(cause)}。请重试。`,
            { cause },
          )
        }
      }
      for (const file of files) {
        const key = fileIdentity(file)
        if (uploadedFileKeys.has(key)) continue
        try {
          await eventsApi.uploadAttachment(event.id, file)
          setUploadedFileKeys(current => new Set(current).add(key))
        } catch (cause) {
          throw new Error(
            `事件信息已保存，但附件“${file.name}”上传失败：${errorDetail(cause)}。可移除该文件后重试。`,
            { cause },
          )
        }
      }
      return event
    },
    onSuccess: (event) => {
      queryClient.invalidateQueries({ queryKey: ['events'] })
      toast.success(isEdit ? '事件已保存' : '事件登记成功', { description: isEdit ? undefined : `事件编号 ${event.eventNo}，可在详情中复制` })
      onClose()
    },
    onError: (cause: any) => setError(cause?.message || cause?.detail || '保存失败'),
  })

  const addFiles = (incoming: File[]) => {
    const oversized = incoming.filter(file => file.size > MAX_ATTACHMENT_BYTES)
    const accepted = incoming.filter(file => file.size <= MAX_ATTACHMENT_BYTES)
    setError(oversized.length
      ? `已忽略 ${oversized.length} 个超过单文件 ${MAX_ATTACHMENT_MB}MB 限制的附件：${oversized.map(file => file.name).join('、')}`
      : '')
    setFiles(current => {
      const known = new Set(current.map(fileIdentity))
      return [...current, ...accepted.filter(file => !known.has(fileIdentity(file)))]
    })
  }

  const labelClass = 'mb-1.5 block text-sm font-medium text-foreground'
  const controlClass = 'h-10 w-full rounded-lg border border-border bg-card px-3 text-sm text-foreground shadow-sm transition-all placeholder:text-[var(--color-text-tertiary)] focus-visible:border-ring focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring'
  const visibleExistingCount = existingAttachments.filter(attachment => !removedAttachmentIds.has(attachment.id)).length

  return (
    <Dialog open={open} onOpenChange={next => { if (!next && !mutation.isPending) onClose() }}>
      <DialogContent
        className="flex max-h-[min(88dvh,900px)] w-[min(92vw,64rem)] flex-col"
        dismissible={!mutation.isPending}
      >
        <DialogHeader icon={<FilePlus2 size={18} />}>
          <DialogTitle>{isEdit ? '编辑事件' : '登记事件'}</DialogTitle>
          {!isEdit && <DialogDescription>记录一条业务事件，供后续本体优化挖掘</DialogDescription>}
        </DialogHeader>
      <div className="max-h-[68vh] space-y-5 overflow-y-auto px-1 pb-1 pr-2">
        {error && (
          <Alert variant="danger" role="alert" className="items-center">
            {error}
          </Alert>
        )}

        <div>
          <label htmlFor="event-title" className={labelClass}>事件标题 <span className="text-[var(--color-danger)]">*</span></label>
          <input id="event-title" required aria-required="true" value={title} onChange={event => setTitle(event.target.value)} placeholder="简要描述发生了什么" className={controlClass} />
        </div>

        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          <div>
            <label htmlFor="event-type" className={labelClass}>事件类型 <span className="text-[var(--color-danger)]">*</span></label>
            <input
              id="event-type"
              required
              aria-required="true"
              list="event-type-options"
              value={eventType}
              onChange={event => setEventType(event.target.value)}
              placeholder="选择或输入类型"
              className={controlClass}
            />
            <datalist id="event-type-options">
              {EVENT_TYPE_SUGGESTIONS.map(type => <option key={type} value={type} />)}
            </datalist>
          </div>
          <div>
            <label htmlFor="event-severity" className={labelClass}>严重程度 <span className="text-[var(--color-danger)]">*</span></label>
            <FormSelect value={severity} onValueChange={setSeverity}>
              <FormSelectTrigger id="event-severity" aria-required="true" className={controlClass}>
                <FormSelectValue />
              </FormSelectTrigger>
              <FormSelectContent>
                {SEVERITY_OPTIONS.map(option => <FormSelectItem key={option.value} value={option.value}>{option.label}</FormSelectItem>)}
              </FormSelectContent>
            </FormSelect>
          </div>
        </div>

        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          <div>
            <label htmlFor="event-occurred-at" className={labelClass}>发生时间</label>
            <input id="event-occurred-at" type="datetime-local" value={occurredAt} onChange={event => setOccurredAt(event.target.value)} className={controlClass} />
          </div>
          <div>
            <label htmlFor="event-ontology" className={labelClass}>关联本体（后续挖掘目标）</label>
            <FormSelect value={ontologyId} onValueChange={setOntologyId}>
              <FormSelectTrigger id="event-ontology" className={controlClass}>
                <FormSelectValue />
              </FormSelectTrigger>
              <FormSelectContent>
                {ontologyOptions.map(option => <FormSelectItem key={option.value} value={option.value}>{option.label}</FormSelectItem>)}
              </FormSelectContent>
            </FormSelect>
          </div>
        </div>

        <div>
          <label htmlFor="event-description" className={labelClass}>详细描述 <span className="text-[var(--color-danger)]">*</span></label>
          <textarea
            id="event-description"
            required
            aria-required="true"
            value={description}
            onChange={event => setDescription(event.target.value)}
            rows={4}
            className="w-full resize-none rounded-lg border border-border bg-card px-3 py-2.5 text-sm leading-6 text-foreground shadow-sm transition-all placeholder:text-[var(--color-text-tertiary)] focus-visible:border-ring focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            placeholder="事件的完整经过、背景、影响……"
          />
        </div>

        <div>
          <div className="mb-2 flex items-center justify-between gap-3">
            <div>
              <label className="text-sm font-medium text-foreground">附件（可选，可多选）</label>
              <p className="mt-0.5 text-xs text-[var(--color-text-tertiary)]">
                支持邮件、文档、表格、图片、音视频、压缩包等文件，单文件不超过 {MAX_ATTACHMENT_MB}MB
              </p>
            </div>
            {(visibleExistingCount > 0 || files.length > 0 || removedAttachmentIds.size > 0) && (
              <span className="text-right text-xs font-medium text-[var(--color-success)]">
                {visibleExistingCount > 0 ? `已有 ${visibleExistingCount} 个` : ''}
                {visibleExistingCount > 0 && files.length > 0 ? ' · ' : ''}
                {files.length > 0 ? `新增 ${files.length} 个` : ''}
                {removedAttachmentIds.size > 0 ? ` · 待删除 ${removedAttachmentIds.size} 个` : ''}
              </span>
            )}
          </div>
          <label className="flex cursor-pointer items-center justify-center gap-2 rounded-xl border border-dashed border-[color-mix(in_srgb,var(--color-success)_35%,transparent)] bg-[var(--color-success-bg)] px-4 py-4 text-sm font-medium text-[var(--color-success)] transition-all hover:border-[color-mix(in_srgb,var(--color-success)_35%,transparent)] hover:bg-[var(--color-success-bg)] focus-within:ring-2 focus-within:ring-ring">
            <Upload size={16} /> 选择多个附件
            <input
              type="file"
              multiple
              className="sr-only"
              onChange={event => {
                addFiles(Array.from(event.target.files || []))
                event.currentTarget.value = ''
              }}
            />
          </label>
          {isEdit && eventDetailQuery.isLoading && existingAttachments.length === 0 && (
            <div className="mt-3 flex items-center justify-center gap-2 rounded-xl border border-border bg-muted px-3 py-4 text-sm text-muted-foreground">
              <Loader2 size={15} className="animate-spin" /> 正在加载已有附件…
            </div>
          )}
          {isEdit && eventDetailQuery.isError && (
            <div className="mt-3 flex items-center justify-between gap-3 rounded-xl border border-[color-mix(in_srgb,var(--color-danger)_35%,transparent)] bg-[var(--color-danger-bg)] px-3 py-2.5 text-sm text-[var(--color-danger)]">
              <span className="flex items-center gap-2"><AlertTriangle size={15} /> 已有附件加载失败</span>
              <button
                type="button"
                onClick={() => void eventDetailQuery.refetch()}
                className="inline-flex items-center gap-1 rounded-md px-2 py-1 text-xs font-medium hover:bg-[var(--color-danger-bg)]"
              >
                <RefreshCcw size={13} /> 重试
              </button>
            </div>
          )}
          {(existingAttachments.length > 0 || files.length > 0) && (
            <div className="mt-3 overflow-hidden rounded-xl border border-border bg-card">
              {existingAttachments.map(attachment => {
                const removed = removedAttachmentIds.has(attachment.id)
                return (
                  <div key={attachment.id} className={`group flex items-center gap-3 border-t border-border px-3 py-2.5 first:border-t-0 ${removed ? 'bg-[var(--color-danger-bg)]' : ''}`}>
                    <span className={`flex h-9 w-9 shrink-0 items-center justify-center rounded-lg ${removed ? 'bg-[var(--color-danger-bg)] text-[var(--color-danger)]' : 'bg-[var(--color-success-bg)] text-[var(--color-success)]'}`}>
                      <Paperclip size={15} />
                    </span>
                    <div className="min-w-0 flex-1">
                      <p className={`truncate text-sm font-medium ${removed ? 'text-[var(--color-text-tertiary)] line-through' : 'text-foreground'}`} title={attachment.filename}>{attachment.filename}</p>
                      <p className={`mt-0.5 text-xs ${removed ? 'text-[var(--color-danger)]' : 'text-[var(--color-text-tertiary)]'}`}>
                        {formatBytes(attachment.fileSize)} · {removed ? '保存后删除' : '已有附件'}
                      </p>
                    </div>
                    <button
                      type="button"
                      onClick={() => setRemovedAttachmentIds(current => {
                        const next = new Set(current)
                        if (removed) next.delete(attachment.id)
                        else next.add(attachment.id)
                        return next
                      })}
                      className={`flex h-8 w-8 items-center justify-center rounded-lg transition-colors ${removed ? 'text-[var(--color-text-tertiary)] hover:bg-[var(--color-success-bg)] hover:text-[var(--color-success)]' : 'text-[var(--color-text-tertiary)] hover:bg-[var(--color-danger-bg)] hover:text-[var(--color-danger)]'}`}
                      title={removed ? `撤销删除 ${attachment.filename}` : `删除 ${attachment.filename}`}
                      aria-label={removed ? `撤销删除 ${attachment.filename}` : `删除 ${attachment.filename}`}
                    >
                      {removed ? <Undo2 size={15} /> : <Trash2 size={15} />}
                    </button>
                  </div>
                )
              })}
              {files.map((file, index) => {
                const uploaded = uploadedFileKeys.has(fileIdentity(file))
                return (
                <div key={fileIdentity(file)} className="group flex items-center gap-3 border-t border-border px-3 py-2.5 first:border-t-0">
                  <span className={`flex h-9 w-9 shrink-0 items-center justify-center rounded-lg ${uploaded ? 'bg-[var(--color-success-bg)] text-[var(--color-success)]' : 'bg-muted text-muted-foreground'}`}>
                    {uploaded ? <CheckCircle2 size={16} /> : <Paperclip size={15} />}
                  </span>
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-sm font-medium text-foreground" title={file.name}>{file.name}</p>
                    <p className={`mt-0.5 text-xs ${uploaded ? 'text-[var(--color-success)]' : 'text-[var(--color-text-tertiary)]'}`}>
                      {formatBytes(file.size)}{uploaded ? ' · 已上传，重试时将自动跳过' : ''}
                    </p>
                  </div>
                  {!uploaded && (
                    <button
                      type="button"
                      onClick={() => setFiles(current => current.filter((_, currentIndex) => currentIndex !== index))}
                      className="flex h-8 w-8 items-center justify-center rounded-lg text-[var(--color-text-tertiary)] transition-colors hover:bg-[var(--color-danger-bg)] hover:text-[var(--color-danger)]"
                      title={`移除 ${file.name}`}
                      aria-label={`移除 ${file.name}`}
                    >
                      <Trash2 size={15} />
                    </button>
                  )}
                </div>
                )
              })}
            </div>
          )}
        </div>
      </div>
      <DialogFooter className="flex w-full justify-center gap-3">
        <button
          type="button"
          onClick={onClose}
          disabled={mutation.isPending}
          className="h-10 rounded-lg border border-border bg-card px-6 text-sm font-medium text-muted-foreground transition-all hover:bg-muted active:scale-[0.98] disabled:opacity-50"
        >
          取消
        </button>
        <button
          type="button"
          onClick={() => mutation.mutate()}
          disabled={mutation.isPending}
          className="inline-flex h-10 min-w-24 items-center justify-center gap-2 rounded-lg bg-[var(--color-success)] px-6 text-sm font-medium text-[var(--color-text-inverse)] shadow-sm transition-all hover:bg-[var(--color-success)] active:scale-[0.98] disabled:opacity-50"
        >
          {mutation.isPending && <span className="h-4 w-4 animate-spin rounded-full border-2 border-border border-t-white" />}
          {isEdit ? '保存' : '登记'}
        </button>
      </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
