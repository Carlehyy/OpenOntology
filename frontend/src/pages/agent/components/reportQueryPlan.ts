/**
 * 报告模板查询计划的编辑器模型（纯函数）。
 *
 * 报告执行端（backend/app/ontologies/agent_runtime/reporting.py）只接受
 * aggregate_objects / search_objects / query_objects 三种只读工具，并对参数
 * 做白名单规范化。编辑器必须：
 * - 旧模板（aggregate/search）可无损加载、编辑、试跑、发布；
 * - query_objects 的统一查询参数（select/group_by/aggregates/order_by/
 *   page_size/joins/window 相对时间）原样保留，不能被表单过滤掉；
 * - 不提供任意 SQL / 自由 JSON 输入；保存前按工具白名单校验未知参数。
 */
import type { ReportQuery, ReportSection, ReportVisualization } from '@/api/agent'

export type ReportQueryTool = ReportQuery['tool']

export type EditorQuery = ReportQuery
export type EditorSection = ReportSection


export const REPORT_QUERY_TOOLS: readonly { value: ReportQueryTool; label: string; hint: string }[] = [
  { value: 'aggregate_objects', label: '聚合统计', hint: '单一指标 · 单维分组' },
  { value: 'search_objects', label: '关键词检索', hint: '样本 · 命中数量' },
  { value: 'query_objects', label: '统一查询', hint: '明细 · 多维分组 · 相对时间' },
]

export const AGGREGATE_METRICS = ['count', 'sum', 'avg', 'min', 'max'] as const
export const QUERY_AGGREGATE_FNS = ['count', 'count_distinct', 'sum', 'avg', 'min', 'max'] as const
export const QUERY_BUCKETS = ['day', 'week', 'month', 'quarter', 'year'] as const
export const FILTER_OPS = ['eq', 'neq', 'contains', 'in', 'gt', 'gte', 'lt', 'lte'] as const
export const QUERY_FILTER_OPS = [
  'eq', 'neq', 'contains', 'in', 'gt', 'gte', 'lt', 'lte', 'is_null', 'is_not_null',
] as const
export const WINDOW_UNITS = ['second', 'minute', 'hour', ...QUERY_BUCKETS] as const
export const REPORT_VISUALIZATIONS = ['auto', 'kpi', 'bar', 'line', 'pie', 'table', 'none'] as const
export const DEFAULT_QUERY_PAGE_SIZE = 12
export const MAX_QUERY_PAGE_SIZE = 20
export const MAX_QUERIES_PER_SECTION = 4

export const AGGREGATE_ARGUMENT_KEYS = [
  'object_type', 'metric', 'metric_property', 'group_by', 'filters',
] as const
export const SEARCH_ARGUMENT_KEYS = [
  'object_type', 'filters', 'keyword', 'limit',
] as const
export const QUERY_ARGUMENT_KEYS = [
  'object_type', 'joins', 'filters', 'select', 'group_by', 'aggregates',
  'order_by', 'page_size', 'window',
] as const

const TOOL_ARGUMENT_KEYS: Record<ReportQueryTool, readonly string[]> = {
  aggregate_objects: AGGREGATE_ARGUMENT_KEYS,
  search_objects: SEARCH_ARGUMENT_KEYS,
  query_objects: QUERY_ARGUMENT_KEYS,
}

type UnknownRecord = Record<string, unknown>

const isRecord = (value: unknown): value is UnknownRecord =>
  Boolean(value) && typeof value === 'object' && !Array.isArray(value)

const stringOf = (value: unknown): string => typeof value === 'string' ? value : ''

const numberOf = (value: unknown, fallback: number): number => {
  if (value === null || value === undefined || value === '') return fallback
  const parsed = typeof value === 'number' ? value : Number(value)
  return Number.isFinite(parsed) ? parsed : fallback
}

function cloneJson<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T
}

export function toolLabel(tool: string): string {
  return REPORT_QUERY_TOOLS.find(item => item.value === tool)?.label || tool || '未选择工具'
}

export function isReportQueryTool(tool: string): tool is ReportQueryTool {
  return REPORT_QUERY_TOOLS.some(item => item.value === tool)
}

export function argumentString(query: EditorQuery, key: string): string {
  return stringOf(query.arguments[key])
}

export function argumentNumber(query: EditorQuery, key: string, fallback: number): number {
  return numberOf(query.arguments[key], fallback)
}

export function withArgument(query: EditorQuery, key: string, value: unknown): EditorQuery {
  const args = { ...query.arguments }
  if (value === undefined) delete args[key]
  else args[key] = value
  return { ...query, arguments: args }
}

export function unknownArgumentKeys(query: EditorQuery): string[] {
  if (!isReportQueryTool(query.tool)) return Object.keys(query.arguments)
  const allowed = new Set(TOOL_ARGUMENT_KEYS[query.tool])
  return Object.keys(query.arguments).filter(key => !allowed.has(key))
}

/** 切换工具时只保留对象类型，避免旧工具参数被新工具误解释。 */
export function switchQueryTool(query: EditorQuery, tool: ReportQueryTool): EditorQuery {
  if (query.tool === tool) return query
  const objectType = argumentString(query, 'object_type')
  const args: UnknownRecord = objectType ? { object_type: objectType } : {}
  if (tool === 'aggregate_objects') args.metric = 'count'
  if (tool === 'search_objects') args.limit = DEFAULT_QUERY_PAGE_SIZE
  if (tool === 'query_objects') args.page_size = DEFAULT_QUERY_PAGE_SIZE
  return { tool, arguments: args }
}

export function defaultEditorQuery(objectType = ''): EditorQuery {
  return {
    tool: 'aggregate_objects',
    arguments: {
      ...(objectType ? { object_type: objectType } : {}),
      metric: 'count',
    },
  }
}

export function normalizeQueryPlan(value: unknown): EditorQuery[] {
  if (!Array.isArray(value)) return []
  return value.flatMap(item => {
    if (!isRecord(item)) return []
    const tool = stringOf(item.tool)
    if (!isReportQueryTool(tool)) return []
    return [{
      tool,
      arguments: isRecord(item.arguments) ? cloneJson(item.arguments) : {},
    }]
  })
}

export function normalizeEditorSections(value: unknown): EditorSection[] {
  if (!Array.isArray(value)) return []
  return value.map((raw, index) => {
    const section = isRecord(raw) ? raw : {}
    const visualization = stringOf(section.visualization)
    return {
      id: stringOf(section.id) || `section-${index + 1}`,
      title: stringOf(section.title),
      goal: stringOf(section.goal),
      visualization: (REPORT_VISUALIZATIONS as readonly string[]).includes(visualization)
        ? visualization as ReportVisualization
        : 'auto',
      queryPlan: normalizeQueryPlan(section.queryPlan ?? section.query_plan),
    }
  })
}

/** API 边界仍使用 api/agent.ts 的 ReportSection/ReportQuery，编辑器不做字段过滤。 */
export function reportSectionsFromEditor(sections: EditorSection[]): ReportSection[] {
  return sections.map(section => ({
    ...section,
    queryPlan: section.queryPlan.map(query => ({
      tool: query.tool,
      arguments: cloneJson(query.arguments),
    })),
  }))
}

// ---------- 过滤条件 ----------

export interface FilterRow {
  property: string
  op: string
  value: unknown
  objectType: string
}

export function filterRows(query: EditorQuery): FilterRow[] {
  const value = query.arguments.filters
  if (!Array.isArray(value)) return []
  return value.flatMap(item => {
    if (!isRecord(item)) return []
    return [{
      property: stringOf(item.property),
      op: stringOf(item.op) || 'eq',
      value: item.value,
      objectType: stringOf(item.object_type),
    }]
  })
}

export function withFilterRows(query: EditorQuery, rows: FilterRow[]): EditorQuery {
  const filters = rows.map(row => {
    const entry: UnknownRecord = {
      property: row.property,
      op: row.op || 'eq',
    }
    if (row.objectType) entry.object_type = row.objectType
    if (row.op !== 'is_null' && row.op !== 'is_not_null') {
      entry.value = row.value === undefined ? '' : row.value
    }
    return entry
  })
  return withArgument(query, 'filters', filters.length ? filters : undefined)
}

const NUMBER_PATTERN = /^-?(?:\d+|\d*\.\d+)$/

/** 过滤值输入框约定：JSON 数组/对象、null、布尔、纯数字自动还原类型，其余按字符串。 */
export function filterValueToText(value: unknown): string {
  if (value === undefined) return ''
  if (typeof value === 'string') return value
  if (value === null) return 'null'
  if (typeof value === 'object') {
    try {
      return JSON.stringify(value)
    } catch {
      return ''
    }
  }
  return String(value)
}

export function filterValueFromText(text: string): unknown {
  const trimmed = text.trim()
  if (trimmed === '') return ''
  if (trimmed === 'null') return null
  if (trimmed === 'true') return true
  if (trimmed === 'false') return false
  if (NUMBER_PATTERN.test(trimmed) && String(Number(trimmed)) === trimmed) {
    return Number(trimmed)
  }
  if (trimmed.startsWith('[') || trimmed.startsWith('{')) {
    try {
      return JSON.parse(trimmed)
    } catch {
      return text
    }
  }
  return text
}

// ---------- query_objects：投影 / 分组 / 聚合 / 排序 / 关联 / 窗口 ----------

/** 输出属性以「objectType.property」或裸属性名编辑，逗号分隔。 */
export function querySelectText(query: EditorQuery): string {
  const value = query.arguments.select
  if (!Array.isArray(value)) return ''
  return value.flatMap(item => {
    if (typeof item === 'string') return [item.trim()]
    if (!isRecord(item)) return []
    const property = stringOf(item.property).trim()
    if (!property) return []
    const objectType = stringOf(item.object_type).trim()
    return [objectType ? `${objectType}.${property}` : property]
  }).filter(Boolean).join(', ')
}

export function withQuerySelectText(query: EditorQuery, text: string): EditorQuery {
  const items = text.split(/[,，\n]/).map(token => token.trim()).filter(Boolean).map(token => {
    const dot = token.indexOf('.')
    if (dot > 0) {
      const objectType = token.slice(0, dot).trim()
      const property = token.slice(dot + 1).trim()
      if (objectType && property) return { object_type: objectType, property }
    }
    return token
  })
  return withArgument(query, 'select', items.length ? items : undefined)
}

export interface GroupRow {
  property: string
  bucket: string
  objectType: string
}

export function groupRows(query: EditorQuery): GroupRow[] {
  const value = query.arguments.group_by
  if (!Array.isArray(value)) return []
  return value.flatMap(item => {
    if (typeof item === 'string') return [{ property: item, bucket: '', objectType: '' }]
    if (!isRecord(item)) return []
    return [{
      property: stringOf(item.property),
      bucket: stringOf(item.bucket),
      objectType: stringOf(item.object_type),
    }]
  })
}

export function withGroupRows(query: EditorQuery, rows: GroupRow[]): EditorQuery {
  const items = rows.map(row => {
    if (!row.bucket && !row.objectType) return row.property
    return {
      property: row.property,
      ...(row.objectType ? { object_type: row.objectType } : {}),
      ...(row.bucket ? { bucket: row.bucket } : {}),
    }
  })
  return withArgument(query, 'group_by', items.length ? items : undefined)
}

export interface AggregateRow {
  fn: string
  property: string
  as: string
  objectType: string
}

export function aggregateRows(query: EditorQuery): AggregateRow[] {
  const value = query.arguments.aggregates
  if (!Array.isArray(value)) return []
  return value.flatMap(item => {
    if (!isRecord(item)) return []
    return [{
      fn: stringOf(item.fn) || 'count',
      property: stringOf(item.property),
      as: stringOf(item.as),
      objectType: stringOf(item.object_type),
    }]
  })
}

export function withAggregateRows(query: EditorQuery, rows: AggregateRow[]): EditorQuery {
  const items = rows.map(row => ({
    fn: row.fn || 'count',
    ...(row.property ? { property: row.property } : {}),
    ...(row.objectType ? { object_type: row.objectType } : {}),
    ...(row.as ? { as: row.as } : {}),
  }))
  return withArgument(query, 'aggregates', items.length ? items : undefined)
}

export interface OrderRow {
  output: string
  direction: string
}

export function orderRows(query: EditorQuery): OrderRow[] {
  const value = query.arguments.order_by
  if (!Array.isArray(value)) return []
  return value.flatMap(item => {
    if (!isRecord(item)) return []
    return [{ output: stringOf(item.output), direction: stringOf(item.direction) || 'asc' }]
  })
}

export function withOrderRows(query: EditorQuery, rows: OrderRow[]): EditorQuery {
  const items = rows.map(row => ({
    output: row.output,
    direction: row.direction || 'asc',
  }))
  return withArgument(query, 'order_by', items.length ? items : undefined)
}

export interface JoinRow {
  objectType: string
  linkType: string
  type: string
}

export function joinRows(query: EditorQuery): JoinRow[] {
  const value = query.arguments.joins
  if (!Array.isArray(value)) return []
  return value.flatMap(item => {
    if (!isRecord(item)) return []
    return [{
      objectType: stringOf(item.object_type),
      linkType: stringOf(item.link_type),
      type: stringOf(item.type) || 'inner',
    }]
  })
}

export function withJoinRows(query: EditorQuery, rows: JoinRow[]): EditorQuery {
  const items = rows.map(row => ({
    object_type: row.objectType,
    link_type: row.linkType,
    type: row.type || 'inner',
  }))
  return withArgument(query, 'joins', items.length ? items : undefined)
}

export interface WindowRow {
  property: string
  timezone: string
  mode: 'last' | 'range'
  unit: string
  count: number
  start: string
  end: string
}

export function windowRow(query: EditorQuery): WindowRow | null {
  const value = query.arguments.window
  if (!isRecord(value)) return null
  const last = isRecord(value.last) ? value.last : null
  return {
    property: stringOf(value.property),
    timezone: stringOf(value.timezone) || 'Asia/Shanghai',
    mode: last ? 'last' : 'range',
    unit: stringOf(last?.unit) || 'day',
    count: numberOf(last?.count, 7),
    start: stringOf(value.start),
    end: stringOf(value.end),
  }
}

export function withWindowRow(query: EditorQuery, row: WindowRow | null): EditorQuery {
  if (!row) return withArgument(query, 'window', undefined)
  const window: UnknownRecord = {
    property: row.property,
    timezone: row.timezone,
  }
  if (row.mode === 'last') {
    window.last = { unit: row.unit, count: row.count }
  } else {
    window.start = row.start
    window.end = row.end
  }
  return withArgument(query, 'window', window)
}

// ---------- 保存前校验 ----------

function validateQuery(query: EditorQuery, sectionTitle: string): string | null {
  const prefix = `章节「${sectionTitle || '未命名'}」`
  if (!isReportQueryTool(query.tool)) {
    return `${prefix}包含不支持的数据查询工具：${query.tool || '未选择'}`
  }
  const allowed = new Set(TOOL_ARGUMENT_KEYS[query.tool])
  const unknown = Object.keys(query.arguments).filter(key => !allowed.has(key))
  if (unknown.length) {
    return `${prefix}的 ${query.tool} 含界面不支持的参数：${unknown.join('、')}`
  }
  if (!argumentString(query, 'object_type').trim()) {
    return `${prefix}的数据查询缺少对象类型`
  }

  const filters = query.arguments.filters
  const filterLimit = query.tool === 'query_objects' ? 20 : 10
  if (filters !== undefined && filters !== null && (!Array.isArray(filters) || filters.length > filterLimit)) {
    return `${prefix}的过滤条件必须是最多 ${filterLimit} 项的数组`
  }
  if (Array.isArray(filters)) {
    for (const item of filters) {
      if (!isRecord(item) || !stringOf(item.property).trim()) {
        return `${prefix}的过滤条件必须包含属性`
      }
      const op = stringOf(item.op) || 'eq'
      const ops: readonly string[] = query.tool === 'query_objects' ? QUERY_FILTER_OPS : FILTER_OPS
      if (!ops.includes(op)) return `${prefix}包含不支持的过滤操作符：${op}`
    }
  }

  if (query.tool === 'aggregate_objects') {
    const metric = stringOf(query.arguments.metric) || 'count'
    if (!(AGGREGATE_METRICS as readonly string[]).includes(metric)) {
      return `${prefix}使用了不支持的聚合指标：${metric}`
    }
    if (metric !== 'count' && !argumentString(query, 'metric_property').trim()) {
      return `${prefix}的 ${metric} 聚合缺少属性`
    }
    const groupBy = query.arguments.group_by
    if (groupBy !== undefined && groupBy !== null && typeof groupBy !== 'string') {
      return `${prefix}的分组维度必须是单个属性名`
    }
  }

  if (query.tool === 'search_objects') {
    const keyword = query.arguments.keyword
    if (keyword !== undefined && keyword !== null && typeof keyword !== 'string') {
      return `${prefix}的检索关键词必须是文本`
    }
    const limit = numberOf(query.arguments.limit, DEFAULT_QUERY_PAGE_SIZE)
    if (limit < 1 || limit > MAX_QUERY_PAGE_SIZE) {
      return `${prefix}的检索条数需在 1..${MAX_QUERY_PAGE_SIZE}`
    }
  }

  if (query.tool === 'query_objects') {
    const selects = query.arguments.select
    if (selects !== undefined && selects !== null && (!Array.isArray(selects) || selects.length > 32)) {
      return `${prefix}的输出属性最多 32 项`
    }
    if (Array.isArray(selects)) {
      for (const item of selects) {
        const property = typeof item === 'string' ? item : isRecord(item) ? stringOf(item.property) : ''
        if (!property.trim()) return `${prefix}的输出属性必须包含属性名`
      }
    }
    const groups = query.arguments.group_by
    if (groups !== undefined && groups !== null && (!Array.isArray(groups) || groups.length > 8)) {
      return `${prefix}的分组维度最多 8 项`
    }
    if (Array.isArray(groups)) {
      for (const item of groups) {
        const record = typeof item === 'string' ? { property: item } : isRecord(item) ? item : null
        if (!record || !stringOf(record.property).trim()) {
          return `${prefix}的分组维度必须包含属性`
        }
        const bucket = stringOf(record.bucket)
        if (bucket && !(QUERY_BUCKETS as readonly string[]).includes(bucket)) {
          return `${prefix}的时间分桶非法：${bucket}`
        }
      }
    }
    const aggregates = query.arguments.aggregates
    if (aggregates !== undefined && aggregates !== null && (!Array.isArray(aggregates) || aggregates.length > 8)) {
      return `${prefix}的聚合指标最多 8 项`
    }
    if (Array.isArray(aggregates)) {
      for (const item of aggregates) {
        if (!isRecord(item)) return `${prefix}的聚合指标格式不正确`
        const fn = stringOf(item.fn)
        if (!(QUERY_AGGREGATE_FNS as readonly string[]).includes(fn)) {
          return `${prefix}包含不支持的聚合函数：${fn || '空'}`
        }
        if (fn !== 'count' && !stringOf(item.property).trim()) {
          return `${prefix}的 ${fn} 聚合缺少属性`
        }
      }
    }
    const orders = query.arguments.order_by
    if (orders !== undefined && orders !== null && (!Array.isArray(orders) || orders.length > 8)) {
      return `${prefix}的排序规则最多 8 项`
    }
    if (Array.isArray(orders)) {
      for (const item of orders) {
        if (!isRecord(item) || !stringOf(item.output).trim()) {
          return `${prefix}的排序规则需要输出字段`
        }
        const direction = stringOf(item.direction) || 'asc'
        if (direction !== 'asc' && direction !== 'desc') {
          return `${prefix}的排序方向只能是 asc/desc`
        }
      }
    }
    const pageSize = numberOf(query.arguments.page_size, DEFAULT_QUERY_PAGE_SIZE)
    if (pageSize < 1 || pageSize > MAX_QUERY_PAGE_SIZE) {
      return `${prefix}的每页条数需在 1..${MAX_QUERY_PAGE_SIZE}`
    }
    const joins = query.arguments.joins
    if (joins !== undefined && joins !== null && (!Array.isArray(joins) || joins.length > 6)) {
      return `${prefix}的关联跳数最多 6 跳`
    }
    if (Array.isArray(joins)) {
      for (const item of joins) {
        if (!isRecord(item) || !stringOf(item.object_type).trim() || !stringOf(item.link_type).trim()) {
          return `${prefix}的关联需要对象类型与链接类型`
        }
        const joinType = stringOf(item.type) || 'inner'
        if (joinType !== 'inner' && joinType !== 'left') {
          return `${prefix}的关联方式只能是 inner/left`
        }
      }
    }
    const window = query.arguments.window
    if (window !== undefined && window !== null) {
      if (!isRecord(window) || !stringOf(window.property).trim() || !stringOf(window.timezone).trim()) {
        return `${prefix}的时间窗口需要属性与时区`
      }
      const unknownWindow = Object.keys(window).filter(key => (
        !['property', 'timezone', 'last', 'start', 'end'].includes(key)
      ))
      if (unknownWindow.length) {
        return `${prefix}的时间窗口含未知字段：${unknownWindow.join('、')}`
      }
      const last = isRecord(window.last) ? window.last : null
      if (last) {
        if (!(WINDOW_UNITS as readonly string[]).includes(stringOf(last.unit))) {
          return `${prefix}的相对时间单位非法`
        }
        const count = numberOf(last.count, 0)
        if (count < 1 || count > 1000) return `${prefix}的相对时间数量需在 1..1000`
      } else if (!stringOf(window.start).trim() || !stringOf(window.end).trim()) {
        return `${prefix}的固定时间窗口需要开始与结束时间`
      }
    }
  }

  return null
}

export function validateEditorSections(sections: EditorSection[]): string | null {
  if (!sections.length) return '报告模板至少需要一个分析章节'
  for (const section of sections) {
    if (!section.title.trim() || !section.goal.trim()) {
      return '每个分析章节都需要标题与分析目标'
    }
    if (!section.queryPlan.length) return `章节「${section.title}」至少需要一个数据查询`
    if (section.queryPlan.length > MAX_QUERIES_PER_SECTION) {
      return `章节「${section.title}」最多允许 ${MAX_QUERIES_PER_SECTION} 个查询`
    }
    for (const query of section.queryPlan) {
      const issue = validateQuery(query, section.title)
      if (issue) return issue
    }
  }
  return null
}
