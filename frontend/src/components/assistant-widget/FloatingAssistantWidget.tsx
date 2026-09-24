import { lazy, Suspense, useEffect, useLayoutEffect, useMemo, useRef, useState, type PointerEvent as ReactPointerEvent } from 'react'
import { useLocation } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { Loader2, Sparkles } from 'lucide-react'

import { superAssistantApi } from '@/api/superAssistant'
import { PLATFORM_NAV_ITEMS } from '@/config/navigation'
import { useAssistantWidgetStore } from '@/stores/assistantWidgetStore'
import {
  WIDGET_DRAG_THRESHOLD_PX,
  WIDGET_FAB_BOTTOM,
  WIDGET_POSITION_STORAGE_KEY,
  WIDGET_Z,
  clampWidgetPosition,
  parseWidgetPosition,
  widgetAnchor,
  widgetPanelPlacement,
  widgetVisibleOnPath,
  type WidgetViewportPoint,
} from '@/components/assistant-widget/logic'

// 面板依赖 antd / @ant-design/x，体积大，懒加载分包：首屏只承载这个轻量悬浮球
const AssistantWidgetPanel = lazy(() => import('./AssistantWidgetPanel'))

function placementFor(node: HTMLElement) {
  const rect = node.getBoundingClientRect()
  return widgetPanelPlacement(
    { left: rect.left, top: rect.top, width: rect.width, height: rect.height },
    { width: window.innerWidth, height: window.innerHeight },
    {
      width: Math.min(384, window.innerWidth - 40),
      height: Math.min(600, window.innerHeight - 112),
    },
  )
}

function readStoredPosition(): WidgetViewportPoint | null {
  if (typeof window === 'undefined') return null
  return parseWidgetPosition(window.localStorage.getItem(WIDGET_POSITION_STORAGE_KEY))
}

function writeStoredPosition(point: WidgetViewportPoint | null) {
  try {
    if (!point) window.localStorage.removeItem(WIDGET_POSITION_STORAGE_KEY)
    else window.localStorage.setItem(WIDGET_POSITION_STORAGE_KEY, JSON.stringify(point))
  } catch {
    // 隐私模式或配额不足时，本次会话内仍可拖动，只是刷新后回到默认位置。
  }
}

/** 面板首次加载（拉取 antd 分包）时的占位骨架，保持点击反馈即时可见 */
function PanelSkeleton({ placementClass }: { placementClass: string }) {
  return (
    <div className={`absolute ${placementClass} flex h-[min(600px,calc(100dvh-7rem))] w-[min(384px,calc(100vw-2.5rem))] items-center justify-center rounded-2xl border border-[var(--color-border)] bg-[var(--color-bg-elevated)] shadow-[0_24px_64px_rgba(15,23,42,0.22)]`}>
      <Loader2 size={22} className="animate-spin text-teal-600" />
    </div>
  )
}

/**
 * 全局悬浮 AI 助手入口。未拖动时停在右下角（个别页面按 widgetAnchor 上移避让）。
 * 按住球拖动可改位置，坐标写入 localStorage，不入库；点击（移动不足阈值）仍是开合。
 * 挂载于 Layout，自动排除登录页与公开分享页。
 * 层级按 widgetAnchor 分级（见 logic.ts）：常规页面 z-40
 * （让抽屉/模态/toast 正常覆盖），图谱编辑器等全屏覆盖层页面抬升至 z-[10000]。
 * 页面可见范围由管理员在 系统设置 → 超级助手 平台级配置（隐藏名单）；
 * 配置拉取失败或未配置时保持全部页面可见（与功能上线前一致）。
 */
export default function FloatingAssistantWidget() {
  const open = useAssistantWidgetStore(state => state.open)
  const streaming = useAssistantWidgetStore(state => state.streaming)
  const awaitingDecision = useAssistantWidgetStore(state => state.pending !== null)
  const toggle = useAssistantWidgetStore(state => state.toggle)
  const location = useLocation()
  const anchor = widgetAnchor(location.pathname)
  const [position, setPosition] = useState<WidgetViewportPoint | null>(readStoredPosition)
  const wrapperRef = useRef<HTMLDivElement>(null)
  const suppressClick = useRef(false)
  const dragRef = useRef<{
    pointerId: number
    startX: number
    startY: number
    originLeft: number
    originTop: number
    moved: boolean
  } | null>(null)
  const [placement, setPlacement] = useState<{ vertical: 'above' | 'below'; align: 'left' | 'right' }>({
    vertical: 'above',
    align: 'right',
  })

  const { data: widgetConfig } = useQuery({
    queryKey: ['assistant-widget-config'],
    queryFn: () => superAssistantApi.widgetConfig(),
    staleTime: 60_000,
    retry: 1,
  })
  const hiddenMenuKeys = useMemo(
    () => new Set(Array.isArray(widgetConfig?.hidden_menu_keys) ? widgetConfig.hidden_menu_keys : []),
    [widgetConfig],
  )
  const visible = widgetVisibleOnPath(location.pathname, PLATFORM_NAV_ITEMS, hiddenMenuKeys)

  useEffect(() => {
    const syncToViewport = () => {
      setPosition(current => {
        if (!current) return current
        const next = clampWidgetPosition(current, { width: window.innerWidth, height: window.innerHeight })
        if (next.left === current.left && next.top === current.top) return current
        writeStoredPosition(next)
        return next
      })
      const node = wrapperRef.current
      if (!node) return
      const nextPlacement = placementFor(node)
      setPlacement(current => (
        current.vertical === nextPlacement.vertical && current.align === nextPlacement.align ? current : nextPlacement
      ))
    }
    syncToViewport()
    window.addEventListener('resize', syncToViewport)
    return () => window.removeEventListener('resize', syncToViewport)
  }, [])

  useLayoutEffect(() => {
    const node = wrapperRef.current
    if (!node) return
    const next = placementFor(node)
    setPlacement(current => (
      current.vertical === next.vertical && current.align === next.align ? current : next
    ))
  }, [position, open, location.pathname, anchor, visible])

  if (!visible) return null

  const placementClass = `${placement.vertical === 'above' ? 'bottom-[calc(100%+0.5rem)]' : 'top-[calc(100%+0.5rem)]'} ${placement.align === 'right' ? 'right-0' : 'left-0'}`
  const resetPosition = () => {
    writeStoredPosition(null)
    setPosition(null)
  }
  const onPointerDown = (event: ReactPointerEvent<HTMLButtonElement>) => {
    if (event.button !== 0) return
    const rect = wrapperRef.current?.getBoundingClientRect() ?? event.currentTarget.getBoundingClientRect()
    dragRef.current = {
      pointerId: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      originLeft: rect.left,
      originTop: rect.top,
      moved: false,
    }
    event.currentTarget.setPointerCapture(event.pointerId)
  }
  const onPointerMove = (event: ReactPointerEvent<HTMLButtonElement>) => {
    const drag = dragRef.current
    if (!drag || drag.pointerId !== event.pointerId) return
    const dx = event.clientX - drag.startX
    const dy = event.clientY - drag.startY
    if (!drag.moved && Math.hypot(dx, dy) < WIDGET_DRAG_THRESHOLD_PX) return
    drag.moved = true
    setPosition(clampWidgetPosition(
      { left: drag.originLeft + dx, top: drag.originTop + dy },
      { width: window.innerWidth, height: window.innerHeight },
    ))
  }
  const finishDrag = (event: ReactPointerEvent<HTMLButtonElement>) => {
    const drag = dragRef.current
    if (!drag || drag.pointerId !== event.pointerId) return
    if (drag.moved) {
      suppressClick.current = true
      const next = clampWidgetPosition(
        {
          left: drag.originLeft + (event.clientX - drag.startX),
          top: drag.originTop + (event.clientY - drag.startY),
        },
        { width: window.innerWidth, height: window.innerHeight },
      )
      writeStoredPosition(next)
      setPosition(next)
      // click 在 pointerup 之后、下一帧之前派发。没派发 click 时不要把下一次点击吃掉。
      requestAnimationFrame(() => { suppressClick.current = false })
    }
    dragRef.current = null
  }

  return (
    <div
      ref={wrapperRef}
      className={`fixed h-12 w-12 ${position ? '' : `right-5 ${WIDGET_FAB_BOTTOM[anchor]}`} ${WIDGET_Z[anchor]}`}
      style={position ? { left: position.left, top: position.top } : undefined}
    >
      {open && (
        <Suspense fallback={<PanelSkeleton placementClass={placementClass} />}>
          <AssistantWidgetPanel placementClass={placementClass} onResetPosition={position ? resetPosition : null} />
        </Suspense>
      )}
      <button
        type="button"
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={finishDrag}
        onPointerCancel={finishDrag}
        onClick={() => {
          if (suppressClick.current) {
            suppressClick.current = false
            return
          }
          toggle()
        }}
        aria-label={open ? '关闭 AI 助手' : '打开 AI 助手'}
        aria-expanded={open}
        title="AI 助手，按住可拖动位置"
        data-testid="assistant-widget-fab"
        className="relative flex h-12 w-12 cursor-grab touch-none items-center justify-center rounded-full bg-[var(--color-nav-bg)] text-white shadow-[0_10px_30px_rgba(13,148,136,0.35)] transition-transform hover:scale-105 hover:shadow-[0_14px_36px_rgba(13,148,136,0.45)] active:scale-95 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
      >
        <Sparkles size={22} />
        {(streaming || awaitingDecision) && !open && (
          <span className="absolute -right-0.5 -top-0.5 flex h-3 w-3" aria-hidden="true">
            <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-amber-400 opacity-75" />
            <span className="relative inline-flex h-3 w-3 rounded-full bg-amber-500" />
          </span>
        )}
      </button>
    </div>
  )
}
