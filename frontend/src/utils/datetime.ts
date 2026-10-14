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

export interface RelativeTimeOptions extends FormatOptions {
  /** 计算相对值的基准时刻（测试注入）；缺省取当前时间 */
  now?: Date
}

/**
 * 相对时间：刚刚 / N 分钟前 / N 小时前 / 昨天 HH:mm / MM-DD HH:mm /
 * 跨年 YYYY-MM-DD HH:mm。未来时刻（时钟偏差）回退绝对时间。
 * 用于列表卡片 meta 行等「多久之前」场景；精确时间仍走 formatDateTime。
 */
export function formatRelativeTime(value: DateInput, opts?: RelativeTimeOptions): string {
  const date = toSafeDate(value)
  if (!date) return opts?.fallback ?? '—'
  const now = opts?.now ?? new Date()
  const diffMs = now.getTime() - date.getTime()
  if (diffMs < 0) return formatDateTime(date, { fallback: opts?.fallback })
  const diffMinutes = Math.floor(diffMs / 60_000)
  if (diffMinutes < 1) return '刚刚'
  if (diffMinutes < 60) return `${diffMinutes} 分钟前`
  const diffHours = Math.floor(diffMinutes / 60)
  if (diffHours < 24) return `${diffHours} 小时前`
  // 按日历日差判断「昨天」（跨夏令时的天长可能是 23/25 小时，取 round）
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate())
  const startOfValue = new Date(date.getFullYear(), date.getMonth(), date.getDate())
  const days = Math.round((startOfToday.getTime() - startOfValue.getTime()) / 86_400_000)
  if (days === 1) return `昨天 ${pad2(date.getHours())}:${pad2(date.getMinutes())}`
  if (date.getFullYear() === now.getFullYear()) {
    return `${pad2(date.getMonth() + 1)}-${pad2(date.getDate())} ${pad2(date.getHours())}:${pad2(date.getMinutes())}`
  }
  return formatDateTime(date, { fallback: opts?.fallback })
}

export interface DurationOptions extends FormatOptions {
  /** 区间终点；缺省取当前时间（进行中的已运行时长） */
  until?: DateInput
}

/**
 * 时长：N 秒 / N 分钟 / N 小时 M 分 / N 天 M 小时（不足 1 秒按 1 秒计）。
 * 用于任务耗时、已运行时长等区间展示。
 */
export function formatDuration(from: DateInput, opts?: DurationOptions): string {
  const start = toSafeDate(from)
  if (!start) return opts?.fallback ?? '—'
  const end = toSafeDate(opts?.until) ?? new Date()
  const totalSeconds = Math.max(1, Math.round((end.getTime() - start.getTime()) / 1000))
  if (totalSeconds < 60) return `${totalSeconds} 秒`
  const totalMinutes = Math.floor(totalSeconds / 60)
  if (totalMinutes < 60) return `${totalMinutes} 分钟`
  const totalHours = Math.floor(totalMinutes / 60)
  if (totalHours < 24) {
    const minutes = totalMinutes % 60
    return minutes ? `${totalHours} 小时 ${minutes} 分` : `${totalHours} 小时`
  }
  const days = Math.floor(totalHours / 24)
  const hours = totalHours % 24
  return hours ? `${days} 天 ${hours} 小时` : `${days} 天`
}
