// 事件登记域共享的展示元件与格式化助手：列表页与详情抽屉共用，保持唯一事实源。

import { formatDateTime } from '@/utils/datetime'

export const PALETTE = {
  blue: '#3B82F6', teal: '#5EEAD4', gold: '#FCD34D', orange: '#FDBA74',
  red: '#FB7185',
}

// 后端所有时间列均为 naive UTC 序列化（无 Z 后缀），必须经 parseServerTime
// 补 UTC 语义后再展示，直接 new Date 会按本地时区解析、上海慢 8 小时（UX 评审 A1）。
export function fmt(iso: string | null | undefined): string {
  return formatDateTime(iso)
}

// ─── 级别标签 ────────────────────────────────────────────
export function SeverityBadge({ sev }: { sev: string }) {
  const map: Record<string, { bg: string; text: string; dot: string; label: string; glow: string }> = {
    critical: { bg: 'bg-[var(--color-danger-bg)]', text: 'text-[var(--color-danger)]', dot: PALETTE.red, label: '严重', glow: 'rgba(251,113,133,0.4)' },
    high: { bg: 'bg-viz-orange-soft', text: 'text-viz-orange', dot: PALETTE.orange, label: '高级', glow: 'rgba(253,186,116,0.4)' },
    medium: { bg: 'bg-[var(--color-warning-bg)]', text: 'text-[var(--color-warning)]', dot: PALETTE.gold, label: '中级', glow: 'rgba(252,211,77,0.4)' },
    low: { bg: 'bg-brand-soft', text: 'text-brand-ink', dot: PALETTE.teal, label: '低级', glow: 'rgba(94,234,212,0.4)' },
    info: { bg: 'bg-[var(--color-info-bg)]', text: 'text-[var(--color-info)]', dot: PALETTE.blue, label: '信息', glow: 'rgba(59,130,246,0.4)' },
  }
  const c = map[sev] ?? map.info
  return (
    <span className={`inline-flex items-center gap-1.5 rounded-full px-2 py-1 ${c.bg} ${c.text} text-xs font-medium whitespace-nowrap`}>
      <span className="h-1.5 w-1.5 rounded-full" style={{ background: c.dot, boxShadow: `0 0 4px ${c.glow}` }} />{c.label}
    </span>
  )
}
