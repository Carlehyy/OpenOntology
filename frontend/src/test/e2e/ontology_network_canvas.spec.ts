import assert from 'node:assert/strict'

import { expect, test, type Page, type Route } from '@playwright/test'

/**
 * 本体网络画布（ECharts graph 内核，MYW-58 起为确定性分区布局）mocked E2E。
 *
 * 覆盖：图数据渲染（svg 文本级断言）、默认结构层（L1）、头部统计、图例、
 * 节点点击打开详情、工具条缩放按钮、搜索触发的新图请求。全部请求本地 mock，
 * 不依赖后端。布局确定性后无力导向收敛期，节点标签位置稳定可点击。
 */

/**
 * 确定性布局下标签位置稳定：读取任一可见实例标签的屏幕坐标，等其连续两次
 * 采样位置一致后点击标签上方偏移命中节点圆心；若命中偏移点空，自动重试。
 * 返回前确保详情抽屉已打开。
 */
async function clickVisibleInstanceNode(page: Page, card: ReturnType<Page['getByTestId']>) {
  const instanceLabels = ['华东制造', '华南贸易', 'SO-2026-001', '设备-77']
  const readTarget = (names: string[]) => page.evaluate((labelNames) => {
    for (const element of document.querySelectorAll('[data-testid="network-chart-host"] svg text')) {
      const content = (element.textContent || '').trim()
      if (!labelNames.includes(content)) continue
      const rect = element.getBoundingClientRect()
      if (!rect.width && !rect.height) continue
      return { x: rect.x + rect.width / 2, y: rect.y }
    }
    return null
  }, names)

  await expect.poll(async () => (await card.getByTestId('network-chart-host').locator('svg text').allTextContents())
    .map(text => text.trim()).filter(text => instanceLabels.includes(text)).length,
    { timeout: 5000 }).toBeGreaterThanOrEqual(1)

  for (let attempt = 0; attempt < 3; attempt++) {
    let target = await readTarget(instanceLabels)
    assert.ok(target, '应能找到可见的实例标签')
    // 等待标签位置稳定（连续两次采样一致）再点击，避免命中漂移中的空位
    for (let sample = 0; sample < 8; sample++) {
      await page.waitForTimeout(240)
      const next = await readTarget(instanceLabels)
      if (!next) break
      const settled = Math.abs(next.x - target.x) < 1 && Math.abs(next.y - target.y) < 1
      target = next
      if (settled) break
    }
    // 标签挂在节点正下方：真实移动鼠标到节点圆心悬停，等 tooltip 浮层出现后
    // 禁用其 pointer-events，再原地 mousedown/up（zrender 由 down/up 对推导 click，
    // 且点击不再被 tooltip 拦截）。
    const nodeX = target!.x
    const nodeY = target!.y - 12
    await page.mouse.move(nodeX, nodeY)
    await page.waitForTimeout(150)
    await page.evaluate(() => {
      const host = document.querySelector('[data-testid="network-chart-host"]')
      if (!host) return
      const svg = host.querySelector('svg')
      for (const div of host.querySelectorAll('div')) {
        if (svg && div.contains(svg)) continue
        div.style.setProperty('pointer-events', 'none', 'important')
      }
    })
    await page.mouse.down()
    await page.mouse.up()
    const inspector = page.getByTestId('network-inspector')
    try {
      await expect(inspector).toBeVisible({ timeout: 2500 })
      return inspector
    } catch { /* 点空（背景点击清除选中）：等布局进一步收敛后重试 */ }
  }
  throw new Error('三次尝试均未能通过实例标签命中节点')
}

const overview = [
  {
    id: 'o-supply', name: '供应链', domain: '供应链', published: true,
    releaseId: 'rel-supply', version: 'v2', typeCount: 2, linkTypeCount: 1, instanceCount: 3,
  },
  {
    id: 'o-device', name: '设备台账', domain: '设备', published: false,
    releaseId: null, version: null, typeCount: 1, linkTypeCount: 0, instanceCount: 1,
  },
]

const graphNodes = [
  { id: 'type:t-customer', entityId: 't-customer', kind: 'object_type', label: '客户', technicalName: 'customer',
    objectTypeId: 't-customer', ontologyId: 'o-supply', ontologyName: '供应链', count: 2 },
  { id: 'type:t-order', entityId: 't-order', kind: 'object_type', label: '订单', technicalName: 'sales_order',
    objectTypeId: 't-order', ontologyId: 'o-supply', ontologyName: '供应链', count: 1 },
  { id: 'instance:c1', entityId: 'c1', kind: 'instance', label: '华东制造', objectTypeId: 't-customer',
    objectTypeLabel: '客户', ontologyId: 'o-supply', ontologyName: '供应链' },
  { id: 'instance:c2', entityId: 'c2', kind: 'instance', label: '华南贸易', objectTypeId: 't-customer',
    objectTypeLabel: '客户', ontologyId: 'o-supply', ontologyName: '供应链' },
  { id: 'instance:o9', entityId: 'o9', kind: 'instance', label: 'SO-2026-001', objectTypeId: 't-order',
    objectTypeLabel: '订单', ontologyId: 'o-supply', ontologyName: '供应链' },
  { id: 'type:d-customer', entityId: 'd-customer', kind: 'object_type', label: '客户', technicalName: 'customer',
    objectTypeId: 'd-customer', ontologyId: 'o-device', ontologyName: '设备台账', count: 1 },
  { id: 'instance:d1', entityId: 'd1', kind: 'instance', label: '设备-77', objectTypeId: 'd-customer',
    objectTypeLabel: '客户', ontologyId: 'o-device', ontologyName: '设备台账' },
]

const graphEdges = [
  { id: 'link:l1', kind: 'relation', source: 'instance:c1', target: 'instance:o9', label: '下达',
    ontologyId: 'o-supply', ontologyName: '供应链' },
  { id: 'link:l2', kind: 'relation', source: 'instance:c2', target: 'instance:o9', label: '下达',
    ontologyId: 'o-supply', ontologyName: '供应链' },
  { id: 'schema:s1', kind: 'schema_relation', source: 'type:t-customer', target: 'type:t-order', label: '下游',
    ontologyId: 'o-supply', ontologyName: '供应链' },
  { id: 'bridge:type:t-customer::type:d-customer', kind: 'bridge', source: 'type:t-customer',
    target: 'type:d-customer', label: '同名类型', crossOntology: true },
]

const graphResponse = {
  level: 2,
  query: null,
  limitPerType: 10,
  ontologies: overview.map(item => ({ ...item, error: undefined })),
  errors: [],
  nodes: graphNodes,
  edges: graphEdges,
  bridges: {
    enabled: true,
    groups: [{
      key: 'bridge-group-fix-1',
      label: '客户',
      members: [
        { nodeId: 'type:t-customer', entityId: 't-customer', ontologyId: 'o-supply', ontologyName: '供应链', label: '客户' },
        { nodeId: 'type:d-customer', entityId: 'd-customer', ontologyId: 'o-device', ontologyName: '设备台账', label: '客户' },
      ],
    }],
  },
  meta: {
    nodeBudget: 800, edgeBudget: 2000, truncated: false, droppedEdges: 0,
    nodeCount: 7, edgeCount: 4, selectedOntologies: 2, totalInstances: 4,
  },
}

const instanceDetail = {
  id: 'c1',
  label: '华东制造',
  objectType: {
    id: 't-customer', name: 'customer', displayName: '客户', primaryKey: 'code',
    properties: [{ name: 'region', displayName: '区域', type: 'string' }],
  },
  properties: { region: '华东' },
  computed: {},
  source: 'seed',
  externalId: null,
}

async function mockNetworkApi(
  page: Page,
  fixtures?: { overview?: typeof overview; graph?: typeof graphResponse },
) {
  await page.addInitScript(() => {
    localStorage.setItem('token', 'e2e-token')
    localStorage.setItem('auth-store', JSON.stringify({
      state: { token: 'e2e-token', user: { id: 'u1', username: 'tester', role: 'admin' } },
      version: 0,
    }))
  })

  const overviewPayload = fixtures?.overview ?? overview
  const graphPayload = fixtures?.graph ?? graphResponse
  const graphQueries: string[] = []
  const overviewQueries: string[] = []
  await page.route(/^https?:\/\/[^/]+\/api\/v[12]\//, async (route: Route) => {
    const request = route.request()
    const url = new URL(request.url())
    const ok = (data: unknown, status = 200) => route.fulfill({
      status,
      contentType: 'application/json',
      body: JSON.stringify({ data, message: 'ok' }),
    })

    if (url.pathname === '/api/v2/ontology-network/overview') {
      overviewQueries.push(url.searchParams.toString())
      return ok(overviewPayload)
    }
    if (url.pathname === '/api/v2/ontology-network/graph') {
      graphQueries.push(url.searchParams.toString())
      return ok(graphPayload)
    }
    if (/^\/api\/v2\/ontology-network\/[^/]+\/instances\/[^/]+$/.test(url.pathname)) {
      return ok(instanceDetail)
    }
    if (url.pathname === '/api/v2/inbox/summary') return ok({ unread: 0, actionable: 0 })
    if (url.pathname === '/api/v2/inbox') return ok({ items: [], total: 0 })
    return ok({})
  })
  return { graphQueries, overviewQueries }
}

test('本体网络画布渲染全局图：默认结构层、统计、图例与节点标签可见', async ({ page }) => {
  const { graphQueries } = await mockNetworkApi(page)
  await page.goto('/#/ontology-model/network', { waitUntil: 'domcontentloaded' })

  const card = page.getByTestId('network-canvas-card')
  const opsCard = page.getByTestId('network-ops-card')
  await expect(card).toBeVisible()
  // MYW-58：默认进入结构层（L1），首个图请求应携带 level=1
  await expect(opsCard.getByRole('button', { name: 'L1' })).toHaveAttribute('aria-pressed', 'true')
  await expect.poll(() => graphQueries.some(query => query.includes('level=1'))).toBe(true)
  await expect(card.getByText('2 个本体 · 7 节点 / 4 边')).toBeVisible()

  // 图例（按本体着色）+ 桥接提示
  const legend = card.locator('.backdrop-blur').filter({ hasText: '图例' })
  await expect(legend.getByText('供应链')).toBeVisible()
  await expect(legend.getByText('设备台账')).toBeVisible()
  await expect(legend.getByText('同名类型桥接（启发式）')).toBeVisible()

  // ECharts svg 渲染：节点标签以 <text> 呈现。确定性布局下坐标稳定，
  // labelLayout.hideOverlap 仍可能隐藏少量重叠标签，因此断言
  // 「标签总数下限 + 至少两个实例标签」，不对具体标签逐字强断言。
  const chartTexts = card.getByTestId('network-chart-host').locator('svg text')
  await expect(chartTexts.first()).toBeVisible()
  await expect.poll(async () => (await chartTexts.allTextContents()).map(text => text.trim()).filter(Boolean).length,
    { timeout: 5000 }).toBeGreaterThanOrEqual(5)
  const labelTexts = (await chartTexts.allTextContents()).map(text => text.trim()).filter(Boolean)
  const instanceLabels = ['华东制造', '华南贸易', 'SO-2026-001', '设备-77']
  const visibleInstances = instanceLabels.filter(label => labelTexts.includes(label))
  assert.ok(
    visibleInstances.length >= 2,
    ['实例标签至少 2/4 可见，实际可见：', visibleInstances.join('、') || '无'].join(''),
  )

  // MYW-58 二期：碰撞消解后节点符号之间应无深重叠。
  // 注意 ECharts SVG 会把圆形符号渲染为 path（细长的连线也是 path），
  // 因此按"近正方形且尺寸 > 12px"过滤出节点符号，避免断言空转。
  const deepOverlaps = await page.evaluate(() => {
    const rects = [...document.querySelectorAll('[data-testid="network-chart-host"] svg path, [data-testid="network-chart-host"] svg circle')]
      .map(el => el.getBoundingClientRect())
      .filter(rect => rect.width > 12 && rect.height > 12 && rect.width <= 70 && Math.abs(rect.width - rect.height) <= 2)
    let hits = 0
    for (let i = 0; i < rects.length; i++) {
      for (let j = i + 1; j < rects.length; j++) {
        const a = rects[i], b = rects[j]
        const ox = Math.min(a.right, b.right) - Math.max(a.left, b.left)
        const oy = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top)
        // 允许 1px 级渲染误差：任一轴侵入超过较小直径 25% 才计为深重叠
        if (ox > Math.min(a.width, b.width) * 0.25 && oy > Math.min(a.height, b.height) * 0.25) hits += 1
      }
    }
    return hits
  })
  assert.equal(deepOverlaps, 0, `节点符号深重叠应为 0，实际 ${deepOverlaps}`)
})

test('点击实例节点打开详情抽屉，可关闭', async ({ page }) => {
  await mockNetworkApi(page)
  await page.goto('/#/ontology-model/network', { waitUntil: 'domcontentloaded' })

  const card = page.getByTestId('network-canvas-card')
  // 确定性布局下实例标签均应可见，自适应命中任一实例标签即可；
  // 详情接口由 mock 返回统一实例载荷。
  const inspector = await clickVisibleInstanceNode(page, card)
  await expect(inspector.getByText('华东制造')).toBeVisible()
  await expect(inspector.getByRole('button', { name: /设为起点/ })).toBeVisible()

  await inspector.getByRole('button', { name: '关闭节点详情' }).click()
  await expect(inspector).toHaveCount(0)
})

test('工具条缩放/适应可用，搜索触发带 query 的图请求', async ({ page }) => {
  const { graphQueries } = await mockNetworkApi(page)
  await page.goto('/#/ontology-model/network', { waitUntil: 'domcontentloaded' })

  const card = page.getByTestId('network-canvas-card')
  await expect(card.getByText('2 个本体 · 7 节点 / 4 边')).toBeVisible()

  await card.getByRole('button', { name: '放大画布' }).click()
  await card.getByRole('button', { name: '缩小画布' }).click()
  await card.getByRole('button', { name: '适应画布' }).click()
  // 未截断时不出现预算提示
  await expect(card.getByText(/已按预算截断/)).toHaveCount(0)

  await page.getByLabel('搜索实例').fill('华南')
  await page.getByLabel('搜索实例').press('Enter')
  await expect.poll(() => graphQueries.some(query => query.includes('query='))).toBe(true)
})


test('悬停节点：一跳邻接强亮、其余 blur 淡出并可恢复', async ({ page }) => {
  await mockNetworkApi(page)
  await page.goto('/#/ontology-model/network', { waitUntil: 'domcontentloaded' })

  const host = page.getByTestId('network-chart-host')
  await expect(host.locator('svg text').first()).toBeVisible()

  // 任选一个可见的类型标签（订单/客户），等其位置稳定后悬停标签上方的节点圆心
  const readTarget = () => page.evaluate(() => {
    for (const name of ['订单', '客户']) {
      for (const element of document.querySelectorAll('[data-testid="network-chart-host"] svg text')) {
        if ((element.textContent || '').trim() !== name) continue
        const rect = element.getBoundingClientRect()
        if (!rect.width && !rect.height) continue
        return { x: rect.x + rect.width / 2, y: rect.y }
      }
    }
    return null
  })
  let target = null as { x: number; y: number } | null
  for (let i = 0; i < 30; i++) {
    const next = await readTarget()
    if (!next) { await page.waitForTimeout(300); continue }
    if (target && Math.abs(next.x - target.x) < 1 && Math.abs(next.y - target.y) < 1) break
    target = next
    await page.waitForTimeout(300)
  }
  assert.ok(target, '至少一个类型标签应可见')

  // 淡出元素计数：zrender 把 blur 透明度写在 fill/stroke-opacity 上；
  // 桥接边基线就 <0.5，因此用「悬停前后差值」断言而非绝对值。
  const dimmedCount = () => page.evaluate(() => {
    let count = 0
    for (const path of document.querySelectorAll('[data-testid="network-chart-host"] svg path')) {
      for (const attr of ['fill-opacity', 'stroke-opacity', 'opacity']) {
        const raw = path.getAttribute(attr)
        if (raw === null) continue
        const value = parseFloat(raw)
        if (Number.isFinite(value) && value > 0 && value < 0.5) { count += 1; break }
      }
    }
    return count
  })
  const baseline = await dimmedCount()

  await page.mouse.move(target.x, target.y - 14)
  await expect.poll(dimmedCount).toBeGreaterThan(baseline)

  // 移出画布后 blur 恢复，淡出元素回到基线
  await page.mouse.move(5, 5)
  await expect.poll(dimmedCount).toBe(baseline)
})

// 与 governance_async_refresh.spec.ts 同款：覆写 visibilityState 后派发事件，
// 模拟「切走标签页再切回」触发的 React Query 窗口聚焦。
async function setDocumentVisibility(page: Page, state: 'visible' | 'hidden') {
  await page.evaluate(nextState => {
    Object.defineProperty(document, 'visibilityState', {
      configurable: true,
      get: () => nextState,
    })
    document.dispatchEvent(new Event('visibilitychange'))
    if (nextState === 'visible') window.dispatchEvent(new Event('focus'))
  }, state)
}

test('切回浏览器标签页不触发全局图谱重建（无新请求、无构建遮罩）', async ({ page }) => {
  const { graphQueries, overviewQueries } = await mockNetworkApi(page)
  await page.goto('/#/ontology-model/network', { waitUntil: 'domcontentloaded' })

  const card = page.getByTestId('network-canvas-card')
  await expect(card.getByText('2 个本体 · 7 节点 / 4 边')).toBeVisible()
  const graphBefore = graphQueries.length
  const overviewBefore = overviewQueries.length
  assert.ok(graphBefore > 0 && overviewBefore > 0, '首屏应已发出 overview 与 graph 请求')

  await setDocumentVisibility(page, 'hidden')
  await setDocumentVisibility(page, 'visible')

  // 给潜在的聚焦 refetch 留出执行窗口：overview/graph 均不应重新请求
  await page.waitForTimeout(600)
  expect(graphQueries.length).toBe(graphBefore)
  expect(overviewQueries.length).toBe(overviewBefore)
  await expect(card.getByText('正在构建全局图谱')).toHaveCount(0)
})

const chainOverview = [
  {
    id: 'o-chain', name: '链路', domain: '链路', published: true,
    releaseId: 'rel-chain', version: 'v1', typeCount: 3, linkTypeCount: 2, instanceCount: 0,
  },
]

const chainGraph = {
  level: 1,
  query: null,
  limitPerType: 10,
  ontologies: chainOverview.map(item => ({ ...item, error: undefined })),
  errors: [] as [],
  nodes: [
    { id: 'type:a', entityId: 'a', kind: 'object_type' as const, label: '甲', technicalName: 'a',
      objectTypeId: 'a', ontologyId: 'o-chain', ontologyName: '链路', count: 0 },
    { id: 'type:b', entityId: 'b', kind: 'object_type' as const, label: '乙', technicalName: 'b',
      objectTypeId: 'b', ontologyId: 'o-chain', ontologyName: '链路', count: 0 },
    { id: 'type:c', entityId: 'c', kind: 'object_type' as const, label: '丙', technicalName: 'c',
      objectTypeId: 'c', ontologyId: 'o-chain', ontologyName: '链路', count: 0 },
  ],
  edges: [],
  bridges: { enabled: true, groups: [] as [] },
  meta: {
    nodeBudget: 800, edgeBudget: 2000, truncated: false, droppedEdges: 0,
    nodeCount: 3, edgeCount: 0, selectedOntologies: 1, totalInstances: 0,
  },
}

test('同一行的对象类型色块是正圆，而不是被拉高的椭圆', async ({ page }) => {
  await mockNetworkApi(page, { overview: chainOverview, graph: chainGraph })
  await page.goto('/#/ontology-model/network', { waitUntil: 'domcontentloaded' })

  const host = page.getByTestId('network-chart-host')
  await expect(host.locator('svg text', { hasText: '甲' })).toBeVisible()
  await expect(host.locator('svg text', { hasText: '丙' })).toBeVisible()

  const readSymbols = () => page.evaluate(() => {
    const rows = [...document.querySelectorAll('[data-testid="network-chart-host"] svg path, [data-testid="network-chart-host"] svg circle, [data-testid="network-chart-host"] svg ellipse')]
      .map(el => {
        const rect = el.getBoundingClientRect()
        return {
          tag: el.tagName,
          width: Math.round(rect.width * 10) / 10,
          height: Math.round(rect.height * 10) / 10,
        }
      })
      .filter(rect => rect.width > 1 || rect.height > 1)
    const round = rows.filter(rect => rect.width >= 12 && rect.height >= 12 && rect.width <= 70 && rect.height <= 70
      && Math.abs(rect.width - rect.height) <= 2).length
    return { round, rows: rows.slice(0, 12) }
  })
  await expect.poll(async () => (await readSymbols()).round, { timeout: 5000 }).toBeGreaterThanOrEqual(3)
})
