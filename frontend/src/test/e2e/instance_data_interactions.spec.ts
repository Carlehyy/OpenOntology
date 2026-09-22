import { expect, test, type Page, type Route } from '@playwright/test'

// 实例数据页交互契约:date 无幽灵时间、汇总条与来源列、实例详情抽屉、
// 关系端点跳转、搜索空态清除、跳页、未发布引导。全部本地 mock,不触网。
async function mockInstanceInteractions(page: Page) {
  await page.addInitScript(() => {
    localStorage.setItem('token', 'e2e-token')
    localStorage.setItem('auth-store', JSON.stringify({
      state: { token: 'e2e-token', user: { id: 'u1', username: 'tester', role: 'admin' } },
      version: 0,
    }))
  })

  type MockRow = {
    id: string
    objectTypeId: string
    properties: Record<string, unknown>
    computed: Record<string, unknown>
    source: string
    externalId: string
    createdAt: string
    updatedAt: string
  }
  const orderRows: MockRow[] = [
    {
      id: 'row-order-1',
      objectTypeId: 'ot-order',
      properties: { order_id: 'O-1001', promised_date: '2026-07-20', amount: 120000, note: '加急' },
      computed: {},
      source: 'pipeline',
      externalId: 'ext-o-1001',
      createdAt: '2026-07-28T06:39:11Z',
      updatedAt: '2026-07-30T14:33:20Z',
    },
    {
      id: 'row-order-2',
      objectTypeId: 'ot-order',
      properties: { order_id: 'O-1002', promised_date: '2026-07-25', amount: 99000, note: '正常' },
      computed: {},
      source: 'action',
      externalId: 'ext-o-1002',
      createdAt: '2026-07-28T06:39:12Z',
      updatedAt: '2026-07-29T09:00:00Z',
    },
  ]
  const supplierRows: MockRow[] = [
    {
      id: 'row-sup-1',
      objectTypeId: 'ot-supplier',
      properties: { supplier_id: 'S-001', supplier_name: '华东供应商' },
      computed: {},
      source: 'pipeline',
      externalId: 'ext-s-001',
      createdAt: '2026-07-28T06:39:13Z',
      updatedAt: '2026-07-28T06:39:13Z',
    },
  ]

  await page.route('**/api/**', async (route: Route) => {
    const url = new URL(route.request().url())
    if (!url.pathname.startsWith('/api/')) return route.continue()
    const ok = (data: unknown) => route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ data, message: 'ok' }),
    })

    if (url.pathname === '/api/v1/ontologies/ontology-trade') {
      return ok({
        id: 'ontology-trade',
        name: '贸易本体',
        domain: '供应链',
        description: '订单与供应商',
        version: 'v1',
        current_release_id: 'release-v1',
        current_release_version: 'v1',
        status: 'published',
        entity_count: 2,
        relation_count: 1,
        action_count: 0,
        sentinel_count: 0,
        created_by: 'tester',
        created_at: '2026-07-23T00:00:00Z',
        updated_at: '2026-07-23T00:00:00Z',
      })
    }
    if (url.pathname === '/api/v1/ontologies/ontology-draft') {
      return ok({
        id: 'ontology-draft',
        name: '未发布本体',
        domain: '测试',
        description: '还没有发布版本',
        version: 'v0',
        current_release_id: null,
        current_release_version: null,
        status: 'draft',
        entity_count: 0,
        relation_count: 0,
        action_count: 0,
        sentinel_count: 0,
        created_by: 'tester',
        created_at: '2026-07-23T00:00:00Z',
        updated_at: '2026-07-23T00:00:00Z',
      })
    }
    if (url.pathname.endsWith('/instance-browser/catalog')) {
      if (url.pathname.includes('/ontologies/ontology-draft/')) {
        return route.fulfill({
          status: 409,
          contentType: 'application/json',
          body: JSON.stringify({
            detail: { code: 'current_release_missing', message: '本体还没有当前发布版本' },
          }),
        })
      }
      return ok({
        release: { id: 'release-v1', version: 'v1', publishedAt: '2026-07-23T00:00:00Z' },
        objectTypes: [
          {
            id: 'ot-order',
            name: 'trade_order',
            displayName: '订单',
            primaryKey: 'prop-order-id',
            properties: [
              { id: 'prop-order-id', name: 'order_id', displayName: '订单编号', type: 'string', required: true },
              { id: 'prop-promised', name: 'promised_date', displayName: '承诺日期', type: 'date', required: true },
              { id: 'prop-amount', name: 'amount', displayName: '订单金额', type: 'number' },
              { id: 'prop-note', name: 'note', displayName: '备注', type: 'string' },
            ],
            instanceCount: 2,
            associatedDatasets: [],
          },
          {
            id: 'ot-supplier',
            name: 'supplier',
            displayName: '供应商',
            primaryKey: 'prop-supplier-id',
            properties: [
              { id: 'prop-supplier-id', name: 'supplier_id', displayName: '供应商编号', type: 'string', required: true },
              { id: 'prop-supplier-name', name: 'supplier_name', displayName: '供应商名称', type: 'string' },
            ],
            instanceCount: 1,
            associatedDatasets: [],
          },
        ],
        linkTypes: [{
          id: 'lt-fulfilled',
          name: 'fulfilled_by',
          displayName: '由供应商履约',
          sourceObjectTypeId: 'ot-order',
          targetObjectTypeId: 'ot-supplier',
          properties: [],
          instanceCount: 1,
          associatedDatasets: [],
        }],
        legacyProjection: {
          objectInstances: 0,
          linkInstances: 0,
          total: 0,
          canAdopt: false,
          recommendedAction: 'none',
          blockingReasons: [],
        },
      })
    }
    if (url.pathname.endsWith('/instance-browser/objects')) {
      const objectTypeId = url.searchParams.get('object_type_id')
      const keyword = (url.searchParams.get('keyword') || '').trim()
      const pageNumber = Number(url.searchParams.get('page') || '1')
      let items = objectTypeId === 'ot-supplier' ? supplierRows : orderRows
      if (keyword) {
        items = items.filter(item => (
          item.id.includes(keyword)
          || (item.externalId || '').includes(keyword)
          || (item.source || '').includes(keyword)
          || JSON.stringify(item.properties).includes(keyword)
        ))
      }
      return ok({
        release: { id: 'release-v1', version: 'v1' },
        objectTypeId,
        items,
        total: keyword ? items.length : 45,
        page: pageNumber,
        pageSize: 20,
      })
    }
    if (url.pathname.endsWith('/instance-browser/links')) {
      return ok({
        release: { id: 'release-v1', version: 'v1' },
        linkTypeId: 'lt-fulfilled',
        items: [{
          id: 'link-1',
          linkTypeId: 'lt-fulfilled',
          sourceObjectId: 'row-order-1',
          targetObjectId: 'row-sup-1',
          sourceObject: { id: 'row-order-1', objectTypeId: 'ot-order', label: 'O-1001', externalId: 'ext-o-1001' },
          targetObject: { id: 'row-sup-1', objectTypeId: 'ot-supplier', label: 'S-001', externalId: 'ext-s-001' },
          properties: {},
          createdAt: '2026-07-28T06:40:00Z',
        }],
        total: 1,
        page: 1,
        pageSize: 20,
      })
    }
    if (url.pathname.endsWith('/instances/row-order-1/facts')) {
      return ok([
        {
          id: 'fact-0',
          instanceId: 'row-order-1',
          propertyName: 'exists',
          value: true,
          present: true,
          kind: 'object',
          source: 'ontology-release://7fac5392-3660-45c0-b0e9-d3c020cfe7fa',
          recordedAt: '2026-07-30T14:33:25Z',
        },
        {
          id: 'fact-1',
          instanceId: 'row-order-1',
          propertyName: 'note',
          value: '加急',
          present: true,
          kind: 'property',
          source: 'pipeline',
          recordedAt: '2026-07-30T14:33:20Z',
        },
        {
          id: 'fact-2',
          instanceId: 'row-order-1',
          propertyName: 'risk_score',
          value: 92,
          present: true,
          kind: 'derived',
          source: 'fn:risk_score',
          recordedAt: '2026-07-30T14:33:21Z',
        },
      ])
    }
    if (url.pathname.endsWith('/overview')) {
      return ok({
        release: { id: 'release-v1', version: 'v1' },
        model: {},
        data: { instances: 3, instancesBySource: { pipeline: 2, action: 1 }, linkInstances: 1 },
        runtime: {},
        facts: {},
        health: [],
      })
    }
    return ok([])
  })
}

test('汇总条呈现总数与来源构成,date 列无幽灵时间,对象表带来源列', async ({ page }) => {
  await mockInstanceInteractions(page)
  await page.goto('/#/ontologies/ontology-trade?tab=data', { waitUntil: 'domcontentloaded' })

  const summaryBar = page.getByTestId('instance-summary-bar')
  await expect(summaryBar).toBeVisible()
  await expect(summaryBar).toContainText('对象实例 3')
  await expect(summaryBar).toContainText('关系实例 1')
  await expect(summaryBar).toContainText('管道灌入 2')
  await expect(summaryBar).toContainText('动作执行 1')
  await expect(summaryBar).toContainText('当前发布 v1')

  // date 列只渲染日期,不再捏造 08:00:00 幽灵时刻
  await expect(page.getByText('2026/07/20', { exact: true })).toBeVisible()
  await expect(page.getByText('2026/07/20 08:00:00')).toHaveCount(0)

  const tbody = page.locator('tbody')
  await expect(tbody.getByText('管道灌入', { exact: true })).toBeVisible()
  await expect(tbody.getByText('动作执行', { exact: true })).toBeVisible()
})

test('点击对象行打开实例详情抽屉并加载事实历史', async ({ page }) => {
  await mockInstanceInteractions(page)
  await page.goto('/#/ontologies/ontology-trade?tab=data', { waitUntil: 'domcontentloaded' })

  await page.locator('tbody tr', { hasText: 'O-1001' }).click()

  const drawer = page.getByTestId('instance-detail-drawer')
  await expect(drawer).toBeVisible()
  await expect(drawer.getByText('ext-o-1001')).toBeVisible()
  await expect(drawer.getByText('管道灌入', { exact: true }).first()).toBeVisible()

  const facts = drawer.getByTestId('instance-facts-list')
  await expect(facts.getByText('加急')).toBeVisible()
  await expect(facts.getByText('派生', { exact: true })).toBeVisible()
  await expect(facts.getByText('risk_score', { exact: true })).toBeVisible()
  // 事实属性行用列的中文展示名;目录外字段(如 risk_score)保留原名
  await expect(facts.getByText('备注', { exact: true })).toBeVisible()
  await expect(facts.getByText('来源:管道灌入')).toBeVisible()
  // 存在性事实说人话,发布回放来源不裸露协议 URI
  await expect(facts.getByText('实例创建', { exact: true })).toBeVisible()
  await expect(facts.getByText('来源:发布快照')).toBeVisible()

  await page.keyboard.press('Escape')
  await expect(page.getByTestId('instance-detail-drawer')).toHaveCount(0)
})

test('关系端点可点击,跳转到对应对象类型并按端点标签定位实例', async ({ page }) => {
  await mockInstanceInteractions(page)
  await page.goto('/#/ontologies/ontology-trade?tab=data', { waitUntil: 'domcontentloaded' })

  await page.locator('[data-catalog-kind="link"] button', { hasText: '订单 → 供应商' }).click()
  await expect(page.getByRole('columnheader', { name: '创建时间' })).toBeVisible()

  await page.getByRole('button', { name: 'S-001' }).click()

  await expect(page.getByRole('heading', { name: '供应商' })).toBeVisible()
  await expect(page.getByPlaceholder('搜索外部 ID 或属性值')).toHaveValue('S-001')
  await expect(page.locator('tbody').getByText('S-001')).toBeVisible()
})

test('搜索无结果时空态提供一键清除', async ({ page }) => {
  await mockInstanceInteractions(page)
  await page.goto('/#/ontologies/ontology-trade?tab=data', { waitUntil: 'domcontentloaded' })

  await page.getByPlaceholder('搜索外部 ID 或属性值').fill('不存在的关键词')
  await page.getByRole('button', { name: '查询' }).click()

  await expect(page.getByText('没有匹配的实例数据')).toBeVisible()
  await page.getByRole('button', { name: '清除查询条件' }).click()
  await expect(page.locator('tbody').getByText('O-1001')).toBeVisible()
})

test('搜索清除收进输入框内，查询按钮位置不再漂移', async ({ page }) => {
  await mockInstanceInteractions(page)
  await page.goto('/#/ontologies/ontology-trade?tab=data', { waitUntil: 'domcontentloaded' })

  const input = page.getByPlaceholder('搜索外部 ID 或属性值')
  // exact 必须开:默认子串匹配会误中「清除查询条件」「全部清除」
  const clear = page.getByRole('button', { name: '清除搜索', exact: true })

  await expect(clear).toHaveCount(0)
  await input.fill('O-10')
  await expect(clear).toBeVisible()

  await page.getByRole('button', { name: '查询' }).click()
  await expect(page.locator('tbody').getByText('O-1001')).toBeVisible()

  await clear.click()
  await expect(input).toHaveValue('')
  await expect(clear).toHaveCount(0)
  await expect(page.locator('tbody').getByText('O-1002')).toBeVisible()
})

test('未发布本体展示旅程引导而非报错死胡同', async ({ page }) => {
  await mockInstanceInteractions(page)
  await page.goto('/#/ontologies/ontology-draft?tab=data', { waitUntil: 'domcontentloaded' })

  await expect(page.getByText('当前本体还没有发布版本')).toBeVisible()
  await expect(page.getByText('实例目录加载失败')).toHaveCount(0)

  await page.getByRole('button', { name: '查看版本演进' }).click()
  await expect(page.getByText('本体版本演进')).toBeVisible()
})

test('分页支持跳转到指定页', async ({ page }) => {
  await mockInstanceInteractions(page)
  await page.goto('/#/ontologies/ontology-trade?tab=data', { waitUntil: 'domcontentloaded' })

  const jump = page.getByTestId('page-jump')
  await expect(jump).toHaveValue('1')
  await jump.fill('3')
  await jump.press('Enter')

  await expect(jump).toHaveValue('3')
  await expect(page.getByText('/ 3', { exact: true })).toBeVisible()
})
