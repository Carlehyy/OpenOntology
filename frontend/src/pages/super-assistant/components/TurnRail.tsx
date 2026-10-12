import { useCallback, useEffect, useRef, useState, type RefObject } from 'react'

import type { TurnRailItem } from './turnRailModel'

/** 聊天滚动区宽度低于该值时不渲染竖条：内容列 max-w-4xl(896px) 居中，
 *  低于阈值时左缘没有足够留白，刻度会压到消息文本上 */
const RAIL_HOST_MIN_WIDTH = 1024

/** 视口顶缘向下该距离内的轮次视为「当前轮次」（与贴底判定同一量级） */
const ACTIVE_TOP_OFFSET = 96

/** 预览面板估算高度：用于把靠近底部的预览抬回导航条内 */
const PREVIEW_PANEL_HEIGHT = 104

function previewText(item: TurnRailItem) {
  if (item.assistantPreviewKind === 'text') return item.assistantPreview
  if (item.assistantPreviewKind === 'running') return '助手正在回复…'
  return '暂无助手回复'
}

/** 悬停刻度的鱼眼波及：命中刻度放大变实，±1/±2 依次衰减（对齐 zcode Turn Navigator） */
function fisheyeOf(distance: number) {
  if (distance === 0) return { scale: 2.6, opacity: 1 }
  if (distance === 1) return { scale: 1.7, opacity: 0.86 }
  if (distance === 2) return { scale: 1.25, opacity: 0.72 }
  return { scale: 1, opacity: 0.58 }
}

interface TurnRailProps {
  items: TurnRailItem[]
  scrollHostRef: RefObject<HTMLDivElement | null>
  onJump: (item: TurnRailItem) => void
}

/** 对话问题导航竖条：聊天区左缘按提问轮次排列的刻度列，
 *  hover 出两段式预览、点击定位到该提问、实时高亮视口所在轮次。
 *  刻度少于 2 根或聊天区过窄时整体不渲染 */
export function TurnRail({ items, scrollHostRef, onJump }: TurnRailProps) {
  const [hostWidth, setHostWidth] = useState(0)
  const [activeIndex, setActiveIndex] = useState(0)
  const [hover, setHover] = useState<{ index: number; top: number } | null>(null)
  const navRef = useRef<HTMLElement | null>(null)
  const stripRef = useRef<HTMLDivElement | null>(null)

  const visible = items.length >= 2 && hostWidth >= RAIL_HOST_MIN_WIDTH

  // 聊天区宽度受右侧配置面板拖拽影响。内嵌 webview（如 ZCode IAB）实测存在
  // 两种脏态：ResizeObserver 首回调不派发、或派发 0 宽脏值；挂载瞬间布局
  // 未就绪时 clientWidth 也可能读到 0——单次测量会把这些瞬态固化成「竖条
  // 永不渲染」。因此初测用有界重试（每次从 ref 现取宿主），RO 回调忽略 0 宽
  // 脏值，并常驻 window resize 兜底
  useEffect(() => {
    let timer = 0
    let tries = 0
    const measure = () => {
      const host = scrollHostRef.current
      if (!host || host.clientWidth === 0) {
        if (tries < 40) {
          tries += 1
          timer = window.setTimeout(measure, 150)
        }
        return
      }
      setHostWidth(host.clientWidth)
    }
    measure()
    const onResize = () => {
      const host = scrollHostRef.current
      if (host && host.clientWidth > 0) setHostWidth(host.clientWidth)
    }
    window.addEventListener('resize', onResize)
    let observer: ResizeObserver | undefined
    if (typeof ResizeObserver !== 'undefined') {
      observer = new ResizeObserver(entries => {
        const width = entries[0]?.contentRect.width ?? 0
        if (width > 0) setHostWidth(width)
      })
      const host = scrollHostRef.current
      if (host) observer.observe(host)
    }
    return () => {
      window.clearTimeout(timer)
      window.removeEventListener('resize', onResize)
      observer?.disconnect()
    }
  }, [scrollHostRef, visible])

  const measureActive = useCallback(() => {
    const host = scrollHostRef.current
    if (!host || items.length === 0) return
    const hostTop = host.getBoundingClientRect().top
    let next = 0
    for (let index = 0; index < items.length; index += 1) {
      const anchor = document.getElementById(`super-assistant-msg-${items[index].userMessageId}`)
      if (!anchor) continue
      if (anchor.getBoundingClientRect().top - hostTop <= ACTIVE_TOP_OFFSET) next = index
      else break
    }
    setActiveIndex(prev => (prev === next ? prev : next))
  }, [items, scrollHostRef])

  // 视口所在轮次跟随滚动实时更新（rAF 节流）
  useEffect(() => {
    const host = scrollHostRef.current
    if (!host || !visible) return
    let frame = 0
    const onScroll = () => {
      if (frame) return
      frame = requestAnimationFrame(() => {
        frame = 0
        measureActive()
      })
    }
    host.addEventListener('scroll', onScroll, { passive: true })
    measureActive()
    return () => {
      host.removeEventListener('scroll', onScroll)
      if (frame) cancelAnimationFrame(frame)
    }
  }, [visible, measureActive, scrollHostRef])

  // 当前刻度被 rail 内部滚动移出可见段时拉回（流式贴底时最新刻度始终可见）
  useEffect(() => {
    const strip = stripRef.current
    if (!strip) return
    const item = strip.querySelector<HTMLElement>(`[data-index="${activeIndex}"]`)
    if (!item) return
    if (item.offsetTop < strip.scrollTop || item.offsetTop > strip.scrollTop + strip.clientHeight - 12) {
      strip.scrollTop = item.offsetTop - strip.clientHeight / 2
    }
  }, [activeIndex, visible])

  const showPreview = (index: number, tick: HTMLElement) => {
    const nav = navRef.current
    if (!nav) {
      setHover({ index, top: tick.offsetTop })
      return
    }
    const top = tick.getBoundingClientRect().top - nav.getBoundingClientRect().top - 8
    setHover({ index, top: Math.min(Math.max(top, 8), Math.max(nav.clientHeight - PREVIEW_PANEL_HEIGHT, 8)) })
  }

  if (!visible) return null
  const hovered = hover ? items[hover.index] : null

  return (
    <nav
      ref={node => { navRef.current = node }}
      aria-label="对话问题导航"
      data-testid="super-assistant-turn-rail"
      className="pointer-events-none absolute inset-y-0 left-0 z-10 w-12"
    >
      <div
        ref={stripRef}
        onMouseLeave={() => setHover(null)}
        className="pointer-events-auto absolute left-3 top-1/2 max-h-[calc(100%-6rem)] w-9 -translate-y-1/2 overflow-x-hidden overflow-y-auto py-1 [scrollbar-width:none] [&::-webkit-scrollbar]:hidden"
      >
        {items.map((item, index) => {
          const distance = hover ? Math.abs(index - hover.index) : -1
          const fisheye = distance >= 0 ? fisheyeOf(distance) : null
          const isActive = index === activeIndex
          const opacity = fisheye ? fisheye.opacity : isActive ? 0.9 : item.isRunning ? 0.72 : 0.58
          const tone = distance === 0 || isActive
            ? 'bg-[var(--color-text-primary)]'
            : item.isRunning
              ? 'bg-brand'
              : 'bg-[var(--color-text-tertiary)]'
          return (
            <button
              key={item.userMessageId}
              type="button"
              data-turn-rail-item=""
              data-index={index}
              data-active={isActive ? 'true' : 'false'}
              data-running={item.isRunning ? 'true' : 'false'}
              aria-label={`跳转到第 ${index + 1} 条提问`}
              aria-posinset={index + 1}
              aria-setsize={items.length}
              aria-current={isActive ? 'location' : undefined}
              onMouseEnter={event => showPreview(index, event.currentTarget)}
              onClick={() => onJump(item)}
              className="flex h-2.5 w-9 items-center rounded-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              <span
                className={`block h-0.5 w-3 origin-left rounded-full transition-[transform,opacity,background-color] duration-150 ease-out motion-reduce:transition-none ${tone}`}
                style={{ transform: `scaleX(${fisheye ? fisheye.scale : 1})`, opacity }}
              />
            </button>
          )
        })}
      </div>
      {hovered && (
        <div
          data-testid="super-assistant-turn-rail-preview"
          className="pointer-events-none absolute left-full z-20 ml-2 w-72 rounded-lg border border-[var(--color-border)] bg-card p-3 text-left shadow-lg"
          style={{ top: hover ? hover.top : 8 }}
        >
          <p className="line-clamp-2 text-xs font-medium leading-5 text-[var(--color-text-primary)]">
            {hovered.userPreview || '用户输入'}
          </p>
          <p className="mt-1.5 line-clamp-3 text-xs leading-5 text-[var(--color-text-tertiary)]">
            {previewText(hovered)}
          </p>
        </div>
      )}
    </nav>
  )
}
