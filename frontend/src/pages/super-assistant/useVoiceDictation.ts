import { useCallback, useEffect, useRef, useState, type RefObject } from 'react'
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

/** 前端自动停止线：留出余量低于后端 120s 上限，避免录满才吃 413。
 *  录音条按此自动收尾（间隔定时器在录音条内驱动）。 */
export const MAX_RECORDING_SECONDS = 110

export interface VoiceDictation {
  phase: DictationPhase
  /** 录音器引用：录音条自行 100ms 轮询 level/elapsed 并把状态留在
   *  录音条局部——避免 10Hz 更新触发整个聊天页面（含消息列表）重渲染 */
  recorderRef: RefObject<VoiceRecorder | null>
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
 * 高频状态（电平/计时）刻意留在录音条组件内，本 hook 只维护低频 phase。
 */
export function useVoiceDictation(options: {
  getLanguage: () => string
  onTranscript: (text: string) => void
}): VoiceDictation {
  const [phase, setPhase] = useState<DictationPhase>('idle')

  const recorderRef = useRef<VoiceRecorder | null>(null)
  // getUserMedia 是异步授权：await 返回前 phase 仍是 idle，按钮防重入
  // 覆盖不到这个窗口，必须用同步标记挡住连点
  const startingRef = useRef(false)
  // 手动停止/自动停止/取消的互斥：第二路调用静默返回，
  // 不再弹出“当前没有进行中的录音”这类误导性错误
  const finishingRef = useRef(false)
  const optionsRef = useRef(options)
  optionsRef.current = options

  const teardown = useCallback(() => {
    recorderRef.current = null
    finishingRef.current = false
    setPhase('idle')
  }, [])

  // 路由切走/组件卸载：录音中必须立刻释放麦克风，不能只停轮询定时器
  useEffect(() => () => {
    recorderRef.current?.abort()
    recorderRef.current = null
  }, [])

  const start = useCallback(async () => {
    if (recorderRef.current || startingRef.current) return
    if (!voiceCaptureSupported()) {
      notifyError(RECORDER_ERROR_MESSAGES['insecure-context'])
      return
    }
    startingRef.current = true
    try {
      recorderRef.current = await createVoiceRecorder({
        // 麦克风热拔/系统抢占：录音已被系统终止且数据不可用
        onInterrupt: () => {
          toast.error('麦克风已断开', { description: '本次录音已取消，请检查麦克风后重试' })
          teardown()
        },
      })
      finishingRef.current = false
      setPhase('recording')
    } catch (error) {
      recorderRef.current = null
      const code = error instanceof Error ? error.message : ''
      notifyError(RECORDER_ERROR_MESSAGES[code] ?? errorText(error, '无法启动录音'))
    } finally {
      startingRef.current = false
    }
  }, [teardown])

  const finish = useCallback(async () => {
    if (!recorderRef.current || finishingRef.current) return
    finishingRef.current = true
    setPhase('transcribing')
    try {
      const recording = await recorderRef.current.stop()
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

  const cancel = useCallback(() => {
    recorderRef.current?.abort()
    teardown()
  }, [teardown])

  return { phase, recorderRef, start, finish, cancel }
}
