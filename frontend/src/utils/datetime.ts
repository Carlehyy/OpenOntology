// 后端 SQLAlchemy DateTime 列不带时区，序列化出的 ISO 串没有 Z 后缀但语义是 UTC。
// 直接 new Date(value) 会被 JS 按本地时区解析，中国上海（UTC+8）下时间慢 8 小时。
// 已有显式时区（Z 或 ±HH:MM）的串原样解析，否则按 UTC 补齐。
// 与 pages/pipelines/sync-tasks/SyncTasksTab.tsx 的既有先例同规则。
//
// 全站日期时间展示唯一入口（UIUX 审查 S12：格式族全站唯一，YYYY-MM-DD HH:mm 基准）。
// 手写 padStart 输出，不经 toLocale*，保证不同浏览器/locale 输出一致。
// 页面代码禁止直用 toLocaleDateString/toLocaleString/Intl.DateTimeFormat/手写日期模板。
const EXPLICIT_TIMEZONE_RE = /(Z|[+-]\d\d:?\d\d)$/

export type DateInput = string | Date | null | undefined

export function parseServerTime(value: string): Date | null {
  const date = new Date(EXPLICIT_TIMEZONE_RE.test(value) ? value : `${value}Z`)
  return Number.isNaN(date.getTime()) ? null : date
}

function toSafeDate(value: DateInput): Date | null {
  if (value instanceof Date) return Number.isNaN(value.getTime()) ? null : value
  if (typeof value !== 'string' || !value.trim()) return null
  return parseServerTime(value)
}

const pad2 = (n: number) => String(n).padStart(2, '0')

export interface FormatOptions {
  /** 值缺失/非法时的占位文案，默认 '—' */
  fallback?: string
}

export interface DateTimeFormatOptions extends FormatOptions {
  /** 是否带秒（日志、历史流水等需要秒精度的场景） */
  seconds?: boolean
}

/** 完整日期时间：YYYY-MM-DD HH:mm（默认）或 YYYY-MM-DD HH:mm:ss */
export function formatDateTime(value: DateInput, opts?: DateTimeFormatOptions): string {
  const date = toSafeDate(value)
  if (!date) return opts?.fallback ?? '—'
  const base = `${date.getFullYear()}-${pad2(date.getMonth() + 1)}-${pad2(date.getDate())} ${pad2(date.getHours())}:${pad2(date.getMinutes())}`
  return opts?.seconds ? `${base}:${pad2(date.getSeconds())}` : base
}

/** 仅日期：YYYY-MM-DD */
export function formatDate(value: DateInput, opts?: FormatOptions): string {
  const date = toSafeDate(value)
  if (!date) return opts?.fallback ?? '—'
  return `${date.getFullYear()}-${pad2(date.getMonth() + 1)}-${pad2(date.getDate())}`
}

/** 紧凑日期：MM-DD（仅限图表坐标轴、同年度列表等窄空间场景） */
export function formatShortDate(value: DateInput, opts?: FormatOptions): string {
  const date = toSafeDate(value)
  if (!date) return opts?.fallback ?? '—'
  return `${pad2(date.getMonth() + 1)}-${pad2(date.getDate())}`
}

/** 仅时间：HH:mm（默认）或 HH:mm:ss */
export function formatTime(value: DateInput, opts?: DateTimeFormatOptions): string {
  const date = toSafeDate(value)
  if (!date) return opts?.fallback ?? '—'
  const base = `${pad2(date.getHours())}:${pad2(date.getMinutes())}`
  return opts?.seconds ? `${base}:${pad2(date.getSeconds())}` : base
}

/** 兼容保留：超级助手会话列表等旧调用点；输出已统一为 YYYY-MM-DD HH:mm */
export function formatSessionTime(value: DateInput, opts?: FormatOptions): string {
  return formatDateTime(value, { fallback: '时间未知', ...opts })
}
