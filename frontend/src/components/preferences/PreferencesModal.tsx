import { Check, Moon, Settings, Sun } from 'lucide-react'

import { Modal } from '@/components/ui/Modal'
import { cn } from '@/lib/utils'
import type { Theme } from '@/lib/theme'
import { useThemeStore } from '@/stores/themeStore'

const themeOptions: Array<{
  value: Theme
  label: string
  description: string
  icon: typeof Sun
}> = [
  { value: 'light', label: '浅色', description: '默认浅色背景（当前默认）', icon: Sun },
  { value: 'dark', label: '深色', description: '深色背景，适合弱光环境', icon: Moon },
]

/**
 * 偏好设置弹窗（用户头像下拉 → 偏好设置）。
 * 当前包含“外观”分区（浅色/深色主题切换）；语言、通知等分区后续在此扩展。
 */
export default function PreferencesModal({
  open,
  onClose,
}: {
  open: boolean
  onClose: () => void
}) {
  const theme = useThemeStore(s => s.theme)
  const setTheme = useThemeStore(s => s.setTheme)

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="偏好设置"
      description="管理你的界面偏好，设置会立即生效并记住选择"
      size="sm"
      headerIcon={<Settings size={19} />}
    >
      <section aria-label="外观">
        <h4 className="text-sm font-medium text-[var(--color-text-primary)]">外观</h4>
        <p className="mt-0.5 text-xs text-[var(--color-text-tertiary)]">
          选择平台界面的颜色主题
        </p>
        <div role="radiogroup" aria-label="主题" className="mt-3 grid grid-cols-2 gap-3">
          {themeOptions.map(option => {
            const Icon = option.icon
            const active = theme === option.value
            return (
              <button
                key={option.value}
                type="button"
                role="radio"
                aria-checked={active}
                onClick={() => setTheme(option.value)}
                className={cn(
                  'relative flex flex-col items-center gap-2 rounded-xl border px-3 py-4 text-center transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
                  active
                    ? 'border-[var(--color-nav-bg)] bg-[var(--color-nav-light)] text-[var(--color-nav-bg)]'
                    : 'border-[var(--color-border)] text-[var(--color-text-secondary)] hover:border-[var(--color-border-hover)] hover:bg-[var(--color-bg-hover)]',
                )}
              >
                <Icon size={20} aria-hidden="true" />
                <span className="text-sm font-medium">{option.label}</span>
                <span className={cn(
                  'text-xs leading-4',
                  active ? 'text-[var(--color-nav-bg)]' : 'text-[var(--color-text-tertiary)]',
                )}>
                  {option.description}
                </span>
                {active && (
                  <span
                    aria-hidden="true"
                    className="absolute right-2 top-2 flex h-5 w-5 items-center justify-center rounded-full bg-[var(--color-nav-bg)] text-white"
                  >
                    <Check size={12} strokeWidth={3} />
                  </span>
                )}
              </button>
            )
          })}
        </div>
      </section>
    </Modal>
  )
}
