/**
 * 运营报告结构化计划模型（plan v1，无 SQL）：纯函数，Node 直跑单测。
 *
 * 与 backend/app/operations/plan.py 的 v1 契约逐条对齐：
 * - 数量与长度界限：块 ≤50、查询块 ≤20、每查询输出列 ≤20、排序键 ≤3、
 *   筛选器 ≤8、图表系列 ≤4、文本块 ≤20000 字符、limit 1–200（缺省编译
 *   取默认 200，与阅读 execute 默认预算同源：QUERY_DEFAULT_LIMIT）；
 * - 标识符规则：查询名/围栏名 `^[A-Za-z][A-Za-z0-9_-]{0,80}$`，
 *   筛选器名/输出别名 `^[A-Za-z_][A-Za-z0-9_]{0,63}$`；源字段允许中文
 *   （SQL 由后端双引号引用拼接），但输出别名必须是合法 ASCII 标识符，
 *   中文/特殊字段经 asciiAlias 生成 ≤63 字符的合法别名；
 * - 语义：聚合 sum/avg 仅数值列、min/max 标量列、时间桶仅时间列、
 *   排序键与图表 x/y 必须引用查询输出列（别名 + `{field}__{unit}` 桶列），
 *   kpi/pie 恰好一个 y、bar/line 需要至少一个 y、table 不支持 x/y。
 *
 * normalizeReportPlan 只做结构整饬（null→undefined），未知结构整体拒绝，
 * 绝不静默丢块或修正越界值：可识别的块一律保留，深度校验交给
 * validateReportPlan（镜像后端 normalize_plan 的用户侧预检）。
 * 不做任意 SQL→计划转换：旧 Markdown 文章继续以 Markdown 形态读写。
 */
import type {
  OperationsDatasetColumn,
  ReportPlanFilterPayload,
  ReportPlanPayload,
} from '@/api/v2/operations-reports'

export type PlanChartKind = 'kpi' | 'bar' | 'line' | 'pie' | 'table'
export type PlanFilterType = 'text' | 'number' | 'date'
export type PlanAgg = 'count' | 'count_distinct' | 'sum' | 'avg' | 'min' | 'max'
export type PlanBucket = 'minute' | 'hour' | 'day' | 'week' | 'month'

export interface PlanTextBlock { type: 'text'; text: string }
export interface PlanQueryColumn { field?: string; agg?: PlanAgg; alias?: string }
export interface PlanTimeBucket { field: string; unit: PlanBucket; alias?: string }
export interface PlanQueryBlock {
  type: 'query'
  name: string
  columns: PlanQueryColumn[]
  time_bucket?: PlanTimeBucket
  sort?: { field: string; dir: 'asc' | 'desc' }[]
  limit?: number
}
export interface PlanChartBlock {
  type: 'chart'
  chart: PlanChartKind
  query: string
  x?: string
  y?: string[]
}
export type PlanBlock = PlanTextBlock | PlanQueryBlock | PlanChartBlock
export interface ReportPlan {
  version: 1
  filters: ReportPlanFilterPayload[]
  blocks: PlanBlock[]
}

export const PLAN_CHARTS: readonly PlanChartKind[] = ['kpi', 'bar', 'line', 'pie', 'table']
export const PLAN_FILTERS: readonly PlanFilterType[] = ['text', 'number', 'date']
export const PLAN_AGGS: readonly PlanAgg[] = ['count', 'count_distinct', 'sum', 'avg', 'min', 'max']
export const PLAN_BUCKETS: readonly PlanBucket[] = ['minute', 'hour', 'day', 'week', 'month']

export const PLAN_AGG_LABELS: Record<PlanAgg, string> = {
  count: '计数',
  count_distinct: '去重计数',
  sum: '求和',
  avg: '平均值',
  min: '最小值',
  max: '最大值',
}
export const PLAN_BUCKET_LABELS: Record<PlanBucket, string> = {
  minute: '按分钟',
  hour: '按小时',
  day: '按天',
  week: '按周',
  month: '按月',
}
export const PLAN_CHART_LABELS: Record<PlanChartKind, string> = {
  kpi: '指标卡',
  bar: '柱状图',
  line: '折线图',
  pie: '饼图',
  table: '表格',
}
export const PLAN_FILTER_TYPE_LABELS: Record<PlanFilterType, string> = {
  text: '文本',
  number: '数字',
  date: '日期',
}

// 与 backend/app/operations/plan.py 的常量一一对应。
export const PLAN_BLOCKS_MAX = 50
export const PLAN_QUERY_BLOCKS_MAX = 20
export const PLAN_QUERY_COLUMNS_MAX = 20
export const PLAN_SORT_KEYS_MAX = 3
export const PLAN_FILTERS_MAX = 8
export const PLAN_CHART_SERIES_MAX = 4
export const PLAN_TEXT_MAX_CHARS = 20_000
export const PLAN_QUERY_LIMIT_MAX = 200
// 与阅读 execute 默认预算一致（backend QUERY_DEFAULT_LIMIT）：每条查询
// 编译产物恒带 LIMIT，未显式声明时取该默认值。
export const PLAN_QUERY_LIMIT_DEFAULT = 200

const QUERY_NAME_RE = /^[A-Za-z][A-Za-z0-9_-]{0,80}$/
const IDENT_RE = /^[A-Za-z_][A-Za-z0-9_]{0,63}$/
const FENCE_LINE_RE = /(?:^|\n)\s*(?:`{3,}|~{3,})/
const HTML_TAG_RE = /<[A-Za-z!/][^>]*>/

// 与后端 _normalized_type / _NUMERIC_TYPES / _TEMPORAL_TYPES / _SCALAR_TYPES 同源。
const TYPE_SYNONYMS: Record<string, string> = {
  numeric: 'number',
  double: 'float',
  'double precision': 'float',
  int: 'integer',
  bigint: 'integer',
  datetime64: 'timestamp',
}
const NUMERIC_TYPES = new Set(['integer', 'float', 'number', 'decimal'])
const TEMPORAL_TYPES = new Set(['date', 'timestamp', 'datetime'])
const SCALAR_TYPES = new Set([...NUMERIC_TYPES, ...TEMPORAL_TYPES, 'string', 'boolean'])
const FILTER_FIELD_TYPES: Record<PlanFilterType, Set<string>> = {
  text: new Set(['string']),
  number: NUMERIC_TYPES,
  date: TEMPORAL_TYPES,
}

export function normalizedColumnType(raw: string): string {
  const value = String(raw || 'string').trim().toLowerCase()
  return TYPE_SYNONYMS[value] ?? value
}

export function isNumericColumn(column: OperationsDatasetColumn): boolean {
  return NUMERIC_TYPES.has(normalizedColumnType(column.type))
}

export function isTemporalColumn(column: OperationsDatasetColumn): boolean {
  return TEMPORAL_TYPES.has(normalizedColumnType(column.type))
}

export function isValidIdentifier(value: string): boolean {
  // 与后端 _legal_alias 对齐：ASCII 短标识符且 ≤63 字节（UTF-8）。
  return IDENT_RE.test(value) && new TextEncoder().encode(value).length <= 63
}

/** 中文/特殊字段 → 合法 ASCII 标识符（≤63 字符）；无法保留可读内容时用 fallback。 */
export function asciiAlias(source: string, fallback = 'value'): string {
  const clean = (input: string) => input.replace(/[^A-Za-z0-9_]+/g, '_').replace(/^_+|_+$/g, '')
  const base = clean(source) || clean(fallback) || 'value'
  return (/^[0-9]/.test(base) ? `c_${base}` : base).slice(0, 63)
}

/** 聚合列的默认别名（后端默认 `{agg}_{field}` 对中文字段不合法，这里落到 ASCII）。 */
export function defaultAlias(agg: PlanAgg, field?: string): string {
  if (agg === 'count' && !field) return 'count'
  return `${agg}_${asciiAlias(field ?? '', 'value')}`.slice(0, 63)
}

/** 时间桶输出列名：显式 alias 优先，否则默认 `{field}__{unit}`。 */
export function bucketAliasOf(bucket: PlanTimeBucket): string {
  return bucket.alias?.trim() || `${bucket.field}__${bucket.unit}`
}

/**
 * 块内输出别名的最终解析（镜像 backend _resolved_alias/_auto_alias）：
 * 显式别名合法即用；缺省用默认候选（字段名 / {agg}_{field} / count /
 * {field}__{unit}）；默认候选不是合法 ASCII 别名（中文源字段等）时按块内
 * 顺序派生稳定自动别名 col_N / {agg}_N / bucket_N。排序键与图表 x/y、
 * 编译 SQL 的 AS 投影都以解析结果为准。
 */
export function resolveQueryAliases(block: PlanQueryBlock): { columnAliases: string[]; bucketAlias?: string; outputs: string[] } {
  const outputs: string[] = []
  const taken = new Set<string>()
  let seq = 0
  const autoAlias = (prefix: string) => {
    for (;;) {
      seq += 1
      const candidate = `${prefix}${seq}`
      if (!taken.has(candidate)) return candidate
    }
  }
  const resolve = (explicit: string | undefined, fallback: string, prefix: string): string => {
    const trimmed = (explicit ?? '').trim()
    if (trimmed) return trimmed
    return isValidIdentifier(fallback) ? fallback : autoAlias(prefix)
  }
  const columnAliases = block.columns.map(column => {
    const alias = column.agg
      ? resolve(column.alias, column.field ? `${column.agg}_${column.field}` : 'count', `${column.agg}_`)
      : resolve(column.alias, column.field ?? '', 'col_')
    if (!taken.has(alias)) {
      taken.add(alias)
      outputs.push(alias)
    }
    return alias
  })
  let bucketAlias: string | undefined
  if (block.time_bucket) {
    bucketAlias = resolve(block.time_bucket.alias, `${block.time_bucket.field}__${block.time_bucket.unit}`, 'bucket_')
    if (!taken.has(bucketAlias)) {
      taken.add(bucketAlias)
      outputs.push(bucketAlias)
    }
  }
  return { columnAliases, bucketAlias, outputs }
}

export function emptyReportPlan(): ReportPlan {
  return {
    version: 1,
    filters: [],
    blocks: [{ type: 'text', text: '请在这里说明本篇报告要回答的业务问题。' }],
  }
}

export function cloneReportPlan(plan: ReportPlan): ReportPlan {
  return JSON.parse(JSON.stringify(plan)) as ReportPlan
}

export function queryBlocks(plan: ReportPlan): PlanQueryBlock[] {
  return plan.blocks.filter((block): block is PlanQueryBlock => block.type === 'query')
}

export function queryNames(plan: ReportPlan): string[] {
  return queryBlocks(plan).map(block => block.name).filter(Boolean)
}

/** 查询块的输出列（列别名 + 时间桶列），排序键与图表 x/y 只能从中选择。 */
export function queryOutputColumns(block: PlanQueryBlock): string[] {
  return resolveQueryAliases(block).outputs
}

export function makeQueryBlock(name: string, columns: OperationsDatasetColumn[] = []): PlanQueryBlock {
  const first = columns[0]
  return {
    type: 'query',
    name: name || 'query',
    columns: first ? [{ field: first.name, alias: asciiAlias(first.name, 'col') }] : [{ agg: 'count', alias: 'count' }],
    limit: PLAN_QUERY_LIMIT_DEFAULT,
  }
}

export function makeChartBlock(query: string, chart: PlanChartKind = 'table'): PlanChartBlock {
  return { type: 'chart', chart, query, y: [] }
}

// 结构白名单（与后端 Pydantic extra="forbid" 的字段集一致）。
const PLAN_KEYS: readonly string[] = ['version', 'filters', 'blocks']
const FILTER_KEYS: readonly string[] = ['name', 'type', 'field']
const TEXT_BLOCK_KEYS: readonly string[] = ['type', 'text']
const QUERY_BLOCK_KEYS: readonly string[] = ['type', 'name', 'columns', 'time_bucket', 'sort', 'limit']
const COLUMN_KEYS: readonly string[] = ['field', 'agg', 'alias']
const BUCKET_KEYS: readonly string[] = ['field', 'unit', 'alias']
const SORT_KEYS: readonly string[] = ['field', 'dir']
const CHART_BLOCK_KEYS: readonly string[] = ['type', 'chart', 'query', 'x', 'y']

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value)
}

function hasOnlyKeys(raw: Record<string, unknown>, allowed: readonly string[]): boolean {
  return Object.keys(raw).every(key => allowed.includes(key))
}

/**
 * 严格结构识别（与后端 Pydantic fail-closed 同向）：
 * - 版本必须是 1；顶层/块/列/时间桶/排序键/筛选器出现未知键 → 整体 null；
 * - 块类型只有 text/query/chart，未知块类型 → 整体 null；
 * - 聚合、图表类型、筛选类型、时间粒度、排序方向必须是白名单枚举，
 *   未知值 → 整体 null（绝不静默降级成普通列/table/text/asc）；
 * - 可选字段允许 null/缺省（后端 plan_json 的 null → undefined）；
 * - 语义问题原样保留给 validateReportPlan 提示：空白文本、空列、空查询名、
 *   越界/非整数 limit（不做 clamp 静默改语义）都不在结构层拦截。
 */
export function normalizeReportPlan(value: unknown): ReportPlan | null {
  if (!isPlainObject(value)) return null
  const input = value
  if (!hasOnlyKeys(input, PLAN_KEYS) || input.version !== 1 || !Array.isArray(input.blocks)) return null
  if (input.filters != null && !Array.isArray(input.filters)) return null

  const filters: ReportPlanFilterPayload[] = []
  for (const item of (input.filters ?? []) as unknown[]) {
    if (!isPlainObject(item) || !hasOnlyKeys(item, FILTER_KEYS)) return null
    if (typeof item.name !== 'string' || !PLAN_FILTERS.includes(item.type as PlanFilterType)) return null
    if (item.field != null && typeof item.field !== 'string') return null
    filters.push({
      name: item.name.trim(),
      type: item.type as PlanFilterType,
      ...(item.field != null ? { field: item.field } : {}),
    })
  }

  const blocks: PlanBlock[] = []
  for (const rawValue of input.blocks as unknown[]) {
    if (!isPlainObject(rawValue)) return null
    if (rawValue.type === 'text') {
      if (!hasOnlyKeys(rawValue, TEXT_BLOCK_KEYS) || typeof rawValue.text !== 'string') return null
      blocks.push({ type: 'text', text: rawValue.text })
      continue
    }
    if (rawValue.type === 'query') {
      if (!hasOnlyKeys(rawValue, QUERY_BLOCK_KEYS)) return null
      if (typeof rawValue.name !== 'string' || !Array.isArray(rawValue.columns)) return null
      const columns: PlanQueryColumn[] = []
      for (const item of rawValue.columns as unknown[]) {
        if (!isPlainObject(item) || !hasOnlyKeys(item, COLUMN_KEYS)) return null
        if (item.field != null && typeof item.field !== 'string') return null
        if (item.agg != null && !PLAN_AGGS.includes(item.agg as PlanAgg)) return null
        if (item.alias != null && typeof item.alias !== 'string') return null
        columns.push({
          ...(item.field != null ? { field: item.field } : {}),
          ...(item.agg != null ? { agg: item.agg as PlanAgg } : {}),
          ...(item.alias != null ? { alias: item.alias } : {}),
        })
      }
      let time_bucket: PlanTimeBucket | undefined
      if (rawValue.time_bucket != null) {
        if (!isPlainObject(rawValue.time_bucket) || !hasOnlyKeys(rawValue.time_bucket, BUCKET_KEYS)) return null
        const bucket = rawValue.time_bucket
        if (typeof bucket.field !== 'string' || !PLAN_BUCKETS.includes(bucket.unit as PlanBucket)) return null
        if (bucket.alias != null && typeof bucket.alias !== 'string') return null
        time_bucket = {
          field: bucket.field,
          unit: bucket.unit as PlanBucket,
          ...(bucket.alias != null ? { alias: bucket.alias } : {}),
        }
      }
      let sort: { field: string; dir: 'asc' | 'desc' }[] = []
      if (rawValue.sort != null) {
        if (!Array.isArray(rawValue.sort)) return null
        const keys: { field: string; dir: 'asc' | 'desc' }[] = []
        for (const item of rawValue.sort as unknown[]) {
          if (!isPlainObject(item) || !hasOnlyKeys(item, SORT_KEYS)) return null
          if (typeof item.field !== 'string') return null
          // 后端 dir 缺省为 asc；未知方向是结构违规，整体拒绝。
          const dir = item.dir ?? 'asc'
          if (dir !== 'asc' && dir !== 'desc') return null
          keys.push({ field: item.field, dir })
        }
        sort = keys
      }
      let limit: number | undefined
      if (rawValue.limit != null) {
        if (typeof rawValue.limit !== 'number' || !Number.isFinite(rawValue.limit)) return null
        limit = rawValue.limit
      }
      blocks.push({
        type: 'query',
        name: rawValue.name.trim(),
        columns,
        sort,
        ...(time_bucket ? { time_bucket } : {}),
        ...(limit !== undefined ? { limit } : {}),
      })
      continue
    }
    if (rawValue.type === 'chart') {
      if (!hasOnlyKeys(rawValue, CHART_BLOCK_KEYS)) return null
      if (!PLAN_CHARTS.includes(rawValue.chart as PlanChartKind) || typeof rawValue.query !== 'string') return null
      if (rawValue.x != null && typeof rawValue.x !== 'string') return null
      let y: string[] = []
      if (rawValue.y != null) {
        if (!Array.isArray(rawValue.y)) return null
        for (const item of rawValue.y as unknown[]) {
          if (typeof item !== 'string') return null
        }
        y = rawValue.y as string[]
      }
      blocks.push({
        type: 'chart',
        chart: rawValue.chart as PlanChartKind,
        query: rawValue.query.trim(),
        ...(rawValue.x ? { x: rawValue.x } : {}),
        y,
      })
      continue
    }
    return null
  }
  return { version: 1, filters, blocks }
}

export function planPayload(plan: ReportPlan): ReportPlanPayload {
  return cloneReportPlan(plan) as ReportPlanPayload
}

// 与后端 _UNSAFE_FIELD_RE 同源：字段含换行/控制字符（C0 及 DEL）或
// 围栏标记时服务端拒绝；用码点判断而不是控制字符正则（no-control-regex）。
function isUnsafeField(field: string): boolean {
  if (field.includes('```') || field.includes('~~~')) return true
  for (let index = 0; index < field.length; index += 1) {
    const code = field.charCodeAt(index)
    if (code <= 0x1f || code === 0x7f) return true
  }
  return false
}

function unsafeFieldIssue(field: string, where: string): string {
  return `${where}：字段「${field}」含换行/控制字符或围栏标记，不能用于报告计划`
}

/**
 * 保存/试跑前的用户侧预检，规则与 backend normalize_plan 逐条镜像，
 * 返回中文问题列表（空数组 = 通过）。
 *
 * columns 参数三态：省略（undefined）表示调用方没有数据集 schema（如试跑前
 * 的纯结构/语义校验），跳过字段存在性与列类型判断交服务端兜底；显式传入
 * 列清单时才做字段级校验——包括空数组代表的真实无字段资产（全部字段引用
 * 都会判为不存在）。
 */
export function validateReportPlan(plan: ReportPlan, columns?: OperationsDatasetColumn[]): string[] {
  const issues: string[] = []
  const known = new Map((columns ?? []).map(column => [column.name, normalizedColumnType(column.type)]))
  const typed = columns !== undefined

  if (!plan.blocks.length) issues.push('计划至少需要一个内容块')
  if (plan.blocks.length > PLAN_BLOCKS_MAX) issues.push(`内容块不能超过 ${PLAN_BLOCKS_MAX} 个`)
  if (plan.filters.length > PLAN_FILTERS_MAX) issues.push(`筛选器不能超过 ${PLAN_FILTERS_MAX} 个`)
  if (queryBlocks(plan).length > PLAN_QUERY_BLOCKS_MAX) issues.push(`查询块不能超过 ${PLAN_QUERY_BLOCKS_MAX} 个`)

  const filterNames = new Set<string>()
  plan.filters.forEach((filter, index) => {
    const label = filter.name || `第 ${index + 1} 个筛选器`
    if (!isValidIdentifier(filter.name)) {
      issues.push(`筛选器「${label}」名称必须是字母或下划线开头的 ASCII 标识符`)
    } else if (filterNames.has(filter.name)) {
      issues.push(`筛选器「${filter.name}」重复`)
    }
    filterNames.add(filter.name)
    const field = filter.field || filter.name
    if (isUnsafeField(field)) {
      issues.push(unsafeFieldIssue(field, `筛选器「${label}」`))
    } else {
      const fieldType = known.get(field)
      if (typed && fieldType === undefined) {
        issues.push(`筛选器「${label}」字段「${field}」不在数据集中`)
      } else if (fieldType !== undefined && !FILTER_FIELD_TYPES[filter.type].has(fieldType)) {
        issues.push(`筛选器「${label}」类型（${PLAN_FILTER_TYPE_LABELS[filter.type]}）与字段「${field}」的类型 ${fieldType} 不兼容`)
      }
    }
  })

  const queryOutputs = new Map<string, Set<string>>()
  const queryNamesSeen = new Set<string>()
  plan.blocks.forEach((block, index) => {
    const where = `第 ${index + 1} 块`
    if (block.type === 'text') {
      if (!block.text.trim()) issues.push(`${where}：说明文字不能为空白`)
      if (block.text.length > PLAN_TEXT_MAX_CHARS) issues.push(`${where}：说明文字不能超过 ${PLAN_TEXT_MAX_CHARS} 字符`)
      if (FENCE_LINE_RE.test(block.text)) issues.push(`${where}：说明文字不能包含围栏标记（\`\`\` 或 ~~~）`)
      if (HTML_TAG_RE.test(block.text)) issues.push(`${where}：说明文字不能包含 HTML 标签`)
      return
    }
    if (block.type !== 'query') return
    const label = block.name || where
    if (!QUERY_NAME_RE.test(block.name)) {
      issues.push(`${where}：查询名「${block.name}」必须以字母开头，仅含字母、数字、下划线或中划线`)
    } else if (queryNamesSeen.has(block.name)) {
      issues.push(`${where}：查询名「${block.name}」重复`)
    }
    queryNamesSeen.add(block.name)
    if (block.columns.length < 1) issues.push(`${where}：查询「${label}」至少需要一个输出列`)
    if (block.columns.length > PLAN_QUERY_COLUMNS_MAX) issues.push(`${where}：查询「${label}」输出列不能超过 ${PLAN_QUERY_COLUMNS_MAX} 个`)

    // 输出别名解析与后端一致：显式别名必须合法；缺省/中文默认值由
    // col_N、{agg}_N、bucket_N 自动兜底（不报错），重复输出列仍显式拦截。
    const resolved = resolveQueryAliases(block)
    const outputs = new Set<string>()
    resolved.columnAliases.forEach((alias, columnIndex) => {
      if (outputs.has(alias)) issues.push(`查询「${label}」第 ${columnIndex + 1} 列：输出列「${alias}」重复`)
      else outputs.add(alias)
    })
    block.columns.forEach((column, columnIndex) => {
      const cwhere = `查询「${label}」第 ${columnIndex + 1} 列`
      const explicitAlias = column.alias?.trim()
      if (explicitAlias && !isValidIdentifier(explicitAlias)) {
        issues.push(`${cwhere}：别名「${explicitAlias}」必须是字母/下划线开头的 ASCII 标识符（不超过 63 字节）`)
      }
      if (!column.agg) {
        if (!column.field) {
          issues.push(`${cwhere}：原始字段列必须选择字段`)
          return
        }
        if (isUnsafeField(column.field)) {
          issues.push(unsafeFieldIssue(column.field, cwhere))
          return
        }
        const fieldType = known.get(column.field)
        if (typed && fieldType === undefined) issues.push(`${cwhere}：字段「${column.field}」不在数据集中`)
        return
      }
      if (column.agg !== 'count' && !column.field) {
        issues.push(`${cwhere}：聚合「${PLAN_AGG_LABELS[column.agg]}」必须选择字段`)
        return
      }
      if (column.field && isUnsafeField(column.field)) {
        issues.push(unsafeFieldIssue(column.field, cwhere))
      } else if (column.field) {
        const fieldType = known.get(column.field)
        if (typed && fieldType === undefined) {
          issues.push(`${cwhere}：字段「${column.field}」不在数据集中`)
        } else if (fieldType !== undefined) {
          if ((column.agg === 'sum' || column.agg === 'avg') && !NUMERIC_TYPES.has(fieldType)) {
            issues.push(`${cwhere}：聚合「${PLAN_AGG_LABELS[column.agg]}」只支持数值列，字段「${column.field}」是 ${fieldType}`)
          } else if ((column.agg === 'min' || column.agg === 'max') && !SCALAR_TYPES.has(fieldType)) {
            issues.push(`${cwhere}：聚合「${PLAN_AGG_LABELS[column.agg]}」不支持 ${fieldType} 列`)
          }
        }
      }
    })

    if (block.time_bucket) {
      const bucket = block.time_bucket
      const bucketAlias = resolved.bucketAlias
      if (bucket.alias?.trim() && !isValidIdentifier(bucket.alias.trim())) {
        issues.push(`${where}：时间桶别名「${bucket.alias}」必须是字母/下划线开头的 ASCII 标识符（不超过 63 字节）`)
      }
      const fieldType = known.get(bucket.field)
      if (isUnsafeField(bucket.field)) {
        issues.push(unsafeFieldIssue(bucket.field, `${where} 时间桶`))
      } else if (typed && fieldType === undefined) {
        issues.push(`${where}：时间桶字段「${bucket.field}」不在数据集中`)
      } else if (fieldType !== undefined && !TEMPORAL_TYPES.has(fieldType)) {
        issues.push(`${where}：时间桶字段「${bucket.field}」必须是日期/时间列（实际 ${fieldType}）`)
      }
      if (bucketAlias) {
        if (outputs.has(bucketAlias)) issues.push(`${where}：时间桶输出列「${bucketAlias}」重复`)
        else outputs.add(bucketAlias)
      }
    }

    if (block.sort?.length) {
      if (block.sort.length > PLAN_SORT_KEYS_MAX) issues.push(`${where}：排序键不能超过 ${PLAN_SORT_KEYS_MAX} 个`)
      const sortSeen = new Set<string>()
      block.sort.forEach(key => {
        if (!outputs.has(key.field)) issues.push(`${where}：排序键「${key.field}」必须是查询「${label}」的输出列`)
        else if (sortSeen.has(key.field)) issues.push(`${where}：排序键「${key.field}」重复`)
        sortSeen.add(key.field)
      })
    }

    if (block.limit !== undefined && (block.limit < 1 || block.limit > PLAN_QUERY_LIMIT_MAX || !Number.isInteger(block.limit))) {
      issues.push(`${where}：查询「${label}」返回上限必须是 1–${PLAN_QUERY_LIMIT_MAX} 的整数`)
    }
    if (block.name) queryOutputs.set(block.name, outputs)
  })

  plan.blocks.forEach((block, index) => {
    if (block.type !== 'chart') return
    const where = `第 ${index + 1} 块`
    const chartLabel = PLAN_CHART_LABELS[block.chart]
    const outputs = queryOutputs.get(block.query.trim())
    if (!outputs) {
      issues.push(`${where}：图表引用的查询「${block.query}」不存在`)
      return
    }
    const x = block.x
    const y = block.y ?? []
    if ((block.chart === 'kpi' || block.chart === 'table') && x) issues.push(`${where}：${chartLabel}不支持 x 列`)
    if ((block.chart === 'bar' || block.chart === 'line' || block.chart === 'pie') && !x) issues.push(`${where}：${chartLabel}需要选择 x 列`)
    if (block.chart === 'kpi' || block.chart === 'pie') {
      if (y.length !== 1) issues.push(`${where}：${chartLabel}需要恰好一个 y 列`)
    } else if (block.chart === 'bar' || block.chart === 'line') {
      if (y.length < 1) issues.push(`${where}：${chartLabel}至少需要一个 y 列`)
    } else if (y.length) {
      issues.push(`${where}：表格不支持 y 列（按查询输出列全量展示）`)
    }
    if (y.length > PLAN_CHART_SERIES_MAX) issues.push(`${where}：图表系列不能超过 ${PLAN_CHART_SERIES_MAX} 个`)
    for (const target of [x, ...y]) {
      if (target && !outputs.has(target)) issues.push(`${where}：图表列「${target}」不是查询「${block.query}」的输出列`)
    }
  })
  return issues
}

export function planBlockLabel(block: PlanBlock): string {
  if (block.type === 'text') return '说明文字'
  if (block.type === 'query') return `查询 · ${block.name}`
  return `图表 · ${PLAN_CHART_LABELS[block.chart]}`
}

export interface ReportPlanTemplate {
  id: string
  name: string
  description: string
  build: (columns: OperationsDatasetColumn[]) => ReportPlan
  /** 返回不可选原因（undefined = 可选），如趋势模板要求数据集存在时间列。 */
  disabledReason?: (columns: OperationsDatasetColumn[]) => string | undefined
}

/** 逐列生成互不冲突的合法 ASCII 别名（中文/特殊字段回退到 fallback 前缀）。 */
function uniqueAliases(fields: string[], fallbackPrefix = 'col'): string[] {
  const taken = new Set<string>()
  return fields.map((field, index) => {
    const base = isValidIdentifier(field) ? field.slice(0, 63) : asciiAlias(field, `${fallbackPrefix}_${index + 1}`)
    let candidate = base
    let suffix = 2
    while (taken.has(candidate)) candidate = `${base.slice(0, 63 - String(suffix).length)}_${suffix++}`
    taken.add(candidate)
    return candidate
  })
}

export const REPORT_PLAN_TEMPLATES: readonly ReportPlanTemplate[] = [
  {
    id: 'overview',
    name: '数据概览',
    description: '总量 KPI 与一张样例明细表',
    build: columns => {
      const sampleFields = columns.slice(0, 6).map(column => column.name)
      const aliases = uniqueAliases(sampleFields)
      return { version: 1, filters: [], blocks: [
        { type: 'text', text: '本篇报告展示数据集的整体规模与样例明细。' },
        { type: 'query', name: 'overview_total', columns: [{ agg: 'count', alias: 'total_rows' }], limit: 1 },
        { type: 'chart', chart: 'kpi', query: 'overview_total', y: ['total_rows'] },
        {
          type: 'query',
          name: 'overview_sample',
          columns: sampleFields.length
            ? sampleFields.map((field, index) => ({ field, alias: aliases[index] }))
            : [{ agg: 'count', alias: 'count' }],
          limit: 20,
        },
        { type: 'chart', chart: 'table', query: 'overview_sample' },
      ] }
    },
  },
  {
    id: 'trend',
    name: '趋势分析',
    description: '按时间字段聚合的折线趋势（需要日期/时间列）',
    disabledReason: columns => columns.some(isTemporalColumn)
      ? undefined
      : '数据集没有日期/时间类型的字段，无法按时间构建趋势分析。',
    build: columns => {
      const timeColumn = columns.find(isTemporalColumn)
      const unit: PlanBucket = 'day'
      // 中文时间字段的默认桶名不是合法别名，模板直接派生 ASCII 别名，
      // 保证保存/重读后 sort 与图表 x 引用不漂移。
      const bucket = timeColumn
        ? {
          field: timeColumn.name,
          unit,
          ...(isValidIdentifier(`${timeColumn.name}__${unit}`)
            ? {}
            : { alias: `${asciiAlias(timeColumn.name, 'time')}__${unit}` }),
        }
        : undefined
      const bucketAlias = bucket ? bucketAliasOf(bucket) : ''
      return { version: 1, filters: [], blocks: [
        { type: 'text', text: timeColumn
          ? `按「${timeColumn.name}」${PLAN_BUCKET_LABELS[unit]}聚合事件量，先试跑确认口径再保存。`
          : '请选择时间字段和指标，试跑后再保存趋势分析。' },
        {
          type: 'query',
          name: 'trend',
          columns: [{ agg: 'count', alias: 'count' }],
          ...(bucket ? { time_bucket: bucket } : {}),
          sort: bucket ? [{ field: bucketAlias, dir: 'asc' }] : [],
          limit: PLAN_QUERY_LIMIT_DEFAULT,
        },
        {
          type: 'chart',
          chart: bucket ? 'line' : 'table',
          query: 'trend',
          ...(bucket ? { x: bucketAlias, y: ['count'] } : {}),
        },
      ] }
    },
  },
]

export function planFromTemplate(template: ReportPlanTemplate, columns: OperationsDatasetColumn[]): ReportPlan {
  return template.build(columns)
}

function quoteIdentifier(identifier: string): string {
  return `"${identifier.replaceAll('"', '""')}"`
}

// 与 backend compile_query_sql 逐语句一致：count_distinct → COUNT(DISTINCT …)、
// 时间桶 DATE_TRUNC 投影 + 分组、number/date 筛选比较侧 CAST、
// ORDER BY 引用输出别名、LIMIT 恒有（未声明取默认预算）。
const FILTER_PARAM_CASTS: Record<PlanFilterType, string | null> = { text: null, number: 'NUMERIC', date: 'DATE' }

function compileQuerySql(block: PlanQueryBlock, filters: ReportPlanFilterPayload[]): string {
  const { columnAliases, bucketAlias } = resolveQueryAliases(block)
  const projections: string[] = []
  const groupExprs: string[] = []
  let hasAggregate = false
  block.columns.forEach((column, index) => {
    const field = column.field ?? ''
    const alias = columnAliases[index]
    if (!column.agg) {
      projections.push(`${quoteIdentifier(field)} AS ${quoteIdentifier(alias)}`)
      groupExprs.push(quoteIdentifier(field))
      return
    }
    hasAggregate = true
    const inner = column.agg === 'count'
      ? (field ? quoteIdentifier(field) : '*')
      : column.agg === 'count_distinct'
        ? `DISTINCT ${quoteIdentifier(field)}`
        : quoteIdentifier(field)
    const sqlFunc = column.agg === 'count_distinct' ? 'COUNT' : column.agg.toUpperCase()
    projections.push(`${sqlFunc}(${inner}) AS ${quoteIdentifier(alias)}`)
  })
  if (block.time_bucket && bucketAlias) {
    hasAggregate = true
    const expression = `DATE_TRUNC('${block.time_bucket.unit}', ${quoteIdentifier(block.time_bucket.field)})`
    projections.push(`${expression} AS ${quoteIdentifier(bucketAlias)}`)
    groupExprs.push(expression)
  }
  const lines = [`SELECT ${projections.join(', ')}`, 'FROM dataset']
  if (filters.length) {
    const conditions = filters.map(filter => {
      let fieldExpr = quoteIdentifier(filter.field || filter.name)
      const cast = FILTER_PARAM_CASTS[filter.type]
      if (cast) fieldExpr = `CAST(${fieldExpr} AS ${cast})`
      return `(:${filter.name} IS NULL OR ${fieldExpr} = :${filter.name})`
    })
    lines.push(`WHERE ${conditions.join(' AND ')}`)
  }
  if (hasAggregate && groupExprs.length) lines.push(`GROUP BY ${groupExprs.join(', ')}`)
  if (block.sort?.length) {
    lines.push(`ORDER BY ${block.sort.map(key => `${quoteIdentifier(key.field)} ${key.dir === 'asc' ? 'ASC' : 'DESC'}`).join(', ')}`)
  }
  lines.push(`LIMIT ${block.limit ?? PLAN_QUERY_LIMIT_DEFAULT}`)
  return lines.join('\n')
}

/**
 * 计划 → 文章 Markdown（与 backend plan_markdown 输出同构）：结构化计划
 * 经此在同一阅读器渲染，查询 SQL 仅用于展示，真正执行仍走服务端编译。
 */
export function reportPlanToMarkdown(plan: ReportPlan): string {
  const chunks: string[] = []
  for (const block of plan.blocks) {
    if (block.type === 'text') {
      chunks.push(block.text.trim())
    } else if (block.type === 'query') {
      chunks.push(`\`\`\`sql ${block.name}\n${compileQuerySql(block, plan.filters)}\n\`\`\``)
    } else {
      const config = [`query: ${block.query}`, `type: ${block.chart}`]
      if (block.x) config.push(`x: ${block.x}`)
      if (block.y?.length) config.push(`y: ${block.y.join(', ')}`)
      chunks.push(`\`\`\`chart\n${config.join('\n')}\n\`\`\``)
    }
  }
  for (const filter of plan.filters) {
    chunks.push(`\`\`\`filter\nname: ${filter.name}\ntype: ${filter.type}\n\`\`\``)
  }
  return chunks.filter(Boolean).join('\n\n') + '\n'
}

export function normalizeReportPlanCandidate(value: unknown): ReportPlan | null {
  if (!value || typeof value !== 'object') return null
  const raw = value as Record<string, unknown>
  return normalizeReportPlan(raw.plan ?? raw)
}

export type ReportPlanBlockDiffStatus = 'unchanged' | 'changed' | 'added' | 'removed'
export interface ReportPlanBlockDiff {
  index: number
  status: ReportPlanBlockDiffStatus
  current: PlanBlock | null
  candidate: PlanBlock | null
}

function planBlocksEqual(left: PlanBlock, right: PlanBlock): boolean {
  if (left.type !== right.type) return false
  return JSON.stringify(left) === JSON.stringify(right)
}

/** 块级差异（接受 AI 候选前的审阅视图）：按位置对比，能标记新增与删除。 */
export function diffReportPlanBlocks(current: PlanBlock[], candidate: PlanBlock[]): ReportPlanBlockDiff[] {
  const rows: ReportPlanBlockDiff[] = []
  for (let index = 0; index < Math.max(current.length, candidate.length); index++) {
    const before = current[index] ?? null
    const after = candidate[index] ?? null
    if (!before) rows.push({ index, status: 'added', current: null, candidate: after })
    else if (!after) rows.push({ index, status: 'removed', current: before, candidate: null })
    else rows.push({ index, status: planBlocksEqual(before, after) ? 'unchanged' : 'changed', current: before, candidate: after })
  }
  return rows
}
