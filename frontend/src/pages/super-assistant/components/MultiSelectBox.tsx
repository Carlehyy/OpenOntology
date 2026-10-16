import { useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { Check, ChevronDown } from 'lucide-react'
import type { LucideIcon } from 'lucide-react'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'

export interface MultiSelectOption {
  value: string
  label: string
  /** 次级说明（如渠道模板名），行尾弱化展示 */
  secondary?: string
  icon?: LucideIcon
}

interface MultiSelectBoxProps {
  options: MultiSelectOption[]
  /** 已解析的选中集（"null=默认全选"之类语义由调用方解析后传入） */
  selectedIds: ReadonlySet<string>
  onToggle: (value: string) => void
  /** 恢复默认全选；选项 ≥3 且未全选时才渲染该行 */
  onSelectAll?: () => void
  /** 触发器占位文案（0 选中时展示） */
  placeholder?: string
  children?: ReactNode
}

/**
 * 下拉多选框（邮件收件人模式）：触发器内常显已选 chips（去向可见是发送类操作的
 * 硬约束——折叠后只给计数会让"发错渠道"变容易），点开是勾选列表，可连续多选。
 * 行用 role=checkbox/aria-checked（弹层勾选清单），Esc 关闭由 Radix DismissableLayer
 * 分层处理（先关弹层再透传弹窗）。
 */
export default function MultiSelectBox({
  options,
  selectedIds,
  onToggle,
  onSelectAll,
  placeholder = '未选择',
  children,
}: MultiSelectBoxProps) {
  const [open, setOpen] = useState(false)
  const listRef = useRef<HTMLDivElement>(null)
  const showSelectAll = options.length >= 3 && selectedIds.size < options.length && !!onSelectAll
  const selectedOptions = options.filter(option => selectedIds.has(option.value))

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <button
          type="button"
          data-notifications-compose-multiselect
          className={`flex min-h-9 max-h-28 w-full flex-wrap items-center gap-1.5 overflow-y-auto overscroll-contain rounded-lg border bg-[var(--color-bg-base)] px-3 py-1.5 text-left text-sm transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${
            selectedIds.size === 0
              ? 'border-[var(--color-danger)]'
              : 'border-[var(--color-border)] hover:border-[var(--color-border-hover)]'
          }`}
        >
          {selectedOptions.length === 0 ? (
            <span className="text-sm text-[var(--color-text-tertiary)]">{placeholder}</span>
          ) : (
            selectedOptions.map(option => (
              <span
                key={option.value}
                className="flex items-center gap-1 rounded-full bg-brand-soft px-2 py-0.5 text-xs font-medium text-brand-ink"
              >
                {option.icon && <option.icon size={12} className="shrink-0" />}
                <span className="max-w-40 truncate">{option.label}</span>
              </span>
            ))
          )}
          <span className="ml-auto flex shrink-0 items-center gap-1.5 pl-2 text-xs text-[var(--color-text-tertiary)]">
            {options.length > 0 && `${selectedIds.size}/${options.length}`}
            <ChevronDown size={14} />
          </span>
        </button>
      </PopoverTrigger>
      <PopoverContent
        align="start"
        className="z-[calc(var(--z-modal)+1)] max-h-64 w-[var(--radix-popover-trigger-width)] overflow-y-auto overscroll-contain rounded-lg border-[var(--color-border)] bg-[var(--color-bg-base)] p-1 text-[var(--color-text-primary)]"
      >
        {children}
        <div ref={listRef} className="flex flex-col">
          {options.map(option => {
            const checked = selectedIds.has(option.value)
            const Icon = option.icon
            return (
              <button
                key={option.value}
                type="button"
                role="checkbox"
                aria-checked={checked}
                onClick={() => onToggle(option.value)}
                data-notifications-compose-channel={option.value}
                className="flex items-center gap-2 rounded-lg px-2 py-1.5 text-left text-sm text-[var(--color-text-primary)] transition-colors hover:bg-[var(--color-bg-hover)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                <span
                  className={`flex h-4 w-4 shrink-0 items-center justify-center rounded border ${
                    checked
                      ? 'border-transparent bg-brand-soft text-brand-ink'
                      : 'border-[var(--color-border)]'
                  }`}
                >
                  {checked && <Check size={12} />}
                </span>
                {Icon && <Icon size={14} className="shrink-0 text-[var(--color-text-secondary)]" />}
                <span className="min-w-0 flex-1 truncate">{option.label}</span>
                {option.secondary && (
                  <span className="max-w-32 shrink-0 truncate text-xs text-[var(--color-text-tertiary)]">
                    {option.secondary}
                  </span>
                )}
              </button>
            )
          })}
          {showSelectAll && (
            <>
              <div className="my-1 h-px bg-[var(--color-border)]" />
              <button
                type="button"
                onClick={() => {
                  onSelectAll?.()
                  // 本行随即卸载（全选后不再渲染）：焦点移交给列表首项。不能留给 body——
                  // popover 是 body 级 portal，dialog 的 FocusScope 兜底 observe 不到这里
                  requestAnimationFrame(() =>
                    listRef.current?.querySelector<HTMLButtonElement>('button[role="checkbox"]')?.focus(),
                  )
                }}
                data-notifications-compose-select-all
                className="rounded-lg px-2 py-1.5 text-left text-xs text-[var(--color-primary)] underline-offset-2 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                全选
              </button>
            </>
          )}
        </div>
      </PopoverContent>
    </Popover>
  )
}
