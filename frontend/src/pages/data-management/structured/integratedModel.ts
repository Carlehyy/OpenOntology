import type { IntegratedAsset, IntegratedRows, IntegratedSyncRun, IntegratedVersion } from '@/api/v2/integrated-datasets'

/** SQL NULL is null; json_text cells are opaque strings, including the JSON literal 'null'. */
export function integratedCell(value: unknown): string {
  if (value === null) return 'NULL'
  if (value === undefined) return '—'
  // Never parse/re-encode JSON text: its numbers may exceed JavaScript precision.
  if (typeof value === 'string') return value
  return typeof value === 'object' ? JSON.stringify(value) : String(value)
}

/**
 * Header label for a logical column: show the configured display name with the
 * alias in parentheses when they differ; unconfigured columns show the name as-is.
 */
export function integratedColumnLabel(column: { name: string; display_name?: string }): string {
  const display = column.display_name?.trim()
  return display && display !== column.name ? `${display}（${column.name}）` : column.name
}

/** Persistent inline errors only; never used to claim a write/sync succeeded. */
export function integratedError(error: unknown): string {
  if (!error || typeof error !== 'object') return '请求失败，请重试'
  const e = error as { status?: number; detail?: unknown; message?: string }
  const detail = e.detail
  if (typeof detail === 'string') return detail
  // FastAPI validation errors are a list of {loc, msg, type}; render the first
  // field path instead of leaking "[object Object]" into a persistent alert.
  if (Array.isArray(detail)) {
    const first = detail[0] as { loc?: unknown[]; msg?: string } | undefined
    const where = Array.isArray(first?.loc)
      ? first.loc.filter((part): part is string => typeof part === 'string').join('.')
      : ''
    const message = typeof first?.msg === 'string' && first.msg ? first.msg : '请求参数不合法'
    return where ? `${where}: ${message}` : message
  }
  if (detail && typeof detail === 'object') {
    const d = detail as { code?: string; message?: string; position?: { line?: number; column?: number } }
    const at = d.position?.line ? `（第 ${d.position.line} 行，第 ${d.position.column ?? 1} 列）` : ''
    return `${d.code ? `[${d.code}] ` : ''}${d.message || '请求失败'}${at}`
  }
  return e.message || (e.status === 403 ? '没有此操作的权限' : '请求失败，请重试')
}

export function parseObject(text: string, label: string): Record<string, unknown> {
  let parsed: unknown
  try { parsed = JSON.parse(text) } catch { throw new Error(`${label}必须是合法 JSON 对象`) }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error(`${label}必须是 JSON 对象`)
  return parsed as Record<string, unknown>
}

/** The backend rejects dots/NUL in either physical identifier; check before requesting. */
export function validateResourceName(schema: string, table: string): string {
  if (!schema.trim() || !table.trim()) return '请填写数据库 Schema 和源表名称'
  for (const [label, value] of [['Schema', schema], ['源表名称', table]] as const) {
    if (value.includes('.')) return `${label}不能包含“.”；请把 Schema 与表名分开填写`
    if (value.includes('\u0000')) return `${label}不能包含 NUL 字符`
  }
  return ''
}

export function primaryKeyOf(columns: string[], values: Record<string, unknown>): Record<string, unknown> {
  if (columns.length === 0) throw new Error('源表未声明主键，不能执行更新或删除')
  for (const key of columns) {
    if (!(key in values) || values[key] === null || values[key] === undefined) throw new Error(`缺少主键字段 ${key}`)
  }
  return Object.fromEntries(columns.map(key => [key, values[key]]))
}

/** Split the transport row into immutable PK + values; JSON cell strings stay opaque and SQL NULL stays null. */
export function sourceMutationPayload(primaryKey: string[], current: Record<string, unknown>, original?: Record<string, unknown>) {
  const key = primaryKeyOf(primaryKey, original ?? current)
  if (original && primaryKey.some(name => JSON.stringify(current[name]) !== JSON.stringify(original[name]))) throw new Error('不能修改源记录主键')
  const values = Object.fromEntries(Object.entries(current).filter(([name]) => !primaryKey.includes(name)))
  return { key, values }
}

export const SYNC_STATUS_LABELS: Record<string, string> = {
  queued: '等待执行', starting: '启动中', loading: '读取中', sealed: '已封存', failed: '失败',
}

export const SYNC_ACTIVE_STATUSES = ['queued', 'starting', 'loading'] as const

export function syncStatusLabel(status: string): string {
  return SYNC_STATUS_LABELS[status] ?? '未核验'
}

export function syncIsActive(run: Pick<IntegratedSyncRun, 'status'> | null | undefined): boolean {
  return !!run && (SYNC_ACTIVE_STATUSES as readonly string[]).includes(run.status)
}

/** A version is complete only with all three independent server facts. */
export function syncIsSealed(run: Pick<IntegratedSyncRun, 'status' | 'complete' | 'version_id'> | null | undefined): boolean {
  return !!run && run.status === 'sealed' && run.complete === true && !!run.version_id
}

export function versionIsVerified(version: Pick<IntegratedVersion, 'status' | 'complete'>): boolean {
  return version.status === 'sealed' && version.complete === true
}

export function versionStateLabel(version: Pick<IntegratedVersion, 'status' | 'complete'>): string {
  return versionIsVerified(version) ? '完整封存已核验' : '完整性未核验'
}

/** Never let an existing version id imply that its sealed manifest was verified. */
export function latestVersionStateLabel(asset: Pick<IntegratedAsset, 'latest_version_id'>, versions: IntegratedVersion[]): string {
  if (!asset.latest_version_id) return '尚无完整版本'
  const version = versions.find(item => item.id === asset.latest_version_id)
  if (!version) return `最新版本 ${asset.latest_version_id}（封存状态未核验）`
  return `最新版本 ${version.id} · ${versionStateLabel(version)}`
}

/** A successful preview proves the source was readable at that instant, not total row count. */
export function sourceReadLabel(rows: IntegratedRows | null | undefined): string {
  return rows ? '最近源读取已核验' : '未核验'
}

export function sourceReadDetail(rows: IntegratedRows | null | undefined): string {
  if (!rows) return '登记不等于当前连通'
  const state = rows.source_state
  const readComplete = state.complete === true ? '完整' : '未完整（预览为有界读取）'
  return `快照时点 ${state.started_at} · 读取完整性 ${readComplete} · 预览最多 ${rows.limit ?? 100} 行，不是总行数`
}

/** DBX availability is a server fact; a capability flag alone cannot claim usable metadata. */
export interface DbxState { configured: boolean; available: boolean; commit: string; version: string; reason: string }

export function dbxState(asset: Pick<IntegratedAsset, 'capabilities' | 'dbx'>): DbxState {
  const configured = asset.dbx?.configured ?? asset.capabilities.dbx_metadata
  const available = asset.dbx?.available ?? configured
  return {
    configured,
    available,
    commit: asset.dbx?.commit ?? '',
    version: asset.dbx?.version ?? '',
    reason: asset.dbx?.reason ?? (configured ? '' : 'Pinned dbx metadata transport is not configured or verified'),
  }
}

export function dbxStateLabel(asset: Pick<IntegratedAsset, 'capabilities' | 'dbx'>): string {
  const state = dbxState(asset)
  if (!state.configured) return '未配置（发现/登记将 503 失败）'
  if (!state.available) return '已配置但不可用'
  return '已配置'
}

export function dbxStateDetail(asset: Pick<IntegratedAsset, 'capabilities' | 'dbx'>): string {
  const state = dbxState(asset)
  const version = state.version ? `v${state.version} · ` : ''
  const commit = state.commit ? `${version}commit ${state.commit}` : 'commit 未知'
  return state.configured ? `${commit}${state.reason ? ` · ${state.reason}` : ''}` : `${commit} · ${state.reason}`
}

export function backendsLabel(asset: Pick<IntegratedAsset, 'backends'>): string {
  const backends = asset.backends
  if (!backends) return '快照 native · 写入 native · 元数据 dbx（可用性见 DBX 状态）'
  return `快照 ${backends.snapshot} · 写入 ${backends.mutation} · 元数据 ${backends.metadata}（可用性见 DBX 状态）`
}

export function receiptNeedsReconciliation(receipt: { status: string } | null | undefined): boolean {
  return !!receipt && (receipt.status === 'unknown' || receipt.status === 'pending')
}

export function receiptStatusLabel(status: string): string {
  return ({ applied: '已应用', conflict: '冲突', failed: '失败', unknown: '结果未知', pending: '待确认' } as Record<string, string>)[status] ?? status
}
