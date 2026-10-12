/**
 * 快捷内容纯逻辑：重排（拖拽/上移下移共用）、投放位置判定、表单校验、
 * 内容预览首行。不依赖 React/DOM 状态，node:test 直接单测。
 */

/** 把 list[from] 移动到 to（越界钳制），返回新数组；原地语义错误时原样返回。 */
export function moveItem<T>(list: readonly T[], from: number, to: number): T[] {
  if (from < 0 || from >= list.length || to < 0 || to >= list.length || from === to) {
    return [...list]
  }
  const next = [...list]
  const [moved] = next.splice(from, 1)
  next.splice(to, 0, moved)
  return next
}

/** 拖拽悬停判定：指针在目标行中线上方 → 投放到该行之前（dropIndex=其原位）。 */
export function isAboveMidline(clientY: number, rect: { top: number; height: number }): boolean {
  return clientY < rect.top + rect.height / 2
}

/** 快捷内容表单校验：返回首个错误文案；通过返回空串（与后端 422 同口径）。 */
export function validateQuickInput(title: string, content: string): string {
  if (!title.trim()) return '请填写内容标题'
  if (title.trim().length > 100) return '标题最长 100 字'
  if (!content.trim()) return '请填写内容详情'
  if (content.trim().length > 2000) return '内容详情最长 2000 字'
  return ''
}

/** 卡片/列表行的内容预览：首个非空行去首尾空白，多行折叠为单行省略展示。 */
export function contentPreview(content: string): string {
  for (const line of content.split('\n')) {
    const trimmed = line.trim()
    if (trimmed) return trimmed
  }
  return ''
}
