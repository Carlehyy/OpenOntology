import type { ReactNode } from 'react'

export type KpiTone = 'neutral' | 'success' | 'brand' | 'info' | 'warning' | 'danger'

interface ToneStyle {
  text: string
  iconBg: string
}

const TONE_MAP: Record<KpiTone, ToneStyle> = {
  neutral: { text: 'text-foreground', iconBg: 'bg-muted text-muted-foreground' },
  success: { text: 'text-[var(--color-success)]', iconBg: 'bg-[var(--color-success-bg)] text-[var(--color-success)]' },
  brand: { text: 'text-brand-ink', iconBg: 'bg-brand-soft text-brand-ink' },
  info: { text: 'text-viz-cyan', iconBg: 'bg-viz-cyan-soft text-viz-cyan' },
  warning: { text: 'text-[var(--color-warning)]', iconBg: 'bg-[var(--color-warning-bg)] text-[var(--color-warning)]' },
  danger: { text: 'text-viz-rose', iconBg: 'bg-viz-rose-soft text-viz-rose' },
}

export interface KpiStatCardProps {
  label: string
  value: number | string
  note?: string
  icon?: ReactNode
  /**
   * 语义色（UIUX 审查 P0-7「红零」治理）：0/空值恒中性灰，
   * 仅当数值确实踩中坏/好消息条件时才着语义色。
   */
  tone?: KpiTone
  /**
   * 语义色激活条件；缺省为「数值 > 0」（数字 0、非数字走中性）。
   * 例：慢请求卡传 `value > 阈值`；恒中性的卡片不传 tone 即可。
   */
  toneActive?: boolean
  /** 语义色激活时的图标角标脉冲（仅 danger/warning 有意义） */
  pulse?: boolean
}

/** 全站统一的 KPI 统计卡：紧凑单行卡（对齐数据任务池/流水线头部形态）。 */
export function KpiStatCard({ label, value, note, icon, tone = 'neutral', toneActive, pulse }: KpiStatCardProps) {
  const numericActive = typeof value === 'number' ? value > 0 : Boolean(value)
  const semantic = tone !== 'neutral' && (toneActive ?? numericActive)
  const style = semantic ? TONE_MAP[tone] : TONE_MAP.neutral
  return (
    <div className="rounded-xl border border-border bg-card px-4 py-3 shadow-sm/50">
      <div className="flex items-center justify-between gap-2">
        <span className="truncate text-[11px] font-medium text-muted-foreground">{label}</span>
        {icon && (
          <span className={`relative grid h-6 w-6 shrink-0 place-items-center rounded-md ${style.iconBg}`}>
            {icon}
            {pulse && semantic && (
              <span className="absolute -right-0.5 -top-0.5 h-1.5 w-1.5 animate-ping rounded-full bg-current opacity-60" />
            )}
          </span>
        )}
      </div>
      <p className={`mt-0.5 text-xl font-semibold leading-none tracking-tight tabular-nums ${style.text}`}>{value}</p>
      {note && (
        <p className="mt-1 truncate text-[10px] text-[var(--color-text-tertiary)]" title={note}>{note}</p>
      )}
    </div>
  )
}
