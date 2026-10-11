import { useEffect, useRef, type RefObject } from 'react'

import type { VoiceRecorder } from '@/lib/voice/recorder'

/** cc-haha 波形视觉规格：2px 宽竖条 + 3px 间距，静音为 2px 小圆点 */
const BAR_WIDTH = 2
const BAR_GAP = 3
const MIN_BAR_HEIGHT = 2
/** 真实语音 RMS 大致落在 0.02~0.25，放大 4 倍让正常音量即可接近满幅 */
const LEVEL_GAIN = 4
/** 采样历史安全上限（110s 录音约 1100 个采样，超限裁掉最旧的一半） */
const MAX_SAMPLES = 4000

/**
 * 录音波形（cc-haha 式）：canvas 逐条绘制细竖条，新采样从右侧进入、
 * 超出宽度的旧采样被裁掉（右端永远是最新）。采样历史存 ref、绘制走
 * canvas，不产生 React 状态更新——10Hz 零重渲染。颜色继承 CSS color
 * （text-secondary 令牌），浅色/深色主题即时跟随；active=false 后停止
 * 采样但保留最后一帧（转写阶段波形定格）。
 */
export default function VoiceWaveform({
  recorderRef,
  active,
}: {
  recorderRef: RefObject<VoiceRecorder | null>
  active: boolean
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const samplesRef = useRef<number[]>([])

  useEffect(() => {
    if (!active) return
    const canvas = canvasRef.current
    const ctx = canvas?.getContext('2d')
    if (!canvas || !ctx) return

    const draw = () => {
      const dpr = window.devicePixelRatio || 1
      const width = canvas.clientWidth
      const height = canvas.clientHeight
      if (width === 0 || height === 0) return
      const pixelWidth = Math.round(width * dpr)
      const pixelHeight = Math.round(height * dpr)
      if (canvas.width !== pixelWidth || canvas.height !== pixelHeight) {
        canvas.width = pixelWidth
        canvas.height = pixelHeight
      }
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
      ctx.clearRect(0, 0, width, height)
      ctx.fillStyle = getComputedStyle(canvas).color
      const capacity = Math.max(1, Math.floor(width / (BAR_WIDTH + BAR_GAP)))
      const samples = samplesRef.current.slice(-capacity)
      const firstX = width - samples.length * (BAR_WIDTH + BAR_GAP)
      for (let index = 0; index < samples.length; index++) {
        const amp = Math.min(1, samples[index] * LEVEL_GAIN)
        const barHeight = Math.max(MIN_BAR_HEIGHT, MIN_BAR_HEIGHT + amp * (height - MIN_BAR_HEIGHT))
        ctx.fillRect(firstX + index * (BAR_WIDTH + BAR_GAP), (height - barHeight) / 2, BAR_WIDTH, barHeight)
      }
    }

    const timer = window.setInterval(() => {
      samplesRef.current.push(recorderRef.current?.level ?? 0)
      if (samplesRef.current.length > MAX_SAMPLES) {
        samplesRef.current = samplesRef.current.slice(-Math.floor(MAX_SAMPLES / 2))
      }
      draw()
    }, 100)
    draw()
    return () => window.clearInterval(timer)
  }, [active, recorderRef])

  return (
    <canvas
      ref={canvasRef}
      aria-hidden="true"
      data-testid="super-assistant-voice-waveform"
      className="h-7 min-w-0 flex-1 text-[var(--color-text-secondary)]"
    />
  )
}
