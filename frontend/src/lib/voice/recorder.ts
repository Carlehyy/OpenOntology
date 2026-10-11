/**
 * 语音听写录音管道（浏览器侧，交互规格参考 cc-haha，MIT）：
 * createVoiceRecorder() 即申请麦克风并开始采集（getUserMedia +
 * MediaRecorder，AnalyserNode 提供实时电平）；stop() 后用独立
 * AudioContext 解码 → 纯 DSP 折叠/重采样到 16k 单声道 → 16-bit PCM WAV。
 * 误触保护：不足 MIN_DURATION_MS 的录音按“太短”处理。
 */
import { downmixToMono, resampleLinear, rmsLevel } from './dsp'
import { encodeWav } from './wav'

export const TARGET_SAMPLE_RATE = 16000
export const MIN_DURATION_MS = 400

export type RecorderError =
  | 'insecure-context'
  | 'permission-denied'
  | 'no-device'
  | 'too-short'
  | 'decode-failed'
  | 'not-recording'

export interface RecordingResult {
  wav: Uint8Array
  durationMs: number
}

export interface VoiceRecorder {
  /** 0~1 实时电平（最近一次分析窗的 RMS），未开始时为 0。 */
  readonly level: number
  stop: () => Promise<RecordingResult>
  /** 丢弃录音并立即释放麦克风（取消路径）。 */
  abort: () => void
}

export function recorderError(code: RecorderError): Error {
  return new Error(code)
}

export interface RecorderInterrupt {
  /** 麦克风被系统/用户中断（设备热拔、系统抢占）：录音已终止且数据不可用 */
  onInterrupt: (reason: 'device-lost') => void
}

export function isRecorderError(error: unknown, code: RecorderError): boolean {
  return error instanceof Error && error.message === code
}

export function voiceCaptureSupported(): boolean {
  return (
    typeof window !== 'undefined' &&
    window.isSecureContext === true &&
    !!navigator.mediaDevices?.getUserMedia &&
    typeof window.MediaRecorder !== 'undefined'
  )
}

function pickMimeType(): string | undefined {
  const candidates = ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4', 'audio/ogg']
  return candidates.find(type => MediaRecorder.isTypeSupported(type))
}

export async function createVoiceRecorder(interrupt?: RecorderInterrupt): Promise<VoiceRecorder> {
  if (!voiceCaptureSupported()) {
    throw recorderError('insecure-context')
  }

  let mediaRecorder: MediaRecorder | null = null
  let audioContext: AudioContext | null = null
  let analyser: AnalyserNode | null = null
  let levelTimer: number | null = null
  let chunks: Blob[] = []
  let startedAt = 0
  let aborted = false
  let currentLevel = 0
  // 自身发起的清理不再触发 onInterrupt（stop/abort 都先置位）
  let tornDown = false

  const cleanup = () => {
    if (levelTimer !== null) {
      window.clearInterval(levelTimer)
      levelTimer = null
    }
    analyser?.disconnect()
    if (audioContext) {
      audioContext.close().catch(() => undefined)
      audioContext = null
    }
    analyser = null
    // 直接从闭包持有的原始 stream 释放轨道：AudioContext 构造失败时
    // mediaRecorder 尚为 null，依赖它会漏掉麦克风
    stream.getTracks().forEach(track => track.stop())
    mediaRecorder = null
  }

  const handleInterrupt = () => {
    if (tornDown || aborted) return
    tornDown = true
    cleanup()
    interrupt?.onInterrupt('device-lost')
  }

  const stream = await navigator.mediaDevices
    .getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    })
    .catch((error: unknown) => {
      const name = (error as { name?: string })?.name ?? ''
      if (name === 'NotAllowedError' || name === 'SecurityError') {
        throw recorderError('permission-denied')
      }
      throw recorderError('no-device')
    })

  audioContext = new AudioContext()
  const source = audioContext.createMediaStreamSource(stream)
  analyser = audioContext.createAnalyser()
  analyser.fftSize = 512
  source.connect(analyser)
  const sampleBuffer = new Float32Array(analyser.fftSize)
  levelTimer = window.setInterval(() => {
    if (!analyser) return
    analyser.getFloatTimeDomainData(sampleBuffer)
    currentLevel = rmsLevel(sampleBuffer)
  }, 100)

  const mimeType = pickMimeType()
  mediaRecorder = new MediaRecorder(stream, mimeType ? { mimeType } : undefined)
  mediaRecorder.ondataavailable = event => {
    if (event.data.size > 0) chunks.push(event.data)
  }
  // 个别浏览器的非规范行为：出错后不发 stop——onerror 兜底保证
  // stop() promise 与资源释放必然结算
  mediaRecorder.onerror = () => handleInterrupt()
  // 麦克风热拔/系统抢占：track 提前 ended 且部分实现不补发 stop，
  // 已采数据不可信，按中断处理而不是让 UI 卡在录音态
  for (const track of stream.getTracks()) {
    track.addEventListener('ended', handleInterrupt)
  }
  mediaRecorder.start(250)
  startedAt = performance.now()

  const stop = () =>
    new Promise<RecordingResult>((resolve, reject) => {
      const recorder = mediaRecorder
      if (!recorder || recorder.state === 'inactive') {
        reject(recorderError('not-recording'))
        return
      }
      tornDown = true
      const durationMs = performance.now() - startedAt
      const blobType = recorder.mimeType || 'audio/webm'
      recorder.onstop = async () => {
        cleanup()
        if (aborted) {
          // abort() 只是释放资源、不关心结果；显式 reject 让悬挂的
          // stop() promise 必然结算，而非依赖调用方“不这样做”的约定
          reject(recorderError('not-recording'))
          return
        }
        if (durationMs < MIN_DURATION_MS) {
          reject(recorderError('too-short'))
          return
        }
        try {
          const blob = new Blob(chunks, { type: blobType })
          chunks = []
          const wav = await decodeToWav16kMono(await blob.arrayBuffer())
          resolve({ wav, durationMs })
        } catch {
          if (durationMs < MIN_DURATION_MS * 2) {
            // MediaRecorder 启动后极快停止会产出无法解码的坏块，按误触处理
            reject(recorderError('too-short'))
          } else {
            reject(recorderError('decode-failed'))
          }
        }
      }
      recorder.stop()
    })

  return {
    get level() {
      return currentLevel
    },
    stop,
    abort: () => {
      aborted = true
      tornDown = true
      cleanup()
    },
  }
}

/** 解码任意浏览器容器音频 → 16k 单声道 16-bit PCM WAV 字节。 */
async function decodeToWav16kMono(arrayBuffer: ArrayBuffer): Promise<Uint8Array> {
  const decodeContext = new AudioContext({ sampleRate: TARGET_SAMPLE_RATE })
  try {
    const buffer = await decodeContext.decodeAudioData(arrayBuffer.slice(0))
    const channels = buffer.numberOfChannels
    const interleaved = new Float32Array(buffer.length * channels)
    for (let channel = 0; channel < channels; channel += 1) {
      const data = buffer.getChannelData(channel)
      for (let index = 0; index < buffer.length; index += 1) {
        interleaved[index * channels + channel] = data[index]
      }
    }
    const mono = downmixToMono(interleaved, channels)
    const resampled = resampleLinear(mono, buffer.sampleRate, TARGET_SAMPLE_RATE)
    return encodeWav(resampled, TARGET_SAMPLE_RATE)
  } finally {
    decodeContext.close().catch(() => undefined)
  }
}
