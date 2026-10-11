import { useEffect, useRef, useState } from 'react'
import { Loader2, Mic, Square, X } from 'lucide-react'

import type { VoiceDictation } from '../useVoiceDictation'

/** 音量轨迹保留的最近采样数（约 3 秒 @100ms） */
const TRAIL_SAMPLES = 30

function formatDuration(ms: number): string {
  const totalSeconds = Math.floor(ms / 1000)
  const minutes = Math.floor(totalSeconds / 60)
  const seconds = totalSeconds % 60
  return `${minutes}:${String(seconds).padStart(2, '0')}`
}

/**
 * 录音条（cc-haha 式）：录音期间原地替换输入框底部按钮行——输入框与
 * 草稿保持可见，只留「取消」与「结束录音」两个图标键。音量轨迹是响度
 * 可视化（峰值渐隐），不是识别中间结果。
 */
export default function VoiceRecordingBar({
  dictation,
  onStop,
  onCancel,
}: {
  dictation: VoiceDictation
  onStop: () => void
  onCancel: () => void
}) {
  const [trail, setTrail] = useState<number[]>([])
  const hostRef = useRef<HTMLDivElement>(null)

  const { phase, level, elapsedMs } = dictation
  const transcribing = phase === 'transcribing'

  // 音量轨迹经 effect 推进：渲染体保持纯函数，避免并发渲染下采样重复
  useEffect(() => {
    if (transcribing) return
    setTrail(current => [...current, level].slice(-TRAIL_SAMPLES))
  }, [level, transcribing])

  useEffect(() => {
    if (transcribing) return
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== 'Escape' || event.defaultPrevented) return
      // 焦点在其他可编辑控件（侧栏搜索、弹窗输入）时，Esc 归属那个控件，
      // 不越权取消录音；仅面向页面级的 Esc 兜底
      const target = event.target as HTMLElement | null
      if (target && (target.isContentEditable
        || ['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName))) return
      event.preventDefault()
      onCancel()
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [transcribing, onCancel])

  return (
    <div
      ref={hostRef}
      data-testid="super-assistant-voice-recording-bar"
      className="flex min-h-12 items-center gap-3 px-2.5 py-2"
    >
      <button
        type="button"
        onClick={onCancel}
        disabled={transcribing}
        aria-label="取消录音"
        title="取消录音（Esc）"
        className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border border-border text-[var(--color-text-tertiary)] transition-colors hover:bg-[var(--color-bg-hover)] hover:text-[var(--color-text-secondary)] active:scale-[0.98] disabled:opacity-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        <X size={15} />
      </button>
      <span className="relative flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-brand-soft text-brand-ink">
        {transcribing ? <Loader2 size={14} className="animate-spin" /> : <Mic size={14} />}
        {!transcribing && (
          <span
            aria-hidden="true"
            className="absolute -right-0.5 -top-0.5 h-2.5 w-2.5 animate-pulse rounded-full bg-red-500 motion-reduce:animate-none"
          />
        )}
      </span>
      <span className="shrink-0 font-mono text-xs tabular-nums text-[var(--color-text-secondary)]" aria-live="polite">
        {transcribing ? '转写中…' : formatDuration(elapsedMs)}
      </span>
      <div
        aria-hidden="true"
        className="flex h-7 min-w-0 flex-1 items-center gap-[3px] overflow-hidden"
      >
        {trail.map((sample, index) => (
          <span
            key={index}
            className="w-[3px] shrink-0 rounded-full bg-brand/60"
            style={{ height: `${Math.max(8, Math.min(100, sample * 140))}%`, opacity: 0.35 + (index / trail.length) * 0.65 }}
          />
        ))}
      </div>
      <button
        type="button"
        onClick={onStop}
        disabled={transcribing}
        aria-label="结束录音并转成文字"
        title="结束录音并转成文字"
        data-testid="super-assistant-voice-stop-button"
        className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-brand text-white transition-all hover:bg-brand-deep active:scale-[0.98] disabled:opacity-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-1"
      >
        {transcribing ? <Loader2 size={13} className="animate-spin" /> : <Square size={12} fill="currentColor" />}
      </button>
    </div>
  )
}
