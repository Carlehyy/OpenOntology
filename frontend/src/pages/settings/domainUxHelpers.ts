// 领域设置的纯 UX 逻辑（toast 文案与错误识别），供 useDomainSettings 消费。
// 与 pages/api-hub/interfaceUxHelpers.ts 同一套 node:test 单测惯例。

export type DomainNoticeAction = 'create' | 'update' | 'delete'

export function domainErrorMessage(error: any, fallback: string) {
  const detail = error?.detail
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail) && typeof detail[0]?.msg === 'string') return detail[0].msg
  if (detail && typeof detail.message === 'string') return detail.message
  if (typeof error?.message === 'string') return error.message
  return fallback
}

// create/update 的 409 detail 契约固定为「领域「xx」已存在」（settings/domains/router.py）；
// 识别后落到名称字段行内错误，不弹 toast
export function isDuplicateNameError(error: any) {
  return typeof error?.detail === 'string' && error.detail.includes('已存在')
}

// 成功 toast：标题写动作结果、说明放对象名。不用页面名「领域设置」当标题，
// 避免读屏连读成「领域设置删除成功」这类歧义（UX 评审 P1-2）。
export function successNotice(action: DomainNoticeAction, name?: string) {
  const title = action === 'create' ? '已创建领域' : action === 'update' ? '已更新领域' : '已删除领域'
  const trimmed = name?.trim()
  return trimmed ? { title, description: `领域「${trimmed}」` } : { title }
}

// 后端删除 409 的 detail 内嵌完整领域名，超长名称会把 toast 撑成多行；
// 被删对象就是本次点击的领域，toast 中改称「该领域」，保留引用次数与处理办法。
export function shortenDeleteDetail(detail: string, name?: string) {
  if (!name) return detail
  const prefix = `领域「${name}」`
  return detail.startsWith(prefix) ? `该领域${detail.slice(prefix.length)}` : detail
}

// 名称必填校验：trim 后为空即失败，文案固定供行内错误展示
export function domainNameValidationError(name: string) {
  return name.trim() ? '' : '请输入领域名称'
}
