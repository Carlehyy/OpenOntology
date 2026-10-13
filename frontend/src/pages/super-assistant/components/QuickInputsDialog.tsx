import { useEffect, useState } from 'react'
import {
  ChevronDown, ChevronUp, GripVertical, Loader2, Pencil, Plus, Trash2, X, Zap,
} from 'lucide-react'
import { toast } from 'sonner'

import type { QuickInput } from '@/api/superAssistant'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'

import { DialogShell } from './AssistantConfiguration'
import { errorText } from './assistantPanelUtils'
import { QuickInputFormFields } from './QuickInputFormFields'
import { contentPreview, isAboveMidline, moveItem, validateQuickInput } from './quickInputLogic'

// 自定义 MIME：行内 dragover/drop 只响应本列表发起的拖拽（文件拖入等互不干扰）
const DRAG_MIME = 'application/x-openontology-quick-input'

/**
 * 快捷内容设置弹窗：新增/编辑/删除 + 展示优先级排序。
 * 列表顺序即小卡片的展示顺序（上者优先），拖拽手柄与↑↓按钮是同一重排
 * 通道的两个入口（键盘等价路径）；重排乐观更新、失败回滚由 useQuickInputs
 * 数据源负责，这里只 toast。布局语言对齐 ScheduledTasksDialog。
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
  const [createOpen, setCreateOpen] = useState(false)
  const [createTitle, setCreateTitle] = useState('')
  const [createContent, setCreateContent] = useState('')
  const [createError, setCreateError] = useState('')
  const [creating, setCreating] = useState(false)

  const [editingId, setEditingId] = useState<string | null>(null)
  const [editTitle, setEditTitle] = useState('')
  const [editContent, setEditContent] = useState('')
  const [editError, setEditError] = useState('')
  const [savingEdit, setSavingEdit] = useState(false)

  const [deleting, setDeleting] = useState<QuickInput | null>(null)
  const [deletePending, setDeletePending] = useState(false)

  const [dragId, setDragId] = useState<string | null>(null)
  const [dropIndex, setDropIndex] = useState<number | null>(null)
  const [reorderPending, setReorderPending] = useState(false)

  const [prevOpen, setPrevOpen] = useState(false)
  useEffect(() => {
    // 打开时列表为空 → 直接展开新增表单，少一次点击
    if (open && !prevOpen && items.length === 0) setCreateOpen(true)
    setPrevOpen(open)
  }, [open, prevOpen, items.length])

  if (!open) return null

  const closeDialog = () => {
    setEditingId(null)
    setDeleting(null)
    setCreateOpen(false)
    setCreateTitle('')
    setCreateContent('')
    setCreateError('')
    setDragId(null)
    setDropIndex(null)
    onClose()
  }

  const submitCreate = async () => {
    if (creating) return
    const validation = validateQuickInput(createTitle, createContent)
    if (validation) { setCreateError(validation); return }
    setCreating(true)
    setCreateError('')
    try {
      await onCreate(createTitle.trim(), createContent.trim())
      toast.success('快捷内容已添加', { description: '在输入框点击 ⚡ 即可插入光标处。' })
      // 连续录入：保持表单展开、清空字段并回焦标题
      setCreateTitle('')
      setCreateContent('')
    }
    catch (err) {
      setCreateError(errorText(err, '添加失败'))
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
  }

  const submitEdit = async () => {
    if (!editingId || savingEdit) return
    const validation = validateQuickInput(editTitle, editContent)
    if (validation) { setEditError(validation); return }
    setSavingEdit(true)
    setEditError('')
    try {
      await onUpdate(editingId, { title: editTitle.trim(), content: editContent.trim() })
      setEditingId(null)
      toast.success('快捷内容已保存')
    }
    catch (err) {
      setEditError(errorText(err, '保存失败'))
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
      toast.success('快捷内容已删除')
    }
    catch (err) {
      toast.error(errorText(err, '删除失败'))
    }
    finally {
      setDeletePending(false)
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
      <DialogShell
        size="large"
        title="快捷内容设置"
        description="在输入框点击 ⚡ 选择标题即可把内容插入光标处；列表越靠前展示越靠前，拖拽或用 ↑↓ 调整。"
        onClose={closeDialog}
        icon={<Zap size={18} />}
      >
        <div className="flex min-h-0 flex-1 flex-col" data-testid="quick-inputs-dialog">
          <div className="min-h-0 flex-1 space-y-4 overflow-y-auto p-5">
            <p className="sr-only" aria-live="polite">{dragId ? '正在拖拽调整顺序，松开完成' : ''}</p>

            {createOpen ? (
              <div className="rounded-xl border border-border p-3.5" data-testid="quick-input-create-form">
                <QuickInputFormFields
                  idPrefix="quick-input-create"
                  title={createTitle}
                  content={createContent}
                  onTitleChange={setCreateTitle}
                  onContentChange={setCreateContent}
                />
                {createError && <p className="mt-2 text-xs text-[var(--color-danger)]">{createError}</p>}
                <div className="mt-3 flex justify-end gap-2">
                  <button
                    type="button"
                    onClick={() => { setCreateOpen(false); setCreateError('') }}
                    className="inline-flex h-8 items-center rounded-lg border border-border px-3 text-xs text-[var(--color-text-secondary)] transition-colors hover:bg-[var(--color-bg-hover)]"
                  >
                    收起
                  </button>
                  <button
                    type="button"
                    disabled={creating}
                    onClick={() => void submitCreate()}
                    data-testid="quick-input-create-submit"
                    className="inline-flex h-8 items-center gap-1.5 rounded-lg bg-brand px-4 text-xs font-medium text-white transition-colors hover:bg-brand-deep focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50"
                  >
                    {creating ? <Loader2 size={13} className="animate-spin" /> : <Plus size={13} />} 添加
                  </button>
                </div>
              </div>
            ) : (
              <button
                type="button"
                onClick={() => setCreateOpen(true)}
                data-testid="quick-input-create-toggle"
                className="flex h-9 w-full items-center justify-center gap-1.5 rounded-lg border border-dashed border-border text-xs font-medium text-[var(--color-text-secondary)] transition-colors hover:border-brand hover:bg-brand-soft hover:text-brand-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                <Plus size={14} /> 新增快捷内容
              </button>
            )}

            {items.length > 0 && (
              <div>
                <p className="px-1 pb-2 text-[11px] text-[var(--color-text-tertiary)]">
                  展示优先级（上者优先） · 共 {items.length} 条
                </p>
                <div className="space-y-1" role="list" aria-label="快捷内容列表，拖拽或使用上移下移调整展示优先级">
                  {items.map((item, index) => (
                    <div key={item.id}>
                      {dragging && dropIndex === index && <div className={lineClass} />}
                      <div
                        role="listitem"
                        data-testid="quick-input-row"
                        onDragOver={event => handleDragOver(event, index)}
                        onDrop={handleDrop}
                        className={`rounded-lg border transition-colors ${
                          editingId === item.id
                            ? 'border-brand bg-brand-soft/40 p-3.5'
                            : dragId === item.id
                              ? 'border-border bg-[var(--color-bg-elevated)] opacity-45'
                              : 'border-border bg-[var(--color-bg-elevated)] px-2.5 py-2 hover:border-brand/50'
                        }`}
                      >
                        {editingId === item.id ? (
                          <div>
                            <QuickInputFormFields
                              idPrefix="quick-input-edit"
                              title={editTitle}
                              content={editContent}
                              onTitleChange={setEditTitle}
                              onContentChange={setEditContent}
                            />
                            {editError && <p className="mt-2 text-xs text-[var(--color-danger)]">{editError}</p>}
                            <div className="mt-3 flex justify-end gap-2">
                              <button
                                type="button"
                                onClick={() => setEditingId(null)}
                                className="inline-flex h-8 items-center gap-1 rounded-lg border border-border px-3 text-xs text-[var(--color-text-secondary)] transition-colors hover:bg-[var(--color-bg-hover)]"
                              >
                                <X size={12} /> 取消
                              </button>
                              <button
                                type="button"
                                disabled={savingEdit}
                                onClick={() => void submitEdit()}
                                data-testid="quick-input-edit-submit"
                                className="inline-flex h-8 items-center gap-1.5 rounded-lg bg-brand px-4 text-xs font-medium text-white transition-colors hover:bg-brand-deep focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50"
                              >
                                {savingEdit ? <Loader2 size={13} className="animate-spin" /> : <Pencil size={12} />} 保存
                              </button>
                            </div>
                          </div>
                        ) : (
                          <div className="flex items-center gap-1">
                            <span
                              draggable
                              onDragStart={event => {
                                event.dataTransfer.effectAllowed = 'move'
                                event.dataTransfer.setData(DRAG_MIME, item.id)
                                setDragId(item.id)
                              }}
                              onDragEnd={() => { setDragId(null); setDropIndex(null) }}
                              aria-label={`拖拽调整 ${item.title} 的展示顺序`}
                              title="拖拽调整展示顺序"
                              className="flex h-8 w-6 shrink-0 cursor-grab items-center justify-center rounded text-[var(--color-text-tertiary)] hover:text-[var(--color-text-secondary)] active:cursor-grabbing"
                            >
                              <GripVertical size={14} />
                            </span>
                            <span className="w-6 shrink-0 text-center font-mono text-[10px] text-[var(--color-text-tertiary)]">#{index + 1}</span>
                            <div className="min-w-0 flex-1" title={item.content}>
                              <p className="truncate text-xs font-medium text-[var(--color-text-primary)]">{item.title}</p>
                              <p className="truncate text-[11px] text-[var(--color-text-tertiary)]">{contentPreview(item.content)}</p>
                            </div>
                            <div className="flex shrink-0 items-center gap-0.5">
                              <button
                                type="button"
                                onClick={() => moveBy(index, -1)}
                                disabled={index === 0 || reorderPending}
                                aria-label={`上移 ${item.title}`}
                                title="上移（提高展示优先级）"
                                data-testid="quick-input-move-up"
                                className="flex h-7 w-7 items-center justify-center rounded-md text-[var(--color-text-tertiary)] transition-colors hover:bg-[var(--color-bg-hover)] hover:text-[var(--color-text-secondary)] disabled:opacity-30"
                              >
                                <ChevronUp size={13} />
                              </button>
                              <button
                                type="button"
                                onClick={() => moveBy(index, 1)}
                                disabled={index === items.length - 1 || reorderPending}
                                aria-label={`下移 ${item.title}`}
                                title="下移（降低展示优先级）"
                                data-testid="quick-input-move-down"
                                className="flex h-7 w-7 items-center justify-center rounded-md text-[var(--color-text-tertiary)] transition-colors hover:bg-[var(--color-bg-hover)] hover:text-[var(--color-text-secondary)] disabled:opacity-30"
                              >
                                <ChevronDown size={13} />
                              </button>
                              <button
                                type="button"
                                onClick={() => startEdit(item)}
                                aria-label={`编辑 ${item.title}`}
                                title="编辑"
                                data-testid="quick-input-edit"
                                className="flex h-7 w-7 items-center justify-center rounded-md text-[var(--color-text-tertiary)] transition-colors hover:bg-[var(--color-bg-hover)] hover:text-[var(--color-text-secondary)]"
                              >
                                <Pencil size={13} />
                              </button>
                              <button
                                type="button"
                                onClick={() => setDeleting(item)}
                                aria-label={`删除 ${item.title}`}
                                title="删除"
                                data-testid="quick-input-delete"
                                className="flex h-7 w-7 items-center justify-center rounded-md text-[var(--color-text-tertiary)] transition-colors hover:bg-[var(--color-danger-bg)] hover:text-[var(--color-danger)]"
                              >
                                <Trash2 size={13} />
                              </button>
                            </div>
                          </div>
                        )}
                      </div>
                      {index === items.length - 1 && dragging && dropIndex === items.length && <div className={lineClass} />}
                    </div>
                  ))}
                </div>
              </div>
            )}

            {items.length === 0 && !createOpen && (
              <div className="flex flex-col items-center gap-2 py-10 text-center">
                <Zap size={20} className="text-[var(--color-text-tertiary)]" />
                <p className="text-sm text-[var(--color-text-secondary)]">还没有快捷内容，添加一条常用内容试试。</p>
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
        title="删除快捷内容？"
        description={deleting ? `「${deleting.title}」将从小卡片中移除，不影响已发送的消息。` : ''}
        confirmText="删除"
        variant="danger"
      />
    </>
  )
}
