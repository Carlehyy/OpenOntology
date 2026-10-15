/**
 * 配置面板的开关控件（label + role=switch 按钮）：SettingSwitch 从
 * AssistantConfiguration 提取为独立模块，供 Skills/MCP 与语音 tab 共用，
 * 避免页面级组件间互相 import 形成生产依赖环。
 * label 可选：列表卡场景（渠道卡）无行内文字标签，只带开关本体；
 * 其余 props 透传到按钮（data-* 锚点等），保持单一规格（44px 命中区 +
 * h-4 w-7 轨道 + busy pulse），不再另起自绘实现。
 */
import type { ButtonHTMLAttributes } from 'react'

export function SettingSwitch({ label, ariaLabel, checked, busy, onToggle, ...rest }: {
  label?: string
  ariaLabel: string
  checked: boolean
  busy: boolean
  onToggle: () => void
} & Omit<ButtonHTMLAttributes<HTMLButtonElement>, 'aria-label' | 'aria-checked' | 'role' | 'onClick' | 'disabled'>) {
  return (
    <div className="inline-flex items-center gap-1.5">
      {label && <span className="text-xs text-[var(--color-text-secondary)]">{label}</span>}
      <button type="button" role="switch" aria-label={ariaLabel} aria-checked={checked} aria-busy={busy} disabled={busy} onClick={onToggle} {...rest}
        className="relative inline-flex h-11 w-11 shrink-0 touch-manipulation items-center justify-center rounded-lg focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-1 disabled:cursor-wait disabled:opacity-60">
        <span aria-hidden="true" className={`relative inline-flex h-4 w-7 items-center rounded-full transition-colors motion-reduce:transition-none ${busy ? 'animate-pulse motion-reduce:animate-none' : ''} ${checked ? 'bg-brand' : 'bg-slate-300'}`}>
          <span className={`inline-block h-3 w-3 rounded-full bg-white shadow-sm transition-transform motion-reduce:transition-none ${checked ? 'translate-x-3.5' : 'translate-x-0.5'}`} />
        </span>
      </button>
    </div>
  )
}
