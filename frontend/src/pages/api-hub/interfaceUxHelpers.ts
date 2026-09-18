/** Pure UX helpers for 接口管理 — kept free of React so unit tests stay cheap. */

export interface BusinessFailureResult {
  failed: boolean
  summary?: string
}

/** Detect common business-level failure shapes inside an otherwise-successful HTTP body. */
export function detectBusinessFailure(body: string): BusinessFailureResult {
  const text = (body ?? '').trim()
  if (!text) return { failed: false }

  let parsed: unknown
  try {
    parsed = JSON.parse(text)
  } catch {
    return { failed: false }
  }

  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return { failed: false }
  const data = parsed as Record<string, unknown>

  if (data.success === false) {
    return { failed: true, summary: summarizeBusinessFailure(data) }
  }
  if (data.ok === false) {
    return { failed: true, summary: summarizeBusinessFailure(data) }
  }

  if (Object.prototype.hasOwnProperty.call(data, 'error')) {
    const error = data.error
    if (typeof error === 'string' && error.trim()) {
      return { failed: true, summary: truncateSummary(error.trim()) }
    }
    if (error && typeof error === 'object') {
      const nested = summarizeErrorValue(error)
      return { failed: true, summary: nested }
    }
  }

  return { failed: false }
}

function summarizeBusinessFailure(data: Record<string, unknown>): string | undefined {
  const candidates: unknown[] = [
    data.message,
    data.msg,
    data.detail,
    data.description,
    data.error,
    data.reason,
  ]
  for (const candidate of candidates) {
    const text = summarizeErrorValue(candidate)
    if (text) return text
  }
  return undefined
}

function summarizeErrorValue(value: unknown): string | undefined {
  if (typeof value === 'string' && value.trim()) return truncateSummary(value.trim())
  if (!value || typeof value !== 'object') return undefined
  const record = value as Record<string, unknown>
  for (const key of ['message', 'msg', 'detail', 'description', 'error']) {
    const nested = record[key]
    if (typeof nested === 'string' && nested.trim()) return truncateSummary(nested.trim())
  }
  try {
    return truncateSummary(JSON.stringify(value))
  } catch {
    return undefined
  }
}

function truncateSummary(text: string, max = 160): string {
  const normalized = text.replace(/\s+/g, ' ').trim()
  if (normalized.length <= max) return normalized
  return `${normalized.slice(0, max - 1)}…`
}

export interface FilterableInterface {
  name: string
  url: string
  group_name?: string | null
}

/** Filter interfaces by name / URL / group_name (case-insensitive substring). */
export function filterInterfaces<T extends FilterableInterface>(items: T[], search: string): T[] {
  const query = search.trim().toLowerCase()
  if (!query) return items
  return items.filter(item => {
    const group = (item.group_name || '').toLowerCase()
    return item.name.toLowerCase().includes(query)
      || item.url.toLowerCase().includes(query)
      || group.includes(query)
  })
}

/** Status banner classes for HTTP 发布弹窗：unpublished = muted, published = brand-soft. */
export function publicationBannerTone(published: boolean): string {
  return published
    ? 'border-brand-line bg-brand-soft'
    : 'border-border bg-muted'
}

export function publicationBannerIconTone(published: boolean): string {
  return published
    ? 'bg-brand-mist text-brand-ink'
    : 'bg-card text-muted-foreground'
}

export function publicationStatusChipTone(published: boolean): string {
  return published
    ? 'bg-brand-mist text-brand-ink'
    : 'border border-border bg-card text-muted-foreground'
}

/** HTTP status chip: 2xx → success, else danger (brand green reserved for actions). */
export function httpStatusChipClass(statusCode: number | null | undefined, stale = false): string {
  if (stale) return 'bg-muted text-muted-foreground'
  if (typeof statusCode === 'number' && statusCode >= 200 && statusCode < 300) {
    return 'bg-[var(--color-success-bg)] text-[var(--color-success)]'
  }
  return 'bg-[var(--color-danger-bg)] text-[var(--color-danger)]'
}

/** METHOD badge tones — GET stays info blue; POST uses brand-soft (not success-green). */
export const methodTone: Record<string, string> = {
  GET: 'text-[var(--color-info)] bg-[var(--color-info-bg)]',
  POST: 'text-brand-ink bg-brand-soft',
  PUT: 'text-[var(--color-warning)] bg-[var(--color-warning-bg)]',
  PATCH: 'text-muted-foreground bg-muted',
  DELETE: 'text-[var(--color-danger)] bg-[var(--color-danger-bg)]',
  HEAD: 'text-muted-foreground bg-muted',
  OPTIONS: 'text-[var(--color-info)] bg-[var(--color-info-bg)]',
}
