import {
  type KeyboardEvent as ReactKeyboardEvent,
  type PointerEvent as ReactPointerEvent,
  type ReactNode,
  type WheelEvent as ReactWheelEvent,
  useCallback,
  useLayoutEffect,
  useRef,
  useState,
} from 'react'
import { Maximize, Minus, Move, Plus } from 'lucide-react'

interface ViewTransform {
  scale: number
  x: number
  y: number
}

interface InteractiveViewportProps {
  children: ReactNode
  contentWidth: number
  contentHeight: number
  ariaLabel: string
  className?: string
  testId?: string
}

const clamp = (value: number, min: number, max: number) =>
  Math.min(max, Math.max(min, value))

/**
 * A dependency-free pan/zoom surface used by generated diagrams and images.
 * Wheel zoom is anchored at the pointer; pointer drag works with mouse, pen,
 * and touch. The initial/reset view always fits the complete asset.
 */
export default function InteractiveViewport({
  children,
  contentWidth,
  contentHeight,
  ariaLabel,
  className = '',
  testId = 'interactive-viewport',
}: InteractiveViewportProps) {
  const viewportRef = useRef<HTMLDivElement>(null)
  const interactionRef = useRef(false)
  const dragRef = useRef<{
    pointerId: number
    startX: number
    startY: number
    originX: number
    originY: number
  } | null>(null)
  const [dragging, setDragging] = useState(false)
  const [view, setView] = useState<ViewTransform>({ scale: 1, x: 0, y: 0 })

  const fit = useCallback(() => {
    const viewport = viewportRef.current
    if (!viewport || contentWidth <= 0 || contentHeight <= 0) return
    const rect = viewport.getBoundingClientRect()
    if (rect.width <= 0 || rect.height <= 0) return
    const padding = 32
    const scale = clamp(Math.min(
      Math.max(1, rect.width - padding) / contentWidth,
      Math.max(1, rect.height - padding) / contentHeight,
      1,
    ), 0.05, 4)
    setView({
      scale,
      x: (rect.width - contentWidth * scale) / 2,
      y: (rect.height - contentHeight * scale) / 2,
    })
  }, [contentHeight, contentWidth])

  useLayoutEffect(() => {
    interactionRef.current = false
    fit()
  }, [fit])

  useLayoutEffect(() => {
    const viewport = viewportRef.current
    if (!viewport || typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver(() => {
      if (!interactionRef.current) fit()
    })
    observer.observe(viewport)
    return () => observer.disconnect()
  }, [fit])

  const zoomAt = useCallback((nextScale: number, clientX?: number, clientY?: number) => {
    const viewport = viewportRef.current
    if (!viewport) return
    interactionRef.current = true
    const rect = viewport.getBoundingClientRect()
    const pointX = clientX == null ? rect.width / 2 : clientX - rect.left
    const pointY = clientY == null ? rect.height / 2 : clientY - rect.top
    setView(current => {
      const scale = clamp(nextScale, 0.05, 4)
      const contentX = (pointX - current.x) / current.scale
      const contentY = (pointY - current.y) / current.scale
      return {
        scale,
        x: pointX - contentX * scale,
        y: pointY - contentY * scale,
      }
    })
  }, [])

  const onWheel = (event: ReactWheelEvent<HTMLDivElement>) => {
    event.preventDefault()
    const factor = Math.exp(-event.deltaY * 0.0015)
    zoomAt(view.scale * factor, event.clientX, event.clientY)
  }

  const onPointerDown = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (event.button !== 0) return
    interactionRef.current = true
    dragRef.current = {
      pointerId: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      originX: view.x,
      originY: view.y,
    }
    event.currentTarget.setPointerCapture(event.pointerId)
    setDragging(true)
  }

  const onPointerMove = (event: ReactPointerEvent<HTMLDivElement>) => {
    const drag = dragRef.current
    if (!drag || drag.pointerId !== event.pointerId) return
    setView(current => ({
      ...current,
      x: drag.originX + event.clientX - drag.startX,
      y: drag.originY + event.clientY - drag.startY,
    }))
  }

  const finishDrag = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (dragRef.current?.pointerId !== event.pointerId) return
    dragRef.current = null
    setDragging(false)
    if (event.currentTarget.hasPointerCapture(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId)
    }
  }

  const reset = () => {
    interactionRef.current = false
    fit()
  }

  const onKeyDown = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    if (event.key === '+' || event.key === '=') {
      event.preventDefault()
      zoomAt(view.scale * 1.2)
    } else if (event.key === '-') {
      event.preventDefault()
      zoomAt(view.scale / 1.2)
    } else if (event.key === '0' || event.key === 'Home') {
      event.preventDefault()
      reset()
    }
  }

  return (
    <div
      ref={viewportRef}
      data-testid={testId}
      data-scale={view.scale.toFixed(4)}
      data-offset-x={view.x.toFixed(1)}
      data-offset-y={view.y.toFixed(1)}
      role="region"
      aria-label={ariaLabel}
      tabIndex={0}
      className={`group/viewport relative isolate overflow-hidden bg-white outline-none touch-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring ${dragging ? 'cursor-grabbing' : 'cursor-grab'} ${className}`}
      onWheel={onWheel}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={finishDrag}
      onPointerCancel={finishDrag}
      onDoubleClick={reset}
      onKeyDown={onKeyDown}
    >
      <div
        className="absolute left-0 top-0 select-none will-change-transform"
        style={{
          width: contentWidth,
          height: contentHeight,
          transform: `translate3d(${view.x}px, ${view.y}px, 0) scale(${view.scale})`,
          transformOrigin: '0 0',
        }}
      >
        {children}
      </div>

      <div
        className="absolute right-3 top-3 z-10 flex items-center gap-1 rounded-lg border border-slate-200/90 bg-white/95 p-1 shadow-sm backdrop-blur"
        onPointerDown={event => event.stopPropagation()}
        onDoubleClick={event => event.stopPropagation()}
      >
        <span className="hidden items-center gap-1 px-1.5 text-[10px] text-slate-500 sm:inline-flex">
          <Move size={11} /> 拖拽移动
        </span>
        <button type="button" aria-label="缩小" title="缩小（-）" onClick={() => zoomAt(view.scale / 1.2)} className="rounded p-1.5 text-slate-600 hover:bg-slate-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
          <Minus size={13} />
        </button>
        <span className="w-11 text-center font-mono text-[10px] tabular-nums text-slate-500">{Math.round(view.scale * 100)}%</span>
        <button type="button" aria-label="放大" title="放大（+）" onClick={() => zoomAt(view.scale * 1.2)} className="rounded p-1.5 text-slate-600 hover:bg-slate-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
          <Plus size={13} />
        </button>
        <button type="button" aria-label="适应窗口" title="适应窗口（0 / Home / 双击）" onClick={reset} className="rounded p-1.5 text-slate-600 hover:bg-slate-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
          <Maximize size={13} />
        </button>
      </div>

      <div className="pointer-events-none absolute bottom-2 left-1/2 -translate-x-1/2 rounded bg-slate-900/65 px-2 py-1 text-[10px] text-white opacity-0 transition-opacity group-hover/viewport:opacity-100 group-focus-within/viewport:opacity-100">
        滚轮缩放 · 拖拽移动 · 双击适应窗口
      </div>
    </div>
  )
}
