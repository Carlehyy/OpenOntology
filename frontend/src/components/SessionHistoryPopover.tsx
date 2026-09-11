import type { ReactNode } from 'react'
import { Download, History, Loader2, Plus, Trash2 } from 'lucide-react'

import { formatSessionTime } from '@/utils/datetime'

export interface SessionHistoryItem {
  id: string
  title: string
  updatedAt: string
}

interface SessionHistoryPopoverProps<T extends SessionHistoryItem> {
  open: boolean
  items: T[]
  currentId?: string | null
  onClose: () => void
  onCreate: () => void | Promise<void>
  onSelect: (id: string) => void | Promise<void>
  onExport?: (id: string) => void | Promise<void>
  exportingId?: string | null
  onDelete?: (id: string) => void | Promise<void>
  renderItemIcon: (item: T) => ReactNode
  emptyDescription: string
  topOffsetClassName?: string
}

export default function SessionHistoryPopover<T extends SessionHistoryItem>({
  open,
  items,
  currentId,
  onClose,
  onCreate,
  onSelect,
  onExport,
  exportingId,
  onDelete,
  renderItemIcon,
  emptyDescription,
  topOffsetClassName = 'mt-[14px]',
}: SessionHistoryPopoverProps<T>) {
  if (!open) return null

  return (
    <>
      <div className="fixed inset-0 z-20" onClick={onClose} aria-hidden="true" />
      <section
        role="dialog"
        aria-label="历史会话"
        className={`absolute right-0 top-full z-30 ${topOffsetClassName} w-[min(380px,calc(100vw-32px))] overflow-hidden rounded-xl border border-[var(--color-border)] bg-[var(--color-bg-elevated)] shadow-[0_18px_52px_rgba(15,23,42,0.16)] animate-slide-up`}
      >
        <header className="flex items-center gap-3 border-b border-[var(--color-border)] px-4 py-2.5">
          <span className="shrink-0 text-sm font-semibold text-[var(--color-text-primary)]">历史会话</span>
          <span className="inline-flex items-center gap-1.5 whitespace-nowrap text-xs text-teal-700">
            <span className="h-1.5 w-1.5 rounded-full bg-teal-500" />
            共 <span className="font-semibold tabular-nums">{items.length}</span> 个
          </span>
          <button
            type="button"
            onClick={() => void onCreate()}
            className="ml-auto inline-flex h-8 items-center gap-1.5 rounded-lg bg-teal-600 px-3 text-xs font-medium text-white transition-all hover:bg-teal-700 active:scale-[0.98] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            <Plus size={13} /> 新建
          </button>
        </header>

        <div className="scrollbar-thin max-h-[420px] overflow-y-auto overflow-x-hidden">
          {items.length === 0 ? (
            <div className="flex flex-col items-center px-6 py-14 text-center">
              <span className="mb-3 flex h-12 w-12 items-center justify-center rounded-full bg-[var(--color-bg-base)] text-[var(--color-text-tertiary)]">
                <History size={21} />
              </span>
              <p className="text-sm font-medium text-[var(--color-text-secondary)]">还没有历史会话</p>
              <p className="mt-1 text-xs leading-5 text-[var(--color-text-tertiary)]">{emptyDescription}</p>
            </div>
          ) : (
            <div className="divide-y divide-[var(--color-border)]">
              {items.map(item => {
                const current = item.id === currentId
                const title = item.title.trim() || '未命名会话'
                return (
                  <div
                    key={item.id}
                    data-session-history-item={item.id}
                    className={`group flex items-center gap-2.5 px-4 py-2 transition-colors ${current
                      ? 'bg-teal-50/70'
                      : 'hover:bg-[var(--color-bg-hover)]'}`}
                  >
                    <button
                      type="button"
                      onClick={() => void onSelect(item.id)}
                      className="flex min-w-0 flex-1 items-center gap-3 text-left focus-visible:outline-none"
                    >
                      <span className={`flex h-8 w-8 shrink-0 items-center justify-center rounded-lg ${current
                        ? 'bg-teal-100 text-teal-700'
                        : 'bg-slate-100 text-slate-500'}`}
                      >
                        {renderItemIcon(item)}
                      </span>
                      <span className="min-w-0 flex-1">
                        <span
                          className={`block truncate text-sm font-medium ${current
                            ? 'text-teal-900'
                            : 'text-[var(--color-text-primary)]'}`}
                          title={title}
                        >
                          {title}
                        </span>
                        <span className="mt-0.5 block text-[10px] tabular-nums text-[var(--color-text-tertiary)]">
                          {formatSessionTime(item.updatedAt)}
                        </span>
                      </span>
                    </button>

                    <span className="flex shrink-0 items-center gap-1">
                      {onExport && (
                        <button
                          type="button"
                          disabled={exportingId === item.id}
                          onClick={() => void onExport(item.id)}
                          title={`导出会话 ${title} 的完整 JSON`}
                          aria-label={`导出会话 ${title} 的完整 JSON`}
                          className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg text-slate-400 opacity-100 transition-all hover:bg-sky-50 hover:text-sky-600 disabled:cursor-wait disabled:opacity-60 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring sm:opacity-0 sm:group-hover:opacity-100 sm:focus-visible:opacity-100"
                        >
                          {exportingId === item.id
                            ? <Loader2 size={14} className="animate-spin" />
                            : <Download size={14} />}
                        </button>
                      )}
                      {onDelete && (
                        <button
                          type="button"
                          onClick={() => void onDelete(item.id)}
                          title={`删除会话 ${title}`}
                          aria-label={`删除会话 ${title}`}
                          className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg text-slate-400 opacity-100 transition-all hover:bg-red-50 hover:text-red-600 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring sm:opacity-0 sm:group-hover:opacity-100 sm:focus-visible:opacity-100"
                        >
                          <Trash2 size={14} />
                        </button>
                      )}
                      {current && (
                        <span className="rounded-md bg-white/80 px-2 py-1 text-[10px] font-medium text-teal-700">
                          当前
                        </span>
                      )}
                    </span>
                  </div>
                )
              })}
            </div>
          )}
        </div>
      </section>
    </>
  )
}
