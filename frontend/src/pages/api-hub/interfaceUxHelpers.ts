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
  method?: string
}

/** Filter interfaces by name / URL / group_name / method (case-insensitive substring). */
export function filterInterfaces<T extends FilterableInterface>(items: T[], search: string): T[] {
  const query = search.trim().toLowerCase()
  if (!query) return items
  return items.filter(item => {
    const group = (item.group_name || '').toLowerCase()
    return item.name.toLowerCase().includes(query)
      || item.url.toLowerCase().includes(query)
      || group.includes(query)
      || (item.method || '').toLowerCase().includes(query)
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

/**
 * 接口名称前端就地校验：去空白后非空且 ≤200 个字符（UTF-16 码元）。
 * 后端 validate_name 按码点计长，前端按码元恒 ≥ 码点数——只会在 emoji 等
 * 增补平面字符上提前拦截，不会放过后端会拒绝的名称；超长文案与后端逐字一致。
 */
export function validateInterfaceName(name: string): string {
  const trimmed = name.trim()
  if (!trimmed) return '请填写接口名称'
  if (trimmed.length > 200) return '接口名称不能超过 200 个字符'
  return ''
}

/** 复制接口时追加「 副本」并按码点截回 200 上限，避免复制出必然保存失败的草稿。 */
export function duplicateInterfaceName(name: string): string {
  return [...`${name} 副本`].slice(0, 200).join('')
}

/** 写方法判定：试调会向真实上游发送可能新增/修改/删除数据的请求。 */
export function isMutatingMethod(method: string): boolean {
  return ['POST', 'PUT', 'PATCH', 'DELETE'].includes(method.trim().toUpperCase())
}

/** 试调按钮文案：写方法显式表达副作用，只读方法保持「调用」。 */
export function invokeActionLabel(method: string): string {
  const normalized = method.trim().toUpperCase()
  if (normalized === 'POST') return '发送 POST'
  if (normalized === 'PUT' || normalized === 'PATCH') return '更新资源'
  if (normalized === 'DELETE') return '执行 DELETE'
  return '调用'
}

/**
 * 调用密钥时间窗校验。入参为 datetime-local 的本地时间字符串（可为空）。
 * 与后端 proxy_keys.py 的 400 校验同口径，提前到提交前就地提示；
 * 编辑已有密钥时后端允许过期时间落在过去（allow_expired），isCreate 区分。
 */
export function validateProxyKeySchedule(
  validFrom: string,
  expiresAt: string,
  options?: { isCreate?: boolean },
): string {
  if (validFrom && expiresAt) {
    const from = new Date(validFrom)
    const expires = new Date(expiresAt)
    if (!Number.isNaN(from.getTime()) && !Number.isNaN(expires.getTime()) && expires.getTime() <= from.getTime()) {
      return '过期时间需要晚于生效时间'
    }
  }
  if (options?.isCreate && expiresAt) {
    const expires = new Date(expiresAt)
    if (!Number.isNaN(expires.getTime()) && expires.getTime() <= Date.now()) {
      return '过期时间需要晚于当前时间'
    }
  }
  return ''
}

/** 与后端 backup.py 的 BACKUP_VERSION 对齐；后端对更高版本同样返回 400。 */
export const SUPPORTED_BACKUP_VERSION = 7

export type BackupInspection =
  | {
    ok: true
    name: string
    version: number
    exportedAt: string
    interfaceCount: number
    groupCount: number
    includesSensitive: boolean
  }
  | { ok: false; error: string }

/** 导入前本地校验并摘要备份文件，不触碰网络。 */
export function summarizeBackup(payload: unknown): BackupInspection {
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
    return { ok: false, error: '备份文件结构无效' }
  }
  const data = payload as Record<string, unknown>
  if (data.app !== 'API-Hub') return { ok: false, error: '这不是接口代理的备份文件' }
  const version = typeof data.version === 'number' ? data.version : NaN
  if (!Number.isFinite(version)) return { ok: false, error: '备份文件缺少版本号' }
  if (version > SUPPORTED_BACKUP_VERSION) {
    return { ok: false, error: `备份文件版本过新（v${version}），当前支持 v${SUPPORTED_BACKUP_VERSION} 及以下` }
  }
  if (!Array.isArray(data.interfaces)) return { ok: false, error: '备份文件缺少接口数据' }
  const groups = new Set<string>()
  data.interfaces.forEach(item => {
    if (item && typeof item === 'object') {
      const group = (item as { group_name?: unknown }).group_name
      if (typeof group === 'string' && group.trim()) groups.add(group.trim())
    }
  })
  return {
    ok: true,
    name: typeof data.name === 'string' && data.name.trim() ? data.name.trim() : '未命名备份',
    version,
    exportedAt: typeof data.exported_at === 'string' ? data.exported_at : '',
    interfaceCount: data.interfaces.length,
    groupCount: groups.size,
    includesSensitive: Boolean(data.includes_sensitive_values),
  }
}

/** 响应头按名称排序，便于扫描定位。 */
export function sortedHeaderEntries(headers: Record<string, string>): Array<[string, string]> {
  return Object.entries(headers).sort(([a], [b]) => a.toLowerCase().localeCompare(b.toLowerCase()))
}

const sensitiveHeaderName = /(authorization|cookie|token|secret|credential|session|api[-_]?key|api-hub-key)/i

/** 响应头值可能携带会话/凭证，默认掩码展示。 */
export function isSensitiveHeader(name: string): boolean {
  return sensitiveHeaderName.test(name)
}
