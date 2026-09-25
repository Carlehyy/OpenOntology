/**
 * 本体结构画布的布局保存状态与文案（纯函数，便于单测）。
 *
 * 保存时机固定为拖拽停止后 3 秒提交（PUT /ontologies/:id/layout），
 * saveStatusLabel 只负责把状态机翻译成用户可见的文案；pending 状态会传入
 * 剩余秒数，由组件每秒递减实现真实倒计时。
 *
 * classifySaveError 负责失败分类：4xx（除 408/409/429）为永久性错误——自动
 * 重试注定失败，组件保持错误态等用户点击重试；网络错误（无响应）、409、5xx
 * 为瞬时错误，组件 3 秒后自动重试。error 形状来自 api/client 响应拦截器：
 * HTTP 错误时 payload = response.data（拦截器在其上挂载 status 字段，detail
 * 为 FastAPI 的字符串或 {code, message} 对象）；网络错误时为 AxiosError 本体。
 */
export type StructureSaveState = 'idle' | 'pending' | 'saving' | 'saved' | 'error'

export const SAVE_COUNTDOWN_SECONDS = 3

export function saveStatusLabel(state: StructureSaveState, countdown: number): string {
  switch (state) {
    case 'pending': {
      const remaining = Math.max(1, Math.min(SAVE_COUNTDOWN_SECONDS, Math.round(countdown)))
      return `${remaining} 秒后自动保存`
    }
    case 'saving':
      return '正在保存布局'
    case 'saved':
      return '布局已保存'
    case 'error':
      return '保存失败'
    default:
      return '拖动后自动保存布局'
  }
}

export interface SaveErrorClassification {
  /** HTTP 状态码；无响应（网络错误）时缺省。 */
  status?: number
  /** 后端 detail.code（如有）。 */
  code?: string
  /** 后端原因原文（detail.message / detail 字符串 / axios message）。 */
  reason: string
  /** true = 不自动重试，保持错误态等用户点击；false = 3 秒后自动重试。 */
  permanent: boolean
  /** 完整文案（含状态码），放状态条 title。 */
  message: string
}

export function classifySaveError(error: unknown): SaveErrorClassification {
  const value = (error ?? {}) as {
    status?: unknown
    code?: unknown
    detail?: unknown
    message?: unknown
  }
  const status = typeof value.status === 'number' ? value.status : undefined
  const detail = value.detail
  let reason = ''
  let code: string | undefined
  if (typeof value.code === 'string') code = value.code
  if (typeof detail === 'string') {
    reason = detail
  } else if (Array.isArray(detail)) {
    // FastAPI 校验错误：detail 为 [{loc, msg, type}, ...]
    const first = detail[0] as { msg?: unknown } | undefined
    if (first && typeof first.msg === 'string') reason = first.msg
  } else if (detail && typeof detail === 'object') {
    const record = detail as { message?: unknown; code?: unknown }
    if (typeof record.message === 'string') reason = record.message
    if (typeof record.code === 'string') code = record.code
  }
  if (!reason && typeof value.message === 'string') reason = value.message
  if (!reason) reason = '布局保存失败'
  const permanent = status !== undefined && status >= 400 && status < 500
    && status !== 408 && status !== 409 && status !== 429
  return {
    status,
    code,
    reason,
    permanent,
    message: status === undefined ? `保存失败：${reason}` : `保存失败（${status}）：${reason}`,
  }
}
