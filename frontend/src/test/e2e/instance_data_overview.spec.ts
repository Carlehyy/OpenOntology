import { expect, test, type Locator, type Page, type Route } from '@playwright/test'

// 实例数据页「数据概览 + 类型画像」交互契约：KPI 与三张图渲染、
// 图表点击联动（类型直达 / 来源精确过滤 / 字段值精确过滤）、筛选 chips 生命周期。
// 全部本地 mock，不触网。

const ONTOLOGY_ID = 'ontology-overview-viz'

/** 点击横向条形图视觉顺序第 barIndex 根条。
 *  注意三个坑：echarts 重渲染会把 hex 色转为 rgb()；hover/点击后 zrender 会
 *  调整 path 的 DOM 次序（nth 不稳定）；动画期间节点瞬时 detach。
 *  因此每次点击都重新读取全部条形的瞬时包围盒并按 (y, x) 空间排序后选目标。 */
async function clickHorizontalBar(page: Page, chart: Locator, barIndex: number) {
  // 卡片内最后一个 svg 才是 echarts 画布（首个可能是标题旁的说明图标）。
  const svg = chart.locator('svg').last()
  await expect(svg).toBeVisible()
  await svg.evaluate(el => el.scrollIntoView({ block: 'center' }))
  // 等图表动画与页面平滑滚动落地：画布包围盒连续两帧不变才算稳定。
  let anchor = await svg.boundingBox()
  for (let attempt = 0; attempt < 20; attempt += 1) {
    await page.waitForTimeout(120)
    const next = await svg.boundingBox()
    const settled = anchor && next
      && Math.abs(next.x - anchor.x) < 1 && Math.abs(next.y - anchor.y) < 1
    anchor = next
    if (settled) break
  }
  const bars = svg.locator('path:not([fill="none"])')
  const count = await bars.count()
  const boxes: Array<{ x: number; y: number; width: number; height: number }> = []
  for (let index = 0; index < count; index += 1) {
    const box = await bars.nth(index).boundingBox()
    if (box) boxes.push(box)
  }
  boxes.sort((a, b) => (a.y - b.y) || (a.x - b.x))
  const target = boxes[barIndex]
  if (!target) throw new Error(`bar #${barIndex} not found (${boxes.length} bars)`)
  await page.mouse.click(
    Math.round(target.x + target.width / 2),
    Math.round(target.y + target.height / 2),
  )
}

async function mockInstanceOverview(page: Page) {
  const objectRequests: URL[] = []

  await page.addInitScript(() => {
    localStorage.setItem('token', 'e2e-token')
    localStorage.setItem('auth-store', JSON.stringify({
      state: { token: 'e2e-token', user: { id: 'u1', username: 'tester', role: 'admin' } },
      version: 0,
    }))
  })

  await page.route('**/api/**', async (route: Route) => {
    const url = new URL(route.request().url())
    if (!url.pathname.startsWith('/api/')) return route.continue()
    const ok = (data: unknown) => route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ data }),
    })

    if (url.pathname === `/api/v1/ontologies/${ONTOLOGY_ID}`) {
      return ok({
        id: ONTOLOGY_ID,
        name: '可视化测试本体',
        domain: '供应链',
        description: '实例数据概览契约',
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
    if (url.pathname.endsWith('/instance-browser/catalog')) {
      return ok({
        release: { id: 'release-v1', version: 'v1', publishedAt: '2026-07-23T00:00:00Z' },
        objectTypes: [
          {
            id: 'ot-order',
            name: 'trade_order',
            displayName: '订单',
            color: '#6366f1',
            primaryKey: 'prop-order-id',
            properties: [
              { id: 'prop-order-id', name: 'order_id', displayName: '订单编号', type: 'string', required: true },
              { id: 'prop-status', name: 'status', displayName: '状态', type: 'string' },
              { id: 'prop-amount', name: 'amount', displayName: '订单金额', type: 'number' },
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
    if (url.pathname.endsWith('/instance-browser/stats')) {
      const objectTypeId = url.searchParams.get('object_type_id')
      const linkTypeId = url.searchParams.get('link_type_id')
      if (linkTypeId) {
        return ok({
          release: { id: 'release-v1', version: 'v1' },
          kind: 'link',
          linkTypeId,
          total: 1,
          truncated: false,
          createdDaily: [{ date: '2026-08-09', count: 1 }],
        })
      }
      return ok({
        release: { id: 'release-v1', version: 'v1' },
        kind: 'object',
        objectTypeId,
        total: 2,
        truncated: false,
        createdDaily: [{ date: '2026-08-08', count: 1 }, { date: '2026-08-09', count: 1 }],
        updatedDaily: [{ date: '2026-08-08', count: 0 }, { date: '2026-08-09', count: 2 }],
        bySource: [{ source: 'pipeline', count: 2 }],
        fields: objectTypeId === 'ot-order' ? [
          {
            name: 'status', label: '状态', type: 'string', kind: 'category',
            coverage: 1, distinct: 2,
            values: [{ value: 'delayed', count: 5 }, { value: 'ok', count: 3 }],
            otherCount: 0,
          },
          {
            name: 'amount', label: '订单金额', type: 'number', kind: 'number',
            coverage: 1, min: 54000, max: 120000, avg: 87000,
            histogram: [{ from: 54000, to: 120000, count: 2 }],
          },
        ] : [],
      })
    }
    if (url.pathname.endsWith('/instance-browser/objects')) {
      objectRequests.push(url)
      const objectTypeId = url.searchParams.get('object_type_id')
      return ok({
        release: { id: 'release-v1', version: 'v1' },
        objectTypeId,
        items: [],
        total: 0,
        page: 1,
        pageSize: 20,
      })
    }
    if (url.pathname.endsWith('/instance-browser/links')) {
      return ok({
        release: { id: 'release-v1', version: 'v1' },
        linkTypeId: 'lt-fulfilled',
        items: [],
        total: 0,
        page: 1,
        pageSize: 20,
      })
    }
    if (url.pathname.endsWith('/overview')) {
      return ok({
        release: { id: 'release-v1', version: 'v1' },
        model: {},
        data: { instances: 3, instancesBySource: { pipeline: 2, action: 1 }, linkInstances: 1 },
        runtime: {
          daily7d: [
            { date: '2026-08-03', firings: { fired: 1, error: 0 }, actionRuns: { success: 2, failed: 0 } },
            { date: '2026-08-04', firings: { fired: 0, error: 1 }, actionRuns: { success: 1, failed: 1 } },
          ],
        },
        facts: {},
        health: [],
      })
    }
    if (url.pathname === '/api/v2/inbox/summary') {
      return ok({ unread_count: 0 })
    }
    return ok([])
  })

  return { objectRequests }
}

test('概览区渲染 KPI 与三张图表，画像区渲染字段分布', async ({ page }) => {
  await mockInstanceOverview(page)
  await page.goto(`/#/ontologies/${ONTOLOGY_ID}?tab=data`, { waitUntil: 'domcontentloaded' })

  const overview = page.getByTestId('instance-overview-section')
  await expect(overview).toBeVisible()
  await expect(overview.getByText('对象实例')).toBeVisible()
  await expect(overview.getByText('关系实例')).toBeVisible()
  await expect(overview.getByText('数据类型')).toBeVisible()
  await expect(overview.getByText('近 7 天活跃')).toBeVisible()

  // 三张概览图 + 画像趋势/字段分布均渲染为 svg（卡片内最后一个 svg 为 echarts 画布）
  await expect(page.getByTestId('overview-type-chart').locator('svg').last()).toBeVisible()
  await expect(page.getByTestId('overview-source-chart').locator('svg').last()).toBeVisible()
  await expect(page.getByTestId('overview-activity-chart').locator('svg').last()).toBeVisible()
  await expect(page.getByTestId('profile-trend-chart').locator('svg').last()).toBeVisible()
  await expect(page.getByTestId('profile-field-status').locator('svg').last()).toBeVisible()
  await expect(page.getByTestId('profile-field-amount')).toContainText('87,000')
})

test('类型分布图点击直达该类型数据', async ({ page }) => {
  const mock = await mockInstanceOverview(page)
  await page.goto(`/#/ontologies/${ONTOLOGY_ID}?tab=data`, { waitUntil: 'domcontentloaded' })

  const typeChart = page.getByTestId('overview-type-chart')
  await expect(typeChart.locator('svg').last()).toBeVisible()
  // 类型按计数降序：订单(2) → 供应商(1) → 由供应商履约(1)，点击第二根条 = 供应商
  await clickHorizontalBar(page, typeChart, 1)

  await expect(page.getByRole('heading', { name: '供应商' })).toBeVisible()
  await expect.poll(() => mock.objectRequests.map(url => url.searchParams.get('object_type_id')))
    .toContain('ot-supplier')
})

test('来源图例点击对实例表施加精确来源过滤，chip 可移除', async ({ page }) => {
  const mock = await mockInstanceOverview(page)
  await page.goto(`/#/ontologies/${ONTOLOGY_ID}?tab=data`, { waitUntil: 'domcontentloaded' })

  await page.getByRole('button', { name: /管道灌入/ }).click()

  await expect(page.getByTestId('active-filters')).toContainText('来源 = 管道灌入')
  await expect.poll(() => mock.objectRequests.some(
    url => url.searchParams.get('source') === 'pipeline',
  )).toBe(true)

  await page.getByRole('button', { name: '移除过滤 来源 = 管道灌入' }).click()
  await expect(page.getByTestId('active-filters')).toHaveCount(0)
  const last = mock.objectRequests.at(-1)!
  expect(last.searchParams.get('source')).toBeNull()
})

test('字段值分布条点击施加精确属性过滤并生成 chip', async ({ page }) => {
  const mock = await mockInstanceOverview(page)
  await page.goto(`/#/ontologies/${ONTOLOGY_ID}?tab=data`, { waitUntil: 'domcontentloaded' })

  const fieldCard = page.getByTestId('profile-field-status')
  await expect(fieldCard.locator('svg').last()).toBeVisible()
  // 值分布按计数降序：delayed(5) 第一根、ok(3) 第二根
  await clickHorizontalBar(page, fieldCard, 0)

  // 激活取值在卡片上给出已过滤提示与取消路径
  await expect(fieldCard).toContainText('已过滤：delayed')
  // chip 用列的中文展示名，英文字段名收进 title
  await expect(page.getByTestId('active-filters')).toContainText('状态 = delayed')
  await expect(page.getByTestId('active-filters').locator('span[title="status = delayed"]')).toHaveCount(1)
  await expect.poll(() => mock.objectRequests.some(
    url => url.searchParams.get('filters') === '{"status":["delayed"]}',
  )).toBe(true)

  // 再次点击同一条形 = 取消该过滤值
  await clickHorizontalBar(page, fieldCard, 0)
  await expect(page.getByTestId('active-filters')).toHaveCount(0)
})

test('图表过滤联动后实例浏览器标题锚定在吸顶头下方', async ({ page }) => {
  await mockInstanceOverview(page)
  await page.goto(`/#/ontologies/${ONTOLOGY_ID}?tab=data`, { waitUntil: 'domcontentloaded' })

  const fieldCard = page.getByTestId('profile-field-status')
  await expect(fieldCard.locator('svg').last()).toBeVisible()
  // 点击前 helper 会把图表滚到视口中央(画像区在页面底部),联动滚动不平凡
  await clickHorizontalBar(page, fieldCard, 0)
  await expect(page.getByTestId('active-filters')).toBeVisible()

  // 等平滑滚动落地:标题行包围盒连续两帧不动
  const header = page.getByTestId('instance-data-header')
  let anchor = await header.boundingBox()
  for (let attempt = 0; attempt < 20; attempt += 1) {
    await page.waitForTimeout(120)
    const next = await header.boundingBox()
    const settled = anchor && next && Math.abs(next.y - anchor.y) < 1
    anchor = next
    if (settled) break
  }
  // 契约:锚定后浏览器标题(含搜索框)完整露出在吸顶头下沿之下,不被遮挡
  const sticky = await page.getByTestId('ontology-detail-header').boundingBox()
  expect(anchor).not.toBeNull()
  expect(sticky).not.toBeNull()
  expect(anchor!.y).toBeGreaterThanOrEqual(sticky!.y + sticky!.height - 2)
})
