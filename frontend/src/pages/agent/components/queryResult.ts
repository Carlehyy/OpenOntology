/**
 * 明细查询（query-view）前端视图模型 — 纯函数。
 *
 * 工具 step 只负责给出 querySpec / 列 / 统计元信息（远程对象甚至不返回行）；
 * 打开表格后由 Web 以 querySpec 重新 query 取第一页，之后分页/排序/下钻
 * 全部使用服务端返回的 cursor 与 drilldown token，不回到 LLM 重新规划。
 */
import type {
  AgentQueryColumn,
  AgentQuerySpec,
  AgentQueryViewBody,
  AgentStep,
} from '@/api/agent'

export interface StepQuerySource {
  querySpec: AgentQuerySpec | null
  rows: Record<string, unknown>[]
  columns: AgentQueryColumn[]
  totalRowCount: number | string | null
  hasNext: boolean
  observedAt: string | null
  resolvedWindow: unknown
  consistency: unknown
  drilldown: Record<string, unknown> | null
  description: string
  dataMode: string | null
}

type UnknownRecord = Record<string, unknown>

const asRecord = (value: unknown): UnknownRecord | null =>
  value && typeof value === 'object' && !Array.isArray(value)
    ? value as UnknownRecord
    : null

const asArray = <T>(value: unknown): T[] => Array.isArray(value) ? value as T[] : []

const asString = (value: unknown): string =>
  typeof value === 'string' ? value : ''

const asStringOrNull = (value: unknown): string | null =>
  typeof value === 'string' && value.length > 0 ? value : null

const asNumberOrNull = (value: unknown): number | null => {
  if (typeof value === 'number' && Number.isFinite(value)) return value
  if (typeof value === 'string' && value.trim() !== '') {
    const parsed = Number(value)
    return Number.isFinite(parsed) ? parsed : null
  }
  return null
}

const asCount = (value: unknown): number | string | null => {
  if (typeof value === 'number' && Number.isFinite(value)) return value
  if (typeof value === 'string' && value.trim() !== '') return value
  return null
}

function columnFrom(value: unknown): AgentQueryColumn | null {
  const record = asRecord(value)
  if (!record) return null
  const name = asString(record.name)
  const output = asString(record.output)
  if (!name || !output) return null
  return {
    key: output,
    label: name,
    name,
    output,
    ...(typeof record.nullable === 'boolean' ? { nullable: record.nullable } : {}),
    ...(asStringOrNull(record.type) ? { type: asString(record.type) } : {}),
  }
}

function normalizeColumns(value: unknown): AgentQueryColumn[] {
  return asArray<unknown>(value)
    .map(columnFrom)
    .filter((column): column is AgentQueryColumn => column !== null)
}

function normalizeRows(value: unknown, columns: AgentQueryColumn[]): Record<string, unknown>[] {
  return asArray<unknown>(value).map(row => {
    if (Array.isArray(row)) {
      return columns.reduce<Record<string, unknown>>((result, column, index) => {
        result[column.key] = row[index]
        return result
      }, {})
    }
    const source = asRecord(row) ?? {}
    const aligned: Record<string, unknown> = { ...source }
    for (const column of columns) {
      if (aligned[column.key] !== undefined) continue
      if (column.label !== column.key && aligned[column.label] !== undefined) {
        aligned[column.key] = aligned[column.label]
      } else if (column.name && aligned[column.name] !== undefined) {
        aligned[column.key] = aligned[column.name]
      } else if (column.output && aligned[column.output] !== undefined) {
        aligned[column.key] = aligned[column.output]
      }
    }
    return aligned
  })
}

/** 从工具 step.result 提取可打开明细表的数据（远程结果允许 rows 为空）。 */
export function stepQuerySource(step: AgentStep): StepQuerySource | null {
  const result = asRecord(step.result)
  if (!result) return null
  const querySpec = asRecord(result.querySpec) as AgentQuerySpec | null
  const rawRows = result.rows
  const hasRows = Array.isArray(rawRows)
  const rawColumns = result.columns
  const hasColumns = Array.isArray(rawColumns)
  const hasDrilldown = result.drilldown !== undefined && result.drilldown !== null
  const hasAggregateMetadata = result.groupValues !== undefined
    || result.totalRowCount !== undefined
    || result.dataMode !== undefined
  if (!querySpec && !hasRows && !hasColumns && !hasDrilldown && !hasAggregateMetadata) {
    return null
  }
  const columns = normalizeColumns(rawColumns)
  const rows = normalizeRows(rawRows, columns)
  const description = describeQuerySpec(result, querySpec)
  return {
    querySpec,
    rows,
    columns,
    totalRowCount: asCount(result.totalRowCount),
    hasNext: result.hasNext === true,
    observedAt: asStringOrNull(result.observedAt),
    resolvedWindow: result.resolvedWindow ?? querySpec?.resolvedWindow ?? null,
    consistency: result.consistency ?? null,
    drilldown: asRecord(result.drilldown),
    description,
    dataMode: asStringOrNull(result.dataMode),
  }
}

function describeQuerySpec(
  result: UnknownRecord,
  spec: AgentQuerySpec | null,
): string {
  const direct = asString(result.description)
  if (direct) return direct
  const description = asRecord(spec?.description)
  if (description) {
    const objectType = asString(description.objectType)
    if (objectType) return objectType
    const note = asString(description.note)
    if (note) return note
  }
  return asString(result.objectType) || '查询结果'
}

export const isQueryResultStep = (step: AgentStep): boolean => stepQuerySource(step) !== null

/** 明细表首开强制现算；cursor 只来自 query-view 响应，不使用 worker cursor。 */
export function buildQueryViewBody(input: {
  conversationId: string
  querySpec?: AgentQuerySpec | null
  cursor?: string | null
  drilldown?: string | null
  pageSize?: number | null
}): AgentQueryViewBody {
  // 契约：首查询（querySpec / drilldown token）生成 Web-owned cursor；
  // 续页必须同时回传原始 querySpec（服务端无进程内 spec 缓存，缺失返回
  // 410 query_spec_required），但不再重复 pageSize。
  if (input.cursor) {
    return {
      conversationId: input.conversationId,
      cursor: input.cursor,
      ...(input.querySpec ? { querySpec: input.querySpec } : {}),
    }
  }
  return {
    conversationId: input.conversationId,
    ...(input.querySpec ? { querySpec: input.querySpec } : {}),
    ...(input.drilldown ? { drilldown: input.drilldown } : {}),
    ...(input.pageSize ? { pageSize: input.pageSize } : {}),
  }
}

function queryRequest(spec: AgentQuerySpec | null): UnknownRecord | null {
  return spec ? asRecord(spec.query) : null
}

/** 仅 objects 形状的结构化 QueryRequest 支持在 Web 侧重排。 */
export function canSortQuerySpec(spec: AgentQuerySpec | null): boolean {
  if (!spec || (spec.shape && spec.shape !== 'objects')) return false
  const query = queryRequest(spec)
  if (!query) return false
  return Array.isArray(query.select) || Array.isArray(query.aggregates)
    || Array.isArray(query.groupBy)
}

/** 列输出别名（query.select[].as / aggregates[].as）与 UI 列 key 对齐。 */
export function sortOutputForColumn(
  spec: AgentQuerySpec | null,
  column: AgentQueryColumn,
): string | null {
  const query = queryRequest(spec)
  if (!query) return null
  const candidates = [
    ...asArray<UnknownRecord>(query.select),
    ...asArray<UnknownRecord>(query.aggregates),
    ...asArray<UnknownRecord>(query.groupBy),
  ]
  const aliases = candidates
    .map(item => asString(item.as) || asString(item.property))
    .filter(Boolean)
  if (aliases.includes(column.key)) return column.key
  if (aliases.includes(column.label)) return column.label
  // 无别名信息时仍允许按列 key 尝试；服务端会以明确错误拒绝未知输出。
  return column.key
}

export function applyQuerySort(
  spec: AgentQuerySpec | null,
  column: AgentQueryColumn,
  direction: 'asc' | 'desc',
): AgentQuerySpec | null {
  if (!spec || !canSortQuerySpec(spec)) return spec
  const output = sortOutputForColumn(spec, column)
  if (!output) return spec
  const query = queryRequest(spec)
  if (!query) return spec
  return {
    ...spec,
    query: {
      ...query,
      orderBy: [{ output, direction }],
    },
  }
}

/** 行级下钻描述：后端可在行内给出 token/description 对象。 */
export function drilldownForRow(row: Record<string, unknown>): Record<string, unknown> | null {
  return asRecord(row.drilldown)
}

export function drilldownForResult(
  rows: Record<string, unknown>[],
  fallback: Record<string, unknown> | null,
): Record<string, unknown> | null {
  for (const row of rows) {
    const drilldown = drilldownForRow(row)
    if (drilldown) return drilldown
  }
  return fallback
}

/**
 * 解析 query-view 的 drilldowns：
 * - 行内 drilldown 优先；
 * - 数组按行序对齐；
 * - 对象可为单描述（含 token）或按行号/分组值索引。
 */
export function drilldownForResultRow(
  result: {
    rows: Record<string, unknown>[]
    drilldowns?: unknown
    columns?: AgentQueryColumn[]
  } | null,
  row: Record<string, unknown>,
  fallback: Record<string, unknown> | null,
): Record<string, unknown> | null {
  const rowDrilldown = drilldownForRow(row)
  if (rowDrilldown) return rowDrilldown
  if (!result) return fallback
  const drilldowns = result.drilldowns
  if (Array.isArray(drilldowns)) {
    const index = result.rows.indexOf(row)
    const candidate = index >= 0 ? drilldowns[index] : null
    return asRecord(candidate) ?? fallback
  }
  const record = asRecord(drilldowns)
  if (!record) return fallback
  if (asRecord(record.token) || typeof record.token === 'string') {
    // 单描述（服务端只在唯一分组下给出）直接作为兜底。
    return record
  }
  const index = result.rows.indexOf(row)
  const byIndex = asRecord(record[String(index)])
  if (byIndex) return byIndex
  const columnValues = (result.columns ?? [])
    .map(column => row[column.key])
    .filter(value => value !== null && value !== undefined)
  const rowValues = Object.entries(row)
    .filter(([key]) => key !== 'drilldown')
    .map(([, value]) => value)
    .filter(value => value !== null && value !== undefined)
  for (const value of [...columnValues, ...rowValues]) {
    const candidate = asRecord(record[String(value)])
    if (candidate) return candidate
  }
  const values = asRecord(record.values)
  if (values) {
    const match = Object.entries(values).find(([, candidate]) => asRecord(candidate))
    if (match) return asRecord(match[1])
  }
  return fallback
}

/** 值渲染必须对对象/数组/布尔/null 都安全，不能出现 [object Object]。 */
export function formatQueryValue(value: unknown): string {
  if (value === null || value === undefined) return '—'
  if (typeof value === 'string') return value
  if (typeof value === 'number') {
    if (!Number.isFinite(value)) return String(value)
    return Number.isInteger(value) ? value.toLocaleString('zh-CN') : String(value)
  }
  if (typeof value === 'boolean') return value ? '是' : '否'
  if (Array.isArray(value)) {
    return value.map(item => formatQueryValue(item)).join('、')
  }
  const record = asRecord(value)
  if (record) {
    return Object.entries(record)
      .map(([key, nested]) => `${key}: ${formatQueryValue(nested)}`)
      .join(' · ')
  }
  return String(value)
}

export function formatResolvedWindow(value: unknown): string {
  const record = asRecord(value)
  if (!record) return formatQueryValue(value)
  const start = record.start ?? record.from ?? record.min ?? record.since
  const end = record.end ?? record.to ?? record.max ?? record.until
  if (start !== undefined || end !== undefined) {
    return `${formatQueryValue(start)} ~ ${formatQueryValue(end)}`
  }
  return formatQueryValue(record)
}

/**
 * 远程 lazy keyset / 非固定快照提示：查询结果不是同一时刻的固定快照，
 * 翻页可能看到查询期间的新数据，必须把服务端 note 原样展示给用户。
 */
export function consistencyNotice(value: unknown): string | null {
  const record = asRecord(value)
  if (!record) return null
  const mode = asString(record.mode)
  const sameSnapshot = record.sameSnapshot === true
  const queryFresh = record.queryFresh === false
  const nonFixed = mode.includes('keyset') || sameSnapshot || queryFresh
  if (!nonFixed) return null
  const note = asString(record.note)
  if (note) return note
  return mode.includes('keyset')
    ? '远程 keyset 为非固定快照：翻页结果反映查询时刻的数据，可能与上一页口径不同。'
    : '该结果不是固定快照：请结合观测时间与一致性字段阅读。'
}

export function formatConsistency(value: unknown): string {
  if (value === null || value === undefined || value === '') return ''
  const record = asRecord(value)
  if (!record) return formatQueryValue(value)
  const level = record.level ?? record.status ?? record.consistency
  if (typeof level === 'string') {
    const extras = Object.entries(record)
      .filter(([key]) => !['level', 'status', 'consistency'].includes(key))
      .map(([key, nested]) => `${key}: ${formatQueryValue(nested)}`)
    return [level, ...extras].join(' · ')
  }
  return formatQueryValue(record)
}

export function queryPageSize(spec: AgentQuerySpec | null, fallback = 50): number {
  const fromSpec = asNumberOrNull(spec?.pageSize)
  if (fromSpec && fromSpec > 0) return Math.min(200, fromSpec)
  return fallback
}

export function describeRowCount(result: {
  totalRowCount: number | string | null
  hasNext: boolean
  rows: unknown[]
}): string {
  const returned = result.rows.length
  if (result.totalRowCount !== null) {
    const total = typeof result.totalRowCount === 'number'
      ? result.totalRowCount.toLocaleString('zh-CN')
      : result.totalRowCount
    return `共 ${total} 行 · 本页 ${returned} 行`
  }
  if (result.hasNext) return `至少 ${returned.toLocaleString('zh-CN')} 行 · 还有下一页`
  return `本页 ${returned} 行`
}

export function releaseBody(input: {
  conversationId: string
  cursor: string
}): { conversationId: string; cursor: string } {
  return {
    conversationId: input.conversationId,
    cursor: input.cursor,
  }
}
