import { expect, test, type Locator, type Page, type Route } from '@playwright/test'

const json = (route: Route, data: unknown) => route.fulfill({
  status: 200,
  contentType: 'application/json',
  body: JSON.stringify({ data }),
})

const jsonError = (route: Route, status: number, body: unknown) => route.fulfill({
  status,
  contentType: 'application/json',
  body: JSON.stringify(body),
})

const reportId = 'operations-report-1'
const article1Id = 'operations-article-1'
const article2Id = 'operations-article-2'
const article3Id = 'operations-article-3'

/** 与后端 query_service/_version_meta 返回结构一致的数据集版本元数据。 */
const datasetMeta = {
  id: 'dataset-orders',
  name: '订单数据集',
  kind: 'curated',
  version_id: 'dataset-version-3',
  version_no: 3,
  as_of: '2026-11-09T08:00:00+00:00',
  rowcount: 240,
  columns_typed: [
    { name: 'region', type: 'string' },
    { name: 'total', type: 'integer' },
  ],
  source_schema: [
    { name: 'region', type: 'string' },
    { name: 'total', type: 'integer' },
  ],
  review_status: 'approved',
}

function queryResult(columns: string[], rows: Record<string, unknown>[], overrides: Record<string, unknown> = {}) {
  return {
    columns,
    rows,
    rowcount: rows.length,
    truncated: false,
    duration_ms: 4,
    dataset: datasetMeta,
    source: 'lake_table',
    ...overrides,
  }
}

/** 可报告数据集目录（与后端 list_report_datasets 返回结构一致）。 */
const datasetCatalog = [
  {
    id: 'dataset-orders',
    name: '订单数据集',
    kind: 'curated',
    updated_at: '2026-11-09T08:00:00+00:00',
    reportable: true,
    status: 'ready',
    reason: null,
    version: { version_no: 3, as_of: '2026-11-09T08:00:00+00:00', rowcount: 240 },
    schema: { columns: [{ name: 'region', type: 'string' }, { name: 'total', type: 'integer' }] },
  },
  {
    id: 'dataset-events',
    name: '订单事件数据集',
    kind: 'structured',
    updated_at: '2026-11-08T08:00:00+00:00',
    reportable: true,
    status: 'ready',
    reason: null,
    version: { version_no: 1, as_of: '2026-11-08T08:00:00+00:00', rowcount: 3 },
    schema: {
      columns: [
        { name: 'region', type: 'string' },
        { name: 'total', type: 'integer' },
        { name: 'created_at', type: 'timestamp' },
      ],
    },
  },
  {
    id: 'dataset-missing-lake',
    name: '湖表缺失演示',
    kind: 'curated',
    updated_at: '2026-11-07T08:00:00+00:00',
    reportable: false,
    status: 'unavailable',
    reason: '成品数据集尚无物理湖表，无法执行报告查询',
    version: { version_no: 2, as_of: '2026-11-07T08:00:00+00:00', rowcount: 100 },
    schema: { columns: [{ name: 'region', type: 'string' }] },
  },
]

/** 计划求值用的原始行（模拟资产湖行粒度，供确定性聚合断言）。 */
const planRawRows: Record<string, Array<Record<string, unknown>>> = {
  'dataset-orders': [
    { region: '华东', total: 12 },
    { region: '华东', total: 8 },
    { region: '华北', total: 8 },
  ],
  'dataset-events': [
    { region: '华东', total: 5, created_at: '2026-11-01' },
    { region: '华北', total: 3, created_at: '2026-11-02' },
    { region: '华北', total: 2, created_at: '2026-11-02' },
  ],
}

const datasetOf = (id: unknown) => datasetCatalog.find(item => item.id === id) ?? datasetCatalog[0]
const datasetColumnsOf = (id: unknown) => datasetOf(id).schema.columns
const datasetNameOf = (id: unknown) => datasetOf(id).name

function datasetVersionMetaOf(id: unknown) {
  const found = datasetOf(id)
  return {
    ...datasetMeta,
    id: found.id,
    name: found.name,
    kind: found.kind,
    version_id: `${found.id}-version-${found.version.version_no}`,
    version_no: found.version.version_no,
    as_of: found.version.as_of,
    rowcount: found.version.rowcount,
  }
}

interface MockPlanColumn { field?: string; agg?: string; alias?: string }
interface MockPlanFilter { name: string; type: string; field?: string }
interface MockPlanQueryBlock {
  type: 'query'
  name: string
  columns: MockPlanColumn[]
  time_bucket?: { field: string; unit: string }
  sort?: Array<{ field: string; dir: string }>
  limit?: number
}
type MockPlanBlock = { type: 'text'; text: string } | MockPlanQueryBlock | { type: 'chart'; chart: string; query: string; x?: string; y?: string[] }
interface MockPlanPayload { version: number; filters: MockPlanFilter[]; blocks: MockPlanBlock[] }

/** AI 设计候选（mock）：字段全部来自 dataset-orders schema，可真实试跑。 */
const aiCandidatePlan: MockPlanPayload = {
  version: 1,
  filters: [],
  blocks: [
    { type: 'text', text: 'AI 候选：按区域统计订单量，并给出总量。' },
    { type: 'query', name: 'by_region', columns: [{ field: 'region', alias: 'region' }, { agg: 'count', alias: 'count' }], limit: 20 },
    { type: 'chart', chart: 'bar', query: 'by_region', x: 'region', y: ['count'] },
    { type: 'query', name: 'total_count', columns: [{ agg: 'count', alias: 'count' }], limit: 1 },
  ],
}

/** AI 失败候选：引用不存在的字段，plan-preview 按 422 拒绝（预览失败演示）。 */
const aiFailingPlan: MockPlanPayload = {
  version: 1,
  filters: [],
  blocks: [
    { type: 'text', text: 'AI 候选（失败演示）：引用了数据集中不存在的字段。' },
    { type: 'query', name: 'bad_field', columns: [{ field: 'nonexistent', alias: 'value' }], limit: 10 },
  ],
}

const quoteIdentifier = (identifier: string) => `"${identifier.replaceAll('"', '""')}"`

function defaultPlanAlias(agg: string, field?: string) {
  if (agg === 'count' && !field) return 'count'
  return `${agg}_${field || 'value'}`
}

/** 与 frontend reportPlanModel.compileQuerySql / backend compile_query_sql 同构。 */
function compileQuerySql(block: MockPlanQueryBlock, filters: MockPlanFilter[]): string {
  const projections: string[] = []
  const groupExprs: string[] = []
  let hasAggregate = false
  for (const column of block.columns ?? []) {
    const field = column.field ?? ''
    const alias = column.alias || (column.agg ? defaultPlanAlias(column.agg, column.field) : field)
    if (!column.agg) {
      projections.push(`${quoteIdentifier(field)} AS ${quoteIdentifier(alias)}`)
      groupExprs.push(quoteIdentifier(field))
      continue
    }
    hasAggregate = true
    const inner = column.agg === 'count'
      ? (field ? quoteIdentifier(field) : '*')
      : column.agg === 'count_distinct'
        ? `DISTINCT ${quoteIdentifier(field)}`
        : quoteIdentifier(field)
    projections.push(`${column.agg.toUpperCase()}(${inner}) AS ${quoteIdentifier(alias)}`)
  }
  if (block.time_bucket) {
    hasAggregate = true
    const expression = `DATE_TRUNC('${block.time_bucket.unit}', ${quoteIdentifier(block.time_bucket.field)})`
    projections.push(`${expression} AS ${quoteIdentifier(`${block.time_bucket.field}__${block.time_bucket.unit}`)}`)
    groupExprs.push(expression)
  }
  const lines = [`SELECT ${projections.join(', ')}`, 'FROM dataset']
  if (filters.length) {
    const conditions = filters.map(filter =>
      `(:${filter.name} IS NULL OR ${quoteIdentifier(filter.field || filter.name)} = :${filter.name})`)
    lines.push(`WHERE ${conditions.join(' AND ')}`)
  }
  if (hasAggregate && groupExprs.length) lines.push(`GROUP BY ${groupExprs.join(', ')}`)
  if (block.sort?.length) {
    lines.push(`ORDER BY ${block.sort.map(key => `${quoteIdentifier(key.field)} ${key.dir === 'asc' ? 'ASC' : 'DESC'}`).join(', ')}`)
  }
  if (block.limit !== undefined) lines.push(`LIMIT ${block.limit}`)
  return lines.join('\n')
}

/** 与 frontend reportPlanToMarkdown / backend plan_markdown 同构：计划 → 兼容阅读器的正文。 */
function planMarkdown(plan: MockPlanPayload): string {
  const chunks: string[] = []
  for (const block of plan?.blocks ?? []) {
    if (block.type === 'text') chunks.push(block.text.trim())
    else if (block.type === 'query') chunks.push(`\`\`\`sql ${block.name}\n${compileQuerySql(block, plan.filters ?? [])}\n\`\`\``)
    else {
      const config = [`query: ${block.query}`, `type: ${block.chart}`]
      if (block.x) config.push(`x: ${block.x}`)
      if (block.y?.length) config.push(`y: ${block.y.join(', ')}`)
      chunks.push(`\`\`\`chart\n${config.join('\n')}\n\`\`\``)
    }
  }
  for (const filter of plan?.filters ?? []) chunks.push(`\`\`\`filter\nname: ${filter.name}\ntype: ${filter.type}\n\`\`\``)
  return chunks.filter(Boolean).join('\n\n') + '\n'
}

function splitTopLevel(text: string): string[] {
  const parts: string[] = []
  let depth = 0
  let quote: '"' | "'" | null = null
  let current = ''
  for (const char of text) {
    if (quote) {
      current += char
      if (char === quote) quote = null
      continue
    }
    if (char === '"' || char === "'") {
      quote = char
      current += char
      continue
    }
    if (char === '(') depth += 1
    if (char === ')') depth -= 1
    if (char === ',' && depth === 0) {
      if (current.trim()) parts.push(current.trim())
      current = ''
      continue
    }
    current += char
  }
  if (current.trim()) parts.push(current.trim())
  return parts
}

function evalExpr(expr: string, rows: Array<Record<string, unknown>>): unknown {
  const trimmed = expr.trim()
  const upper = trimmed.toUpperCase()
  if (upper.startsWith('COUNT(DISTINCT')) {
    const field = trimmed.replace(/^COUNT\(DISTINCT\s*/i, '').replace(/\)\s*$/, '').replace(/"/g, '')
    return new Set(rows.map(row => row[field]).filter(value => value != null && value !== '')).size
  }
  if (upper.startsWith('COUNT(')) {
    const inner = trimmed.replace(/^COUNT\(/i, '').replace(/\)\s*$/, '').trim()
    if (inner === '*') return rows.length
    return rows.filter(row => row[inner.replace(/"/g, '')] != null).length
  }
  const aggregate = trimmed.match(/^(SUM|AVG|MIN|MAX)\((.+)\)$/i)
  if (aggregate) {
    const field = aggregate[2].replace(/"/g, '')
    const values = rows.map(row => row[field]).filter(value => value != null && value !== '')
    if (!values.length) return null
    const kind = aggregate[1].toUpperCase()
    if (kind === 'MIN') return values.reduce((left, right) => (String(left) <= String(right) ? left : right))
    if (kind === 'MAX') return values.reduce((left, right) => (String(left) >= String(right) ? left : right))
    const numbers = values.map(Number)
    return kind === 'SUM' ? numbers.reduce((left, right) => left + right, 0) : numbers.reduce((left, right) => left + right, 0) / numbers.length
  }
  const dateTrunc = trimmed.match(/^DATE_TRUNC\('(\w+)',\s*(.+)\)$/i)
  if (dateTrunc) {
    const value = String(rows[0]?.[dateTrunc[2].replace(/"/g, '')] ?? '')
    if (dateTrunc[1] === 'minute') return value.slice(0, 16)
    if (dateTrunc[1] === 'hour') return value.slice(0, 13)
    return value.slice(0, 10)
  }
  const quoted = trimmed.match(/^"([^"]+)"$/)
  if (quoted) return rows[0]?.[quoted[1]]
  if (/^[A-Za-z0-9_\u4e00-\u9fa5]+$/.test(trimmed)) return rows[0]?.[trimmed]
  return null
}

/** 计划编译 SQL 的确定性求值（只支持计划编译产物的受限语法），供 mock execute/preview。 */
function evalReportSql(sql: string, baseRows: Array<Record<string, unknown>>, params: Record<string, unknown>) {
  const lines = sql.split('\n').map(line => line.trim())
  const selectLine = lines.find(line => line.startsWith('SELECT ')) ?? 'SELECT'
  const whereLine = lines.find(line => line.startsWith('WHERE '))
  const groupLine = lines.find(line => line.startsWith('GROUP BY '))
  const orderLine = lines.find(line => line.startsWith('ORDER BY '))
  const limitLine = lines.find(line => line.startsWith('LIMIT '))

  let rows = baseRows
  if (whereLine) {
    const conditions = [
      ...whereLine.matchAll(/\(:(\w+) IS NULL OR CAST\("([^"]+)" AS [A-Za-z]+\s*\)\s*=\s*:\1\)/g),
      ...whereLine.matchAll(/\(:(\w+) IS NULL OR "([^"]+)" = :\1\)/g),
      ...whereLine.matchAll(/\(:(\w+) IS NULL OR ([A-Za-z0-9_]+) = :\1\)/g),
    ].map(match => ({ name: match[1], field: match[2] }))
    rows = rows.filter(row => conditions.every(condition => {
      const value = params[condition.name]
      if (value == null || value === '') return true
      return String(row[condition.field] ?? '') === String(value)
    }))
  }

  const projections = splitTopLevel(selectLine.replace(/^SELECT /, '')).map(part => {
    const aliased = part.match(/^(.+?) AS "([^"]+)"$/)
    if (aliased) return { expr: aliased[1].trim(), alias: aliased[2] }
    const bareQuoted = part.match(/^"([^"]+)"$/)
    if (bareQuoted) return { expr: part, alias: bareQuoted[1] }
    return { expr: part, alias: part }
  })

  const hasAggregate = projections.some(item => /^(COUNT|SUM|AVG|MIN|MAX)\(/i.test(item.expr))
  let groups: Array<Array<Record<string, unknown>>>
  if (groupLine) {
    const groupExprs = splitTopLevel(groupLine.replace(/^GROUP BY /, ''))
    const byKey = new Map<string, Array<Record<string, unknown>>>()
    for (const row of rows) {
      const key = JSON.stringify(groupExprs.map(expr => evalExpr(expr, [row])))
      const bucket = byKey.get(key) ?? []
      bucket.push(row)
      byKey.set(key, bucket)
    }
    groups = [...byKey.values()]
  } else if (hasAggregate) {
    groups = [rows]
  } else {
    groups = rows.map(row => [row])
  }

  let evaluated = groups.map(group =>
    Object.fromEntries(projections.map(item => [item.alias, evalExpr(item.expr, group)])))
  if (orderLine) {
    const keys = [...orderLine.replace(/^ORDER BY /, '').matchAll(/"?([A-Za-z0-9_]+)"?\s+(ASC|DESC)/g)]
      .map(match => ({ field: match[1], dir: match[2] }))
    evaluated = evaluated.sort((left, right) => {
      for (const key of keys) {
        const a = left[key.field]
        const b = right[key.field]
        const cmp = typeof a === 'number' && typeof b === 'number'
          ? a - b
          : String(a ?? '').localeCompare(String(b ?? ''))
        if (cmp) return key.dir === 'DESC' ? -cmp : cmp
      }
      return 0
    })
  }
  if (limitLine) {
    const limit = Number(limitLine.replace(/^LIMIT /, ''))
    if (Number.isFinite(limit) && limit > 0) evaluated = evaluated.slice(0, limit)
  }
  return { columns: projections.map(item => item.alias), rows: evaluated }
}

const reportSummary = {
  id: reportId,
  name: '订单运营周报',
  description: '订单趋势与区域分布',
  article_count: 3,
  revision: 1,
  created_at: '2026-11-09T08:00:00+00:00',
  updated_at: '2026-11-09T08:00:00+00:00',
}

const article1Summary = {
  id: article1Id,
  report_id: reportId,
  title: '第 47 周订单趋势',
  dataset_id: 'dataset-orders',
  dataset_name: '订单数据集',
  position: 0,
  revision: 1,
  updated_at: '2026-11-09T08:00:00+00:00',
}

const article2Summary = {
  ...article1Summary,
  id: article2Id,
  title: '区域分布明细',
  position: 1,
}

const article3Summary = {
  ...article1Summary,
  id: article3Id,
  title: '失败演示',
  position: 2,
}

const article1Markdown = `# 订单趋势

本周订单按区域分布如下。

\`\`\`sql daily
SELECT region, COUNT(*) AS total FROM dataset GROUP BY region ORDER BY total DESC
\`\`\`

\`\`\`sql totals
SELECT SUM(total) AS total FROM dataset
\`\`\`

\`\`\`chart
type: bar
query: daily
x: region
y: total
title: 区域订单量
\`\`\`
`

const article2Markdown = `# 区域分布

按筛选区域查看订单量。

\`\`\`sql regional
SELECT region, total FROM dataset WHERE (:region IS NULL OR region = :region)
\`\`\`

\`\`\`filter
name: region
label: 区域
type: text
default: 华东
\`\`\`
`

const article3Markdown = `# 失败场景

这条查询的参数未声明 filter，服务端会拒绝执行。

\`\`\`sql failing
SELECT region FROM dataset WHERE region = :missing
\`\`\`
`

/** 可变文章状态：PATCH 保存后 GET 返回保存后的正文，保持读改一致性。 */
function articleFixture(id: string, markdown: string) {
  return {
    ...(id === article1Id ? article1Summary : id === article2Id ? article2Summary : article3Summary),
    markdown,
    created_at: '2026-11-09T08:00:00+00:00',
  }
}

interface ExecuteCall {
  queries?: Record<string, string>
  sql?: string
  params?: Record<string, unknown>
  markdown?: string
  limit?: number
}

interface MockRecords {
  executeCalls: ExecuteCall[]
  patchCalls: Array<Record<string, unknown>>
  reportCreates: Array<Record<string, unknown>>
  articleCreates: Array<Record<string, unknown>>
  planPreviews: Array<Record<string, unknown>>
  planDesigns: Array<Record<string, unknown>>
}

/**
 * 报告域 mock。注意：
 * - execute 同时支持批量 {queries}（阅读/预览）与单条 {sql}（试跑），批量按
 *   查询名与 params 返回可区分的真实数值，供「筛选刷新后数值改变」断言；
 *   存量命名查询（daily/totals/regional）走固定数据，计划编译 SQL 走确定性求值；
 * - 文章创建支持 plan（服务端生成兼容 markdown）与纯 markdown 两种载荷；
 * - plan-preview 只读不落库（校验字段白名单 + 编译求值），plan-design 只返回
 *   候选；两者载荷全部记录供断言；
 * - 未知 operations/reports 端点显式返回 500 错误，绝不静默 []；
 * - 请求全部记录到 records 供载荷断言。
 */
async function mockOperationsReports(page: Page, records: MockRecords) {
  const articles: Record<string, {
    markdown: string
    revision: number
    title: string
    dataset_id: string
    plan: MockPlanPayload | null
    position: number
  }> = {
    [article1Id]: { markdown: article1Markdown, revision: 1, title: article1Summary.title, dataset_id: 'dataset-orders', plan: null, position: 0 },
    [article2Id]: { markdown: article2Markdown, revision: 1, title: article2Summary.title, dataset_id: 'dataset-orders', plan: null, position: 1 },
    [article3Id]: { markdown: article3Markdown, revision: 1, title: article3Summary.title, dataset_id: 'dataset-orders', plan: null, position: 2 },
  }
  const reportList = [{ ...reportSummary }]
  /** 新建报告册详情（articles 为空）：供创建后自动进入「第一篇文章向导」。 */
  const createdReports = new Map<string, Record<string, unknown>>()

  const articleSummaryOf = (id: string) => ({
    id,
    report_id: reportId,
    title: articles[id].title,
    dataset_id: articles[id].dataset_id,
    dataset_name: datasetNameOf(articles[id].dataset_id),
    position: articles[id].position,
    revision: articles[id].revision,
    plan_json: articles[id].plan,
    updated_at: '2026-11-09T08:00:00+00:00',
  })
  const articleDetailOf = (id: string) => ({
    ...articleSummaryOf(id),
    markdown: articles[id].markdown,
    created_at: '2026-11-09T08:00:00+00:00',
  })

  const resultFor = (name: string, params: Record<string, unknown> = {}) => {
    if (name === 'daily') {
      return queryResult(['region', 'total'], [{ region: '华东', total: 12 }, { region: '华北', total: 8 }])
    }
    if (name === 'totals') {
      return queryResult(['total'], [{ total: 20 }], { duration_ms: 3 })
    }
    if (name === 'regional') {
      const region = params.region
      if (region === '华南') return queryResult(['region', 'total'], [{ region: '华南', total: 30 }], { duration_ms: 6 })
      if (typeof region === 'string' && region) return queryResult(['region', 'total'], [{ region, total: 12 }])
      return queryResult(['region', 'total'], [{ region: '华东', total: 12 }, { region: '华北', total: 8 }])
    }
    return queryResult([], [])
  }

  /** 计划编译 SQL → 确定性执行结果（带数据集版本元数据）。 */
  const evalPlanSql = (datasetId: string, sql: string, params: Record<string, unknown> = {}) => {
    const evaluated = evalReportSql(sql, planRawRows[datasetId] ?? planRawRows['dataset-orders'], params)
    return queryResult(evaluated.columns, evaluated.rows, { dataset: datasetVersionMetaOf(datasetId) })
  }

  const planOf = (value: unknown): MockPlanPayload | null =>
    value && typeof value === 'object' && Array.isArray((value as MockPlanPayload).blocks)
      ? value as MockPlanPayload
      : null

  await page.route('**/api/v2/operations/reports**', async route => {
    const request = route.request()
    const url = new URL(request.url())
    const path = url.pathname
    const method = request.method()
    const body = (() => { try { return request.postDataJSON() } catch { return undefined } })()

    if (path === '/api/v2/operations/reports' && method === 'GET') {
      return json(route, { items: reportList, total: reportList.length })
    }
    if (path === '/api/v2/operations/reports' && method === 'POST') {
      records.reportCreates.push(body as Record<string, unknown>)
      const created = {
        id: 'operations-report-created',
        name: String(body?.name ?? ''),
        description: String(body?.description ?? ''),
        article_count: 0,
        revision: 1,
        created_at: '2026-11-10T08:00:00+00:00',
        updated_at: '2026-11-10T08:00:00+00:00',
      }
      reportList.push(created)
      createdReports.set(created.id, created)
      return json(route, created)
    }
    if (path === '/api/v2/operations/reports/datasets' && method === 'GET') {
      return json(route, { items: datasetCatalog, total: datasetCatalog.length })
    }
    // 计划真实预览：只读不落库；字段白名单与「broken 查询名」按 422 拒绝。
    const planPreviewMatch = path.match(new RegExp('/api/v2/operations/reports/([^/]+)/plan-preview$'))
    if (planPreviewMatch && method === 'POST') {
      records.planPreviews.push({
        dataset_id: body?.dataset_id,
        plan: body?.plan,
        params: body?.params ?? {},
        limit: body?.limit,
      })
      const plan = planOf(body?.plan) ?? { version: 1, filters: [], blocks: [] }
      const columns = datasetColumnsOf(body?.dataset_id)
      const errors: string[] = []
      for (const block of plan.blocks) {
        if (block.type !== 'query') continue
        if (String(block.name).includes('broken')) errors.push(`查询名「${block.name}」是本 mock 的失败演示`)
        for (const column of block.columns ?? []) {
          if (column.field && !columns.some(item => item.name === column.field)) {
            errors.push(`查询 ${block.name}：字段 '${column.field}' 不在数据集字段白名单内`)
          }
        }
      }
      if (errors.length) {
        return jsonError(route, 422, { detail: { code: 'invalid_report_plan', message: '计划未通过校验', errors } })
      }
      const datasetId = String(body?.dataset_id ?? 'dataset-orders')
      const params = (body?.params ?? {}) as Record<string, unknown>
      const blocks = plan.blocks.map(block => block.type !== 'query'
        ? block
        : {
          type: 'query',
          name: block.name,
          result: evalPlanSql(datasetId, compileQuerySql(block, plan.filters ?? []), params),
        })
      const versionMeta = datasetVersionMetaOf(datasetId)
      return json(route, {
        plan_version: plan.version,
        plan,
        markdown: planMarkdown(plan),
        dataset: { id: datasetId, name: datasetNameOf(datasetId), kind: datasetOf(datasetId).kind },
        blocks,
        version: { version_id: versionMeta.version_id, version_no: versionMeta.version_no, as_of: versionMeta.as_of },
        source: 'lake_table',
      })
    }
    // AI 设计候选：只返回候选不保存；brief 含「失败」返回引用不存在字段的候选。
    if (path === `/api/v2/operations/reports/${reportId}/plan-design` && method === 'POST') {
      records.planDesigns.push({
        dataset_id: body?.dataset_id,
        brief: body?.brief,
        current_plan: body?.current_plan,
      })
      const failing = String(body?.brief ?? '').includes('失败')
      return json(route, {
        plan: failing ? aiFailingPlan : aiCandidatePlan,
        explanation: failing
          ? 'AI 引用了数据集中不存在的字段，用于预览失败演示。'
          : '按区域统计订单量并给出总量，先试跑核对结果。',
        generation_mode: 'ai',
        dataset: {
          id: String(body?.dataset_id ?? ''),
          name: datasetNameOf(body?.dataset_id),
          kind: datasetOf(body?.dataset_id).kind,
          fields: datasetColumnsOf(body?.dataset_id),
        },
      })
    }
    if (path === `/api/v2/operations/reports/${reportId}` && method === 'GET') {
      return json(route, {
        ...reportSummary,
        articles: Object.keys(articles).map(articleSummaryOf),
      })
    }
    const reportDetailMatch = path.match(new RegExp('/api/v2/operations/reports/([^/]+)$'))
    if (reportDetailMatch && method === 'GET') {
      // 新建报告册：空目录 + createFirstArticle 路由 state → 书页自动打开创建向导
      const createdReport = createdReports.get(reportDetailMatch[1])
      if (createdReport) return json(route, { ...createdReport, articles: [] })
      return jsonError(route, 404, { detail: { code: 'report_not_found', message: '报告册不存在' } })
    }
    if (path === `/api/v2/operations/reports/${reportId}/articles` && method === 'POST') {
      records.articleCreates.push(body as Record<string, unknown>)
      const id = `operations-article-created-${Object.keys(articles).length + 1}`
      const plan = planOf(body?.plan)
      const datasetId = typeof body?.dataset_id === 'string' ? body.dataset_id : 'dataset-orders'
      articles[id] = {
        title: String(body?.title ?? ''),
        dataset_id: datasetId,
        plan,
        markdown: plan ? planMarkdown(plan) : '# 新的运营分析\n\n在这里写下分析背景和结论。\n',
        revision: 1,
        position: Object.keys(articles).length,
      }
      return json(route, articleDetailOf(id))
    }
    const articleMatch = path.match(new RegExp(`/api/v2/operations/reports/${reportId}/articles/([a-z0-9-]+)$`))
    if (articleMatch && method === 'GET') {
      const stored = articles[articleMatch[1]]
      if (!stored) return jsonError(route, 404, { detail: { code: 'article_not_found', message: '文章不存在' } })
      return json(route, articleDetailOf(articleMatch[1]))
    }
    if (articleMatch && method === 'PATCH') {
      const stored = articles[articleMatch[1]]
      if (!stored) return jsonError(route, 404, { detail: { code: 'article_not_found', message: '文章不存在' } })
      records.patchCalls.push(body as Record<string, unknown>)
      // plan 与 markdown 二选一：plan 重生成正文；手写 markdown 与计划脱钩（与后端一致）。
      const nextPlan = planOf(body?.plan)
      if (nextPlan) {
        stored.plan = nextPlan
        stored.markdown = planMarkdown(nextPlan)
      }
      if (typeof body?.markdown === 'string') {
        stored.markdown = body.markdown
        stored.plan = null
      }
      if (typeof body?.title === 'string' && body.title) stored.title = body.title
      stored.revision += 1
      return json(route, articleDetailOf(articleMatch[1]))
    }
    const schemaMatch = path.match(new RegExp(`/api/v2/operations/reports/${reportId}/articles/([a-z0-9-]+)/schema$`))
    if (schemaMatch && method === 'GET') {
      const stored = articles[schemaMatch[1]]
      if (!stored) return jsonError(route, 404, { detail: { code: 'article_not_found', message: '文章不存在' } })
      return json(route, {
        dataset_id: stored.dataset_id,
        dataset_name: datasetNameOf(stored.dataset_id),
        columns: datasetColumnsOf(stored.dataset_id),
      })
    }
    const executeMatch = path.match(new RegExp(`/api/v2/operations/reports/${reportId}/articles/([a-z0-9-]+)/execute$`))
    if (executeMatch && method === 'POST') {
      records.executeCalls.push({
        queries: body?.queries,
        sql: body?.sql,
        params: body?.params,
        markdown: body?.markdown,
        limit: body?.limit,
      })
      if (body?.queries && typeof body.queries === 'object') {
        const names = Object.keys(body.queries)
        if (names.includes('failing')) {
          // pydantic 422 校验错误形态：detail 为 [{loc, msg, type}] 列表
          return jsonError(route, 422, {
            detail: [{ loc: ['body', 'queries', 'failing'], msg: 'SQL 参数必须先在文章 filter 围栏中声明', type: 'value_error' }],
          })
        }
        const articleDataset = articles[executeMatch[1]]?.dataset_id ?? 'dataset-orders'
        const results: Record<string, unknown> = {}
        for (const [name, sql] of Object.entries(body.queries as Record<string, string>)) {
          // 存量命名查询保留可区分的固定数据；计划编译 SQL 走确定性求值。
          results[name] = ['daily', 'totals', 'regional'].includes(name)
            ? resultFor(name, body.params ?? {})
            : evalPlanSql(articleDataset, sql, body.params ?? {})
        }
        return json(route, { results })
      }
      if (typeof body?.sql === 'string') {
        const sql: string = body.sql
        if (sql.includes(':missing')) {
          return jsonError(route, 422, {
            detail: [{ loc: ['body', 'sql'], msg: 'SQL 参数必须先在文章 filter 围栏中声明', type: 'value_error' }],
          })
        }
        const name = sql.includes('SUM(total)') ? 'totals' : sql.includes('GROUP BY') ? 'daily' : 'regional'
        return json(route, resultFor(name, body.params ?? {}))
      }
      return jsonError(route, 422, { detail: [{ loc: ['body'], msg: 'sql 与 queries 至少提供一个', type: 'missing' }] })
    }
    // 未知报告 API：显式失败，禁止静默 []
    return jsonError(route, 500, { detail: { code: 'unmocked_operations_reports_endpoint', message: `unmocked ${method} ${path}` } })
  })
}

/**
 * 看板工作台 mock（真实 DTO 契约）：
 * - 列表返回正常空态 {data:{grafana_available:true,total:0,items:[]}}——不能依赖
 *   mockSession 的兜底 []（它会让列表解包失败、页面落到错误态，遮掩正常预期）；
 * - grafana-session 成功 204 空 body（与后端一致）；
 * - 未知 dashboards 端点显式 500，不静默兜底。
 * 在 mockSession 之后注册、优先命中。
 */
async function mockOperationsDashboards(page: Page) {
  await page.route('**/api/v2/operations/dashboards**', route => {
    const path = new URL(route.request().url()).pathname
    const method = route.request().method()
    if (path === '/api/v2/operations/dashboards' && method === 'GET') {
      return json(route, { grafana_available: true, total: 0, items: [] })
    }
    if (path === '/api/v2/operations/dashboards/grafana-session' && method === 'POST') {
      return route.fulfill({ status: 204, body: '' })
    }
    return jsonError(route, 500, { detail: { code: 'unexpected', message: '未知看板端点' } })
  })
}

async function mockSession(page: Page, user: Record<string, unknown>) {
  await page.addInitScript(sessionUser => {
    localStorage.setItem('token', 'operations-e2e-token')
    localStorage.setItem('auth-store', JSON.stringify({
      state: { token: 'operations-e2e-token', user: sessionUser },
      version: 0,
    }))
  }, user)
  // 壳层请求（收件箱角标等）一律本地兜底；报告 API 在 mockOperationsReports 中覆盖。
  await page.route('**/api/**', route => {
    const path = new URL(route.request().url()).pathname
    if (!path.startsWith('/api/')) return route.continue()
    if (path === '/api/v2/inbox/summary') {
      return json(route, { openAlertCount: 0, actionableCount: 0, unreadCount: 0, resolvedCount: 0 })
    }
    return json(route, [])
  })
}

/** 元素在视口最顶层命中（AGENTS.md §5：可见性断言不检测遮挡，层叠场景须用 elementFromPoint）。 */
async function expectTopmost(locator: Locator) {
  const handle = await locator.elementHandle()
  const topmost = await handle?.evaluate(element => {
    const rect = element.getBoundingClientRect()
    const hit = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2)
    return Boolean(hit && (hit === element || element.contains(hit)))
  })
  expect(topmost).toBe(true)
}

const admin = { id: 'admin', username: 'admin', email: 'admin@example.com', role: 'admin' }

function nonAdmin(role: 'editor' | 'viewer', menuPermissions: string[]) {
  return {
    id: `operations-${role}`,
    username: `operations-${role}`,
    email: `operations-${role}@example.com`,
    role,
    is_active: true,
    created_at: '2026-11-09T08:00:00+00:00',
    menu_permissions: menuPermissions,
  }
}

const emptyRecords = (): MockRecords => ({
  executeCalls: [],
  patchCalls: [],
  reportCreates: [],
  articleCreates: [],
  planPreviews: [],
  planDesigns: [],
})

test('智能运营：报告册卡片、书式阅读、一次批量查询与手动刷新', async ({ page }) => {
  await mockSession(page, admin)
  const records = emptyRecords()
  await mockOperationsReports(page, records)
  await mockOperationsDashboards(page)
  await page.setViewportSize({ width: 1440, height: 900 })

  await page.goto('/#/operations')
  await expect(page).toHaveURL(/\/#\/operations\/dashboard$/)
  // 监控看板已是真实工作台：断言工作台与正常空态（列表契约 mock 返回
  // total 0），而不是请求失败兜底或筛选空态；顶部标题文案块已按 UX 收敛
  // 删除，页面不再有「监控看板」heading（与看板工作台 spec 同口径）。
  await expect(page.getByTestId('operations-dashboard-workbench')).toBeVisible()
  await expect(page.getByRole('heading', { name: '监控看板' })).toHaveCount(0)
  await expect(page.getByTestId('dashboard-list-empty')).toBeVisible()
  await expect(page.getByText('还没有监控看板', { exact: true })).toBeVisible()
  await expect(page.getByTestId('dashboard-create')).toBeVisible()

  const nav = page.getByRole('navigation')
  const dataGroup = nav.getByRole('button', { name: '数据集成', exact: true })
  const operationsGroup = nav.getByRole('button', { name: '智能运营', exact: true })
  const eventsLink = nav.getByRole('link', { name: '事件登记', exact: true })
  await expect(operationsGroup).toHaveAttribute('aria-expanded', 'true')
  const [dataBox, operationsBox, eventsBox] = await Promise.all([
    dataGroup.boundingBox(),
    operationsGroup.boundingBox(),
    eventsLink.boundingBox(),
  ])
  expect(dataBox).not.toBeNull()
  expect(operationsBox).not.toBeNull()
  expect(eventsBox).not.toBeNull()
  expect(dataBox!.y).toBeLessThan(operationsBox!.y)
  expect(operationsBox!.y).toBeLessThan(eventsBox!.y)

  await nav.getByRole('link', { name: '运营报告', exact: true }).click()
  await expect(page).toHaveURL(/#\/operations\/reports$/)
  await expect(page.getByTestId('operations-report-list')).toBeVisible()
  await expect(page.getByText('订单运营周报', { exact: true })).toBeVisible()

  await page.getByText('订单运营周报', { exact: true }).first().click()
  await expect(page).toHaveURL(new RegExp(`/operations/reports/${reportId}`))
  await expect(page.getByTestId('operations-report-book')).toBeVisible()
  await expect(page.getByRole('heading', { name: '第 47 周订单趋势' })).toBeVisible()
  await expect(page.getByTestId('report-result-table').first()).toBeVisible()
  await expect(page.getByTestId('report-chart-block')).toBeVisible()

  // 两条命名 SQL（daily/totals）经 execute 批量请求执行：同版本同事务。
  // （dev 服务器带 StrictMode 双执行 effect，故只断言「每次都是一次批量」
  // 而非逐条 {sql} 并发，这才是本次修复的契约。）
  await expect
    .poll(() => records.executeCalls.length, { message: '阅读态应发出批量 execute' })
    .toBeGreaterThanOrEqual(1)
  const batchCalls = records.executeCalls.filter(call => call.queries)
  expect(batchCalls.length).toBe(records.executeCalls.length)
  expect(records.executeCalls.some(call => call.sql)).toBe(false)
  expect(Object.keys(records.executeCalls[0].queries ?? {})).toEqual(['daily', 'totals'])
  // 结果带数据集版本信息：阅读态展示实际版本号/时间/物理来源
  await expect(page.getByTestId('report-query-version').first()).toContainText('v3')
  await expect(page.getByTestId('report-query-version').first()).toContainText('湖表')

  const callsBeforeRefresh = records.executeCalls.length
  await page.getByTestId('report-refresh').click()
  await expect
    .poll(() => records.executeCalls.length, { message: '手动刷新再发一次批量 execute' })
    .toBe(callsBeforeRefresh + 1)

  await page.reload()
  await expect(page).toHaveURL(new RegExp(`/operations/reports/${reportId}`))
  await expect(page.getByRole('heading', { name: '第 47 周订单趋势' })).toBeVisible()

  // 回归：antd 结果表跟随主题 tokens（ConfigProvider 局部映射 var(--card)/var(--muted)）。
  // 不写死色板：表头/表体解析值必须等于当前主题下同名 token 的计算值（探测元素解析）。
  const readTableStyles = () => page.evaluate(() => {
    const root = document.querySelector('[data-testid="report-result-table"]')
    const header = root?.querySelector('.ant-table-thead th')
    const probe = document.createElement('div')
    probe.style.display = 'none'
    document.body.appendChild(probe)
    const resolveToken = (name: string) => {
      probe.style.background = `var(${name})`
      return getComputedStyle(probe).backgroundColor
    }
    const container = ['.ant-table', '.ant-table-container']
      .map(selector => root?.querySelector(selector))
      .find(element => element && getComputedStyle(element).backgroundColor !== 'rgba(0, 0, 0, 0)')
    const result = {
      headerBg: header ? getComputedStyle(header).backgroundColor : '',
      containerBg: container ? getComputedStyle(container).backgroundColor : '',
      tokenCard: resolveToken('--card'),
      tokenMuted: resolveToken('--muted'),
    }
    probe.remove()
    return result
  })

  await expect(page.getByTestId('report-result-table').first()).toBeVisible()
  const light = await readTableStyles()
  expect(light.headerBg).toBe(light.tokenMuted)
  expect(light.containerBg).toBe(light.tokenCard)

  // 用主题 store 的持久格式切深色并刷新：结果表必须仍跟随 tokens（修复前是白底）
  await page.evaluate(() => localStorage.setItem('theme', JSON.stringify({ state: { theme: 'dark' }, version: 0 })))
  await page.reload()
  await expect(page.locator('html')).toHaveClass(/dark/)
  await expect(page.getByRole('heading', { name: '第 47 周订单趋势' })).toBeVisible()
  await expect(page.getByTestId('report-result-table').first()).toBeVisible()
  const dark = await readTableStyles()
  expect(dark.headerBg).toBe(dark.tokenMuted)
  expect(dark.containerBg).toBe(dark.tokenCard)
  expect(dark.headerBg).not.toBe(light.headerBg)
  expect(dark.containerBg).not.toBe(light.containerBg)

  // 收展行为与其他一级分组一致
  await operationsGroup.click()
  await expect(operationsGroup).toHaveAttribute('aria-expanded', 'false')
  await expect(nav.getByRole('link', { name: '监控看板', exact: true })).toHaveCount(0)
  await operationsGroup.click()
  await expect(operationsGroup).toHaveAttribute('aria-expanded', 'true')
})

test('智能运营：新建报告册提交真实载荷，自动进入第一篇文章向导，取消后列表可见', async ({ page }) => {
  await mockSession(page, admin)
  const records = emptyRecords()
  await mockOperationsReports(page, records)
  await page.setViewportSize({ width: 1440, height: 900 })

  await page.goto('/#/operations/reports')
  await expect(page.getByTestId('operations-report-list')).toBeVisible()

  await page.getByTestId('report-create').click()
  await page.getByLabel('报告册名称').fill('2026 Q4 平台运营报告')
  await page.getByLabel('报告册描述').fill('季度沉淀')
  await page.getByTestId('report-form-submit').click()

  await expect
    .poll(() => records.reportCreates.length, { message: '创建报告册应提交 POST' })
    .toBe(1)
  expect(records.reportCreates[0]).toEqual({ name: '2026 Q4 平台运营报告', description: '季度沉淀' })
  // 创建成功后自动进入新书页，并自动打开「第一篇文章」创建向导
  await expect(page).toHaveURL(/\/#\/operations\/reports\/operations-report-created$/)
  await expect(page.getByRole('heading', { name: '2026 Q4 平台运营报告', exact: true })).toBeVisible()
  await expect(page.getByRole('combobox', { name: '选择数据集' })).toBeVisible()

  // 取消向导：停留在空书页；刷新后不重开（state 已被 replace 清掉）
  await page.getByRole('button', { name: '取消', exact: true }).click()
  await expect(page.getByRole('dialog')).toHaveCount(0)
  await page.reload()
  await expect(page.getByRole('heading', { name: '2026 Q4 平台运营报告', exact: true })).toBeVisible()
  await expect(page.getByRole('dialog')).toHaveCount(0)

  // 返回列表：新报告册卡片出现（真实回读而非假成功）
  await page.getByTestId('operations-report-book').getByRole('link', { name: '运营报告', exact: true }).click()
  await expect(page).toHaveURL(/#\/operations\/reports$/)
  await expect(page.getByText('2026 Q4 平台运营报告', { exact: true })).toBeVisible()
})

test('智能运营：多文章目录深链与切换', async ({ page }) => {
  await mockSession(page, admin)
  const records = emptyRecords()
  await mockOperationsReports(page, records)
  await page.setViewportSize({ width: 1440, height: 900 })

  // 深链直达第二篇文章
  await page.goto(`/#/operations/reports/${reportId}?article=${article2Id}`)
  await expect(page.getByTestId('operations-report-book')).toBeVisible()
  await expect(page.getByRole('heading', { name: '区域分布明细' })).toBeVisible()
  const second = page.getByRole('button', { name: '区域分布明细', exact: true })
  await expect(second).toHaveAttribute('aria-current', 'page')
  // 第二篇的筛选查询按默认值批量执行
  await expect
    .poll(() => records.executeCalls.length, { message: '深链进入第二篇自动批量执行' })
    .toBeGreaterThanOrEqual(1)
  expect(records.executeCalls[0].queries).toHaveProperty('regional')
  expect(records.executeCalls[0].params).toEqual({ region: '华东' })

  // 目录切回第一篇
  await page.getByRole('button', { name: '第 47 周订单趋势', exact: true }).click()
  await expect(page).toHaveURL(new RegExp(`article=${article1Id}$`))
  await expect(page.getByRole('heading', { name: '第 47 周订单趋势' })).toBeVisible()

  // 第三篇（失败演示）也可经目录到达
  await page.getByRole('button', { name: '失败演示', exact: true }).click()
  await expect(page.getByRole('heading', { name: '失败演示' })).toBeVisible()
})

test('智能运营：筛选刷新后表格数值真实变化', async ({ page }) => {
  await mockSession(page, admin)
  const records = emptyRecords()
  await mockOperationsReports(page, records)
  await page.setViewportSize({ width: 1440, height: 900 })

  await page.goto(`/#/operations/reports/${reportId}?article=${article2Id}`)
  await expect(page.getByTestId('report-filter-bar')).toBeVisible()
  // 初始（默认 华东）：真实单元格数值
  await expect(page.getByRole('cell', { name: '华东', exact: true })).toBeVisible()
  await expect(page.getByRole('cell', { name: '12', exact: true })).toBeVisible()

  // 改筛选值 → 点「查询」→ mock 按参数返回不同数据：数值必须真的变
  await page.getByTestId('report-filter-region').fill('华南')
  await page.getByTestId('report-refresh').click()
  await expect(page.getByRole('cell', { name: '华南', exact: true })).toBeVisible()
  await expect(page.getByRole('cell', { name: '30', exact: true })).toBeVisible()
  await expect(page.getByRole('cell', { name: '12', exact: true })).toHaveCount(0)
  const lastCall = records.executeCalls[records.executeCalls.length - 1]
  expect(lastCall.params).toEqual({ region: '华南' })
})

test('智能运营：编辑态试跑携带草稿正文与筛选值，写回并保存', async ({ page }) => {
  await mockSession(page, admin)
  const records = emptyRecords()
  await mockOperationsReports(page, records)
  await page.setViewportSize({ width: 1440, height: 900 })

  await page.goto(`/#/operations/reports/${reportId}?article=${article2Id}`)
  await page.getByTestId('report-edit-article').click()
  await expect(page.getByTestId('report-article-editor')).toBeVisible()

  // 先在正文编辑器写一笔未保存草稿：试跑必须把这份草稿 markdown 上送
  const markdownEditor = page.locator('[data-testid="report-markdown-editor"] .cm-content')
  await markdownEditor.click()
  await page.keyboard.press('ControlOrMeta+End')
  await page.keyboard.type('\n\ne2e draft marker')
  await expect(page.getByText('有未保存修改')).toBeVisible()

  // 预览未保存草稿：阅读器以批量请求执行，且携带草稿 markdown（未保存
  // filter 也可运行期校验）
  await page.getByTestId('report-preview-toggle').click()
  await expect(page.getByTestId('report-article-reader')).toBeVisible()
  await expect
    .poll(
      () => records.executeCalls.some(call =>
        Boolean(call.markdown?.includes('e2e draft marker')) && call.queries !== undefined && 'regional' in call.queries),
      { message: '预览批量执行应携带草稿 markdown' },
    )
    .toBe(true)
  await page.getByTestId('report-preview-toggle').click()
  await expect(markdownEditor).toBeVisible()

  await page.getByTestId('report-trial-toggle').click()
  await expect(page.getByTestId('report-sql-trial')).toBeVisible()
  // 草稿里的 filter default 出现在试跑筛选栏
  await expect(page.getByTestId('report-sql-trial-filters')).toBeVisible()

  await page.getByTestId('report-sql-trial-run').click()
  await expect(page.locator('[data-testid="report-sql-trial"] [data-testid="report-result-table"]')).toBeVisible()
  const trialCall = records.executeCalls[records.executeCalls.length - 1]
  expect(trialCall.sql).toContain('FROM dataset')
  expect(trialCall.markdown).toContain('e2e draft marker')
  expect(trialCall.params).toEqual({ region: '华东' })

  // 编辑 SQL → 写回正文（旧试跑结果随之清空）
  const trialEditor = page.locator('[data-testid="report-sql-trial-editor"] .cm-content')
  await trialEditor.click()
  await page.keyboard.press('ControlOrMeta+End')
  await page.keyboard.type('\n-- trial edit')
  await page.getByRole('button', { name: '写回正文' }).click()
  await expect(page.locator('[data-testid="report-sql-trial"] [data-testid="report-result-table"]')).toHaveCount(0)
  await expect(markdownEditor).toContainText('-- trial edit')

  // 保存草稿：PATCH 携带草稿正文（markdown 与 plan 二选一：手写正文不带 plan）
  await page.getByTestId('report-save-draft').click()
  await expect
    .poll(() => records.patchCalls.length, { message: '保存草稿应提交 PATCH' })
    .toBe(1)
  expect(String(records.patchCalls[0].markdown)).toContain('e2e draft marker')
  expect(String(records.patchCalls[0].markdown)).toContain('-- trial edit')
  expect(records.patchCalls[0].plan).toBeUndefined()
  expect(records.patchCalls[0].expected_revision).toBe(1)
  await expect(page.getByTestId('report-article-editor')).toHaveCount(0)
  await expect(page.getByTestId('report-article-reader')).toBeVisible()
  // 阅读态展示保存后的真实内容（GET 回读一致）
  await expect(page.getByTestId('report-article-reader')).toContainText('e2e draft marker')
})

test('智能运营：保存冲突保留草稿，刷新服务端修订后重试仍使用原修订', async ({ page }) => {
  await mockSession(page, admin)
  const records = emptyRecords()
  await mockOperationsReports(page, records)
  await page.setViewportSize({ width: 1440, height: 900 })

  let latestRevision = 1
  let latestReads = 0
  const saves: Array<Record<string, unknown>> = []
  await page.route(`**/api/v2/operations/reports/${reportId}/articles/${article2Id}`, route => {
    if (route.request().method() === 'PATCH') {
      saves.push(route.request().postDataJSON() as Record<string, unknown>)
      latestRevision = 2
      return jsonError(route, 409, {
        detail: { code: 'article_revision_conflict', message: '文章已被其他编辑更新，请重新加载后再编辑' },
      })
    }
    if (latestRevision === 2) latestReads += 1
    return json(route, { ...articleFixture(article2Id, article2Markdown), revision: latestRevision })
  })
  await page.route(`**/api/v2/operations/reports/${reportId}/articles/reorder`, route =>
    json(route, { items: [article2Summary, article1Summary, article3Summary] }))

  await page.goto(`/#/operations/reports/${reportId}?article=${article2Id}`)
  await page.getByTestId('report-edit-article').click()
  const markdownEditor = page.locator('[data-testid="report-markdown-editor"] .cm-content')
  await markdownEditor.click()
  await page.keyboard.press('ControlOrMeta+End')
  await page.keyboard.type('\n\nlocal draft survives conflict')
  await page.getByTestId('report-save-draft').click()
  await expect.poll(() => saves.length).toBe(1)
  await expect(page.getByText('文章已被其他编辑更新，请重新加载后再编辑')).toBeVisible()
  await expect(markdownEditor).toContainText('local draft survives conflict')

  // 排序会刷新文章查询，模拟编辑期间收到其他终端已保存的 revision=2。
  await page.getByRole('button', { name: '区域分布明细', exact: true }).hover()
  await page.getByLabel('上移文章 区域分布明细', { exact: true }).click()
  await expect.poll(() => latestReads).toBeGreaterThan(0)
  await expect(markdownEditor).toContainText('local draft survives conflict')
  await page.getByTestId('report-save-draft').click()
  await expect.poll(() => saves.length).toBe(2)
  expect(saves.map(save => save.expected_revision)).toEqual([1, 1])
  expect(saves.every(save => String(save.markdown).includes('local draft survives conflict'))).toBe(true)
  await expect(page.getByTestId('report-article-editor')).toBeVisible()
})

test('智能运营：执行失败显式报错，不伪造成功结果', async ({ page }) => {
  await mockSession(page, admin)
  const records = emptyRecords()
  await mockOperationsReports(page, records)
  await page.setViewportSize({ width: 1440, height: 900 })

  await page.goto(`/#/operations/reports/${reportId}?article=${article3Id}`)
  await expect(page.getByRole('heading', { name: '失败演示' })).toBeVisible()
  // pydantic 422 detail 列表被拼成可读文案，就地展示
  const alert = page.getByRole('alert').filter({ hasText: 'body.queries.failing：SQL 参数必须先在文章 filter 围栏中声明' })
  await expect(alert).toBeVisible()
  // 没有任何结果表/图表被伪造出来
  await expect(page.getByTestId('report-result-table')).toHaveCount(0)
  await expect(page.getByTestId('report-chart-block')).toHaveCount(0)
  await expect(page.getByText('查询成功')).toHaveCount(0)
  await expect
    .poll(() => records.executeCalls.length, { message: '失败文章也确实请求过 execute' })
    .toBeGreaterThanOrEqual(1)
})

test('智能运营：viewer 只读（写按钮隐藏）且阅读可用', async ({ page }) => {
  await mockSession(page, nonAdmin('viewer', ['overview', 'operations', 'operations.dashboard', 'operations.reports']))
  const records = emptyRecords()
  await mockOperationsReports(page, records)
  await page.setViewportSize({ width: 1440, height: 900 })

  await page.goto('/#/operations/reports')
  await expect(page.getByTestId('operations-report-list')).toBeVisible()
  await expect(page.getByText('订单运营周报', { exact: true })).toBeVisible()
  // viewer 隐藏全部写入口
  await expect(page.getByTestId('report-create')).toHaveCount(0)
  await expect(page.getByRole('button', { name: '编辑', exact: true })).toHaveCount(0)
  await expect(page.getByLabel('删除报告册 订单运营周报')).toHaveCount(0)

  await page.getByText('订单运营周报', { exact: true }).first().click()
  await expect(page.getByTestId('operations-report-book')).toBeVisible()
  await expect(page.getByTestId('report-create-article')).toHaveCount(0)
  await expect(page.getByTestId('report-edit-article')).toHaveCount(0)
  await expect(page.getByTestId('report-delete-article')).toHaveCount(0)
  await expect(page.getByLabel(/上移文章/)).toHaveCount(0)
  await expect(page.getByLabel(/下移文章/)).toHaveCount(0)
  // 阅读不受影响：批量查询照常返回真实结果
  await expect(page.getByTestId('report-result-table').first()).toBeVisible()
  await expect(page.getByTestId('report-query-version').first()).toContainText('v3')
})

test('智能运营：脏稿退出有确认与几何命中，失败保留草稿', async ({ page }) => {
  await mockSession(page, admin)
  const records = emptyRecords()
  await mockOperationsReports(page, records)
  await page.setViewportSize({ width: 1440, height: 900 })

  await page.goto(`/#/operations/reports/${reportId}?article=${article2Id}`)
  await page.getByTestId('report-edit-article').click()
  const markdownEditor = page.locator('[data-testid="report-markdown-editor"] .cm-content')
  await markdownEditor.click()
  await page.keyboard.press('ControlOrMeta+End')
  await page.keyboard.type('\n\ndirty draft')
  await expect(page.getByText('有未保存修改')).toBeVisible()

  // 浏览器级退出（刷新）触发 beforeunload 保护（evaluate 触发避免 reload 等待
  // 被取消的导航；dismiss 后页面停留原处）
  const dialogTypes: string[] = []
  page.on('dialog', dialog => {
    dialogTypes.push(dialog.type())
    void dialog.dismiss()
  })
  await page.evaluate(() => { window.location.reload() })
  await expect
    .poll(() => dialogTypes.length, { message: '脏稿刷新应触发 beforeunload 对话框' })
    .toBeGreaterThanOrEqual(1)
  expect(dialogTypes).toContain('beforeunload')

  // 应用内「返回阅读」：确认弹窗弹出，且确认按钮在遮罩之上（elementFromPoint 命中）
  await page.getByTestId('report-editor-cancel').click()
  const confirmDialog = page.getByRole('dialog').filter({ hasText: '放弃未保存的修改？' })
  await expect(confirmDialog).toBeVisible()
  const discardButton = confirmDialog.getByRole('button', { name: '放弃修改' })
  await expectTopmost(discardButton)
  await discardButton.click()
  await expect(page.getByTestId('report-article-editor')).toHaveCount(0)
  await expect(page.getByTestId('report-article-reader')).toBeVisible()

  // 返回列表入口同样先确认（脏稿保护），确认后真正离开
  await page.getByTestId('report-edit-article').click()
  await markdownEditor.click()
  await page.keyboard.press('ControlOrMeta+End')
  await page.keyboard.type('\n\ndirty again')
  await page.getByTestId('operations-report-book').getByRole('link', { name: '运营报告', exact: true }).click()
  await expect(page.getByRole('dialog').filter({ hasText: '放弃未保存的修改？' })).toBeVisible()
  await page.getByRole('button', { name: '放弃修改' }).click()
  await expect(page).toHaveURL(/#\/operations\/reports$/)
  expect(records.patchCalls).toHaveLength(0)
})

test('智能运营：已授权的非管理员可见报告册入口', async ({ page }) => {
  await mockSession(page, nonAdmin('editor', ['overview', 'operations', 'operations.dashboard', 'operations.reports']))
  const records = emptyRecords()
  await mockOperationsReports(page, records)
  await page.setViewportSize({ width: 1440, height: 900 })

  await page.goto('/#/operations/reports')
  await expect(page.getByTestId('operations-report-list')).toBeVisible()
  await expect(page.getByText('订单运营周报', { exact: true })).toBeVisible()
  const nav = page.getByRole('navigation')
  await expect(nav.getByRole('button', { name: '智能运营', exact: true })).toBeVisible()
  await expect(nav.getByRole('link', { name: '监控看板', exact: true })).toBeVisible()
})

test('智能运营：未授权的非管理员看不到入口，深链落到无权限页', async ({ page }) => {
  await mockSession(page, nonAdmin('viewer', ['overview', 'events']))
  await page.setViewportSize({ width: 1440, height: 900 })

  await page.goto('/#/operations/dashboard')
  await expect(page.getByRole('heading', { name: '当前页面无法访问' })).toBeVisible()
  await expect(page.getByRole('heading', { name: '监控看板即将上线' })).toHaveCount(0)
  const nav = page.getByRole('navigation')
  await expect(nav.getByRole('link', { name: '事件登记', exact: true })).toBeVisible()
  await expect(nav.getByRole('button', { name: '智能运营', exact: true })).toHaveCount(0)
})

/** 打开「新建文章」弹窗并等待数据资产目录加载完成。 */
async function openArticleDialog(page: Page) {
  await page.getByTestId('report-create-article').click()
  const dialog = page.getByRole('dialog')
  await expect(dialog).toBeVisible()
  await expect(page.getByRole('combobox', { name: '选择数据集' })).toBeVisible()
  return dialog
}

/** 在创建弹窗中选择一个可报告数据资产。 */
async function chooseDataset(page: Page, name: string) {
  await page.getByRole('combobox', { name: '选择数据集' }).click()
  await page.getByRole('option', { name, exact: true }).click()
}

test('智能运营：新建文章四个创作入口与默认资产模式', async ({ page }) => {
  await mockSession(page, admin)
  const records = emptyRecords()
  await mockOperationsReports(page, records)
  await page.setViewportSize({ width: 1440, height: 900 })

  await page.goto(`/#/operations/reports/${reportId}`)
  await expect(page.getByTestId('operations-report-book')).toBeVisible()
  await openArticleDialog(page)

  for (const mode of ['ai', 'asset', 'template', 'markdown']) {
    await expect(page.getByTestId(`report-create-mode-${mode}`)).toBeVisible()
  }
  // 默认资产入口：asset 按下态，选中资产后直接给出计划编辑与手动预览
  await expect(page.getByTestId('report-create-mode-asset')).toHaveAttribute('aria-pressed', 'true')

  // 数据资产 Select 弹层必须真正浮在 Modal 之上（elementFromPoint 几何命中）
  await page.getByRole('combobox', { name: '选择数据集' }).click()
  const ordersOption = page.getByRole('option', { name: '订单数据集', exact: true })
  await expect(ordersOption).toBeVisible()
  await expectTopmost(ordersOption)
  await ordersOption.click()
  await expect(page.getByTestId('report-create-asset-meta')).toContainText('可查询')
  await expect(page.getByTestId('report-plan-editor')).toBeVisible()
  await expect(page.getByTestId('report-plan-preview')).toBeVisible()

  // 模板入口：只有数据概览与趋势分析
  await page.getByTestId('report-create-mode-template').click()
  const templateGroup = page.getByRole('group', { name: '报告模板' })
  await expect(templateGroup).toBeVisible()
  await expect(templateGroup.getByRole('button')).toHaveCount(2)
  await expect(templateGroup.getByRole('button', { name: /数据概览/ })).toBeVisible()
  await expect(templateGroup.getByRole('button', { name: /趋势分析/ })).toBeVisible()

  // AI 入口：AI 助手面板（候选不自动保存）
  await page.getByTestId('report-create-mode-ai').click()
  await expect(page.getByTestId('report-ai-panel')).toBeVisible()

  // 高级 Markdown 入口：显式选择后走纯正文旅程，不再出现计划编辑
  await page.getByTestId('report-create-mode-markdown').click()
  await expect(page.getByTestId('report-ai-panel')).toHaveCount(0)
  await expect(page.getByTestId('report-plan-editor')).toHaveCount(0)
  expect(records.articleCreates).toHaveLength(0)
})

test('智能运营：资产目录展示可用性原因与版本证据，不可用资产不可选', async ({ page }) => {
  await mockSession(page, admin)
  const records = emptyRecords()
  await mockOperationsReports(page, records)
  await page.setViewportSize({ width: 1440, height: 900 })

  await page.goto(`/#/operations/reports/${reportId}`)
  await openArticleDialog(page)

  // 不可用资产的原因直接可见（资产目录显式列出，而不是静默过滤）
  await expect(page.getByText('湖表缺失演示：成品数据集尚无物理湖表，无法执行报告查询')).toBeVisible()

  await page.getByRole('combobox', { name: '选择数据集' }).click()
  const brokenOption = page.getByRole('option', { name: '湖表缺失演示', exact: true })
  await expect(brokenOption).toBeVisible()
  await expect(brokenOption).toBeDisabled()

  // 可用资产带真实版本/行数/字段证据（reportable/status/version/schema 列）
  await page.getByRole('option', { name: '订单数据集', exact: true }).click()
  const meta = page.getByTestId('report-create-asset-meta')
  await expect(meta).toContainText('成品数据集')
  await expect(meta).toContainText('可查询')
  await expect(meta).toContainText('v3')
  await expect(meta).toContainText('240 行')
  await expect(meta).toContainText('region (string)')
  await expect(meta).toContainText('total (integer)')
})

test('智能运营：目录缺字段的历史资产按需读取 schema 后可字段搭建', async ({ page }) => {
  await mockSession(page, admin)
  const records = emptyRecords()
  await mockOperationsReports(page, records)
  // 本场覆盖目录端点：dataset-orders 目录 schema 为空（历史 CSV 资产常见），
  // 字段经新的按需端点 GET /operations/reports/datasets/{id}/schema 读取。
  const schemaCalls: string[] = []
  await page.route('**/api/v2/operations/reports/datasets', route =>
    json(route, {
      items: datasetCatalog.map(item => (item.id === 'dataset-orders' ? { ...item, schema: { columns: [] } } : item)),
      total: datasetCatalog.length,
    }))
  await page.route('**/api/v2/operations/reports/datasets/dataset-orders/schema', route => {
    schemaCalls.push(new URL(route.request().url()).pathname)
    return json(route, {
      dataset_id: 'dataset-orders',
      dataset_name: '订单数据集',
      columns: [{ name: 'region', type: 'string' }, { name: 'total', type: 'integer' }],
    })
  })
  await page.setViewportSize({ width: 1440, height: 900 })

  await page.goto(`/#/operations/reports/${reportId}`)
  await openArticleDialog(page)
  await page.getByLabel('文章标题').fill('历史 CSV 字段回填')
  await chooseDataset(page, '订单数据集')

  // schema 按需读取成功（ready 门控通过）后：资产 meta 显示真实字段
  const meta = page.getByTestId('report-create-asset-meta')
  await expect(meta).toContainText('可查询')
  await expect(meta).toContainText('region (string)')
  await expect(meta).toContainText('total (integer)')
  expect(schemaCalls.length).toBeGreaterThanOrEqual(1)

  // 追加查询块：默认带上第一个字段（region），而不是只会 COUNT
  await page.getByTestId('report-plan-editor').getByRole('button', { name: /^查询（/ }).click()
  const addedQuery = page.locator('[data-testid^="report-plan-query-query_"]')
  await expect(addedQuery).toHaveCount(1)
  await expect(addedQuery.getByRole('combobox').first()).toContainText('region')

  // 试跑成功后可创建；载荷中新查询带 region 字段（字段级搭建证据）
  await page.getByTestId('report-plan-preview-run').click()
  await expect
    .poll(() => records.planPreviews.length, { message: '目录缺 schema 的资产也应能真实试跑' })
    .toBeGreaterThanOrEqual(1)
  await expect(page.getByTestId('report-create-article-submit')).toBeEnabled()
  await page.getByTestId('report-create-article-submit').click()
  await expect.poll(() => records.articleCreates.length).toBe(1)
  const added = ((records.articleCreates[0].plan as {
    blocks: Array<{ type?: string; name?: string; columns?: Array<{ field?: string }> }>
  }).blocks).find(block => block.type === 'query' && String(block.name).startsWith('query_'))
  expect(added?.columns?.some(column => column.field === 'region')).toBe(true)
})

test('智能运营：资产模式创建计划文章，编辑保存计划载荷并回读', async ({ page }) => {
  await mockSession(page, admin)
  const records = emptyRecords()
  await mockOperationsReports(page, records)
  await page.setViewportSize({ width: 1440, height: 900 })

  await page.goto(`/#/operations/reports/${reportId}`)
  await openArticleDialog(page)
  await page.getByLabel('文章标题').fill('区域结构化周报')

  const submit = page.getByTestId('report-create-article-submit')
  await expect(submit).toBeDisabled()
  await chooseDataset(page, '订单数据集')
  // 选中可用资产但计划未试跑成功：仍不能创建
  await expect(submit).toBeDisabled()

  // 手动试跑：真实预览成功后创建按钮才可用
  await page.getByTestId('report-plan-preview-run').click()
  await expect
    .poll(() => records.planPreviews.length, { message: '创建前应先真实试跑计划' })
    .toBeGreaterThanOrEqual(1)
  expect(records.planPreviews[0].dataset_id).toBe('dataset-orders')
  expect(String(JSON.stringify(records.planPreviews[0].plan))).toContain('overview_total')
  const dialogPreview = page.getByRole('dialog').getByTestId('report-plan-preview')
  await expect(dialogPreview.getByTestId('report-result-table').first()).toBeVisible()
  await expect(dialogPreview.getByTestId('report-query-version').first()).toContainText('v3')
  await expect(submit).toBeEnabled()

  // 回归：重复点击当前 active 的资产入口是 no-op——已试跑门禁不被重置
  await page.getByTestId('report-create-mode-asset').click()
  await expect(submit).toBeEnabled()

  // 回归：编辑计划后再点当前 active 模式，草稿保留（编辑值不被重置回模板）
  const dialogText = page.getByRole('dialog').getByLabel('说明文字（第 1 块）内容')
  await dialogText.fill('口径说明：按区域统计')
  await page.getByTestId('report-create-mode-asset').click()
  await expect(dialogText).toHaveValue('口径说明：按区域统计')
  // 编辑改变了计划指纹：须再试跑一次，通过后才可创建
  await page.getByTestId('report-plan-preview-run').click()
  await expect
    .poll(
      () => records.planPreviews.filter(call => String(JSON.stringify(call.plan)).includes('口径说明：按区域统计')).length,
      { message: '编辑后的计划应重新真实试跑' },
    )
    .toBeGreaterThanOrEqual(1)
  await expect(submit).toBeEnabled()

  await submit.click()
  await expect
    .poll(() => records.articleCreates.length, { message: '创建计划文章应提交 POST' })
    .toBe(1)
  const createBody = records.articleCreates[0]
  expect(Object.keys(createBody)).toEqual(['title', 'dataset_id', 'plan'])
  expect(createBody.title).toBe('区域结构化周报')
  expect(createBody.dataset_id).toBe('dataset-orders')
  expect(String(JSON.stringify(createBody.plan))).toContain('overview_total')

  // 计划创建后仍是阅读态：可阅读真实结果，可继续编辑
  await expect(page.getByTestId('report-article-editor')).toHaveCount(0)
  await expect(page.getByRole('heading', { name: '区域结构化周报', exact: true })).toBeVisible()
  await expect(page.getByTestId('report-edit-article')).toBeVisible()
  await expect(page.getByTestId('report-result-table').first()).toBeVisible()

  // 计划文章默认可视化编辑器（不是 Markdown 编辑器）
  await page.getByTestId('report-edit-article').click()
  await expect(page.getByTestId('report-plan-editor')).toBeVisible()
  await expect(page.locator('[data-testid="report-markdown-editor"]')).toHaveCount(0)
  const querySections = page.locator('[data-testid^="report-plan-query-"]')
  await expect(querySections).toHaveCount(2)

  // 修改计划：添加查询块；600ms 防抖后自动真实预览，成功后才能保存
  await page.getByTestId('report-plan-editor').getByRole('button', { name: /^查询（/ }).click()
  await expect(querySections).toHaveCount(3)
  await expect(page.getByText('有未保存修改')).toBeVisible()
  await expect(page.getByTestId('report-save-draft')).toBeDisabled()
  await expect
    .poll(
      () => records.planPreviews.some(call => {
        const blocks = ((call.plan as { blocks?: Array<{ type?: string }> })?.blocks) ?? []
        return blocks.filter(block => block.type === 'query').length === 3
      }),
      { message: '编辑态修改后应自动真实预览' },
    )
    .toBe(true)
  await expect(page.getByTestId('report-save-draft')).toBeEnabled()

  // 保存计划：PATCH 载荷 plan XOR markdown（带 plan 不带 markdown）+ 修订
  await page.getByTestId('report-save-draft').click()
  await expect
    .poll(() => records.patchCalls.length, { message: '保存计划应提交 PATCH' })
    .toBe(1)
  expect(records.patchCalls[0].plan).toBeTruthy()
  expect(records.patchCalls[0].markdown).toBeUndefined()
  expect(records.patchCalls[0].expected_revision).toBe(1)
  const savedBlocks = ((records.patchCalls[0].plan as { blocks: Array<{ type?: string }> }).blocks)
  expect(savedBlocks.filter(block => block.type === 'query')).toHaveLength(3)
  await expect(page.getByTestId('report-article-editor')).toHaveCount(0)

  // 回读：重新进入编辑器，保存过的查询块仍在（GET 回读一致）
  await page.getByTestId('report-edit-article').click()
  await expect(page.getByTestId('report-plan-editor')).toBeVisible()
  await expect(querySections).toHaveCount(3)

  // 计划脏稿退出：确认后放弃，未再发出任何保存请求
  await page.getByTestId('report-plan-editor').getByRole('button', { name: /^查询（/ }).click()
  await expect(page.getByText('有未保存修改')).toBeVisible()
  await page.getByTestId('report-editor-cancel').click()
  const confirmDialog = page.getByRole('dialog').filter({ hasText: '放弃未保存的修改？' })
  await expect(confirmDialog).toBeVisible()
  await confirmDialog.getByRole('button', { name: '放弃修改' }).click()
  await expect(page.getByTestId('report-article-editor')).toHaveCount(0)
  expect(records.patchCalls).toHaveLength(1)
})

test('智能运营：模板入口只有概览与趋势，无时间字段时趋势禁用', async ({ page }) => {
  await mockSession(page, admin)
  const records = emptyRecords()
  await mockOperationsReports(page, records)
  await page.setViewportSize({ width: 1440, height: 900 })

  await page.goto(`/#/operations/reports/${reportId}`)
  await openArticleDialog(page)
  await page.getByLabel('文章标题').fill('概览模板文章')
  await chooseDataset(page, '订单数据集')
  await page.getByTestId('report-create-mode-template').click()

  // 模板只有两个；region/total 无时间字段 → 趋势禁用并说明原因
  const templateGroup = page.getByRole('group', { name: '报告模板' })
  await expect(templateGroup.getByRole('button', { name: /数据概览/ })).toBeEnabled()
  const trendButton = templateGroup.getByRole('button', { name: /趋势分析/ })
  await expect(trendButton).toBeDisabled()
  await expect(trendButton).toContainText('数据集没有日期/时间类型的字段')

  await page.getByTestId('report-plan-preview-run').click()
  const submit = page.getByTestId('report-create-article-submit')
  await expect(submit).toBeEnabled()
  await submit.click()
  await expect.poll(() => records.articleCreates.length).toBe(1)
  expect(String(JSON.stringify(records.articleCreates[0].plan))).toContain('overview_total')

  // 有 created_at 时间字段的数据集：趋势可选，计划携带时间桶
  await openArticleDialog(page)
  await page.getByLabel('文章标题').fill('趋势模板文章')
  await chooseDataset(page, '订单事件数据集')
  await page.getByTestId('report-create-mode-template').click()
  const trendEnabled = page.getByRole('group', { name: '报告模板' }).getByRole('button', { name: /趋势分析/ })
  await expect(trendEnabled).toBeEnabled()
  await trendEnabled.click()

  // 回归：重复点击当前选中模板是 no-op——编辑值与趋势计划保留（不重置回模板初始值）
  const trendText = page.getByRole('dialog').getByLabel('说明文字（第 1 块）内容')
  await trendText.fill('趋势口径：按天下钻')
  await page.getByRole('group', { name: '报告模板' }).getByRole('button', { name: /趋势分析/ }).click()
  await expect(page.getByTestId('report-plan-query-trend')).toBeVisible()
  await expect(trendText).toHaveValue('趋势口径：按天下钻')

  await page.getByTestId('report-plan-preview-run').click()
  const preview = page.getByRole('dialog').getByTestId('report-plan-preview')
  await expect(preview.getByRole('cell', { name: '2026-11-01', exact: true })).toBeVisible()
  await expect(preview.getByRole('cell', { name: '2026-11-02', exact: true })).toBeVisible()
  await expect(page.getByTestId('report-create-article-submit')).toBeEnabled()
  await page.getByTestId('report-create-article-submit').click()
  await expect.poll(() => records.articleCreates.length).toBe(2)
  const trendPlan = records.articleCreates[1].plan as { blocks: Array<Record<string, unknown>> }
  const trendQuery = trendPlan.blocks.find(block => block.type === 'query' && block.name === 'trend')
  expect(trendQuery?.time_bucket).toEqual({ field: 'created_at', unit: 'day' })
})

test('智能运营：高级 Markdown 入口显式选择后创建纯正文文章', async ({ page }) => {
  await mockSession(page, admin)
  const records = emptyRecords()
  await mockOperationsReports(page, records)
  await page.setViewportSize({ width: 1440, height: 900 })

  await page.goto(`/#/operations/reports/${reportId}`)
  await openArticleDialog(page)
  // 旧旅程改为显式点击 Markdown 入口：此后没有计划编辑，也没有计划保存
  await page.getByTestId('report-create-mode-markdown').click()
  await page.getByLabel('文章标题').fill('手写周报')
  await chooseDataset(page, '订单数据集')
  await expect(page.getByTestId('report-plan-editor')).toHaveCount(0)

  const submit = page.getByTestId('report-create-article-submit')
  // Markdown 入口不需要计划试跑
  await expect(submit).toBeEnabled()
  await submit.click()
  await expect.poll(() => records.articleCreates.length).toBe(1)
  expect(records.articleCreates[0]).toEqual({ title: '手写周报', dataset_id: 'dataset-orders' })

  // 旧文仍是 Markdown：编辑态是 Markdown 编辑器，没有可视化切换
  await page.getByTestId('report-edit-article').click()
  await expect(page.locator('[data-testid="report-markdown-editor"]')).toBeVisible()
  await expect(page.getByTestId('report-plan-editor')).toHaveCount(0)
  await expect(page.getByTestId('report-mode-plan')).toHaveCount(0)
  await expect(page.getByTestId('report-mode-markdown')).toHaveAttribute('aria-pressed', 'true')
})

test('智能运营：AI 候选试跑成功接受后方可提交计划', async ({ page }) => {
  await mockSession(page, admin)
  const records = emptyRecords()
  await mockOperationsReports(page, records)
  await page.setViewportSize({ width: 1440, height: 900 })

  await page.goto(`/#/operations/reports/${reportId}`)
  await openArticleDialog(page)
  await page.getByTestId('report-create-mode-ai').click()
  await page.getByLabel('文章标题').fill('AI 结构化周报')
  await chooseDataset(page, '订单数据集')

  await page.getByLabel('AI 报告目标').fill('按区域统计订单量与总量')
  await page.getByTestId('report-ai-generate').click()
  await expect.poll(() => records.planDesigns.length).toBe(1)
  expect(records.planDesigns[0].dataset_id).toBe('dataset-orders')
  expect(records.planDesigns[0].brief).toBe('按区域统计订单量与总量')

  const candidate = page.getByTestId('report-ai-candidate')
  await expect(candidate).toBeVisible()
  await expect(page.getByText('按区域统计订单量并给出总量，先试跑核对结果。')).toBeVisible()

  // 候选未试跑：接受与创建都被阻止；候选本身不产生任何持久化请求
  await expect(page.getByTestId('report-ai-accept')).toBeDisabled()
  await expect(page.getByTestId('report-create-article-submit')).toBeDisabled()

  // 候选预览：共享 Reader 按「查询」真实试跑
  await candidate.getByTestId('report-plan-preview-run').click()
  await expect
    .poll(() => records.planPreviews.length, { message: '候选预览应请求 plan-preview' })
    .toBeGreaterThanOrEqual(1)
  expect(records.planPreviews[0].dataset_id).toBe('dataset-orders')
  await expect(candidate.getByTestId('report-result-table').first()).toBeVisible()
  await expect(page.getByTestId('report-ai-accept')).toBeEnabled()

  // 接受候选：回到资产表单，编辑器显示候选内容，创建被允许
  await page.getByTestId('report-ai-accept').click()
  await expect(page.getByTestId('report-ai-candidate')).toHaveCount(0)
  await expect(page.getByTestId('report-plan-editor')).toBeVisible()
  await expect(page.getByTestId('report-plan-query-by_region')).toBeVisible()
  await expect(page.getByTestId('report-create-article-submit')).toBeEnabled()

  // 回归：重复点击当前 active 的 AI 入口是 no-op——已接受候选与已试跑门禁保留
  await page.getByTestId('report-create-mode-ai').click()
  await expect(page.getByTestId('report-plan-query-by_region')).toBeVisible()
  await expect(page.getByTestId('report-create-article-submit')).toBeEnabled()

  await page.getByTestId('report-create-article-submit').click()
  await expect.poll(() => records.articleCreates.length).toBe(1)
  const createBody = records.articleCreates[0]
  expect(Object.keys(createBody)).toEqual(['title', 'dataset_id', 'plan'])
  const queryNames = ((createBody.plan as { blocks: Array<{ type: string; name?: string }> }).blocks)
    .filter(block => block.type === 'query')
    .map(block => block.name)
  expect(queryNames).toEqual(['by_region', 'total_count'])
  // 计划创建后仍阅读态
  await expect(page.getByTestId('report-article-editor')).toHaveCount(0)
  await expect(page.getByTestId('report-edit-article')).toBeVisible()
})

test('智能运营：AI 候选生成与丢弃全程不保存文章', async ({ page }) => {
  await mockSession(page, admin)
  const records = emptyRecords()
  await mockOperationsReports(page, records)
  await page.setViewportSize({ width: 1440, height: 900 })

  await page.goto(`/#/operations/reports/${reportId}`)
  await openArticleDialog(page)
  await page.getByTestId('report-create-mode-ai').click()
  await page.getByLabel('文章标题').fill('AI 丢弃演示')
  await chooseDataset(page, '订单数据集')
  await page.getByLabel('AI 报告目标').fill('按区域统计订单量与总量')
  await page.getByTestId('report-ai-generate').click()
  const candidate = page.getByTestId('report-ai-candidate')
  await expect(candidate).toBeVisible()
  await candidate.getByTestId('report-plan-preview-run').click()
  await expect(page.getByTestId('report-ai-accept')).toBeEnabled()

  // 丢弃候选：没有任何创建/保存请求
  await page.getByTestId('report-ai-discard').click()
  await expect(page.getByTestId('report-ai-candidate')).toHaveCount(0)
  await page.getByRole('button', { name: '取消', exact: true }).click()
  await expect(page.getByRole('dialog')).toHaveCount(0)
  expect(records.articleCreates).toHaveLength(0)
  expect(records.patchCalls).toHaveLength(0)
  await expect(page.getByRole('button', { name: 'AI 丢弃演示' })).toHaveCount(0)
})

test('智能运营：AI 候选预览失败显式报错且不伪造成功', async ({ page }) => {
  await mockSession(page, admin)
  const records = emptyRecords()
  await mockOperationsReports(page, records)
  await page.setViewportSize({ width: 1440, height: 900 })

  await page.goto(`/#/operations/reports/${reportId}`)
  await openArticleDialog(page)
  await page.getByTestId('report-create-mode-ai').click()
  await page.getByLabel('文章标题').fill('AI 失败演示')
  await chooseDataset(page, '订单数据集')
  // brief 含「失败」：mock 返回引用不存在字段的候选，预览按 422 拒绝
  await page.getByLabel('AI 报告目标').fill('生成一个失败演示计划')
  await page.getByTestId('report-ai-generate').click()
  const candidate = page.getByTestId('report-ai-candidate')
  await expect(candidate).toBeVisible()

  await candidate.getByTestId('report-plan-preview-run').click()
  await expect.poll(() => records.planPreviews.length).toBeGreaterThanOrEqual(1)
  // 422 校验错误就地可见；没有任何结果表被伪造出来
  await expect(page.getByText('查询执行失败：计划未通过校验').first()).toBeVisible()
  await expect(page.getByTestId('report-plan-preview').getByTestId('report-result-table')).toHaveCount(0)
  // 接受与创建仍然被阻止
  await expect(page.getByTestId('report-ai-accept')).toBeDisabled()
  await expect(page.getByTestId('report-create-article-submit')).toBeDisabled()
  expect(records.articleCreates).toHaveLength(0)
  expect(records.patchCalls).toHaveLength(0)
})

test('智能运营：创建前计划试跑失败阻止创建', async ({ page }) => {
  await mockSession(page, admin)
  const records = emptyRecords()
  await mockOperationsReports(page, records)
  await page.setViewportSize({ width: 1440, height: 900 })

  await page.goto(`/#/operations/reports/${reportId}`)
  await openArticleDialog(page)
  await page.getByLabel('文章标题').fill('失败演示计划')
  await chooseDataset(page, '订单数据集')
  // 资产默认计划上追加一个会失败的查询（mock 对 broken 查询名返回 422）
  await page.getByTestId('report-plan-editor').getByRole('button', { name: /^查询（/ }).click()
  const addedQuery = page.locator('[data-testid^="report-plan-query-query_"]')
  await expect(addedQuery).toHaveCount(1)
  await addedQuery.getByLabel('查询名称').fill('broken')

  await page.getByTestId('report-plan-preview-run').click()
  await expect.poll(() => records.planPreviews.length).toBeGreaterThanOrEqual(1)
  await expect(page.getByText('查询执行失败：计划未通过校验').first()).toBeVisible()
  await expect(page.getByTestId('report-plan-preview').getByTestId('report-result-table')).toHaveCount(0)
  await expect(page.getByTestId('report-create-article-submit')).toBeDisabled()
  expect(records.articleCreates).toHaveLength(0)
  await page.getByRole('button', { name: '取消', exact: true }).click()
})

test('智能运营：编辑态 AI 候选在草稿变化后失效需重新生成', async ({ page }) => {
  await mockSession(page, admin)
  const records = emptyRecords()
  await mockOperationsReports(page, records)
  await page.setViewportSize({ width: 1440, height: 900 })

  // 先用资产入口创建一篇计划文章（试跑成功后创建）
  await page.goto(`/#/operations/reports/${reportId}`)
  await openArticleDialog(page)
  await page.getByLabel('文章标题').fill('AI 编辑演示')
  await chooseDataset(page, '订单数据集')
  await page.getByTestId('report-plan-preview-run').click()
  await expect(page.getByTestId('report-create-article-submit')).toBeEnabled()
  await page.getByTestId('report-create-article-submit').click()
  await expect.poll(() => records.articleCreates.length).toBe(1)

  // 编辑态打开 AI 助手：生成候选并试跑成功
  await page.getByTestId('report-edit-article').click()
  await expect(page.getByTestId('report-plan-editor')).toBeVisible()
  await page.getByTestId('report-ai-toggle').click()
  await page.getByLabel('AI 报告目标').fill('按区域统计订单量与总量')
  await page.getByTestId('report-ai-generate').click()
  const candidate = page.getByTestId('report-ai-candidate')
  await expect(candidate).toBeVisible()
  await candidate.getByTestId('report-plan-preview-run').click()
  await expect(page.getByTestId('report-ai-accept')).toBeEnabled()

  // 候选生成后修改草稿：候选标记过期，接受被阻止。
  // 主计划编辑器是 DOM 中最后一个 report-plan-editor（候选的只读副本在 AI 面板内）。
  await page.getByTestId('report-plan-editor').last().getByRole('button', { name: '说明文字', exact: true }).click()
  await expect(page.getByText('当前草稿在候选生成后有变化，请重新生成候选。')).toBeVisible()
  await expect(page.getByTestId('report-ai-accept')).toBeDisabled()

  // 丢弃候选：除最初的创建外没有任何持久化请求
  await page.getByTestId('report-ai-discard').click()
  expect(records.articleCreates).toHaveLength(1)
  expect(records.patchCalls).toHaveLength(0)
})
