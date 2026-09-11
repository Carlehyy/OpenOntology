import { useReducedMotion } from 'motion/react'

export type ListEntryInitial = { opacity: number; y: number } | false

/**
 * 行/卡片级入场动画的统一开关（UIUX 审查 P0-6「幽灵行」治理）。
 *
 * framer-motion 的弹簧仿真按 rAF 帧推进：组件在页面不可见（后台标签）时挂载，
 * `initial={{ opacity: 0 }}` 的行会停在中途透明度，切回前台仍可能残留半透明
 * ——用户看到的是"坏掉"的表格。两种情况直接跳过入场动画、呈现终态：
 * 1. 用户开启 prefers-reduced-motion；
 * 2. 挂载时 document.hidden（后台标签页）。
 *
 * 用法：`const entryInitial = useListEntryInitial()`，
 * `<motion.tr initial={entryInitial} animate={{ opacity: 1, y: 0 }} …>`。
 * 返回 false 时 motion 跳过 initial，直接呈现 animate 终态。
 */
export function useListEntryInitial(): ListEntryInitial {
  const reduce = useReducedMotion()
  const hiddenAtMount = typeof document !== 'undefined' && document.hidden
  if (reduce || hiddenAtMount) return false
  return { opacity: 0, y: 6 }
}
