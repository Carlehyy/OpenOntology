import { expect, test, type Page, type Route } from '@playwright/test'

/**
 * 本体网络页 · 结构投影（structure-projection）mocked E2E。
 *
 * 覆盖：状态徽章（ready/failed/stale/unavailable）如实展示、
 * failed 错误原因可见、重建入口仅 admin 可见且 503 不冒充成功、viewer
 * 无重建入口但状态可读、unavailable 不可展开旧图、单选 ready 时
 * 结构图预览可见（结构层无实例节点）。
 * 结构投影状态与旧实例投影状态（projection_status）是两套事实，本 spec 只
 * 走 /structure-projection 端点，不读取实例投影字段。
 */

const authScript = (role: string) => {
  localStorage.setItem('token', 'e2e-token')
  localStorage.setItem('auth-store', JSON.stringify({
    state: { token: 'e2e-token', user: { id: 'u1', username: 'tester', role } },
    version: 0,
  }))
}

const overview = [
  { id: 'o-supply', name: '供应链', domain: '供应链', published: true, releaseId: 'rel-supply', version: 'v2', typeCount: 2, linkTypeCount: 1, instanceCount: 3 },
  { id: 'o-device', name: '设备台账', domain: '设备', published: false, releaseId: null, version: null, typeCount: 1, linkTypeCount: 0, instanceCount: 1 },
]

const projectionByStatus: Record<string, Record<string, unknown>> = {
  ready: {
    status: 'ready', releaseId: 'rel-supply', version: 'v2', snapshotHash: 'hash-a1',
    drift: false, error: null, nodeCount: 12, edgeCount: 9,
  },
  failed: {
    status: 'failed', releaseId: 'rel-device', version: 'v1', snapshotHash: 'hash-b0',
    drift: false, error: 'Neo4j 写入中断', nodeCount: 0, edgeCount: 0,
  },
  stale: {
    status: 'stale', releaseId: 'rel-supply', version: 'v2', snapshotHash: 'hash-a1',
    drift: true, error: null, nodeCount: 12, edgeCount: 9,
  },
  unavailable: {
    status: 'unavailable', releaseId: 'rel-supply', version: 'v2', snapshotHash: 'hash-a1',
    drift: false, error: '结构图暂不可读（Neo4j 不可达）', nodeCount: 0, edgeCount: 0,
  },
}

const structureGraph = {
  release: { releaseId: 'rel-supply', version: 'v2', snapshotHash: 'hash-a1' },
  status: 'ready',
  nodes: [
    { id: 'n-customer', kind: 'object_type', localId: 't-customer', name: 'customer', displayName: '客户', description: null, attributes: {} },
    { id: 'n-tax', kind: 'property', localId: 'p-tax', name: 'tax_id', displayName: null, description: null, attributes: {} },
  ],
  edges: [{ id: 'e1', type: 'has_property', source: 'n-customer', target: 'n-tax' }],
  overlays: { joinAssertions: [] },
}

const graphResponse = {
  level: 2, query: null, limitPerType: 10,
  ontologies: overview.map(item => ({ ...item, error: undefined })),
  errors: [], nodes: [], edges: [],
  bridges: { enabled: false, groups: [] },
  meta: { nodeBudget: 800, edgeBudget: 2000, truncated: false, droppedEdges: 0, nodeCount: 0, edgeCount: 0, selectedOntologies: 2, totalInstances: 4 },
}

interface StructureOptions {
  /** 登录角色（默认 admin；viewer 断言无重建入口） */
  role?: string
  /** o-supply 的状态（默认 ready）；o-device 恒 failed，用于错误如实展示 */
  supplyStatus?: keyof typeof projectionByStatus
  /** POST rebuild 的行为：'ok' 返回 ready，'error' 503 */
  rebuildBehavior?: 'ok' | 'error'
  /** 单选结构图预览所需的结构图数据开关 */
  graphUnavailable?: boolean
}

async function mockNetworkApi(page: Page, options: StructureOptions = {}) {
  await page.addInitScript(authScript, options.role ?? 'admin')
  const supplyStatus = options.supplyStatus ?? 'ready'
  const rebuildBehavior = options.rebuildBehavior ?? 'ok'
  let rebuildCalls = 0
  await page.route(/^https?:\/\/[^/]+\/api\/v[12]\//, async (route: Route) => {
    const url = new URL(route.request().url())
    const ok = (data: unknown) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ data, message: 'ok' }) })
    const fail = (status: number, detail: unknown) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify({ detail }) })

    if (url.pathname === '/api/v2/ontology-network/overview') return ok(overview)
    if (url.pathname === '/api/v2/ontology-network/graph') return ok(graphResponse)
    if (url.pathname.endsWith('/structure-projection/rebuild') && route.request().method() === 'POST') {
      rebuildCalls += 1
      if (rebuildBehavior === 'error') return fail(503, { code: 'structure_projection_rebuild_failed', message: '结构投影重建失败：图存储不可用' })
      return ok({ ...projectionByStatus.ready, nodeCount: 14, edgeCount: 10 })
    }
    if (/\/structure-projection$/.test(url.pathname)) {
      return ok(url.pathname.includes('o-supply') ? projectionByStatus[supplyStatus] : projectionByStatus.failed)
    }
    if (/\/structure-graph$/.test(url.pathname)) {
      if (options.graphUnavailable) return fail(503, { code: 'ontology_structure_projection_not_ready', message: '结构投影未就绪' })
      return ok(structureGraph)
    }
    if (url.pathname === '/api/v2/inbox/summary') return ok({ unread: 0, actionable: 0 })
    if (url.pathname === '/api/v2/inbox') return ok({ items: [], total: 0 })
    return ok({})
  })
  return { getRebuildCalls: () => rebuildCalls }
}

test('结构投影状态如实展示：ready/failed 并列、失败原因可见、admin 有重建入口', async ({ page }) => {
  await mockNetworkApi(page, { supplyStatus: 'ready' })
  await page.goto('/#/ontology-model/network', { waitUntil: 'domcontentloaded' })
  const section = page.getByTestId('network-structure-projection')
  await expect(section).toBeVisible()
  await expect(section.getByTestId('structure-projection-status-o-supply')).toContainText('已就绪')
  await expect(section.getByTestId('structure-projection-row-o-supply')).toContainText('发布 v2')
  await expect(section.getByTestId('structure-projection-row-o-supply')).toContainText('结构 12 节点 / 9 边')
  // 失败状态不冒充成功：徽章为「构建失败」且服务端 error 原样可见
  await expect(section.getByTestId('structure-projection-status-o-device')).toContainText('构建失败')
  await expect(section.getByTestId('structure-projection-row-o-device')).toContainText('Neo4j 写入中断')
  // admin 可见重建入口
  await expect(section.getByRole('button', { name: '重建 供应链 结构投影' })).toBeVisible()
  await expect(section.getByRole('button', { name: '重建 设备台账 结构投影' })).toBeVisible()
})

test('stale 状态显示漂移提示且不宣称就绪', async ({ page }) => {
  await mockNetworkApi(page, { supplyStatus: 'stale' })
  await page.goto('/#/ontology-model/network', { waitUntil: 'domcontentloaded' })
  const row = page.getByTestId('structure-projection-row-o-supply')
  await expect(page.getByTestId('structure-projection-status-o-supply')).toContainText('已漂移')
  await expect(row).toContainText('已漂移，建议重建')
  await expect(row).not.toContainText('已就绪')
})

test('unavailable 状态如实展示：不冒充 ready、不展开旧图、admin 可重建', async ({ page }) => {
  await mockNetworkApi(page, { supplyStatus: 'unavailable' })
  await page.goto('/#/ontology-model/network', { waitUntil: 'domcontentloaded' })
  const row = page.getByTestId('structure-projection-row-o-supply')
  await expect(page.getByTestId('structure-projection-status-o-supply')).toContainText('暂不可用')
  await expect(row).toContainText('结构图暂不可读（Neo4j 不可达）')
  // 图不可读：不提供结构图预览入口（不可展开旧图冒充可读）
  await expect(row.getByText('结构图预览', { exact: false })).toHaveCount(0)
  // 管理员仍可重建
  await expect(page.getByRole('button', { name: '重建 供应链 结构投影' })).toBeVisible()
})

test('admin 重建：成功后徽章按服务端响应更新；失败保持失败态且错误可见', async ({ page }) => {
  const { getRebuildCalls } = await mockNetworkApi(page, { rebuildBehavior: 'ok' })
  await page.goto('/#/ontology-model/network', { waitUntil: 'domcontentloaded' })
  await page.getByRole('button', { name: '重建 设备台账 结构投影' }).click()
  // 同步返回重建后的权威状态：徽章变为已就绪并带上新计数（不弹假成功 toast）
  await expect(page.getByTestId('structure-projection-status-o-device')).toContainText('已就绪', { timeout: 5000 })
  await expect(page.getByTestId('structure-projection-row-o-device')).toContainText('结构 14 节点 / 10 边')
  expect(getRebuildCalls()).toBe(1)
})

test('admin 重建失败：503 错误如实透出，徽章不得变为就绪', async ({ page }) => {
  await mockNetworkApi(page, { rebuildBehavior: 'error' })
  await page.goto('/#/ontology-model/network', { waitUntil: 'domcontentloaded' })
  await page.getByRole('button', { name: '重建 设备台账 结构投影' }).click()
  await expect(page.getByTestId('structure-projection-row-o-device')).toContainText('结构投影重建失败', { timeout: 5000 })
  await expect(page.getByTestId('structure-projection-status-o-device')).not.toContainText('已就绪')
})

test('viewer 无重建入口，但投影状态照常可读', async ({ page }) => {
  await mockNetworkApi(page, { role: 'viewer' })
  await page.goto('/#/ontology-model/network', { waitUntil: 'domcontentloaded' })
  const section = page.getByTestId('network-structure-projection')
  await expect(section.getByTestId('structure-projection-status-o-supply')).toContainText('已就绪')
  await expect(section.getByRole('button', { name: '重建 供应链 结构投影' })).toHaveCount(0)
  await expect(section.getByRole('button', { name: '重建 设备台账 结构投影' })).toHaveCount(0)
})

test('单选 ready 本体提供结构图预览，结构层不含实例节点', async ({ page }) => {
  await mockNetworkApi(page)
  await page.goto('/#/ontology-model/network', { waitUntil: 'domcontentloaded' })
  // 先只选一个本体：清空后单选供应链
  await page.getByRole('button', { name: '清空' }).click()
  await page.getByTestId('network-ontology-list').getByText('供应链', { exact: true }).click()
  const row = page.getByTestId('structure-projection-row-o-supply')
  await row.getByText('结构图预览', { exact: false }).click()
  const host = page.getByTestId('structure-graph-host')
  await expect(host).toBeVisible()
  await expect(host).toContainText('客户')
  // 结构层纪律：实例名称（来自全局图的 mock 数据）不得出现在结构图预览中
  await expect(host).not.toContainText('华东制造')
})
