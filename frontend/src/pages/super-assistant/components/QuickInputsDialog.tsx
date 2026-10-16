import { useEffect, useRef, useState } from 'react'
import {
  ArrowLeft, ChevronDown, ChevronUp, GripVertical, Pencil, Plus, Trash2, Zap,
} from 'lucide-react'
import { toast } from 'sonner'

import type { QuickInput } from '@/api/superAssistant'
import { Button } from '@/components/ui/Button'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip'
import { formatDateTime } from '@/utils/datetime'

import { DialogShell } from './AssistantConfiguration'
import { errorText } from './assistantPanelUtils'
import { QuickInputFormFields, type QuickInputErrorField } from './QuickInputFormFields'
import { contentPreview, isAboveMidline, moveItem, validateQuickInput } from './quickInputLogic'

// 自定义 MIME：行内 dragover/drop 只响应本列表发起的拖拽（文件拖入等互不干扰）
const DRAG_MIME = 'application/x-openontology-quick-input'

/**
 * 快捷内容设置弹窗：左列表 + 右详情双栏，布局语言对齐 NotificationsDialog 的
 * 「全部消息列表 / 查看指定消息」。列表整行点选 → 右侧查看/编辑/删除；
 * 列表顺序即快捷内容的展示顺序（上者优先），拖拽手柄与↑↓按钮是同一重排
 * 通道的两个入口（键盘等价路径）；重排乐观更新、失败回滚由 useQuickInputs
 * 数据源负责，这里只 toast。窄屏（<md）由选中态驱动「列表 ↔ 详情」单栏切换
 * （详情态顶部返回条）；Esc 分层：新增/编辑表单先退（丢弃未保存修改），
 * 窄屏详情查看态先回列表，其余交 Radix 关弹窗。
 */
export default function QuickInputsDialog({
  open,
  onClose,
  items,
  onCreate,
  onUpdate,
  onDelete,
  onReorder,
}: {
  open: boolean
  onClose: () => void
  items: QuickInput[]
  onCreate: (title: string, content: string) => Promise<unknown>
  onUpdate: (id: string, patch: { title?: string; content?: string }) => Promise<unknown>
  onDelete: (id: string) => Promise<void>
  onReorder: (ids: string[]) => Promise<unknown>
}) {
  const [selectedId, setSelectedId] = useState<string | null>(null)

  const [createOpen, setCreateOpen] = useState(false)
  const [createTitle, setCreateTitle] = useState('')
  const [createContent, setCreateContent] = useState('')
  const [createError, setCreateError] = useState('')
  const [createErrorField, setCreateErrorField] = useState<QuickInputErrorField | null>(null)
  const [creating, setCreating] = useState(false)

  const [editingId, setEditingId] = useState<string | null>(null)
  const [editTitle, setEditTitle] = useState('')
  const [editContent, setEditContent] = useState('')
  const [editError, setEditError] = useState('')
  const [editErrorField, setEditErrorField] = useState<QuickInputErrorField | null>(null)
  const [savingEdit, setSavingEdit] = useState(false)

  const createTitleRef = useRef<HTMLInputElement>(null)
  const createContentRef = useRef<HTMLTextAreaElement>(null)
  const createToggleRef = useRef<HTMLButtonElement>(null)
  const editTitleRef = useRef<HTMLInputElement>(null)
  const editContentRef = useRef<HTMLTextAreaElement>(null)

  const [deleting, setDeleting] = useState<QuickInput | null>(null)
  const [deletePending, setDeletePending] = useState(false)

  const [dragId, setDragId] = useState<string | null>(null)
  const [dropIndex, setDropIndex] = useState<number | null>(null)
  const [reorderPending, setReorderPending] = useState(false)

  const [prevOpen, setPrevOpen] = useState(false)
  useEffect(() => {
    // 打开时列表为空 → 详情区直接进新增表单，少一次点击
    if (open && !prevOpen && items.length === 0) setCreateOpen(true)
    setPrevOpen(open)
  }, [open, prevOpen, items.length])

  // 进入编辑即聚焦标题框：键盘用户可直接开始修改
  useEffect(() => {
    if (editingId) editTitleRef.current?.focus()
  }, [editingId])

  // 保存/取消/Esc 退出编辑后回焦详情头部「编辑」按钮：焦点所在的提交按钮随表单
  // 卸载，不主动归还的话键盘焦点会掉回弹窗容器，需从头 Tab
  const editHeaderRef = useRef<HTMLButtonElement>(null)
  const [pendingFocusEdit, setPendingFocusEdit] = useState(false)
  useEffect(() => {
    if (!editingId && pendingFocusEdit) {
      editHeaderRef.current?.focus()
      setPendingFocusEdit(false)
    }
  }, [editingId, pendingFocusEdit])

  // 取消/Esc 退出新增后回焦列头「新增」按钮（理由同上；空列表时该按钮随取消重新出现）
  const [pendingFocusCreate, setPendingFocusCreate] = useState(false)
  useEffect(() => {
    if (!createOpen && pendingFocusCreate) {
      createToggleRef.current?.focus()
      setPendingFocusCreate(false)
    }
  }, [createOpen, pendingFocusCreate])

  if (!open) return null

  const selected = selectedId ? items.find(item => item.id === selectedId) ?? null : null
  // 详情区是否呈现内容（选中/表单任一）：窄屏据此做「列表 ↔ 详情」单栏切换
  const detailActive = createOpen || editingId !== null || selected !== null

  const closeDialog = () => {
    setSelectedId(null)
    setEditingId(null)
    setDeleting(null)
    setCreateOpen(false)
    setCreateTitle('')
    setCreateContent('')
    setCreateError('')
    setCreateErrorField(null)
    setPendingFocusEdit(false)
    setPendingFocusCreate(false)
    setDragId(null)
    setDropIndex(null)
    onClose()
  }

  // 错误随输入清除：任一字段变化即撤销当前报错（含服务端错误），下次提交重新校验
  const changeCreateTitle = (value: string) => {
    setCreateTitle(value)
    if (createError) { setCreateError(''); setCreateErrorField(null) }
  }
  const changeCreateContent = (value: string) => {
    setCreateContent(value)
    if (createError) { setCreateError(''); setCreateErrorField(null) }
  }
  const changeEditTitle = (value: string) => {
    setEditTitle(value)
    if (editError) { setEditError(''); setEditErrorField(null) }
  }
  const changeEditContent = (value: string) => {
    setEditContent(value)
    if (editError) { setEditError(''); setEditErrorField(null) }
  }

  // 退出新增：草稿丢弃，详情回未选中占位（焦点回列头「新增」按钮）
  const cancelCreate = () => {
    setCreateOpen(false)
    setCreateTitle('')
    setCreateContent('')
    setCreateError('')
    setCreateErrorField(null)
    setPendingFocusCreate(true)
  }

  const submitCreate = async () => {
    if (creating) return
    const validation = validateQuickInput(createTitle, createContent)
    if (validation) {
      setCreateError(validation.message)
      setCreateErrorField(validation.field)
      ;(validation.field === 'title' ? createTitleRef : createContentRef).current?.focus()
      return
    }
    setCreating(true)
    setCreateError('')
    setCreateErrorField(null)
    try {
      await onCreate(createTitle.trim(), createContent.trim())
      toast.success('快捷内容已添加', { description: '在输入框点击 ⚡ 即可插入光标处。' })
      // 连续录入：保持表单展开、清空字段并回焦标题
      setCreateTitle('')
      setCreateContent('')
      createTitleRef.current?.focus()
    }
    catch (err) {
      setCreateError(errorText(err, '添加失败'))
      setCreateErrorField(null)
    }
    finally {
      setCreating(false)
    }
  }

  const startEdit = (item: QuickInput) => {
    setEditingId(item.id)
    setEditTitle(item.title)
    setEditContent(item.content)
    setEditError('')
    setEditErrorField(null)
  }

  // 退出编辑：未保存修改丢弃，详情回查看态（焦点回「编辑」按钮）
  const exitEdit = () => {
    setEditingId(null)
    setEditError('')
    setEditErrorField(null)
    setPendingFocusEdit(true)
  }

  const submitEdit = async () => {
    if (!editingId || savingEdit) return
    const validation = validateQuickInput(editTitle, editContent)
    if (validation) {
      setEditError(validation.message)
      setEditErrorField(validation.field)
      ;(validation.field === 'title' ? editTitleRef : editContentRef).current?.focus()
      return
    }
    setSavingEdit(true)
    setEditError('')
    setEditErrorField(null)
    try {
      await onUpdate(editingId, { title: editTitle.trim(), content: editContent.trim() })
      setEditingId(null)
      setPendingFocusEdit(true)
      toast.success('快捷内容已保存')
    }
    catch (err) {
      setEditError(errorText(err, '保存失败'))
      setEditErrorField(null)
    }
    finally {
      setSavingEdit(false)
    }
  }

  const confirmDelete = async () => {
    if (!deleting || deletePending) return
    setDeletePending(true)
    try {
      await onDelete(deleting.id)
      setDeleting(null)
      // 删除成功清空选中：详情回未选中占位
      setSelectedId(null)
      toast.success('快捷内容已删除')
    }
    catch (err) {
      toast.error(errorText(err, '删除失败'))
    }
    finally {
      setDeletePending(false)
    }
  }

  // 点选列表行 → 详情查看；新增/编辑草稿不跨行保留（与 Esc 退出同语义：丢弃）
  const selectItem = (item: QuickInput) => {
    if (createOpen) {
      setCreateOpen(false)
      setCreateTitle('')
      setCreateContent('')
      setCreateError('')
      setCreateErrorField(null)
    }
    if (editingId) {
      setEditingId(null)
      setEditError('')
      setEditErrorField(null)
    }
    setSelectedId(item.id)
  }

  // 窄屏详情态返回条：清空选中与表单临时态，回列表
  const backToList = () => {
    if (createOpen) {
      setCreateOpen(false)
      setCreateTitle('')
      setCreateContent('')
      setCreateError('')
      setCreateErrorField(null)
    }
    if (editingId) {
      setEditingId(null)
      setEditError('')
      setEditErrorField(null)
    }
    setSelectedId(null)
  }

  // Esc 分层回退：新增/编辑表单先退（丢弃未保存修改），窄屏详情查看态先回列表；
  // 其余交 Radix 关弹窗。对齐 NotificationsDialog.handleDialogEscape。
  const handleDialogEscape = (event: KeyboardEvent) => {
    if (createOpen) {
      event.preventDefault()
      cancelCreate()
      return
    }
    if (editingId) {
      event.preventDefault()
      exitEdit()
      return
    }
    if (selected && window.matchMedia('(max-width: 767px)').matches) {
      event.preventDefault()
      setSelectedId(null)
    }
  }

  const commitReorder = (nextIds: string[]) => {
    if (reorderPending) return
    setReorderPending(true)
    onReorder(nextIds)
      .catch(err => toast.error(errorText(err, '顺序保存失败')))
      .finally(() => setReorderPending(false))
  }

  const moveBy = (index: number, delta: -1 | 1) => {
    const next = moveItem(items, index, index + delta)
    if (next.length !== items.length || next.some((item, i) => item.id !== items[i].id)) {
      commitReorder(next.map(item => item.id))
    }
  }

  const dragFrom = dragId ? items.findIndex(item => item.id === dragId) : -1
  const effectiveTo = dropIndex === null ? -1 : (dropIndex > dragFrom ? dropIndex - 1 : dropIndex)
  const dragging = dragId !== null && effectiveTo !== dragFrom
  const lineClass = 'h-0.5 rounded-full bg-brand'

  const handleDragOver = (event: React.DragEvent, index: number) => {
    // 重排在途时不给投放反馈：指示线会暗示“松手即生效”，但 commitReorder 会拒绝
    if (reorderPending) return
    if (!event.dataTransfer.types.includes(DRAG_MIME) || dragId === null) return
    event.preventDefault()
    const rect = event.currentTarget.getBoundingClientRect()
    const target = isAboveMidline(event.clientY, rect) ? index : index + 1
    if (target !== dropIndex) setDropIndex(target)
  }

  const handleDrop = (event: React.DragEvent) => {
    if (!event.dataTransfer.types.includes(DRAG_MIME) || dragId === null || dropIndex === null) return
    event.preventDefault()
    const next = moveItem(items, dragFrom, effectiveTo)
    setDragId(null)
    setDropIndex(null)
    if (next.some((item, i) => item.id !== items[i].id)) commitReorder(next.map(item => item.id))
  }

  return (
    <>
      <TooltipProvider delayDuration={300}>
        <DialogShell
          size="large"
          title="快捷内容设置"
          description="在输入框点击 ⚡ 即可将所选内容插入光标处；列表顺序即展示顺序，可拖拽或用箭头按钮调整。"
          onClose={closeDialog}
          icon={<Zap size={18} />}
          onEscapeKeyDown={handleDialogEscape}
          // 误触遮罩不应关弹窗：新增/编辑草稿会随关闭一并丢失；只挡遮罩——
          // Esc 分层回退与右上关闭按钮保持可用（工作台型弹窗惯例）
          onInteractOutside={event => event.preventDefault()}
          contentClassName="h-[min(85dvh,42rem)]"
        >
          <div className="flex min-h-0 flex-1" data-testid="quick-inputs-dialog">
            <p className="sr-only" aria-live="polite">{dragId ? '正在拖拽调整顺序，松开完成' : ''}</p>

            {/* —— 左：列表列（宽屏固定 20rem；窄屏列表态全宽） —— */}
            <div
              className={`${detailActive ? 'hidden md:flex' : 'flex'} w-full shrink-0 flex-col border-r border-[var(--color-border)] md:w-[20rem]`}
            >
              {/* 列表列头：计数 + 新增入口；h-10 与右侧详情头同高——两列分割线落在同一水平线 */}
              <div className="flex h-10 shrink-0 items-center gap-2 border-b border-[var(--color-border)] px-3">
                <p className="min-w-0 flex-1 truncate text-xs text-[var(--color-text-tertiary)]">
                  共 {items.length} 条
                </p>
                {/* 空列表自动进新增态时藏起入口：当前已是新增表单，按钮无目标 */}
                {!(items.length === 0 && createOpen) && (
                  <Tooltip>
                    <TooltipTrigger asChild>
                      <button
                        type="button"
                        ref={createToggleRef}
                        onClick={() => setCreateOpen(true)}
                        aria-label="新增快捷内容"
                        data-testid="quick-input-create-toggle"
                        className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md text-[var(--color-text-secondary)] transition-colors hover:bg-[var(--color-bg-hover)] hover:text-[var(--color-text-primary)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                      >
                        <Plus size={15} />
                      </button>
                    </TooltipTrigger>
                    <TooltipContent align="end">新增快捷内容</TooltipContent>
                  </Tooltip>
                )}
              </div>

              <div className="scrollbar-none min-h-0 flex-1 overflow-y-auto p-2">
                {items.length === 0 ? (
                  <div className="flex flex-col items-center gap-2 px-4 py-10 text-center">
                    <Zap size={20} className="text-[var(--color-text-tertiary)]" />
                    <p className="text-xs leading-5 text-[var(--color-text-secondary)]">还没有快捷内容，添加一条常用内容试试。</p>
                  </div>
                ) : (
                  <div className="space-y-1" role="list" aria-label="快捷内容列表，拖拽或使用上移下移调整展示优先级">
                    {items.map((item, index) => (
                      <div key={item.id}>
                        {dragging && dropIndex === index && <div className={lineClass} />}
                        <div
                          role="listitem"
                          data-testid="quick-input-row"
                          onDragOver={event => handleDragOver(event, index)}
                          onDrop={handleDrop}
                          className={`flex items-center gap-1 rounded-lg border px-2.5 py-2 transition-colors ${
                            selectedId === item.id
                              ? 'border-transparent bg-brand-soft'
                              : dragId === item.id
                                ? 'border-transparent bg-[var(--color-bg-elevated)] opacity-45'
                                : 'border-transparent hover:bg-[var(--color-bg-hover)]'
                          }`}
                        >
                          <span
                            draggable
                            onDragStart={event => {
                              event.dataTransfer.effectAllowed = 'move'
                              event.dataTransfer.setData(DRAG_MIME, item.id)
                              setDragId(item.id)
                            }}
                            onDragEnd={() => { setDragId(null); setDropIndex(null) }}
                            aria-label={`拖拽调整 ${item.title} 的展示顺序（键盘用户请使用上移/下移按钮）`}
                            title="拖拽调整展示顺序"
                            className="flex h-8 w-7 shrink-0 cursor-grab items-center justify-center rounded text-[var(--color-text-tertiary)] hover:text-[var(--color-text-secondary)] active:cursor-grabbing"
                          >
                            <GripVertical size={14} />
                          </span>
                          <span className="w-6 shrink-0 text-center font-mono text-xs text-[var(--color-text-tertiary)]">#{index + 1}</span>
                          {/* 选中区是独立 button（不可与上移/下移按钮嵌套）；全文在详情区展示，行上不再挂 title 悬浮提示 */}
                          <button
                            type="button"
                            onClick={() => selectItem(item)}
                            aria-current={selectedId === item.id ? 'true' : undefined}
                            className="min-w-0 flex-1 rounded-md px-1.5 py-0.5 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                          >
                            <span className="block truncate text-xs font-medium text-[var(--color-text-primary)]">{item.title}</span>
                            <span className="block truncate text-xs text-[var(--color-text-tertiary)]">{contentPreview(item.content)}</span>
                          </button>
                          <div className="flex shrink-0 items-center gap-0.5">
                            <button
                              type="button"
                              onClick={() => moveBy(index, -1)}
                              disabled={index === 0 || reorderPending}
                              aria-label={`上移 ${item.title}`}
                              data-testid="quick-input-move-up"
                              className="flex h-7 w-7 items-center justify-center rounded-md text-[var(--color-text-tertiary)] transition-colors hover:bg-[var(--color-bg-hover)] hover:text-[var(--color-text-secondary)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-30"
                            >
                              <ChevronUp size={13} />
                            </button>
                            <button
                              type="button"
                              onClick={() => moveBy(index, 1)}
                              disabled={index === items.length - 1 || reorderPending}
                              aria-label={`下移 ${item.title}`}
                              data-testid="quick-input-move-down"
                              className="flex h-7 w-7 items-center justify-center rounded-md text-[var(--color-text-tertiary)] transition-colors hover:bg-[var(--color-bg-hover)] hover:text-[var(--color-text-secondary)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-30"
                            >
                              <ChevronDown size={13} />
                            </button>
                          </div>
                        </div>
                        {index === items.length - 1 && dragging && dropIndex === items.length && <div className={lineClass} />}
                      </div>
                    ))}
                  </div>
                )}
              </div>
            </div>

            {/* —— 右：详情区（查看 / 编辑 / 新增 / 占位；窄屏详情态全宽） —— */}
            <div
              className={`${detailActive ? 'flex' : 'hidden md:flex'} min-w-0 flex-1 flex-col`}
              data-testid="quick-input-detail"
            >
              {/* 窄屏单栏：详情态统一返回条（查看/编辑/新增都可回列表） */}
              {detailActive && (
                <div className="flex shrink-0 border-b border-[var(--color-border)] md:hidden">
                  <button
                    type="button"
                    onClick={backToList}
                    aria-label="返回快捷内容列表"
                    data-testid="quick-input-back-list"
                    className="flex items-center gap-1 px-3 py-2 text-xs text-[var(--color-text-secondary)] transition-colors hover:bg-[var(--color-bg-hover)] hover:text-[var(--color-text-primary)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  >
                    <ArrowLeft size={12} /> 返回列表
                  </button>
                </div>
              )}

              {createOpen ? (
                <form
                  onSubmit={event => { event.preventDefault(); void submitCreate() }}
                  className="flex min-h-0 flex-1 flex-col"
                  data-testid="quick-input-create-form"
                >
                  <div className="scrollbar-none min-h-0 flex-1 overflow-y-auto p-4">
                    <h3 className="text-sm font-semibold text-[var(--color-text-primary)]">新增快捷内容</h3>
                    <div className="mt-3">
                      <QuickInputFormFields
                        idPrefix="quick-input-create"
                        title={createTitle}
                        content={createContent}
                        error={createError}
                        errorField={createErrorField}
                        onTitleChange={changeCreateTitle}
                        onContentChange={changeCreateContent}
                        titleRef={createTitleRef}
                        contentRef={createContentRef}
                      />
                    </div>
                  </div>
                  <div className="flex shrink-0 justify-end gap-2 border-t border-[var(--color-border)] px-4 py-3">
                    <Button type="button" variant="outline" onClick={cancelCreate}>取消</Button>
                    <Button type="submit" loading={creating} data-testid="quick-input-create-submit">添加</Button>
                  </div>
                </form>
              ) : editingId ? (
                <form
                  onSubmit={event => { event.preventDefault(); void submitEdit() }}
                  className="flex min-h-0 flex-1 flex-col"
                  data-testid="quick-input-edit-form"
                >
                  <div className="scrollbar-none min-h-0 flex-1 overflow-y-auto p-4">
                    <h3 className="text-sm font-semibold text-[var(--color-text-primary)]">编辑快捷内容</h3>
                    <div className="mt-3">
                      <QuickInputFormFields
                        idPrefix="quick-input-edit"
                        title={editTitle}
                        content={editContent}
                        error={editError}
                        errorField={editErrorField}
                        onTitleChange={changeEditTitle}
                        onContentChange={changeEditContent}
                        titleRef={editTitleRef}
                        contentRef={editContentRef}
                      />
                    </div>
                  </div>
                  <div className="flex shrink-0 justify-end gap-2 border-t border-[var(--color-border)] px-4 py-3">
                    <Button type="button" variant="outline" onClick={exitEdit}>取消</Button>
                    <Button type="submit" loading={savingEdit} data-testid="quick-input-edit-submit">保存</Button>
                  </div>
                </form>
              ) : selected ? (
                <>
                  {/* 详情头：标题 + 更新时间 meta + 操作图标组；h-10 与左列列头同高——两列分割线落在同一水平线（对齐 NotificationsDialog 详情头同排手法） */}
                  <div className="flex h-10 shrink-0 items-center gap-1 border-b border-[var(--color-border)] px-4">
                    <h3 className="min-w-0 flex-1 truncate text-sm font-semibold text-[var(--color-text-primary)]" title={selected.title}>{selected.title}</h3>
                    <span className="hidden shrink-0 text-xs text-[var(--color-text-tertiary)] lg:inline" data-testid="quick-input-detail-meta">
                      更新于 {formatDateTime(selected.updated_at)}
                    </span>
                    <div className="ml-auto flex shrink-0 items-center gap-0.5">
                      <Tooltip>
                        <TooltipTrigger asChild>
                          <button
                            type="button"
                            ref={editHeaderRef}
                            onClick={() => startEdit(selected)}
                            aria-label={`编辑 ${selected.title}`}
                            data-testid="quick-input-edit"
                            className="flex h-7 w-7 items-center justify-center rounded-md text-[var(--color-text-secondary)] transition-colors hover:bg-[var(--color-bg-hover)] hover:text-[var(--color-text-primary)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                          >
                            <Pencil size={14} />
                          </button>
                        </TooltipTrigger>
                        <TooltipContent align="end">编辑</TooltipContent>
                      </Tooltip>
                      <Tooltip>
                        <TooltipTrigger asChild>
                          <button
                            type="button"
                            onClick={() => setDeleting(selected)}
                            aria-label={`删除 ${selected.title}`}
                            data-testid="quick-input-delete"
                            className="flex h-7 w-7 items-center justify-center rounded-md text-[var(--color-text-secondary)] transition-colors hover:bg-[var(--color-danger-bg)] hover:text-[var(--color-danger)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                          >
                            <Trash2 size={14} />
                          </button>
                        </TooltipTrigger>
                        <TooltipContent align="end">删除</TooltipContent>
                      </Tooltip>
                    </div>
                  </div>
                  {/* 正文完整展示：单行截断、长内容不可读正是旧布局被诟病的核心 */}
                  <div className="scrollbar-none min-h-0 flex-1 overflow-y-auto p-4">
                    <p className="whitespace-pre-wrap break-words text-sm leading-6 text-[var(--color-text-primary)]">{selected.content}</p>
                  </div>
                </>
              ) : (
                <div className="flex flex-1 items-center justify-center p-6">
                  <p className="max-w-56 text-center text-xs leading-5 text-[var(--color-text-tertiary)]" data-testid="quick-input-detail-empty">
                    选择左侧快捷内容查看或编辑
                  </p>
                </div>
              )}
            </div>
          </div>
        </DialogShell>
      </TooltipProvider>

      <ConfirmDialog
        open={deleting !== null}
        onClose={() => setDeleting(null)}
        onConfirm={() => void confirmDelete()}
        loading={deletePending}
        title="删除快捷内容？"
        description={deleting ? `「${deleting.title}」将从快捷内容列表中移除，不影响已发送的消息。` : ''}
        confirmText="删除"
        variant="danger"
      />
    </>
  )
}
