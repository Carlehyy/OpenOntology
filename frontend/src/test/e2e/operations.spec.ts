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

/**
 * 报告域 mock。注意：
 * - execute 同时支持批量 {queries}（阅读/预览）与单条 {sql}（试跑），批量按
 *   查询名与 params 返回可区分的真实数值，供「筛选刷新后数值改变」断言；
 * - 未知 operations/reports 端点显式返回 500 错误，绝不静默 []；
 * - 请求全部记录到 executeCalls/patchCalls 供载荷断言。
 */
async function mockOperationsReports(page: Page, records: {
  executeCalls: ExecuteCall[]
  patchCalls: Array<Record<string, unknown>>
  reportCreates: Array<Record<string, unknown>>
}) {
  const articles: Record<string, { markdown: string; revision: number; title: string }> = {
    [article1Id]: { markdown: article1Markdown, revision: 1, title: article1Summary.title },
    [article2Id]: { markdown: article2Markdown, revision: 1, title: article2Summary.title },
    [article3Id]: { markdown: article3Markdown, revision: 1, title: article3Summary.title },
  }
  const reportList = [{ ...reportSummary }]

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

  const schemaFixture = {
    dataset_id: 'dataset-orders',
    dataset_name: '订单数据集',
    columns: [
      { name: 'region', type: 'string' },
      { name: 'total', type: 'integer' },
    ],
  }

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
      return json(route, created)
    }
    if (path === `/api/v2/operations/reports/${reportId}` && method === 'GET') {
      return json(route, {
        ...reportSummary,
        articles: [
          { ...article1Summary, title: articles[article1Id].title },
          { ...article2Summary, title: articles[article2Id].title },
          { ...article3Summary, title: articles[article3Id].title },
        ],
      })
    }
    const articleMatch = path.match(new RegExp(`/api/v2/operations/reports/${reportId}/articles/([a-z0-9-]+)$`))
    if (articleMatch && method === 'GET') {
      const stored = articles[articleMatch[1]]
      if (!stored) return jsonError(route, 404, { detail: { code: 'article_not_found', message: '文章不存在' } })
      return json(route, { ...articleFixture(articleMatch[1], stored.markdown), title: stored.title, revision: stored.revision })
    }
    if (articleMatch && method === 'PATCH') {
      const stored = articles[articleMatch[1]]
      if (!stored) return jsonError(route, 404, { detail: { code: 'article_not_found', message: '文章不存在' } })
      records.patchCalls.push(body as Record<string, unknown>)
      if (typeof body?.markdown === 'string') stored.markdown = body.markdown
      if (typeof body?.title === 'string' && body.title) stored.title = body.title
      stored.revision += 1
      return json(route, { ...articleFixture(articleMatch[1], stored.markdown), title: stored.title, revision: stored.revision })
    }
    const schemaMatch = path.match(new RegExp(`/api/v2/operations/reports/${reportId}/articles/([a-z0-9-]+)/schema$`))
    if (schemaMatch && method === 'GET') {
      if (!articles[schemaMatch[1]]) return jsonError(route, 404, { detail: { code: 'article_not_found', message: '文章不存在' } })
      return json(route, schemaFixture)
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
        const results: Record<string, unknown> = {}
        for (const name of names) results[name] = resultFor(name, body.params ?? {})
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
    if (path === '/api/v2/operations/reports/datasets' && method === 'GET') {
      return json(route, {
        items: [{ id: 'dataset-orders', name: '订单数据集', kind: 'curated', updated_at: '2026-11-09T08:00:00+00:00' }],
        total: 1,
      })
    }
    // 未知报告 API：显式失败，禁止静默 []
    return jsonError(route, 500, { detail: { code: 'unmocked_operations_reports_endpoint', message: `unmocked ${method} ${path}` } })
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

const emptyRecords = () => ({
  executeCalls: [] as ExecuteCall[],
  patchCalls: [] as Array<Record<string, unknown>>,
  reportCreates: [] as Array<Record<string, unknown>>,
})

test('智能运营：报告册卡片、书式阅读、一次批量查询与手动刷新', async ({ page }) => {
  await mockSession(page, admin)
  const records = emptyRecords()
  await mockOperationsReports(page, records)
  await page.setViewportSize({ width: 1440, height: 900 })

  await page.goto('/#/operations')
  await expect(page).toHaveURL(/\/#\/operations\/dashboard$/)
  await expect(page.getByRole('heading', { name: '监控看板即将上线' })).toBeVisible()

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

  // 收展行为与其他一级分组一致
  await operationsGroup.click()
  await expect(operationsGroup).toHaveAttribute('aria-expanded', 'false')
  await expect(nav.getByRole('link', { name: '监控看板', exact: true })).toHaveCount(0)
  await operationsGroup.click()
  await expect(operationsGroup).toHaveAttribute('aria-expanded', 'true')
})

test('智能运营：新建报告册提交真实载荷并出现在列表', async ({ page }) => {
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
  // 创建成功后列表刷新出现新卡片（真实数据而非假成功）
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

  // 保存草稿：PATCH 携带草稿正文，成功后回到阅读态展示保存后的正文
  await page.getByTestId('report-save-draft').click()
  await expect
    .poll(() => records.patchCalls.length, { message: '保存草稿应提交 PATCH' })
    .toBe(1)
  expect(String(records.patchCalls[0].markdown)).toContain('e2e draft marker')
  expect(String(records.patchCalls[0].markdown)).toContain('-- trial edit')
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
