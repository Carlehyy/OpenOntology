import { Brain, Loader2 } from 'lucide-react'

import {
  memoryStatusPresentation,
  type MemoryStatusSnapshot,
  type MemoryStatusTone,
} from '@/components/assistant-widget/logic'

/**
 * 记忆运行时状态的轻量状态条（主聊天区与悬浮助手共用）。
 * 记忆状态与消息生成完全独立：永不作为聊天 error 呈现，
 * 只以独立的浅底状态条明确可见（含未配置提示）。
 */
const TONE_CLASS: Record<MemoryStatusTone, string> = {
  ok: 'border-[var(--color-border)] bg-[var(--color-bg-elevated)] text-[var(--color-text-secondary)]',
  busy: 'border-[var(--color-border)] bg-[var(--color-bg-elevated)] text-[var(--color-text-secondary)]',
  idle: 'border-[var(--color-border)] bg-[var(--color-bg-elevated)] text-[var(--color-text-tertiary)]',
  warn: 'border-[var(--color-warning)] bg-[var(--color-warning-bg)] text-[var(--color-warning)]',
}

export function MemoryStatusNotice({ status, className = '' }: { status: MemoryStatusSnapshot; className?: string }) {
  const { label, tone } = memoryStatusPresentation(status.state)
  return (
    <div
      role="status"
      data-testid={`memory-status-${status.state}`}
      className={`flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-[11px] leading-4 ${TONE_CLASS[tone]} ${className}`}
    >
      {tone === 'busy'
        ? <Loader2 size={11} className="shrink-0 animate-spin" aria-hidden="true" />
        : <Brain size={11} className="shrink-0" aria-hidden="true" />}
      <span className="shrink-0 font-medium">{label}</span>
      {status.message && tone === 'warn' && (
        <span className="min-w-0 truncate" title={status.message}>{status.message}</span>
      )}
      {status.state === 'ready' && status.lastSyncedAt && (
        <span className="shrink-0 text-[var(--color-text-tertiary)]" title={`最近同步 ${status.lastSyncedAt}`}>
          · 已更新
        </span>
      )}
    </div>
  )
}
