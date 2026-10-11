import { useCallback, useEffect, useRef, useState } from 'react'
import { toast } from 'sonner'

import { superAssistantApi } from '@/api/superAssistant'
import {
  createVoiceRecorder,
  isRecorderError,
  voiceCaptureSupported,
  type VoiceRecorder,
} from '@/lib/voice/recorder'
import { errorText } from './components/assistantPanelUtils'

export type DictationPhase = 'idle' | 'recording' | 'transcribing'

/** 前端自动停止线：留出余量低于后端 120s 上限，避免录满才吃 413 */
export const MAX_RECORDING_SECONDS = 110

export interface VoiceDictation {
  phase: DictationPhase
  /** 0~1 实时电平（录音条音量轨迹用） */
  level: number
  /** 已录制时长 */
  elapsedMs: number
  start: () => Promise<void>
  /** 停止录音并转写，文本经 onTranscript 插入输入框（不自动发送） */
  finish: () => Promise<void>
  /** 取消并释放麦克风，丢弃录音 */
  cancel: () => void
}

const RECORDER_ERROR_MESSAGES: Record<string, string> = {
  'insecure-context': '当前页面不是安全上下文（需 HTTPS 或 localhost），浏览器不提供麦克风',
  'permission-denied': '麦克风权限被拒绝，请在浏览器地址栏的权限设置中允许后重试',
  'no-device': '无法访问麦克风设备，请检查设备连接',
  'too-short': '录音时间太短，已忽略',
  'decode-failed': '录音解码失败，请重试',
  'not-recording': '当前没有进行中的录音',
}

function notifyError(message: string) {
  toast.error('语音听写失败', { description: message })
}

/**
 * 超级助手聊天输入区的语音听写状态机：录音（createVoiceRecorder 即开始）
 * → 停止 → 16k WAV 上传后端本地转写 → onTranscript 回调交由页面按插入
 * 策略落进输入框。错误经 toast 呈现（与页面既有反馈一致）。
 */
export function useVoiceDictation(options: {
  getLanguage: () => string
  onTranscript: (text: string) => void
}): VoiceDictation {
  const [phase, setPhase] = useState<DictationPhase>('idle')
  const [level, setLevel] = useState(0)
  const [elapsedMs, setElapsedMs] = useState(0)

  const recorderRef = useRef<VoiceRecorder | null>(null)
  // getUserMedia 是异步授权：await 返回前 phase 仍是 idle，按钮防重入
  // 覆盖不到这个窗口，必须用同步标记挡住连点
  const startingRef = useRef(false)
  const startedAtRef = useRef(0)
  const pollTimerRef = useRef<number | null>(null)
  const autoStoppedRef = useRef(false)
  const optionsRef = useRef(options)
  optionsRef.current = options

  const stopPolling = useCallback(() => {
    if (pollTimerRef.current !== null) {
      window.clearInterval(pollTimerRef.current)
      pollTimerRef.current = null
    }
  }, [])

  const teardown = useCallback(() => {
    stopPolling()
    recorderRef.current = null
    setLevel(0)
    setElapsedMs(0)
    setPhase('idle')
  }, [stopPolling])

  const finishRef = useRef<() => Promise<void>>(async () => {})

  // 路由切走/组件卸载：录音中必须立刻释放麦克风，不能只停轮询定时器
  useEffect(() => () => {
    recorderRef.current?.abort()
    recorderRef.current = null
    stopPolling()
  }, [stopPolling])

  const start = useCallback(async () => {
    if (recorderRef.current || startingRef.current) return
    if (!voiceCaptureSupported()) {
      notifyError(RECORDER_ERROR_MESSAGES['insecure-context'])
      return
    }
    startingRef.current = true
    try {
      recorderRef.current = await createVoiceRecorder()
      autoStoppedRef.current = false
      startedAtRef.current = performance.now()
      setPhase('recording')
      pollTimerRef.current = window.setInterval(() => {
        const recorder = recorderRef.current
        if (!recorder) return
        const elapsed = performance.now() - startedAtRef.current
        setLevel(recorder.level)
        setElapsedMs(elapsed)
        // 到达自动停止线：替用户收尾转写，避免录满 120s 才被后端 413 拒掉
        if (elapsed >= MAX_RECORDING_SECONDS * 1000 && !autoStoppedRef.current) {
          autoStoppedRef.current = true
          toast.info('已达到单次录音上限，自动停止并转写')
          void finishRef.current()
        }
      }, 100)
    } catch (error) {
      recorderRef.current = null
      const code = error instanceof Error ? error.message : ''
      notifyError(RECORDER_ERROR_MESSAGES[code] ?? errorText(error, '无法启动录音'))
    } finally {
      startingRef.current = false
    }
  }, [])

  const finish = useCallback(async () => {
    const recorder = recorderRef.current
    if (!recorder) return
    setPhase('transcribing')
    try {
      const recording = await recorder.stop()
      const result = await superAssistantApi.transcribeVoice(recording.wav, optionsRef.current.getLanguage())
      if (result.text) {
        optionsRef.current.onTranscript(result.text)
      } else {
        toast.info('没有识别到语音内容', { description: '请靠近麦克风后重试' })
      }
    } catch (error) {
      if (isRecorderError(error, 'too-short')) {
        toast.info(RECORDER_ERROR_MESSAGES['too-short'])
      } else if (error instanceof Error && error.message in RECORDER_ERROR_MESSAGES) {
        notifyError(RECORDER_ERROR_MESSAGES[error.message])
      } else {
        notifyError(errorText(error, '转写失败，请重试'))
      }
    } finally {
      teardown()
    }
  }, [teardown])

  finishRef.current = finish

  const cancel = useCallback(() => {
    recorderRef.current?.abort()
    teardown()
  }, [teardown])

  return { phase, level, elapsedMs, start, finish, cancel }
}
