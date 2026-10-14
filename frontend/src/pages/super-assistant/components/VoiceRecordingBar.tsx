import { useEffect, useRef, useState } from 'react'
import { Loader2, Mic, Square, X } from 'lucide-react'
import { toast } from 'sonner'

import { MAX_RECORDING_SECONDS, type VoiceDictation } from '../useVoiceDictation'
import { VoiceWaveform } from './VoiceWaveform'

function formatDuration(ms: number): string {
  const totalSeconds = Math.floor(ms / 1000)
  const minutes = Math.floor(totalSeconds / 60)
  const seconds = totalSeconds % 60
  return `${minutes}:${String(seconds).padStart(2, '0')}`
}

/**
 * 录音条（cc-haha 式）：录音期间原地替换输入框底部按钮行——输入框与
 * 草稿保持可见，只留「取消」与「结束录音」两个图标键。取消键在转写
 * 阶段同样可用（中止上传/推理）：转写最长可达 90s，期间必须可退出。
 * 计时由本组件 100ms 轮询并存在局部 state，波形另由 canvas 组件自绘，
 * 高频更新不触发整个聊天页面（含消息列表）重协调；到达自动停止线
 * 替用户收尾，避免录满后端上限才吃 413。
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
  const [elapsedMs, setElapsedMs] = useState(0)
  const hostRef = useRef<HTMLDivElement>(null)
  const startedAtRef = useRef(performance.now())
  const autoStoppedRef = useRef(false)

  const { phase, recorderRef } = dictation
  const transcribing = phase === 'transcribing'

  useEffect(() => {
    if (transcribing) return
    const timer = window.setInterval(() => {
      const elapsed = performance.now() - startedAtRef.current
      setElapsedMs(elapsed)
      if (elapsed >= MAX_RECORDING_SECONDS * 1000 && !autoStoppedRef.current) {
        autoStoppedRef.current = true
        toast.info('已达到单次录音上限，自动停止并转写')
        onStop()
      }
    }, 100)
    return () => window.clearInterval(timer)
    // onStop 经由父级 onClick 包装稳定引用；轮询生命周期跟随录音态
  }, [transcribing, onStop])

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== 'Escape' || event.defaultPrevented || event.isComposing) return
      const target = event.target as HTMLElement | null
      // Esc 归属更上层的浮层（Radix 弹层/对话框内的输入）：让那边的
      // Esc 语义先成立（关弹层），不越权取消录音。输入框（TEXTAREA）
      // 本身没有 Esc 语义，录音期间它的 Esc 就该取消录音
      if (target?.closest('[data-radix-popper-content-wrapper], [role="dialog"]')) return
      // INPUT/SELECT/可编辑区（侧栏搜索、弹窗表单）保留自身 Esc 行为
      if (target && (target.isContentEditable
        || ['INPUT', 'SELECT'].includes(target.tagName))) return
      event.preventDefault()
      onCancel()
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [onCancel])

  return (
    <div
      ref={hostRef}
      data-testid="super-assistant-voice-recording-bar"
      className="flex min-h-12 items-center gap-3 px-2.5 py-2"
    >
      <button
        type="button"
        onClick={onCancel}
        aria-label={transcribing ? '取消转写' : '取消录音'}
        title={transcribing ? '取消转写' : '取消录音（Esc）'}
        data-testid="super-assistant-voice-cancel-button"
        className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border border-border text-[var(--color-text-tertiary)] transition-colors hover:bg-[var(--color-bg-hover)] hover:text-[var(--color-text-secondary)] active:scale-[0.98] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
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
      <span className="shrink-0 font-mono text-xs tabular-nums text-[var(--color-text-secondary)]">
        {transcribing ? '转写中…' : formatDuration(elapsedMs)}
      </span>
      <VoiceWaveform recorderRef={recorderRef} frozen={transcribing} />
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
