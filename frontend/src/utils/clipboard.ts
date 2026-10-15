/**
 * 写入剪贴板并返回实际使用的路径：
 * - 'clipboard-api'：异步 Clipboard API（secure context），结果可靠；
 * - 'fallback'：execCommand 降级（普通 HTTP 部署）——按 AGENTS.md §5，
 *   该路径「返回 true」不代表已真实写入（非聚焦页面等场景会假成功），
 *   调用方据此把成功文案降级为「已尝试写入，请粘贴验证」，不得宣称「已复制」。
 */
export async function writeTextToClipboard(value: string): Promise<'clipboard-api' | 'fallback'> {
  let clipboardError: unknown

  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(value)
      return 'clipboard-api'
    } catch (error) {
      clipboardError = error
      // Clipboard API is unavailable on ordinary HTTP deployments; fall back below.
    }
  }

  const activeElement = document.activeElement instanceof HTMLElement ? document.activeElement : null
  const selection = window.getSelection()
  const selectedRanges = selection
    ? Array.from({ length: selection.rangeCount }, (_, index) => selection.getRangeAt(index).cloneRange())
    : []
  const textarea = document.createElement('textarea')
  textarea.value = value
  textarea.setAttribute('readonly', '')
  textarea.setAttribute('aria-hidden', 'true')
  Object.assign(textarea.style, {
    position: 'fixed',
    top: '0',
    left: '-9999px',
    opacity: '0',
    pointerEvents: 'none',
  })
  document.body.appendChild(textarea)

  let copied: boolean
  try {
    textarea.focus({ preventScroll: true })
    textarea.select()
    textarea.setSelectionRange(0, value.length)
    copied = document.execCommand('copy')
  } finally {
    textarea.remove()
    if (selection) {
      selection.removeAllRanges()
      selectedRanges.forEach(range => selection.addRange(range))
    }
    activeElement?.focus({ preventScroll: true })
  }

  if (!copied) {
    throw clipboardError instanceof Error
      ? clipboardError
      : new Error('Clipboard copy was rejected')
  }
  return 'fallback'
}
