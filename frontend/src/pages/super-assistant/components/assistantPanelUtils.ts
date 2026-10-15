/** 助手配置面板的共享小工具（独立成模块以避免组件间循环依赖） */

export const errorText = (error: any, fallback = '操作失败') =>
  error?.detail || error?.message || fallback

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
