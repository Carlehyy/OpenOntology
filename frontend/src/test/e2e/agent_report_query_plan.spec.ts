import { expect, test, type Page, type Route } from '@playwright/test'

const now = '2026-11-03T08:00:00+00:00'

const aggregateArguments = {
  object_type: 'Order',
  metric: 'sum',
  metric_property: 'amount',
  group_by: 'month',
  filters: [{ property: 'status', op: 'eq', value: '已完成' }],
}

const queryObjectsArguments = {
  object_type: 'Order',
  joins: [{ object_type: 'Supplier', link_type: 'supplied_by', type: 'left' }],
  filters: [
    { property: 'amount', op: 'gte', value: 100, object_type: 'Order' },
    { property: 'supplier_name', op: 'is_not_null' },
  ],
  select: ['order_no', { property: 'amount', object_type: 'Order' }],
  group_by: [{ property: 'created_at', bucket: 'day' }, 'status'],
  aggregates: [{ fn: 'sum', property: 'amount', as: 'total' }, { fn: 'count' }],
  order_by: [{ output: 'total', direction: 'desc' }],
  page_size: 20,
  window: { property: 'created_at', timezone: 'Asia/Shanghai', last: { unit: 'day', count: 7 } },
}

function baseTemplate() {
  return {
    id: 'report-1',
    ontologyId: 'ontology-1',
    createdBy: 'admin',
    name: '供应链周报',
    description: '按周观察订单规模与异常。',
    sourcePrompt: '',
    generationMode: 'manual',
    status: 'draft' as 'draft' | 'published',
    revision: 1,
    sections: [
      {
        id: 'overview',
        title: '订单规模',
        goal: '统计订单金额与月度结构。',
        visualization: 'kpi',
        queryPlan: [{ tool: 'aggregate_objects', arguments: aggregateArguments }],
      },
      {
        id: 'trend',
        title: '订单趋势与异常',
        goal: '按天观察订单金额趋势并核对异常。',
        visualization: 'line',
        queryPlan: [{ tool: 'query_objects', arguments: queryObjectsArguments }],
      },
    ],
    style: {},
    defaultModelId: null,
    lastPreviewRunId: null as string | null,
    lastPreviewRevision: null as number | null,
    publishedAt: null as string | null,
    createdAt: now,
    updatedAt: now,
  }
}

async function mockReportStudio(page: Page) {
  let template = baseTemplate()
  let run: Record<string, unknown> | null = null
  let lastSave: Record<string, any> | null = null

  await page.addInitScript(() => {
    localStorage.setItem('token', 'e2e-token')
    localStorage.setItem('auth-store', JSON.stringify({
      state: {
        token: 'e2e-token',
        user: { id: 'admin', username: 'admin', email: 'admin@example.com', role: 'admin' },
      },
      version: 0,
    }))
  })

  const json = (route: Route, data: unknown) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ data }),
  })

  const qualityReport = () => ({
    passed: true,
    score: 95,
    threshold: 80,
    summary: '已达到汇报级发布标准',
    blockers: [],
    warnings: [],
    checks: [{ key: 'structure', label: '报告结构', passed: true, detail: '共 2 个分析章节' }],
    templateRevision: template.revision,
  })

  await page.route('**/api/v1/**', route => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/v1/ontologies') return json(route, {
      items: [{
        id: 'ontology-1',
        name: '供应链本体',
        domain: '供应链',
        description: '报告 E2E',
        status: 'draft',
        version: 'v1',
        current_release_id: 'release-1',
        current_release_version: 'v1',
        created_at: now,
        updated_at: now,
      }],
      total: 1,
      page: 1,
      page_size: 20,
    })
    if (path === '/api/v1/domains') return json(route, [])
    if (path === '/api/v1/models') return json(route, [])
    return route.fallback()
  })

  await page.route('**/api/v2/**', route => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/v2/inbox/summary') return json(route, { unread_count: 0 })
    if (path === '/api/v2/formal/ontologies/ontology-1/full') return json(route, {
      id: 'ontology-1',
      name: '供应链本体',
      version: 'v1',
      objectTypes: [{
        id: 'ot-order',
        name: 'Order',
        displayName: '订单',
        primaryKey: 'order_no',
        properties: [
          { id: 'p1', name: 'order_no', displayName: '订单号', type: 'string' },
          { id: 'p2', name: 'status', displayName: '状态', type: 'string' },
          { id: 'p3', name: 'amount', displayName: '金额', type: 'number' },
          { id: 'p4', name: 'created_at', displayName: '创建时间', type: 'datetime' },
        ],
      }, {
        id: 'ot-supplier',
        name: 'Supplier',
        displayName: '供应商',
        primaryKey: 'supplier_no',
        properties: [{ id: 'p5', name: 'supplier_name', displayName: '供应商名称', type: 'string' }],
      }],
      linkTypes: [{
        id: 'lt-supplied-by',
        name: 'supplied_by',
        displayName: '由供应商提供',
        sourceObjectTypeId: 'ot-order',
        targetObjectTypeId: 'ot-supplier',
      }],
      actions: [],
      functions: [],
      instances: [],
      linkInstances: [],
      executionLogs: [],
    })
    if (path === '/api/v2/formal/ontologies/ontology-1/agent/capabilities') return json(route, {
      enabled: true,
      objectTypes: [
        { id: 'ot-order', name: 'Order', displayName: '订单', instanceCount: 12 },
        { id: 'ot-supplier', name: 'Supplier', displayName: '供应商', instanceCount: 3 },
      ],
      linkTypes: [{ id: 'lt-supplied-by', name: 'supplied_by', displayName: '由供应商提供' }],
      actions: [],
      allowActionProposals: true,
      maxRowsPerQuery: 50,
      maxSteps: 8,
      skillCard: '',
      releaseId: 'release-1',
      releaseVersion: 'v1',
    })
    if (path === '/api/v2/formal/ontologies/ontology-1/agent/report-templates/report-1') {
      if (route.request().method() === 'PUT') {
        lastSave = route.request().postDataJSON() as Record<string, any>
        template = {
          ...template,
          ...lastSave,
          revision: template.revision + 1,
          lastPreviewRunId: null,
          lastPreviewRevision: null,
          updatedAt: now,
        }
      }
      return json(route, template)
    }
    if (path === '/api/v2/formal/ontologies/ontology-1/agent/report-templates/report-1/runs') {
      return json(route, run ? [run] : [])
    }
    if (path === '/api/v2/formal/ontologies/ontology-1/agent/report-templates/report-1/preview') {
      run = {
        id: 'run-1',
        templateId: 'report-1',
        ontologyId: 'ontology-1',
        createdBy: 'admin',
        triggerType: 'preview',
        status: 'succeeded',
        templateRevision: template.revision,
        templateSnapshot: {},
        sectionResults: [],
        qualityReport: qualityReport(),
        htmlContent: '<html><body><h1>报告预览</h1><p>真实数据快照</p></body></html>',
        errorMessage: null,
        startedAt: now,
        completedAt: now,
      }
      template = {
        ...template,
        lastPreviewRunId: 'run-1',
        lastPreviewRevision: template.revision,
      }
      return json(route, run)
    }
    if (path === '/api/v2/formal/ontologies/ontology-1/agent/report-templates/report-1/publish') {
      template = { ...template, status: 'published', publishedAt: now }
      return json(route, template)
    }
    if (path === '/api/v2/formal/ontologies/ontology-1/agent/report-runs/run-1') {
      return json(route, run)
    }
    return route.fallback()
  })

  return { save: () => lastSave }
}

test('报告编辑器保留 query_objects 统一查询参数，并完成试运行与发布', async ({ page }) => {
  const mock = await mockReportStudio(page)
  await page.setViewportSize({ width: 1440, height: 950 })
  await page.goto('/#/agent/reports/report-1?ontologyId=ontology-1')

  await expect(page.getByTestId('report-query-plan-editor').first()).toBeVisible()
  const query0 = page.getByTestId('report-query-0')
  await expect(query0.getByRole('combobox', { name: '查询 1 工具' })).toContainText('聚合统计')
  await expect(query0.getByRole('combobox', { name: '聚合指标', exact: true })).toContainText('sum')

  const chapter2 = page.locator('section').filter({ hasText: 'CHAPTER 02' })
  await chapter2.getByText('数据查询配置').click()
  const query1 = chapter2.getByTestId('report-query-0')
  await expect(query1.getByRole('combobox', { name: '查询 1 工具' })).toContainText('统一查询')
  await expect(query1.getByLabel('输出属性')).toHaveValue('order_no, Order.amount')
  await expect(query1.getByLabel('分组 1 属性')).toHaveValue('created_at')
  await expect(query1.getByRole('combobox', { name: '分组 1 时间桶' })).toContainText('day')
  await expect(query1.getByLabel('时间窗口属性')).toHaveValue('created_at')
  await expect(query1.getByLabel('时间窗口时区')).toHaveValue('Asia/Shanghai')
  await expect(query1.getByRole('combobox', { name: '相对时间单位' })).toContainText('day')
  await expect(query1.getByLabel('相对时间数量')).toHaveValue('7')
  // 旧自由 JSON 输入已收口为结构化表单
  await expect(page.getByLabel(/数据查询配置/)).toHaveCount(0)

  await query1.getByRole('spinbutton', { name: '每页条数' }).fill('10')

  const saveRequest = page.waitForRequest(request => (
    request.method() === 'PUT'
    && request.url().includes('/agent/report-templates/report-1')
  ))
  await page.getByRole('button', { name: '真实数据试运行' }).click()
  const request = await saveRequest
  const body = request.postDataJSON() as Record<string, any>

  expect(body.sections[0].queryPlan[0].tool).toBe('aggregate_objects')
  expect(body.sections[0].queryPlan[0].arguments).toEqual(aggregateArguments)
  expect(body.sections[1].queryPlan[0].tool).toBe('query_objects')
  expect(body.sections[1].queryPlan[0].arguments).toEqual({
    ...queryObjectsArguments,
    page_size: 10,
  })
  expect(mock.save()).not.toBeNull()

  await expect(page.getByTestId('analysis-report-preview')).toBeVisible()
  await expect(page.getByRole('button', { name: '发布正式模板' })).toBeEnabled()
  await page.getByRole('button', { name: '发布正式模板' }).click()
  await expect(page.getByText('已发布 · R2')).toBeVisible()
})
