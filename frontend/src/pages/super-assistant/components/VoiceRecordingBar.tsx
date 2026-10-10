import { useEffect, useRef, useState } from 'react'
import { AudioLines, Loader2, Mic, Send, Square, X } from 'lucide-react'

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
 * 录音态输入条：替换输入框显示（Esc 取消、点击停止并转写）。
 * 音量轨迹是响度可视化（峰值渐隐），不是识别中间结果。
 */
export default function VoiceRecordingBar({
  dictation,
  onFinish,
  onCancel,
}: {
  dictation: VoiceDictation
  onFinish: (sendAfter: boolean) => void
  onCancel: () => void
}) {
  const [trail, setTrail] = useState<number[]>([])
  const barRef = useRef<HTMLDivElement>(null)

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
      ref={barRef}
      data-testid="super-assistant-voice-recording-bar"
      className="flex min-h-11 items-center gap-3 px-3 pb-1.5 pt-2.5"
    >
      <div className="flex shrink-0 items-center gap-2">
        <span className="relative flex h-7 w-7 items-center justify-center rounded-full bg-brand-soft text-brand-ink">
          {transcribing ? <Loader2 size={14} className="animate-spin" /> : <Mic size={14} />}
          {!transcribing && (
            <span
              aria-hidden="true"
              className="absolute -right-0.5 -top-0.5 h-2.5 w-2.5 animate-pulse rounded-full bg-red-500 motion-reduce:animate-none"
            />
          )}
        </span>
        <span className="font-mono text-xs tabular-nums text-[var(--color-text-secondary)]" aria-live="polite">
          {transcribing ? '转写中…' : formatDuration(elapsedMs)}
        </span>
      </div>
      <div
        aria-hidden="true"
        className="flex h-7 min-w-0 flex-1 items-center justify-center gap-[3px] overflow-hidden"
      >
        {trail.map((sample, index) => (
          <span
            key={index}
            className="w-[3px] shrink-0 rounded-full bg-brand/60"
            style={{ height: `${Math.max(8, Math.min(100, sample * 140))}%`, opacity: 0.35 + (index / trail.length) * 0.65 }}
          />
        ))}
        {trail.length === 0 && <AudioLines size={16} className="text-[var(--color-text-tertiary)]" />}
      </div>
      <div className="flex shrink-0 items-center gap-1.5">
        <button
          type="button"
          onClick={onCancel}
          disabled={transcribing}
          aria-label="取消录音"
          title="取消录音（Esc）"
          className="flex h-8 w-8 items-center justify-center rounded-lg border border-border text-[var(--color-text-tertiary)] transition-colors hover:bg-[var(--color-bg-hover)] hover:text-[var(--color-text-secondary)] active:scale-[0.98] disabled:opacity-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          <X size={15} />
        </button>
        <button
          type="button"
          onClick={() => onFinish(false)}
          disabled={transcribing}
          aria-label="停止录音并转成文字"
          title="停止录音并转成文字"
          className="flex h-8 items-center gap-1.5 rounded-lg border border-border px-2.5 text-xs text-[var(--color-text-secondary)] transition-colors hover:bg-[var(--color-bg-hover)] hover:text-[var(--color-text-primary)] active:scale-[0.98] disabled:opacity-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          <Square size={11} fill="currentColor" />
          转成文字
        </button>
        <button
          type="button"
          onClick={() => onFinish(true)}
          disabled={transcribing}
          aria-label="停止录音，转写后直接发送"
          title="停止录音，转写后直接发送"
          className="flex h-8 items-center gap-1.5 rounded-lg bg-brand px-2.5 text-xs font-medium text-white transition-all hover:bg-brand-deep active:scale-[0.98] disabled:opacity-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-1"
        >
          <Send size={11} />
          停止并发送
        </button>
      </div>
    </div>
  )
}
