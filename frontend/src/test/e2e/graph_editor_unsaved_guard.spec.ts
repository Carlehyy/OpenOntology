import { expect, test, type Locator, type Page, type Route } from '@playwright/test'

/**
 * 图谱编辑器（/ontologies/:id/graph）交互回归：
 * 1. 拖拽节点后布局自动保存；防抖窗口内页面隐藏时以 keepalive 补发坐标。
 * 2. 结构未保存时，画布左列「返回」与左上角本体图标都弹出同一确认框。
 * 3. 浏览器前进/后退被拦截，确认后才真正离开。
 * 4. 画布右下角菜单不再提供「本体助手」入口。
 */

const ontologyId = 'ontology-unsaved-guard'
const firstDraftId = 'version-draft-guard-1'
const secondDraftId = 'version-draft-guard-2'
const KEEPALIVE_PROBE_KEY = 'guard-keepalive-layout-probe'

function fullOntologyPayload() {
  return {
    id: ontologyId,
    name: '未保存拦截测试本体',
    version: 'v1',
    revision: '1:snapshot',
    workspaceMode: 'draft',
    editable: true,
    objectTypes: [
      {
        id: 'ot-order',
        name: 'Order',
        displayName: '订单',
        primaryKey: 'order_no',
        properties: [
          { id: 'order_no', name: 'order_no', displayName: '订单号', type: 'string', required: true },
        ],
        positionX: 0,
        positionY: 0,
      },
      {
        id: 'ot-customer',
        name: 'Customer',
        displayName: '客户',
        primaryKey: 'cust_no',
        properties: [
          { id: 'cust_no', name: 'cust_no', displayName: '客户编号', type: 'string', required: true },
        ],
        positionX: 700,
        positionY: 0,
      },
    ],
    linkTypes: [{
      id: 'lt-placed-by',
      name: 'placed_by',
      displayName: '下单',
      sourceObjectTypeId: 'ot-order',
      targetObjectTypeId: 'ot-customer',
      cardinality: 'many-to-one',
    }],
    actions: [],
    functions: [],
    instances: [],
    linkInstances: [],
    executionLogs: [],
    sentinels: [],
  }
}

/** 读取 React Flow 节点的流坐标（包装器 style.transform 即 node.position）。 */
async function readFlowPosition(locator: Locator): Promise<{ x: number; y: number }> {
  const transform = await locator.evaluate(element =>
    ((element as HTMLElement).closest('.react-flow__node') as HTMLElement | null)?.style.transform || '')
  const match = /translate\((-?[\d.eE+-]+)px,\s*(-?[\d.eE+-]+)px\)/.exec(transform)
  if (!match) throw new Error(`无法解析节点坐标：${transform}`)
  return { x: Number(match[1]), y: Number(match[2]) }
}

async function mockGraphEditor(page: Page) {
  await page.addInitScript(() => {
    localStorage.setItem('token', 'e2e-token')
    localStorage.setItem('auth-store', JSON.stringify({
      state: {
        token: 'e2e-token',
        user: { id: 'u1', username: 'tester', role: 'admin' },
      },
      version: 0,
    }))
  })
  // 卸载期间的 keepalive fetch 由浏览器网络服务直接发出，Playwright 路由观测不到；
  // 页面侧 fetch 侦听器把调用写进 localStorage，供跨刷新读取断言。
  await page.addInitScript((probeKey: string) => {
    const originalFetch = window.fetch.bind(window)
    window.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === 'string'
        ? input
        : input instanceof URL
          ? input.toString()
          : input.url
      if (init?.keepalive === true && url.includes('/layout')) {
        const previous = JSON.parse(localStorage.getItem(probeKey) || '[]')
        previous.push({ url, body: init.body ? JSON.parse(String(init.body)) : null })
        localStorage.setItem(probeKey, JSON.stringify(previous))
      }
      return originalFetch(input as RequestInfo, init)
    }) as typeof window.fetch
  }, KEEPALIVE_PROBE_KEY)

  const layoutCalls: Array<Record<string, unknown>> = []
  await page.route(/^https?:\/\/[^/]+\/api\/v[12]\//, async (route: Route) => {
    const request = route.request()
    const url = new URL(request.url())
    const ok = (data: unknown) => route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ data, message: 'ok' }),
    })
    if (url.pathname === `/api/v2/ontologies/${ontologyId}/layout` && request.method() === 'PUT') {
      layoutCalls.push(JSON.parse(request.postData() || '{}'))
      return ok({ positions: {} })
    }
    if (url.pathname === `/api/v2/ontologies/${ontologyId}/full`) {
      return ok(fullOntologyPayload())
    }
    if (/^\/api\/v2\/ontologies\/[^/]+\/versions\/[^/]+\/workspace$/.test(url.pathname)) {
      return ok(fullOntologyPayload())
    }
    if (url.pathname === '/api/v2/inbox/summary') {
      return ok({ unread_count: 0 })
    }
    return ok([])
  })
  return { layoutCalls }
}

/** 打开草稿编辑器：draft 模式才可编辑结构，标题/版本状态条可确认加载完成。 */
async function openDraftGraph(page: Page, versionId: string) {
  // 编辑器左列工具条在矮视口会盖住 Header 左上角图标，用近似桌面的视口保证点击可达
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto(`/#/ontologies/${ontologyId}/graph?versionId=${versionId}`, { waitUntil: 'domcontentloaded' })
  await expect(page.getByTestId('graph-workspace-stage')).toContainText('草稿')
  await expect(page.locator('.react-flow__node')).toHaveCount(2)
}

/** 通过面板新建一个对象实体，制造"结构已更新、尚未保存"的脏状态。 */
async function createUnsavedStructureChange(page: Page) {
  await page.getByRole('button', { name: '对象实体', exact: true }).click()
  await page.getByPlaceholder('object_entity_name').fill('guard_probe_entity')
  await page.getByPlaceholder('对象实体显示名称').fill('未保存探针实体')
  await page.getByRole('button', { name: '应用到画布' }).click()
  await expect(page.getByRole('button', { name: '保存全部更改' })).toBeVisible()
}

async function dragNode(page: Page, nodeId: string, deltaX: number, deltaY: number) {
  const node = page.locator(`.react-flow__node[data-id="${nodeId}"]`)
  await expect(node).toBeVisible()
  const box = await node.boundingBox()
  expect(box).toBeTruthy()
  await page.mouse.move(box!.x + box!.width / 2, box!.y + box!.height / 2)
  await page.mouse.down()
  await page.mouse.move(box!.x + box!.width / 2 + deltaX, box!.y + box!.height / 2 + deltaY, { steps: 8 })
  await page.mouse.up()
}

test('拖拽节点后自动保存布局，防抖窗口内 pagehide 以 keepalive 补发坐标', async ({ page }) => {
  const { layoutCalls } = await mockGraphEditor(page)
  await openDraftGraph(page, firstDraftId)
  await page.waitForTimeout(750)

  const before = await readFlowPosition(page.locator('.react-flow__node[data-id="ot-customer"]'))
  await dragNode(page, 'ot-customer', -120, 80)
  const dragged = await readFlowPosition(page.locator('.react-flow__node[data-id="ot-customer"]'))
  expect(Math.abs(dragged.x - before.x)).toBeGreaterThan(20)

  // 650ms 防抖结束后正常保存：布局状态条如实提示，且 PUT 携带新坐标
  await expect.poll(() => layoutCalls.length, { timeout: 5000 }).toBeGreaterThan(0)
  await expect(page.getByTestId('layout-save-status')).toContainText('布局已自动保存')
  const saved = layoutCalls.at(-1) as { positions: Record<string, { x: number; y: number }> }
  expect(saved.positions['ot-customer']).toBeTruthy()

  // 再次拖拽后立刻隐藏页面：防抖未到点也必须用 keepalive 补发，避免返回丢位置
  await dragNode(page, 'ot-order', 90, 60)
  await page.evaluate(() => window.dispatchEvent(new PageTransitionEvent('pagehide')))
  await expect.poll(async () => page.evaluate(
    (key) => JSON.parse(localStorage.getItem(key) || '[]').length,
    KEEPALIVE_PROBE_KEY,
  ), { timeout: 5000 }).toBeGreaterThan(0)
  const probes = await page.evaluate(
    (key) => JSON.parse(localStorage.getItem(key) || '[]') as Array<{ body?: { positions?: Record<string, unknown> } }>,
    KEEPALIVE_PROBE_KEY,
  )
  expect(probes.at(-1)?.body?.positions?.['ot-order']).toBeTruthy()
})

test('结构未保存时，画布返回与左上角图标弹出同一确认框，取消后留在编辑器', async ({ page }) => {
  await mockGraphEditor(page)
  await openDraftGraph(page, firstDraftId)
  await createUnsavedStructureChange(page)

  // 画布左列最下面的返回按钮
  await page.getByTestId('graph-workspace-back').click()
  const dialog = page.getByTestId('graph-unsaved-dialog')
  await expect(dialog).toBeVisible()
  await expect(dialog.getByText('当前结构已更新，尚未保存')).toBeVisible()
  await expect(dialog.getByText(/未保存的结构改动将丢失/)).toBeVisible()
  await dialog.getByTestId('graph-unsaved-stay').click()
  await expect(dialog).toHaveCount(0)
  await expect(page).toHaveURL(new RegExp(`/ontologies/${ontologyId}/graph`))

  // 左上角本体图标与返回按钮行为一致
  await page.getByTestId('graph-header-back').click()
  await expect(dialog).toBeVisible()
  await dialog.getByTestId('graph-unsaved-stay').click()
  await expect(dialog).toHaveCount(0)
})

test('画布返回确认放弃后真正离开，不会再次被守卫拦回', async ({ page }) => {
  await mockGraphEditor(page)
  await openDraftGraph(page, firstDraftId)
  // 追加一条同路由历史记录（切换版本），让「返回」有明确目标
  await page.evaluate((hash) => { window.location.hash = hash }, `#/ontologies/${ontologyId}/graph?versionId=${secondDraftId}`)
  await expect(page.getByTestId('graph-workspace-stage')).toContainText('草稿')
  await expect(page.locator('.react-flow__node')).toHaveCount(2)
  await createUnsavedStructureChange(page)

  await page.getByTestId('graph-workspace-back').click()
  const dialog = page.getByTestId('graph-unsaved-dialog')
  await expect(dialog).toBeVisible()
  await dialog.getByTestId('graph-unsaved-leave').click()
  // 回归点：确认离开后必须真的回到上一页，不能被守卫二次拦截
  await expect(page).toHaveURL(new RegExp(`${firstDraftId}`))
  await expect(page.getByTestId('graph-unsaved-dialog')).toHaveCount(0)
})

test('浏览器后退被拦截：未保存结构不丢，确认后才离开', async ({ page }) => {
  await mockGraphEditor(page)
  await openDraftGraph(page, firstDraftId)
  // 追加一条同路由历史记录（切换版本），让后退有明确目标
  await page.evaluate((hash) => { window.location.hash = hash }, `#/ontologies/${ontologyId}/graph?versionId=${secondDraftId}`)
  await expect(page.getByTestId('graph-workspace-stage')).toContainText('草稿')
  await expect(page.locator('.react-flow__node')).toHaveCount(2)
  await createUnsavedStructureChange(page)

  const dialog = page.getByTestId('graph-unsaved-dialog')
  await page.evaluate(() => window.history.back())
  await expect(dialog).toBeVisible()
  // 后退被取消：地址仍是当前草稿图谱，未离开编辑器
  await expect(page).toHaveURL(new RegExp(`${secondDraftId}`))
  await expect(page.getByTestId('graph-workspace-stage')).toBeVisible()

  // 取消后仍留在编辑器，且再次后退仍会被拦
  await dialog.getByTestId('graph-unsaved-stay').click()
  await expect(dialog).toHaveCount(0)
  await page.evaluate(() => window.history.back())
  await expect(dialog).toBeVisible()

  // 确认放弃后真正离开到上一条记录
  await dialog.getByTestId('graph-unsaved-leave').click()
  await expect(page).toHaveURL(new RegExp(`${firstDraftId}`))
})

test('画布右下角菜单移除「本体助手」，其余运行工具保持可见', async ({ page }) => {
  await mockGraphEditor(page)
  await openDraftGraph(page, firstDraftId)

  await page.getByTitle('打开菜单').click()
  await expect(page.getByTestId('graph-runtime-tool-instances')).toBeVisible()
  await expect(page.getByTestId('graph-runtime-tool-graphdb')).toBeVisible()
  await expect(page.getByTestId('graph-runtime-tool-help')).toHaveCount(0)
  await expect(page.getByTestId('graph-runtime-tool-sentinel')).toHaveCount(1)
  await expect(page.getByTestId('graph-runtime-tool-sentinel')).not.toContainText('本体助手')
})
