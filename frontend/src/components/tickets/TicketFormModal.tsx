import { useEffect, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import {
  AlertTriangle, CheckCircle2, Eye, Megaphone, Paperclip, Trash2, Upload,
} from 'lucide-react'
import { Modal } from '@/components/ui/Modal'
import { toast } from 'sonner'
import {
  ticketsApi, formatBytes,
  TICKET_CATEGORY_META, TICKET_CATEGORY_ORDER, type TicketCategory, type TicketItem,
} from '@/api/tickets'
import { ImagePreviewModal } from '@/pages/tickets/shared'

const MAX_ATTACHMENT_MB = 200
const MAX_ATTACHMENT_BYTES = MAX_ATTACHMENT_MB * 1024 * 1024

function fileIdentity(file: File): string {
  return `${file.name}:${file.size}:${file.lastModified}`
}

function errorDetail(cause: any): string {
  if (typeof cause === 'string') return cause
  return cause?.detail || cause?.message || '请稍后重试'
}

function isImageFile(file: File): boolean {
  return file.type.startsWith('image/')
}

/** 粘贴的图片给一个可读的默认名：粘贴图片-HHMMSS.ext */
function pastedImageName(mime: string): string {
  const ext = (mime.split('/')[1] || 'png').split('+')[0]
  const now = new Date()
  const pad = (value: number) => String(value).padStart(2, '0')
  return `粘贴图片-${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}.${ext}`
}

/**
 * 工单提交弹窗：任何页面均可打开（顶栏弹窗/工单页共用）。
 * - 分类单选（系统故障/体验优化/新增功能/其他）；
 * - 反馈内容支持直接粘贴图片，自动转为附件并可点击预览；不支持粘贴的
 *   内容给出友好提示（仍可用「选择多个附件」添加任意文件）；
 * - 提交时自动记录用户当前所在页面完整地址（含 hash 路由）供审查。
 */
export default function TicketFormModal({
  open,
  onClose,
}: {
  open: boolean
  onClose: () => void
}) {
  const queryClient = useQueryClient()
  const [title, setTitle] = useState('')
  const [category, setCategory] = useState<TicketCategory | ''>('')
  const [content, setContent] = useState('')
  const [files, setFiles] = useState<File[]>([])
  const [uploadedFileKeys, setUploadedFileKeys] = useState<Set<string>>(new Set())
  const [error, setError] = useState('')
  const [pasteNotice, setPasteNotice] = useState<{ tone: 'ok' | 'warn'; text: string } | null>(null)
  const [pageUrl, setPageUrl] = useState('')
  const [preview, setPreview] = useState<{ url: string; filename: string } | null>(null)

  useEffect(() => {
    if (!open) return
    setError('')
    setPasteNotice(null)
    setFiles([])
    setUploadedFileKeys(new Set())
    setTitle('')
    setCategory('')
    setContent('')
    // 打开时捕获当前页面完整地址（HashRouter 下含 #/ 路由），随工单落库
    setPageUrl(window.location.href)
  }, [open])

  const mutation = useMutation({
    mutationFn: async (): Promise<TicketItem> => {
      // 一次报齐全部缺失必填项，避免用户多次试错（对齐事件登记表单）
      const missing: string[] = []
      if (!title.trim()) missing.push('工单标题')
      if (!category) missing.push('工单分类')
      if (!content.trim()) missing.push('反馈内容')
      if (missing.length) throw new Error(`请完善必填项：${missing.join('、')}`)
      const oversized = files.find(file => file.size > MAX_ATTACHMENT_BYTES)
      if (oversized) throw new Error(`附件“${oversized.name}”超过单文件 ${MAX_ATTACHMENT_MB}MB 限制`)

      const ticket = await ticketsApi.create({
        title: title.trim(),
        content: content.trim(),
        category: category || 'other',
        pageUrl: pageUrl || null,
      })
      for (const file of files) {
        try {
          await ticketsApi.uploadAttachment(ticket.id, file)
          setUploadedFileKeys(current => new Set(current).add(fileIdentity(file)))
        } catch (cause) {
          throw new Error(
            `工单已提交，但附件“${file.name}”上传失败：${errorDetail(cause)}。可在工单详情中重试或补充。`,
            { cause },
          )
        }
      }
      return ticket
    },
    onSuccess: (ticket) => {
      queryClient.invalidateQueries({ queryKey: ['tickets'] })
      toast.success('工单提交成功', { description: `工单编号 ${ticket.ticketNo}，当前状态「待处理」` })
      onClose()
    },
    onError: (cause: any) => {
      setError(cause?.message || errorDetail(cause))
      void queryClient.invalidateQueries({ queryKey: ['tickets'] })
    },
  })

  const addFiles = (incoming: File[]) => {
    const oversized = incoming.filter(file => file.size > MAX_ATTACHMENT_BYTES)
    const accepted = incoming.filter(file => file.size <= MAX_ATTACHMENT_BYTES)
    if (oversized.length) {
      setPasteNotice({
        tone: 'warn',
        text: `已忽略 ${oversized.length} 个超过单文件 ${MAX_ATTACHMENT_MB}MB 限制的附件：${oversized.map(file => file.name).join('、')}`,
      })
    }
    setFiles(current => {
      const known = new Set(current.map(fileIdentity))
      return [...current, ...accepted.filter(file => !known.has(fileIdentity(file)))]
    })
  }

  /** 反馈内容粘贴：图片自动转附件；文本粘贴走默认行为；其余格式友好提示。 */
  const handleContentPaste = (event: React.ClipboardEvent<HTMLTextAreaElement>) => {
    const items = Array.from(event.clipboardData?.items ?? [])
    const fileItems = items.filter(item => item.kind === 'file')
    if (!fileItems.length) return // 普通文本粘贴保持默认行为

    event.preventDefault()
    const images: File[] = []
    const unsupported: string[] = []
    for (const item of fileItems) {
      const file = item.getAsFile()
      if (!file) continue
      if (isImageFile(file)) {
        images.push(new File([file], pastedImageName(file.type), { type: file.type }))
      } else {
        unsupported.push(file.name || file.type || '未知内容')
      }
    }
    if (images.length) {
      addFiles(images)
      setPasteNotice({
        tone: 'ok',
        text: `已将粘贴的 ${images.length} 张图片转为附件，提交时随工单一并上传`,
      })
    }
    if (unsupported.length) {
      setPasteNotice({
        tone: 'warn',
        text: `暂不支持直接粘贴「${unsupported.join('、')}」，请使用下方「选择多个附件」按钮添加`,
      })
    }
  }

  const openPreview = (file: File) => {
    setPreview({ url: URL.createObjectURL(file), filename: file.name })
  }

  const closePreview = () => {
    if (preview) URL.revokeObjectURL(preview.url)
    setPreview(null)
  }

  const labelClass = 'mb-1.5 block text-sm font-medium text-slate-700'
  const controlClass = 'h-10 w-full rounded-lg border border-slate-200 bg-white px-3 text-sm text-slate-700 shadow-sm transition-all placeholder:text-slate-400 focus-visible:border-ring focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring'

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="提交工单"
      description="反馈遇到的 Bug 或不好用的地方，提交后自动进入「待处理」"
      size="2xl"
      headerIcon={<Megaphone size={19} className="text-emerald-600" />}
      disableClose={mutation.isPending}
      footer={(
        <div className="flex w-full justify-center gap-3">
          <button
            type="button"
            onClick={onClose}
            disabled={mutation.isPending}
            className="h-10 rounded-lg border border-slate-200 bg-white px-6 text-sm font-medium text-slate-600 transition-all hover:bg-slate-50 active:scale-[0.98] disabled:opacity-50"
          >
            取消
          </button>
          <button
            type="button"
            onClick={() => mutation.mutate()}
            disabled={mutation.isPending}
            className="inline-flex h-10 min-w-24 items-center justify-center gap-2 rounded-lg bg-emerald-600 px-6 text-sm font-medium text-white shadow-sm transition-all hover:bg-emerald-700 active:scale-[0.98] disabled:opacity-50"
          >
            {mutation.isPending && <span className="h-4 w-4 animate-spin rounded-full border-2 border-white/40 border-t-white" />}
            提交工单
          </button>
        </div>
      )}
    >
      <div className="max-h-[68vh] space-y-5 overflow-y-auto px-1 pb-1 pr-2">
        {error && (
          <div className="flex items-center gap-2 rounded-lg border border-red-100 bg-red-50 px-3 py-2 text-sm text-red-600">
            <AlertTriangle size={15} /> {error}
          </div>
        )}

        <div className="grid grid-cols-1 gap-4 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-start">
          <div className="min-w-0">
            <label htmlFor="ticket-title" className={labelClass}>工单标题 <span className="text-red-500">*</span></label>
            <input
              id="ticket-title"
              required
              aria-required="true"
              value={title}
              onChange={event => setTitle(event.target.value)}
              placeholder="一句话概括你遇到的问题"
              className={controlClass}
            />
          </div>
          <div className="min-w-0">
            <span id="ticket-category-label" className={labelClass}>工单分类 <span className="text-red-500">*</span></span>
            <div
              role="radiogroup"
              aria-labelledby="ticket-category-label"
              className="flex flex-wrap items-center gap-1.5"
            >
              {TICKET_CATEGORY_ORDER.map(value => {
                const active = category === value
                return (
                  <button
                    key={value}
                    type="button"
                    role="radio"
                    aria-checked={active}
                    onClick={() => setCategory(value)}
                    className={`h-9 rounded-lg border px-3 text-xs font-medium transition-all ${
                      active
                        ? 'border-emerald-500 bg-emerald-50 text-emerald-700 shadow-sm'
                        : 'border-slate-200 bg-white text-slate-500 hover:border-slate-300 hover:text-slate-700'
                    }`}
                  >
                    {TICKET_CATEGORY_META[value].label}
                  </button>
                )
              })}
            </div>
          </div>
        </div>

        <div>
          <label htmlFor="ticket-content" className={labelClass}>反馈内容 <span className="text-red-500">*</span></label>
          <textarea
            id="ticket-content"
            required
            aria-required="true"
            value={content}
            onChange={event => setContent(event.target.value)}
            onPaste={handleContentPaste}
            rows={5}
            className="w-full resize-none rounded-lg border border-slate-200 bg-white px-3 py-2.5 text-sm leading-6 text-slate-700 shadow-sm transition-all placeholder:text-slate-400 focus-visible:border-ring focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            placeholder="问题的完整经过、复现步骤、影响范围……（可直接 Ctrl+V 粘贴截图）"
          />
          {pageUrl && (
            <p className="mt-1.5 text-xs text-slate-400" title={pageUrl}>
              将随工单记录当前页面：<span className="truncate font-mono">{pageUrl}</span>
            </p>
          )}
        </div>

        <div>
          <div className="mb-2 flex items-center justify-between gap-3">
            <div>
              <label className="text-sm font-medium text-slate-700">附件（可选，可多选）</label>
              <p className="mt-0.5 text-xs text-slate-400">
                支持截图、文档、日志等文件，单文件不超过 {MAX_ATTACHMENT_MB}MB；反馈内容中可直接粘贴图片
              </p>
            </div>
            {files.length > 0 && (
              <span className="text-right text-xs font-medium text-emerald-700">已选 {files.length} 个</span>
            )}
          </div>
          {pasteNotice && (
            <p
              className={`mb-2 flex items-center gap-1.5 rounded-lg px-3 py-2 text-xs ${
                pasteNotice.tone === 'ok'
                  ? 'bg-emerald-50 text-emerald-700'
                  : 'bg-amber-50 text-amber-700'
              }`}
              role="status"
            >
              {pasteNotice.tone === 'ok' ? <CheckCircle2 size={13} /> : <AlertTriangle size={13} />}
              {pasteNotice.text}
            </p>
          )}
          <label className="flex cursor-pointer items-center justify-center gap-2 rounded-xl border border-dashed border-emerald-200 bg-emerald-50/40 px-4 py-4 text-sm font-medium text-emerald-700 transition-all hover:border-emerald-300 hover:bg-emerald-50 focus-within:ring-2 focus-within:ring-ring">
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
          {files.length > 0 && (
            <div className="mt-3 overflow-hidden rounded-xl border border-slate-200 bg-white">
              {files.map((file, index) => {
                const uploaded = uploadedFileKeys.has(fileIdentity(file))
                const image = isImageFile(file)
                return (
                  <div key={fileIdentity(file)} className="group flex items-center gap-3 border-t border-slate-100 px-3 py-2.5 first:border-t-0">
                    <span className={`flex h-9 w-9 shrink-0 items-center justify-center rounded-lg ${uploaded ? 'bg-emerald-50 text-emerald-600' : 'bg-slate-100 text-slate-500'}`}>
                      {uploaded ? <CheckCircle2 size={16} /> : <Paperclip size={15} />}
                    </span>
                    <div className="min-w-0 flex-1">
                      <p className="truncate text-sm font-medium text-slate-700" title={file.name}>{file.name}</p>
                      <p className={`mt-0.5 text-xs ${uploaded ? 'text-emerald-600' : 'text-slate-400'}`}>
                        {formatBytes(file.size)}{uploaded ? ' · 已上传，重试时将自动跳过' : ''}
                      </p>
                    </div>
                    {image && !uploaded && (
                      <button
                        type="button"
                        onClick={() => openPreview(file)}
                        className="flex h-8 items-center gap-1 rounded-lg px-2 text-xs font-medium text-slate-500 transition-colors hover:bg-emerald-50 hover:text-emerald-700"
                        title={`预览 ${file.name}`}
                        aria-label={`预览 ${file.name}`}
                      >
                        <Eye size={14} /> 查看
                      </button>
                    )}
                    {!uploaded && (
                      <button
                        type="button"
                        onClick={() => setFiles(current => current.filter((_, currentIndex) => currentIndex !== index))}
                        className="flex h-8 w-8 items-center justify-center rounded-lg text-slate-400 transition-colors hover:bg-red-50 hover:text-red-600"
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

      <ImagePreviewModal
        open={Boolean(preview)}
        src={preview?.url ?? null}
        filename={preview?.filename ?? ''}
        onClose={closePreview}
      />
    </Modal>
  )
}
