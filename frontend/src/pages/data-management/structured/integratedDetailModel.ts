import { cdcStatusLabel } from '../../../api/v2/integrated-datasets.ts'
import type {
  IntegratedAsset, IntegratedCdcStatus, IntegratedColumn, IntegratedReplica, IntegratedReplicaStatusPayload,
  IntegratedRows, IntegratedSyncRun,
} from '@/api/v2/integrated-datasets'
import { dbxState, primaryKeyOf } from './integratedModel.ts'
import { formatDate, formatDateTime, formatTime } from '../../../utils/datetime.ts'

/*
 * Presentation model for the asset detail dialog. Everything here is a pure
 * function over server facts so the status strip, the data section and the
 * record form share one vocabulary; nothing in this module claims a state the
 * server did not report.
 */

export type StatusTone = 'success' | 'warning' | 'danger' | 'info' | 'neutral'

export interface StatusItemView {
  tone: StatusTone
  label: string
  /** One short fact that fits a fixed-height status strip. */
  fact: string
  /** Technical facts for administrators, shown on demand only. */
  details: string[]
  /** Recovery action offered inline while the item needs attention. */
  action?: 'resync' | 'preflight'
}

const seconds = (value: string | null | undefined, fallback: string) =>
  formatDateTime(value, { seconds: true, fallback })

/** Human reason for a replica error code; the code itself stays in `details`. */
export function replicaErrorCodeHint(code: string | null | undefined): string {
  switch (code) {
    case 'REPLICA_NO_FRESH_EVIDENCE':
      return '一段时间没有收到源端的同步心跳，可能是网络波动；恢复后会自动回到「已就绪」'
    case 'REPLICA_COVERAGE_STALE':
      return '超过 30 秒没有确认源端仍在被观察，数据可能不是最新，但仍可查询'
    case 'REPLICA_APPLY_LAG':
      return '已收到最新变更，正在写入本地数据'
    case 'REPLICA_COVERAGE_UNCONFIRMED':
      return '尚未确认本地数据与源表一致'
    case 'SOURCE_UNREACHABLE':
      return '暂时连不上源库；本地数据仍可查询'
    case 'SOURCE_TRUNCATED':
      return '源表已被清空。为避免返回不完整数据，已停止查询，请重新同步'
    case 'REPLICA_PRIMARY_KEY_REQUIRED':
      return '源表没有主键，无法按行同步。请在源端补主键后点「同步表结构」'
    case 'REPLICA_OFFSETS_AHEAD':
    case 'SOURCE_LOG_GAP':
      return '同步进度与本地数据对不上，需要重新同步'
    case 'REPLICA_TRUNCATE_NOT_PUBLISHED':
      return '源端没有开启清空事件的发布，平台无法感知清空操作'
    default:
      return code ? '同步遇到问题，请重新同步；仍未恢复时请联系管理员' : ''
  }
}

const BLOCKED_FACTS: Record<string, string> = {
  SOURCE_TRUNCATED: '源表已被清空，已停止查询',
  REPLICA_PRIMARY_KEY_REQUIRED: '源表没有主键，无法同步',
  REPLICA_OFFSETS_AHEAD: '同步进度对不上，需重新同步',
  SOURCE_LOG_GAP: '同步进度对不上，需重新同步',
}

/** "数据可用性": can queries be served right now, and how fresh is the data. */
export function dataAvailabilityView(
  replica: IntegratedReplica | null | undefined,
  unverified: boolean,
): StatusItemView {
  if (unverified) {
    return { tone: 'neutral', label: '暂无法确认', fact: '状态获取失败，正在自动重试', details: [] }
  }
  if (!replica) {
    return { tone: 'neutral', label: '未初始化', fact: '尚未同步本地数据', details: [], action: 'resync' }
  }
  const rows = replica.row_count != null ? `${replica.row_count} 行` : '行数未知'
  const confirmed = `确认于 ${seconds(replica.coverage_confirmed_at, '时间未知')}`
  const details = [
    replica.last_error_code ? `错误码 ${replica.last_error_code}` : '',
    `覆盖确认 ${seconds(replica.coverage_confirmed_at, '尚未确认')}`,
    `第 ${replica.generation} 代`,
  ].filter(Boolean)
  const facts = { details }
  switch (replica.status) {
    case 'pending':
      return { tone: 'info', label: '等待初始化', fact: '排队中', ...facts }
    case 'initializing':
      return { tone: 'info', label: '初始化中', fact: '正在导入全量数据', ...facts }
    case 'catching_up':
      return { tone: 'info', label: '追平中', fact: `${rows} · 正在追平增量`, ...facts }
    case 'ready':
      return { tone: 'success', label: '已就绪', fact: `${rows} · ${confirmed}`, ...facts }
    case 'stale':
      // Locally received but unapplied changes are progress, not a warning.
      if (replica.last_error_code === 'REPLICA_APPLY_LAG') {
        return { tone: 'info', label: '同步中', fact: `${rows} · 正在写入最新变更`, ...facts }
      }
      return { tone: 'warning', label: '可能滞后', fact: `${rows} · 仍可查询`, ...facts }
    case 'rebuild_required':
      return { tone: 'danger', label: '需重新同步', fact: '已停止提供查询', ...facts, action: 'resync' }
    case 'blocked':
      return {
        tone: 'danger', label: '已阻断',
        fact: BLOCKED_FACTS[replica.last_error_code ?? ''] ?? '已停止查询',
        ...facts,
        action: replica.last_error_code === 'REPLICA_PRIMARY_KEY_REQUIRED' ? 'preflight' : 'resync',
      }
    case 'stopped':
      return { tone: 'danger', label: '已暂停', fact: '本地数据不可查询', ...facts, action: 'resync' }
    default:
      // An unknown status is never presented as healthy.
      return { tone: 'neutral', label: '暂无法确认', fact: '状态无法识别', details: [] }
  }
}

/** Why the data section cannot show rows; null while the replica admits reads. */
export function dataUnavailableReason(
  replica: IntegratedReplica | null | undefined,
  unverified: boolean,
): { title: string; description: string } | null {
  if (!unverified && replica?.complete === true) return null
  const label = dataAvailabilityView(replica, unverified).label
  if (unverified) return { title: '暂时无法确认本地数据状态', description: '状态查询失败期间不提供数据读取，稍后会自动重试。' }
  if (!replica) return { title: '尚未同步本地数据', description: '开始同步后，平台会保存一份持续更新的本地数据用于查询。' }
  switch (replica.status) {
    case 'pending':
      return { title: label, description: '同步任务已排队，开始后会自动导入全量数据。' }
    case 'initializing':
      return { title: label, description: '正在导入全量数据，完成后即可在这里查看。' }
    case 'stale':
      return { title: label, description: replicaErrorCodeHint(replica.last_error_code) }
    case 'rebuild_required':
      return { title: label, description: replicaErrorCodeHint(replica.last_error_code) || '本地数据已不可用，请重新同步。' }
    case 'blocked':
      return { title: label, description: replicaErrorCodeHint(replica.last_error_code) }
    case 'stopped':
      return { title: label, description: '同步已暂停，本地数据不可查询；重新同步后恢复。源表不受影响。' }
    default:
      return { title: label, description: '本地数据暂不可用。' }
  }
}

export type CdcPollView =
  | { phase: 'checking' }
  | { phase: 'ok'; status: IntegratedCdcStatus }
  | { phase: 'unavailable' }

/** Tone per contract runtime status; labels come from the shared vocabulary. */
const CDC_TONES: Record<string, StatusTone> = {
  starting: 'info', active: 'success', paused: 'warning', error: 'danger', gap: 'danger', stopped: 'neutral',
}

/** "变更捕获": subscriptions are derived from published remote mappings. */
export function cdcCaptureView(poll: CdcPollView): StatusItemView {
  if (poll.phase === 'checking') return { tone: 'neutral', label: '核验中', fact: '', details: [] }
  if (poll.phase === 'unavailable') {
    return { tone: 'warning', label: '暂无法确认', fact: '状态查询失败，正在自动重试', details: [] }
  }
  const status = poll.status
  if (!status.enabled) {
    return { tone: 'neutral', label: '未开启', fact: '发布本体映射后自动开启', details: [] }
  }
  const total = status.subscriptions.length
  const idle = status.subscriptions.filter(subscription => subscription.status !== 'active').length
  const fact = `订阅 ${total} 条${idle ? ` · ${idle} 条未在运行` : ''}`
  const details = [
    `最近变更事件 ${seconds(status.last_event_at, '暂无')}`,
    `增量覆盖起点 ${seconds(status.coverage_started_at, '尚未建立')}`,
    status.last_error_code ? `错误码 ${status.last_error_code}` : '',
  ].filter(Boolean)
  // active with healthy=false is a contradictory server fact: never show it as running.
  if (status.status === 'active' && status.healthy !== true) {
    return { tone: 'warning', label: '暂无法确认', fact, details }
  }
  const tone = CDC_TONES[status.status]
  return tone ? { tone, label: status.status === 'active' ? '运行中' : cdcStatusLabel(status.status), fact, details }
    : { tone: 'neutral', label: '暂无法确认', fact, details }
}

/** "元数据服务": only matters to the user while registering or syncing structure. */
export function metadataServiceView(asset: Pick<IntegratedAsset, 'capabilities' | 'dbx'>): StatusItemView {
  const state = dbxState(asset)
  if (!state.configured) return { tone: 'neutral', label: '未配置', fact: '无法登记新表', details: [] }
  if (!state.available) {
    return { tone: 'warning', label: '不可用', fact: '登记与同步表结构暂不可用', details: state.reason ? [state.reason] : [] }
  }
  return { tone: 'success', label: '正常', fact: state.version ? `v${state.version}` : '', details: [] }
}

/** Data as of the latest confirmed coverage (or the read start when unconfirmed). */
export function previewCutoff(rows: IntegratedRows): string | null | undefined {
  return rows.source_state.coverage_confirmed_at ?? rows.source_state.started_at
}

/**
 * The preview is always bounded by `limit`, so it is never the total row
 * count; the server's staleness flag is repeated here because the strip
 * above only shows the replica lifecycle.
 */
export function previewFootnote(rows: IntegratedRows): string {
  const limit = rows.limit ?? 100
  const stale = rows.stale || rows.source_state.stale
  return `显示 ${rows.rows.length} 行 · 数据截至 ${seconds(previewCutoff(rows), '时间未知')} · 最多展示 ${limit} 行${stale ? ' · 数据可能不是最新' : ''}`
}

/** First 8 characters of an opaque identifier; the full value stays in a tooltip. */
export function shortId(id: string | null | undefined): string {
  if (!id) return '—'
  return id.length > 12 ? id.slice(0, 8) : id
}

export function formatTimestamp(value: string | null | undefined, fallback = '—'): string {
  return seconds(value, fallback)
}

/*
 * Record form ⇄ wire values. The backend decodes every value against the
 * reflected source column (`wire_decode._decode`): integers accept a digit
 * string, exact decimals travel as {"$decimal": "..."}, floats must be JSON
 * numbers and booleans must be JSON booleans. Serialising per logical type
 * keeps precision that a plain JSON number would silently lose.
 */

export type RecordEditMode = 'insert' | 'update'

/** A form draft keyed by logical column name; `nulls` holds explicit SQL NULL. */
export interface RecordFormDraft { fields: Record<string, string>; nulls: string[] }

export type WireValue =
  | string | number | boolean | null
  | { $integer: string } | { $decimal: string } | { $binary: string }

const INTEGER_RE = /^-?[0-9]+$/
const FLOAT_RE = /^[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?$/
const BASE64_RE = /^[A-Za-z0-9+/]+={0,2}$/
const TZ_OFFSET_RE = /(Z|[+-]\d\d:?\d\d)$/

const WIRE_SCALARS = ['$decimal', '$integer', '$binary'] as const

/** Inner text of a `$decimal` / `$integer` / `$binary` wire scalar; null for anything else. */
export function wireScalarText(value: unknown): string | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null
  const [tag] = Object.keys(value)
  if (!tag || Object.keys(value).length !== 1 || !(WIRE_SCALARS as readonly string[]).includes(tag)) return null
  return String((value as Record<string, unknown>)[tag])
}

/** Cell value → editable text: wire scalars are untagged, other objects render as JSON text. */
export function formTextFromCell(value: unknown): string {
  if (value === null || value === undefined) return ''
  const scalar = wireScalarText(value)
  if (scalar !== null) return scalar
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}

/**
 * Preview cell text: wire scalars untagged, temporal types in the platform
 * format, SQL NULL spelled out (it differs from JSON null inside json_text).
 */
export function cellDisplayText(column: Pick<IntegratedColumn, 'type'>, value: unknown): string {
  if (value === null) return 'NULL'
  if (value === undefined) return '—'
  const scalar = wireScalarText(value)
  if (scalar !== null) return scalar
  if (typeof value === 'string') {
    if (column.type === 'timestamp') return seconds(value, value)
    if (column.type === 'date') return formatDate(value, { fallback: value })
    if (column.type === 'time') return formatTime(value, { seconds: true, fallback: value })
    return value
  }
  return typeof value === 'object' ? JSON.stringify(value) : String(value)
}

export function recordFormFromRow(
  columns: readonly { name: string }[],
  values: Record<string, unknown> | null | undefined,
): RecordFormDraft {
  const draft: RecordFormDraft = { fields: {}, nulls: [] }
  for (const column of columns) {
    const value = values?.[column.name]
    if (value === null) draft.nulls.push(column.name)
    else draft.fields[column.name] = formTextFromCell(value)
  }
  return draft
}

/** An empty input means "not supplied"; explicit NULL comes from `draft.nulls`. */
export function wireValueFromFormText(
  column: Pick<IntegratedColumn, 'name' | 'type' | 'timezone'>,
  raw: string,
): { ok: true; value: WireValue | undefined } | { ok: false; error: string } {
  if (column.type === 'string') return raw === '' ? { ok: true, value: undefined } : { ok: true, value: raw }
  const text = raw.trim()
  if (text === '') return { ok: true, value: undefined }
  switch (column.type) {
    case 'boolean':
      if (text === 'true' || text === 'false') return { ok: true, value: text === 'true' }
      return { ok: false, error: '请选择是或否' }
    case 'integer': {
      if (!INTEGER_RE.test(text)) return { ok: false, error: '请输入整数' }
      const numeric = Number(text)
      // Beyond 2^53 the digit string is the only exact transport.
      return { ok: true, value: Number.isSafeInteger(numeric) ? numeric : { $integer: text } }
    }
    case 'decimal':
      // Decimal(value) accepts exponent notation, which is also how the API renders 0E-8 / 1E-7.
      if (!FLOAT_RE.test(text)) return { ok: false, error: '请输入数字' }
      return { ok: true, value: { $decimal: text } }
    case 'float':
      if (!FLOAT_RE.test(text)) return { ok: false, error: '请输入数字' }
      return { ok: true, value: Number(text) }
    case 'json':
      try { JSON.parse(text) } catch { return { ok: false, error: '请输入合法的 JSON' } }
      return { ok: true, value: text }
    case 'binary':
      if (!BASE64_RE.test(text) || text.length % 4 !== 0) return { ok: false, error: '请输入 Base64 内容' }
      return { ok: true, value: { $binary: text } }
    case 'date':
      return { ok: true, value: text }
    case 'timestamp':
    case 'time':
      // The backend parses the literal as-is and refuses a timezone mismatch.
      if (column.timezone === true && !TZ_OFFSET_RE.test(text)) {
        return { ok: false, error: '请带上时区，如 2026-10-29 04:31:54+00:00' }
      }
      if (column.timezone === false && TZ_OFFSET_RE.test(text)) {
        return { ok: false, error: '该字段不带时区，请去掉末尾的时区' }
      }
      return { ok: true, value: text }
    default: {
      // Unknown logical types fall back to a raw JSON value.
      try { return { ok: true, value: JSON.parse(text) as WireValue } } catch { return { ok: false, error: '请输入合法的 JSON 值' } }
    }
  }
}

export interface RecordFormPayload {
  ok: true
  key: Record<string, unknown>
  values: Record<string, unknown>
}

/**
 * Form draft → mutation payload. Inserts must supply every primary-key column
 * (the backend has no auto-increment path); updates keep the original key and
 * send only fields the user actually changed, so an unchanged value that the
 * browser already rounded is never written back to the source.
 */
export function payloadFromRecordForm(args: {
  columns: readonly IntegratedColumn[]
  primaryKey: string[]
  draft: RecordFormDraft
  initial: RecordFormDraft
  mode: RecordEditMode
  original?: Record<string, unknown>
}): RecordFormPayload | { ok: false; errors: Record<string, string> } {
  const { columns, primaryKey, draft, initial, mode, original } = args
  const errors: Record<string, string> = {}
  let key: Record<string, unknown> = {}
  if (mode === 'update') {
    try {
      key = primaryKeyOf(primaryKey, original ?? {})
    } catch (failure) {
      return { ok: false, errors: { form: (failure as Error).message } }
    }
  }
  const values: Record<string, unknown> = {}
  for (const column of columns) {
    const isKey = primaryKey.includes(column.name)
    if (mode === 'update' && isKey) continue
    const explicitNull = draft.nulls.includes(column.name)
    if (mode === 'update' && !isKey) {
      // Decide "unchanged" before validating: an untouched value is never sent,
      // so a form rendering the browser cannot re-parse must not block the edit.
      const wasNull = initial.nulls.includes(column.name)
      const changed = explicitNull !== wasNull
        || (!explicitNull && (draft.fields[column.name] ?? '') !== (initial.fields[column.name] ?? ''))
      if (!changed) continue
    }
    let supplied: WireValue | undefined
    if (explicitNull) {
      if (isKey) { errors[column.name] = '主键不能为空'; continue }
      if (!column.nullable) { errors[column.name] = '该字段不允许为空'; continue }
      supplied = null
    } else {
      const result = wireValueFromFormText(column, draft.fields[column.name] ?? '')
      if (!result.ok) { errors[column.name] = result.error; continue }
      supplied = result.value
    }
    if (isKey) {
      if (supplied === undefined) errors[column.name] = '请填写主键'
      else key[column.name] = supplied
      continue
    }
    if (supplied !== undefined) values[column.name] = supplied
  }
  if (mode === 'update' && Object.keys(values).length === 0) errors.form = '至少修改一个字段后才能写入'
  return Object.keys(errors).length ? { ok: false, errors } : { ok: true, key, values }
}

/**
 * Draft → JSON text for the JSON editor. Fields that do not validate are
 * carried as raw strings so the user can fix them there instead of losing input.
 */
export function jsonTextFromRecordForm(columns: readonly IntegratedColumn[], draft: RecordFormDraft): string {
  const merged: Record<string, unknown> = {}
  for (const column of columns) {
    if (draft.nulls.includes(column.name)) { merged[column.name] = null; continue }
    const raw = draft.fields[column.name] ?? ''
    if (raw === '') continue
    const result = wireValueFromFormText(column, raw)
    merged[column.name] = result.ok && result.value !== undefined ? result.value : raw
  }
  return JSON.stringify(merged, null, 2)
}

/** Latest sync run as a short label/value list; raw ISO timestamps never reach the UI. */
export function syncFacts(run: IntegratedSyncRun): { label: string; value: string }[] {
  return [
    { label: '读取', value: `${run.row_count} / ${run.expected_rows || '待源核验'} 行` },
    { label: '排队', value: seconds(run.created_at, '—') },
    // One closing instant: a "last progress" time next to an "end" time read as an anomaly.
    run.completed_at
      ? { label: '结束', value: seconds(run.completed_at, '—') }
      : { label: '最近更新', value: seconds(run.updated_at, '—') },
  ]
}

/** Server replica DTO → the shape the detail dialog works with. */
export function toReplica(payload: IntegratedReplicaStatusPayload): IntegratedReplica {
  return {
    status: payload.status, complete: payload.complete,
    stale: payload.stale ?? (payload.complete && payload.status !== 'ready'),
    row_count: payload.row_count, applied_at: payload.applied_at,
    coverage_confirmed_at: payload.coverage_confirmed_at,
    last_error_code: payload.last_error_code,
    generation: payload.generation, run_id: payload.run_id,
  }
}
