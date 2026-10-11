import { useEffect, useRef, useState } from 'react'
import { Loader2, Pencil } from 'lucide-react'

import { superAssistantApi, type SuperConversation } from '@/api/superAssistant'
import { toast } from 'sonner'
import { errorText } from './assistantPanelUtils'
import { DialogShell } from './AssistantConfiguration'

/** 会话行右键「重命名会话」弹窗：行级改名（区别于聊天区顶部的选中会话标题编辑）。
    受控 open 常挂：open=false 时 Radix 不渲染弹层内容但组件树保留，
    避免条件渲染卸载 Radix Dialog 的 overlay 残留竞态（关闭后全页不可点）。 */
export default function ConversationRenameDialog({
  open, conversation, onClose, onSaved,
}: {
  open: boolean
  conversation: SuperConversation | null
  onClose: () => void
  onSaved: (updated: SuperConversation) => void
}) {
  const [title, setTitle] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const inputRef = useRef<HTMLInputElement>(null)
  // 保存竞态防线：飞行的 PATCH 完成时目标会话可能已被换掉（用户关掉又右键了
  // 另一会话）——对比 ref 才决定是否收尾，避免甩关新弹窗并弹错 toast。
  const conversationRef = useRef(conversation)
  conversationRef.current = conversation

  // 每次打开按目标会话重置表单（常挂组件不能依赖挂载初值）
  useEffect(() => {
    if (!open || !conversation) return
    setTitle(conversation.title)
    setError('')
    setBusy(false)
    // 打开即聚焦并全选：改名的常见路径是微调尾部，全选便于整段覆盖
    requestAnimationFrame(() => {
      inputRef.current?.focus()
      inputRef.current?.select()
    })
  }, [open, conversation])

  const save = async () => {
    const target = conversationRef.current
    if (!target || busy) return
    const next = title.trim()
    if (!next) {
      setError('会话名称不能为空')
      return
    }
    if (next === target.title) {
      onClose()
      return
    }
    setBusy(true)
    setError('')
    try {
      const updated = await superAssistantApi.updateConversation(target.id, { title: next })
      onSaved(updated)
      // 保存期间目标已被替换：静默收尾，不动新弹窗
      if (conversationRef.current?.id !== target.id) {
        setBusy(false)
        return
      }
      toast.success('会话名称已保存')
      onClose()
    } catch (cause) {
      // 目标会话已被对端删除（404）：侧栏行已消失，弹窗滞留只会困惑，直接收起
      const status = (cause as { status?: number })?.status
      if (status === 404) {
        toast.error('会话已被删除，无法重命名')
        onClose()
        return
      }
      if (conversationRef.current?.id === target.id) {
        setError(errorText(cause, '名称保存失败'))
        setBusy(false)
      }
    }
  }

  return (
    <DialogShell
      open={open && conversation !== null}
      title="重命名会话"
      description="修改侧栏与聊天区显示的会话名称，不影响会话内容。"
      onClose={onClose}
      icon={<Pencil size={18} />}
    >
      <form
        className="flex-1 space-y-4 overflow-y-auto p-5"
        onSubmit={event => { event.preventDefault(); void save() }}
      >
        <label className="block text-xs text-[var(--color-text-secondary)]">会话名称 <span className="text-[var(--color-danger)]">*</span>
          <input
            ref={inputRef}
            value={title}
            maxLength={200}
            onChange={event => setTitle(event.target.value)}
            data-rename-input
            className="mt-1.5 min-h-11 w-full rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-base)] px-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring"
          />
        </label>
        {error && <p role="alert" className="text-xs text-[var(--color-danger)]">{error}</p>}
      </form>
      <footer className="flex justify-center gap-2 border-t border-[var(--color-border)] px-5 py-4">
        <button
          type="button"
          onClick={onClose}
          disabled={busy}
          className="min-h-10 rounded-lg px-4 text-xs text-[var(--color-text-secondary)] hover:bg-[var(--color-bg-hover)]"
        >
          取消
        </button>
        <button
          type="button"
          onClick={() => void save()}
          disabled={busy}
          className="inline-flex min-h-10 items-center gap-2 rounded-lg bg-brand px-4 text-xs font-medium text-white hover:bg-brand-deep disabled:opacity-50"
        >
          {busy && <Loader2 size={13} className="animate-spin" />} 保存
        </button>
      </footer>
    </DialogShell>
  )
}
