/**
 * 本体卡片轮播：未选择本体时占据本体助手右侧工作区大区域。
 *
 * Coverflow 交互：
 *   - 卡片环形布局：最热卡居中，右侧按选用次数递减，末尾卡环绕到左侧；
 *   - 鼠标按住左右拖拽浏览，松手吸附；滚轮向下等价向右切换、向上等价向左；
 *     >=3 张时可单向无限循环；
 *   - 单击侧边卡片仅聚焦居中，单击聚焦卡片（或「开始使用」）才确认选中；
 *   - 左右箭头、指示点与键盘 ←/→/Enter 提供等价操作；
 *   - 焦点动画由 requestAnimationFrame 逐帧驱动，环绕接缝处卡片从一侧
 *     淡出、另一侧淡入，避免 CSS transition 造成的瞬移或横穿。
 * 卡片顺序由父级按全局选用次数排好（rankOntologyCards），这里只管呈现。
 */
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { ChevronLeft, ChevronRight, Flame, Network, Sparkles } from 'lucide-react'
import type { OntologyListItem } from '@/types/ontology'
import { OntologyAvatar } from '@/components/OntologyAvatar'
import { rankOntologyCards } from './ontologyCardRanking'
import { circularCardPosition, maxVisibleSideRings, normalizeCardIndex } from './ontologyCarouselMath'

const CARD_WIDTH = 300
const FOCUS_STEP_X = CARD_WIDTH * 0.7
const CLICK_SLOP_PX = 6
/** 滚轮累积滚动量达到该阈值才切换一张，避免触控板一次手势连切多张。 */
const WHEEL_SWITCH_THRESHOLD = 40
/** 两次滚轮切换的最小间隔（毫秒），吸收触控板惯性滚动。 */
const WHEEL_SWITCH_COOLDOWN_MS = 200

const clamp = (value: number, min: number, max: number) => Math.min(max, Math.max(min, value))

const prefersReducedMotion = () =>
  typeof window !== 'undefined'
  && typeof window.matchMedia === 'function'
  && window.matchMedia('(prefers-reduced-motion: reduce)').matches

function cardMetrics(item: OntologyListItem) {
  return [
    { label: '对象实体', value: item.entity_count ?? 0 },
    { label: '实体关系', value: item.relation_count ?? 0 },
    { label: '执行动作', value: item.action_count ?? 0 },
    { label: '哨兵引擎', value: item.sentinel_count ?? 0 },
  ]
}

export function OntologyCardCarousel({
  items,
  onSelect,
}: {
  items: OntologyListItem[]
  onSelect: (item: OntologyListItem) => void
}) {
  const navigate = useNavigate()
  const ranked = useMemo(() => rankOntologyCards(items), [items])
  const count = ranked.length
  const looped = count >= 3

  const [focus, setFocus] = useState(0)
  const [dragging, setDragging] = useState(false)
  const dragRef = useRef({ startX: 0, startFocus: 0, moved: false })
  const focusRef = useRef(0)
  const targetRef = useRef(0)
  const rafRef = useRef<number | null>(null)
  const stageRef = useRef<HTMLDivElement>(null)
  const [stageWidth, setStageWidth] = useState(0)

  const activeIndex = normalizeCardIndex(focus, count)

  // 依据面板宽度计算两侧最多完整展示的卡环数；分栏拖动或窗口缩放时随动。
  useLayoutEffect(() => {
    const stage = stageRef.current
    if (!stage) return
    const measure = () => setStageWidth(stage.clientWidth)
    measure()
    const observer = new ResizeObserver(measure)
    observer.observe(stage)
    return () => observer.disconnect()
  }, [count])
  const maxSide = maxVisibleSideRings(stageWidth, CARD_WIDTH, FOCUS_STEP_X)
  // 淡出边界需早于环绕接缝（count/2），保证接缝处的环绕传送始终不可见。
  const fadeBeyond = Math.min(maxSide + 0.45, looped ? count / 2 - 0.15 : 2)
  const hideBeyond = fadeBeyond + 0.25

  const cancelAnimation = useCallback(() => {
    if (rafRef.current !== null) {
      cancelAnimationFrame(rafRef.current)
      rafRef.current = null
    }
  }, [])

  const applyFocus = useCallback((next: number) => {
    focusRef.current = next
    targetRef.current = next
    setFocus(next)
  }, [])

  const animateTo = useCallback((target: number) => {
    targetRef.current = target
    if (prefersReducedMotion()) {
      cancelAnimation()
      applyFocus(target)
      return
    }
    if (rafRef.current !== null) return
    const step = () => {
      const delta = targetRef.current - focusRef.current
      if (Math.abs(delta) < 0.002) {
        applyFocus(targetRef.current)
        rafRef.current = null
        return
      }
      focusRef.current += delta * 0.16
      setFocus(focusRef.current)
      rafRef.current = requestAnimationFrame(step)
    }
    rafRef.current = requestAnimationFrame(step)
  }, [applyFocus, cancelAnimation])

  useEffect(() => cancelAnimation, [cancelAnimation])

  // 列表在轮播展示期间被刷新（如计数回填）时，把焦点收回有效范围。
  useEffect(() => {
    if (count === 0) return
    if (!looped && focusRef.current > count - 1) applyFocus(count - 1)
    if (looped) applyFocus(normalizeCardIndex(focusRef.current, count))
  }, [count, looped, applyFocus])

  /** 吸附到最近的整数焦点；looped 时不规整，保持环绕方向上的连续运动。 */
  const snapTo = useCallback((target: number) => {
    if (count === 0) return
    animateTo(looped ? target : clamp(target, 0, count - 1))
  }, [animateTo, count, looped])

  // 滚轮切换：向下滚与向右拖等价（切到下一张），向上滚与向左拖等价（切到上一张）。
  // 用原生非 passive 监听（与 OntologyNetworkView 的滚轮缩放同一模式），
  // 并按累积滚动量 + 冷却时间节流，避免触控板一次手势连切多张。
  useEffect(() => {
    const stage = stageRef.current
    if (!stage) return
    let accumulated = 0
    let lastSwitchAt = 0
    const handleWheel = (event: WheelEvent) => {
      event.preventDefault()
      if (count < 2) return
      const now = performance.now()
      if (now - lastSwitchAt < WHEEL_SWITCH_COOLDOWN_MS) {
        accumulated = 0
        return
      }
      accumulated += event.deltaY
      if (Math.abs(accumulated) < WHEEL_SWITCH_THRESHOLD) return
      const direction = accumulated > 0 ? 1 : -1
      accumulated = 0
      lastSwitchAt = now
      snapTo(Math.round(focusRef.current) + direction)
    }
    stage.addEventListener('wheel', handleWheel, { passive: false })
    return () => stage.removeEventListener('wheel', handleWheel)
  }, [count, snapTo])

  /** 侧边卡片点击聚焦：沿环形最短路径滑过去。 */
  const focusCard = useCallback((index: number) => {
    const delta = circularCardPosition(index, focusRef.current, count)
    animateTo(focusRef.current + delta)
  }, [animateTo, count])

  const handlePointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    if (e.button !== 0 || count === 0) return
    if ((e.target as Element).closest('button')) return
    cancelAnimation()
    e.currentTarget.setPointerCapture(e.pointerId)
    dragRef.current = { startX: e.clientX, startFocus: focusRef.current, moved: false }
    setDragging(true)
  }

  const handlePointerMove = (e: React.PointerEvent<HTMLDivElement>) => {
    if (!dragging) return
    const deltaX = e.clientX - dragRef.current.startX
    if (Math.abs(deltaX) > CLICK_SLOP_PX) dragRef.current.moved = true
    const raw = dragRef.current.startFocus - deltaX / FOCUS_STEP_X
    if (looped) {
      applyFocus(raw)
      return
    }
    // 非循环（1~2 张卡）边缘橡皮筋：越界拖拽施加阻尼，松手后回弹吸附。
    const resisted = raw < 0 ? raw * 0.35 : raw > count - 1 ? count - 1 + (raw - (count - 1)) * 0.35 : raw
    applyFocus(resisted)
  }

  const handlePointerUp = (e: React.PointerEvent<HTMLDivElement>) => {
    if (!dragging) return
    setDragging(false)
    if (e.currentTarget.hasPointerCapture(e.pointerId)) {
      e.currentTarget.releasePointerCapture(e.pointerId)
    }
    if (dragRef.current.moved) {
      snapTo(Math.round(focusRef.current))
      return
    }
    // 未拖动的按下-抬起视为点击：命中侧边卡片聚焦，命中聚焦卡片确认选中。
    const hit = document.elementFromPoint(e.clientX, e.clientY)?.closest('[data-card-index]')
    const hitIndex = hit ? Number((hit as HTMLElement).dataset.cardIndex) : NaN
    if (Number.isNaN(hitIndex)) return
    if (hitIndex === activeIndex) onSelect(ranked[hitIndex])
    else focusCard(hitIndex)
  }

  const handlePointerCancel = () => {
    if (!dragging) return
    setDragging(false)
    snapTo(Math.round(focusRef.current))
  }

  const handleKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
    if (count === 0) return
    if (e.key === 'ArrowLeft') {
      e.preventDefault()
      snapTo(Math.round(focusRef.current) - 1)
    } else if (e.key === 'ArrowRight') {
      e.preventDefault()
      snapTo(Math.round(focusRef.current) + 1)
    } else if (e.key === 'Enter') {
      e.preventDefault()
      onSelect(ranked[activeIndex])
    }
  }

  if (count === 0) {
    return (
      <div className="workspace-topology-surface flex h-full flex-col items-center justify-center px-6 text-center">
        <div className="mb-4 flex h-14 w-14 items-center justify-center rounded-2xl border border-[color-mix(in_srgb,var(--color-info)_35%,transparent)] bg-card text-[var(--color-info)] shadow-sm">
          <Network size={24} />
        </div>
        <h3 className="text-sm font-semibold text-foreground dark:text-foreground">暂无已发布本体</h3>
        <p className="mt-1 max-w-xs text-xs leading-relaxed text-muted-foreground dark:text-[var(--color-text-tertiary)]">
          先在本体管理中创建并发布一个本体，再回到这里开始智能对话。
        </p>
        <button
          type="button"
          onClick={() => navigate('/ontologies')}
          className="mt-4 inline-flex items-center gap-1.5 rounded-lg border border-brand-line bg-brand-soft px-4 py-2 text-sm font-medium text-brand-ink transition-colors hover:bg-brand-soft"
        >
          前往本体管理
        </button>
      </div>
    )
  }

  return (
    <div className="workspace-topology-surface flex h-full flex-col overflow-hidden">
      <div
        ref={stageRef}
        data-testid="ontology-card-carousel"
        role="listbox"
        aria-label="已发布本体卡片轮播"
        aria-activedescendant={`ontology-card-${ranked[activeIndex]?.id}`}
        tabIndex={0}
        onKeyDown={handleKeyDown}
        onPointerDown={handlePointerDown}
        onPointerMove={handlePointerMove}
        onPointerUp={handlePointerUp}
        onPointerCancel={handlePointerCancel}
        // isolate：卡片/箭头的 z-index（最高 110）只在舞台内部竞争。若不隔离，
        // 它们会进入根层叠上下文并压过全局悬浮 AI 助手面板（z-40）。
        className="isolate scrollbar-none relative min-h-0 flex-1 cursor-grab touch-none select-none outline-none focus-visible:ring-2 focus-visible:ring-ring active:cursor-grabbing"
      >
        {ranked.map((item, index) => {
          const pos = circularCardPosition(index, focus, count)
          const absPos = Math.abs(pos)
          const isFocused = index === activeIndex && !dragging
          const hidden = absPos > hideBeyond
          const clicks = item.assistant_card_clicks ?? 0
          const style: React.CSSProperties = {
            width: CARD_WIDTH,
            left: '50%',
            // 垂直方向居中略偏上，给指示点留出视觉余量。
            top: '46%',
            zIndex: 100 - Math.round(absPos * 10),
            opacity: absPos > fadeBeyond ? 0 : 1 - Math.min(absPos, 2) * 0.26,
            transform: [
              'translate(-50%, -50%)',
              `translateX(${pos * FOCUS_STEP_X}px)`,
              `translateY(${Math.min(absPos, 2) * 10}px)`,
              // 居中卡（pos 为 0）不施加 3D 透视与缩放：常驻 GPU 光栅化纹理会让
              // 文字、数字发虚；侧边卡保留浅幅 rotateY/scale 维持 coverflow 纵深感。
              ...(pos === 0 ? [] : [
                `perspective(1200px) rotateY(${clamp(-pos * 6, -20, 20)}deg)`,
                `scale(${1 - Math.min(absPos, 2.5) * 0.06})`,
              ]),
            ].join(' '),
            pointerEvents: hidden ? 'none' : 'auto',
            visibility: hidden ? 'hidden' : 'visible',
          }
          return (
            <div
              key={item.id}
              id={`ontology-card-${item.id}`}
              data-testid="ontology-card"
              data-card-index={index}
              data-focused={isFocused || undefined}
              role="option"
              aria-selected={index === activeIndex}
              aria-label={`本体卡片 ${item.name}`}
              style={style}
              className={`absolute flex flex-col overflow-hidden rounded-2xl border bg-card shadow-xl will-change-transform dark:bg-accent ${
                index === activeIndex
                  ? 'border-brand-line shadow-2xl ring-1 ring-ring dark:border-brand'
                  : 'border-border dark:border-[var(--color-border-hover)]'
              }`}
            >
              <div className="flex items-start gap-3 px-4 pb-3 pt-4">
                <OntologyAvatar icon={item.icon || undefined} size="lg" />
                <div className="min-w-0 flex-1">
                  <div
                    data-testid="ontology-card-name"
                    className="truncate text-[15px] font-semibold text-foreground dark:text-foreground"
                    title={item.name}
                  >
                    {item.name}
                  </div>
                  <div className="mt-1.5 flex min-w-0 items-center gap-1.5">
                    <span className="inline-flex min-w-0 max-w-full truncate rounded-md border border-brand-line bg-brand-soft px-2 py-0.5 text-[11px] font-medium leading-4 text-brand-ink">
                      {item.domain || '未设置领域'}
                    </span>
                    <span className="inline-flex shrink-0 rounded-md border border-viz-violet-soft bg-viz-violet-soft px-2 py-0.5 font-mono text-[11px] font-medium leading-4 text-viz-violet">
                      {item.current_release_version || item.version}
                    </span>
                    {clicks > 0 && (
                      <span
                        data-testid="ontology-card-clicks"
                        title={`已被选用 ${clicks} 次`}
                        className="ml-auto inline-flex shrink-0 items-center gap-1 rounded-full border border-[color-mix(in_srgb,var(--color-warning)_35%,transparent)] bg-[var(--color-warning-bg)] px-2 py-0.5 text-[10px] font-semibold text-[var(--color-warning)]"
                      >
                        <Flame size={11} />×{clicks}
                      </span>
                    )}
                  </div>
                </div>
              </div>

              <p
                className="min-h-[40px] px-4 text-xs leading-5 text-muted-foreground dark:text-[var(--color-text-tertiary)]"
                style={{ display: '-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient: 'vertical', overflow: 'hidden' }}
                title={item.description || '暂无描述'}
              >
                {item.description || '暂无描述'}
              </p>

              <div className="mt-3 grid grid-cols-4 gap-1.5 px-4">
                {cardMetrics(item).map(metric => (
                  <div key={metric.label} className="min-w-0 rounded-xl bg-muted px-0.5 py-2 text-center dark:bg-accent">
                    <p className="whitespace-nowrap text-[10px] font-medium text-[var(--color-text-tertiary)]">{metric.label}</p>
                    <p className="mt-0.5 text-base font-semibold tabular-nums text-foreground dark:text-foreground">{metric.value}</p>
                  </div>
                ))}
              </div>

              <div className="mt-auto px-4 pb-4 pt-3">
                <div
                  data-testid={index === activeIndex ? 'ontology-card-confirm' : undefined}
                  className={`flex h-9 items-center justify-center gap-1.5 rounded-lg text-sm font-medium transition-colors duration-300 ${
                    index === activeIndex
                      ? 'bg-brand text-[var(--color-text-inverse)] shadow-sm'
                      : 'bg-muted text-[var(--color-text-tertiary)] dark:bg-accent'
                  }`}
                >
                  {index === activeIndex ? (
                    <><Sparkles size={14} />开始使用</>
                  ) : (
                    '点击卡片聚焦'
                  )}
                </div>
              </div>
            </div>
          )
        })}

        {count > 1 && (
          <>
            <button
              type="button"
              aria-label="上一张本体卡片"
              disabled={!looped && activeIndex === 0}
              onClick={() => snapTo(Math.round(focusRef.current) - 1)}
              className="absolute left-3 top-1/2 z-[110] flex h-9 w-9 -translate-y-1/2 items-center justify-center rounded-full border border-border bg-card text-muted-foreground shadow-md backdrop-blur transition-all hover:border-brand-line hover:text-brand-ink disabled:cursor-not-allowed disabled:opacity-30 dark:border-[var(--color-border-hover)] dark:bg-accent"
            >
              <ChevronLeft size={16} />
            </button>
            <button
              type="button"
              aria-label="下一张本体卡片"
              disabled={!looped && activeIndex >= count - 1}
              onClick={() => snapTo(Math.round(focusRef.current) + 1)}
              className="absolute right-3 top-1/2 z-[110] flex h-9 w-9 -translate-y-1/2 items-center justify-center rounded-full border border-border bg-card text-muted-foreground shadow-md backdrop-blur transition-all hover:border-brand-line hover:text-brand-ink disabled:cursor-not-allowed disabled:opacity-30 dark:border-[var(--color-border-hover)] dark:bg-accent"
            >
              <ChevronRight size={16} />
            </button>
          </>
        )}
      </div>

      {count > 1 && (
        <div className="flex shrink-0 items-center justify-center gap-1.5 pb-4 pt-2">
          {ranked.map((item, index) => (
            <button
              key={item.id}
              type="button"
              aria-label={`聚焦第 ${index + 1} 张本体卡片`}
              onClick={() => focusCard(index)}
              className={`h-1.5 rounded-full transition-all duration-300 ${
                index === activeIndex
                  ? 'w-5 bg-brand'
                  : 'w-1.5 bg-accent hover:bg-accent dark:bg-accent'
              }`}
            />
          ))}
        </div>
      )}
    </div>
  )
}
