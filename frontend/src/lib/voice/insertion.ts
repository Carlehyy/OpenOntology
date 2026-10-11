/**
 * 语音听写转写文本的插入策略（交互规格参考 cc-haha，MIT）：
 * - 插入录音开始时的光标位置，而非追加到末尾；
 * - 录音期间草稿被编辑过（revision 计数）或输入法正在组词时，文本先
 *   “扣留”在气泡里由用户决定插入/丢弃，绝不覆盖用户正在写的内容；
 * - 中英混排智能补空格：仅两侧均为字母/数字时补一格，CJK 直接拼接。
 * 纯字符串逻辑，node:test 直接单测。
 */

const LATIN_ADJACENT = /[A-Za-z0-9]$/
const LATIN_PRECEDING = /^[A-Za-z0-9]/

/** 判断插入侧是否需要补空格（仅拉丁字母/数字边界补，CJK 边界直连）。 */
export function needsSpacingBoundary(before: string, after: string): boolean {
  return LATIN_ADJACENT.test(before) && LATIN_PRECEDING.test(after)
}

/** 把转写文本接进既有文本片段，自动处理中英边界空格。 */
export function withDictationSpacing(existing: string, insert: string): string {
  if (!existing || !insert) return existing + insert
  return needsSpacingBoundary(existing, insert) ? `${existing} ${insert}` : existing + insert
}

export interface InsertionPlan {
  /** 插入后的完整草稿 */
  text: string
  /** 插入结束后光标应停留的位置 */
  nextCursor: number
}

/** 在光标位置插入转写文本（前后边界按 spacing 规则补空格）。 */
export function insertAtCursor(text: string, cursor: number, insert: string): InsertionPlan {
  const clampedCursor = Math.max(0, Math.min(cursor, text.length))
  const before = text.slice(0, clampedCursor)
  const after = text.slice(clampedCursor)
  const head = withDictationSpacing(before, insert)
  const gapNeeded = needsSpacingBoundary(head, after) ? ' ' : ''
  const merged = `${head}${gapNeeded}${after}`
  return { text: merged, nextCursor: head.length + gapNeeded.length }
}

/** 录音期间草稿是否被外部改动（按 revision 计数，不做内容 diff）。 */
export function draftChangedSince(startRevision: number, currentRevision: number): boolean {
  return currentRevision !== startRevision
}

/**
 * 转写完成后能否静默直插。任一条件不满足即扣留：
 * 草稿被改过 / 输入法组词中 / 光标位置已不可信。
 */
export function shouldHoldInsertion(
  startRevision: number,
  currentRevision: number,
  isComposing: boolean,
): boolean {
  return draftChangedSince(startRevision, currentRevision) || isComposing
}
