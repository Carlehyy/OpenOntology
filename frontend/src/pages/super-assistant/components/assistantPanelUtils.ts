/** 助手配置面板的共享小工具（独立成模块以避免组件间循环依赖） */

export const errorText = (error: any, fallback = '操作失败') => {
  const detail = error?.detail
  // FastAPI 422 校验错误的 detail 是对象数组（loc/msg/input）：原样返回会给 toast
  // 塞进非字符串 children，React 渲染即抛错且无 ErrorBoundary 兜底（整树白屏）。
  // 折叠为「字段: 消息」的可读字符串再交给 toast；空数组是 truthy，须显式落 message。
  if (Array.isArray(detail)) {
    const messages = detail
      .map((item: any) => {
        if (item?.msg === undefined) return typeof item === 'object' ? JSON.stringify(item) : String(item)
        const field = Array.isArray(item.loc) ? item.loc.slice(1).join('.') : ''
        return field ? `${field}: ${item.msg}` : String(item.msg)
      })
      .filter(Boolean)
    if (messages.length > 0) return `参数校验失败：${messages.join('；')}`
  } else if (detail) {
    return detail
  }
  return error?.message || fallback
}

/** 触发浏览器下载（Blob → objectURL → <a download>）；副作用兜底见 AGENTS.md §5：
 *  下载结果由 E2E 断言最终文件内容，提示文案如实（"已尝试下载"）。 */
export function saveBlob(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  URL.revokeObjectURL(url)
}
