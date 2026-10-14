import { Loader2, Settings2, Zap } from 'lucide-react'

import type { QuickInput } from '@/api/superAssistant'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'

import { contentPreview } from './quickInputLogic'

/**
 * 输入框工具栏的「快捷输入」小卡片：列出快捷内容标题（按展示优先级），
 * 点击即把内容详情插入输入框光标处；右上角文字入口跳转设置弹窗。
 * 结构对齐「我发送的消息」卡片（受控 Popover + header + 滚动列表），
 * 插入锚点与光标回填由页面统一处理（与语音听写同一机制）。
 */
export default function QuickInputMenu({
  open,
  onOpenChange,
  items,
  loading,
  error,
  onRetry,
  onInsert,
  onOpenSettings,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  items: QuickInput[]
  loading: boolean
  error: string
  onRetry: () => void
  onInsert: (content: string) => void
  onOpenSettings: () => void
}) {
  return (
    <Popover open={open} onOpenChange={onOpenChange}>
      <PopoverTrigger asChild>
        <button
          type="button"
          title="快捷内容：点击插入常用内容到光标处"
          aria-label="快捷内容"
          aria-haspopup="dialog"
          data-testid="super-assistant-quick-input-button"
          className={`flex h-8 w-8 shrink-0 items-center justify-center rounded-lg transition-colors active:scale-[0.98] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${
            open
              ? 'bg-brand-soft text-brand-ink'
              : 'text-[var(--color-text-tertiary)] hover:bg-[var(--color-bg-hover)] hover:text-brand-ink'
          }`}
        >
          <Zap size={16} />
        </button>
      </PopoverTrigger>
      <PopoverContent
        side="top"
        align="start"
        data-testid="super-assistant-quick-input-menu"
        className="w-72 overflow-hidden rounded-lg border-border p-0"
      >
        <div className="flex items-center justify-between border-b border-border px-3 py-2">
          <span className="text-xs font-medium text-[var(--color-text-secondary)]">快捷内容</span>
          <button
            type="button"
            onClick={onOpenSettings}
            data-testid="super-assistant-quick-input-settings"
            className="-my-1 inline-flex h-7 items-center gap-1 rounded px-1.5 text-xs font-medium text-brand-ink transition-colors hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            <Settings2 size={13} /> 快捷内容设置
          </button>
        </div>
        {/* stale-while-revalidate：已有列表时后台刷新不打断浏览，仅首开/空列表走 spinner 与错误块 */}
        {loading && items.length === 0 ? (
          <div className="flex items-center justify-center gap-2 px-3 py-6 text-xs text-[var(--color-text-tertiary)]">
            <Loader2 size={13} className="animate-spin" /> 加载中…
          </div>
        ) : error && items.length === 0 ? (
          <div className="flex flex-col items-center gap-2 px-3 py-5 text-center">
            <p className="text-xs leading-5 text-[var(--color-text-secondary)]">{error}</p>
            <button
              type="button"
              onClick={onRetry}
              className="inline-flex h-7 items-center rounded-md border border-border px-2.5 text-xs text-[var(--color-text-secondary)] transition-colors hover:bg-[var(--color-bg-hover)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              重试
            </button>
          </div>
        ) : items.length === 0 ? (
          <div className="flex flex-col items-center gap-1.5 px-3 py-6 text-center" data-testid="super-assistant-quick-input-empty">
            <Zap size={16} className="text-[var(--color-text-tertiary)]" />
            <p className="text-xs leading-5 text-[var(--color-text-secondary)]">
              还没有快捷内容
              <br />
              点击右上角「快捷内容设置」添加常用内容
            </p>
          </div>
        ) : (
          <div className="scrollbar-none max-h-64 overflow-y-auto py-1">
            {items.map(item => (
              <button
                type="button"
                key={item.id}
                onClick={() => onInsert(item.content)}
                data-testid="super-assistant-quick-input-item"
                title={item.content}
                className="flex w-full items-start gap-2 px-3 py-1.5 text-left transition-colors hover:bg-[var(--color-bg-hover)] focus-visible:bg-[var(--color-bg-hover)] focus-visible:outline-none"
              >
                <Zap size={11} className="mt-1 shrink-0 text-brand-ink" />
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-xs font-medium text-[var(--color-text-primary)]">{item.title}</span>
                  <span className="block truncate text-xs text-[var(--color-text-tertiary)]">{contentPreview(item.content)}</span>
                </span>
              </button>
            ))}
          </div>
        )}
      </PopoverContent>
    </Popover>
  )
}
