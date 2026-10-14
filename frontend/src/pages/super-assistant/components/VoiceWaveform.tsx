import { memo, useEffect, useRef } from 'react'

import type { VoiceRecorder } from '@/lib/voice/recorder'
import type { RefObject } from 'react'

/** 一个刻度折叠的时长窗口（秒）：窗口内取峰值，落定后成为一条 mark */
const SAMPLE_SECONDS = 0.075
/** 刻度中心距与宽度（px），视觉规格同 cc-haha */
const SPACING = 5
const MARK_WIDTH = 2.5
/** 低于该电平保持圆点：房间是安静的 */
const QUIET = 0.06
/** 最老的刻度淡出到最新的这个比例 */
const OLDEST_ALPHA = 0.5
/** 左缘溶解像素数：刻度滑出时淡去而不是被切断 */
const EDGE_FADE = 28
/** 尚未录到的区域以这个透明度画占位圆点 */
const UNRECORDED_ALPHA = 0.26
/** 电平包络：随音节抬升、词间回落（秒） */
const ATTACK_SECONDS = 0.025
const RELEASE_SECONDS = 0.09
/** 单帧可折叠的最长时间：后台页切回时不猛跳 */
const MAX_FRAME_SECONDS = 0.1
/** 转写期间高光扫过冻结轨迹的周期与宽度 */
const SWEEP_MS = 1200
const SWEEP_WIDTH = 34
/** 真实语音 RMS 大致 0.02~0.25，放大让正常音量即接近满幅 */
const LEVEL_GAIN = 4
/** mark 数量上限：远超最宽输入框，更早的早已滚出屏外 */
const MAX_MARKS = 600

interface Props {
  /** 录音器引用：每帧读 level（直读分析窗，跟随帧率） */
  recorderRef: RefObject<VoiceRecorder | null>
  /** 转写中：停止监听、保留已录轨迹并让高光扫过 */
  frozen: boolean
}

interface Trace {
  /** 每个已完成窗口的峰值，旧在前 */
  marks: number[]
  /** 正在填充窗口的峰值：画为最新的那条 mark */
  peak: number
  /** 当前窗口已进行秒数；也即轨迹向下一格滚动的进度 */
  windowElapsed: number
  level: number
}

function readColor(canvas: HTMLCanvasElement, token: string, fallback: string): string {
  const style = getComputedStyle(canvas)
  return style.getPropertyValue(token).trim() || fallback
}

/**
 * 录音波形（cc-haha 式，交互规格与渲染机制参考其 MIT 实现）：
 * 安静时是圆点、说话时竖条起伏、旧刻度更淡、整条向左连续滚动。
 *
 * 流畅感的机制：requestAnimationFrame 逐帧绘制；电平经攻击/释放指数
 * 包络（随音节抬升、词间回落），每个 75ms 窗口取峰值落为一条 mark，
 * 窗口内的进度让整条轨迹连续位移——刻度是滑走的而不是逐拍弹入。
 * 转写阶段冻结轨迹并由高光周期性扫过。全部状态存 ref + canvas 直绘，
 * 电平不触碰 React 状态，页面不因波形重渲染。
 */
export const VoiceWaveform = memo(function VoiceWaveform({ recorderRef, frozen }: Props) {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const recorderRefStable = useRef(recorderRef)
  recorderRefStable.current = recorderRef
  const traceRef = useRef<Trace>({ marks: [], peak: 0, windowElapsed: 0, level: 0 })

  useEffect(() => {
    const canvas = canvasRef.current
    const context = canvas?.getContext('2d')
    if (!canvas || !context) return

    const height = 28
    const trace = traceRef.current
    const reducedMotion = typeof window.matchMedia === 'function'
      ? window.matchMedia('(prefers-reduced-motion: reduce)')
      : null
    let width = 0
    let pixelRatio = 1
    let voiceColor = ''
    let quietColor = ''
    let frame: number | null = null
    let lastTime = 0
    let sweepOrigin: number | null = null

    const drawMark = (x: number, markHeight: number) => {
      const top = height / 2 - markHeight / 2
      const left = x - MARK_WIDTH / 2
      context.beginPath()
      // Safari 16 前没有 roundRect：退化为方头，损失可忽略
      if (typeof context.roundRect === 'function') {
        context.roundRect(left, top, MARK_WIDTH, markHeight, MARK_WIDTH / 2)
        context.fill()
      } else {
        context.fillRect(left, top, MARK_WIDTH, markHeight)
      }
    }

    const paint = (now: number) => {
      if (width <= 0) return
      context.setTransform(pixelRatio, 0, 0, pixelRatio, 0, 0)
      context.clearRect(0, 0, width, height)

      const tallest = height - 6
      const count = Math.floor((width - 4) / SPACING) + 2
      const progress = trace.windowElapsed / SAMPLE_SECONDS
      let sweep: number | null = null
      if (frozen && !reducedMotion?.matches) {
        // 起止都完全在条外，高光不在边缘突现
        const reach = SWEEP_WIDTH * 2
        sweepOrigin ??= now
        sweep = (((now - sweepOrigin) % SWEEP_MS) / SWEEP_MS) * (width + 2 * reach) - reach
      }

      for (let index = 0; index < count; index += 1) {
        const x = width - 3 - (index + progress) * SPACING
        if (x < 1) break
        const edge = Math.min(1, x / EDGE_FADE)
        const value = index === 0 ? trace.peak : trace.marks[trace.marks.length - index]
        if (value === undefined) {
          context.globalAlpha = UNRECORDED_ALPHA * edge
          context.fillStyle = quietColor
          drawMark(x, MARK_WIDTH)
          continue
        }
        const voiced = value >= QUIET
        const markHeight = voiced
          ? Math.min(tallest, MARK_WIDTH + (tallest - MARK_WIDTH) * Math.pow(value, 0.8))
          : MARK_WIDTH
        let alpha = (1 - (1 - OLDEST_ALPHA) * (index / count)) * edge
        if (frozen) {
          alpha *= sweep === null ? 0.6 : 0.35 + 0.65 * Math.exp(-(((x - sweep) / SWEEP_WIDTH) ** 2))
        }
        context.globalAlpha = alpha
        context.fillStyle = voiced ? voiceColor : quietColor
        drawMark(x, markHeight)
      }
      context.globalAlpha = 1
    }

    const listen = (elapsed: number) => {
      const target = Math.min(1, (recorderRefStable.current.current?.level ?? 0) * LEVEL_GAIN)
      const seconds = target > trace.level ? ATTACK_SECONDS : RELEASE_SECONDS
      trace.level += (target - trace.level) * (1 - Math.exp(-elapsed / seconds))
      trace.peak = Math.max(trace.peak, trace.level)
      trace.windowElapsed += elapsed
      while (trace.windowElapsed >= SAMPLE_SECONDS) {
        trace.marks.push(trace.peak)
        trace.peak = trace.level
        trace.windowElapsed -= SAMPLE_SECONDS
      }
      if (trace.marks.length > MAX_MARKS) trace.marks.splice(0, trace.marks.length - MAX_MARKS)
    }

    const tick = (now: number) => {
      const elapsed = lastTime ? Math.min(MAX_FRAME_SECONDS, (now - lastTime) / 1000) : 0
      lastTime = now
      if (!frozen) listen(elapsed)
      paint(now)
      frame = requestAnimationFrame(tick)
    }
    const start = () => {
      if (frame !== null || document.hidden) return
      lastTime = 0
      frame = requestAnimationFrame(tick)
    }
    const stop = () => {
      if (frame === null) return
      cancelAnimationFrame(frame)
      frame = null
    }
    // 隐藏窗口空转循环没有意义；回前台恢复
    const onVisibilityChange = () => (document.hidden ? stop() : start())

    const layout = (nextWidth: number) => {
      width = Math.max(0, Math.round(nextWidth))
      pixelRatio = window.devicePixelRatio || 1
      canvas.width = Math.round(width * pixelRatio)
      canvas.height = Math.round(height * pixelRatio)
      voiceColor = readColor(canvas, '--color-text-secondary', getComputedStyle(canvas).color)
      quietColor = readColor(canvas, '--color-text-tertiary', voiceColor)
      paint(performance.now())
    }

    const observer = typeof ResizeObserver === 'function'
      ? new ResizeObserver(entries => {
        const entry = entries[entries.length - 1]
        if (entry) layout(entry.contentRect.width)
      })
      : null
    observer?.observe(canvas)
    layout(canvas.clientWidth)

    // 冻结且用户要求减少动效：一张静止图即可，不启循环
    const animate = !frozen || !reducedMotion?.matches
    if (animate) {
      document.addEventListener('visibilitychange', onVisibilityChange)
      start()
    }
    return () => {
      stop()
      observer?.disconnect()
      document.removeEventListener('visibilitychange', onVisibilityChange)
    }
  }, [frozen])

  return (
    <canvas
      ref={canvasRef}
      aria-hidden="true"
      data-testid="super-assistant-voice-waveform"
      className="block h-7 min-w-0 flex-1"
    />
  )
})
