import { expect, test, type Locator, type Page } from '@playwright/test'

/**
 * api-hub 接口管理页 UX 评审回归（全部走本地路由 mock，不触真实后端）：
 * - A01 首载失败显示错误面板 + 重试恢复，不误导为「空列表」
 * - A02 有未保存修改时发布：先确认，确认后先保存（PUT）再打开发布弹窗
 * - A03 写方法（DELETE）试调：按钮文案显式表达副作用，每配置首次调用需确认
 * - A08 个人变量复制：剪贴板不可用时如实显示「复制失败」并给手动兜底
 * - A13 调用密钥弹窗在小视口（800×600）下标题与关闭按钮不被遮挡
 * - A14 列表搜索计数「显示 x / y 个接口」，且搜索匹配请求方法
 */

interface HubInterfaceFixture {
  id: number
  name: string
  method: string
  url: string
  group_name?: string
  [key: string]: unknown
}

function makeInterface(overrides: HubInterfaceFixture) {
  return {
    description: '',
    group_name: '',
    query_params: [],
    headers: [],
    body_type: 'none',
    body_content: '',
    file_fields: [],
    mcp_enabled: false,
    open_enabled: false,
    http_enabled: false,
    proxy_slug: '',
    proxy_query_keys: [],
    proxy_header_keys: [],
    proxy_body_enabled: false,
    proxy_body_keys: [],
    parameter_schema: [],
    config_revision: 1,
    updated_at: '2026-09-19T08:00:00.000Z',
    ...overrides,
  }
}

const ORDER_QUERY = makeInterface({ id: 1, name: '订单详情', method: 'GET', url: 'https://vendor.example/v1/orders' })
const ORDER_DELETE = makeInterface({ id: 2, name: '删除订单', method: 'DELETE', url: 'https://vendor.example/v1/orders/A-1' })

interface MockOptions {
  /** 每次 GET /interfaces 调用时求值，支持「先失败后成功」场景。 */
  listInterfaces?: () => { status?: number; body: unknown }
  onPutInterface?: (body: Record<string, unknown>) => Record<string, unknown>
  onPreviewRunRaw?: () => void
}

async function mockApp(page: Page, options: MockOptions = {}) {
  // 与 models.spec.ts 同款：直接 seed 登录态，跳过 UI 登录
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
  await page.route('**/api/v2/inbox/summary', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({
      data: { openAlertCount: 0, actionableCount: 0, unreadCount: 0, resolvedCount: 0 },
    }),
  }))
  await page.route('**/api/v1/**', route => {
    const path = new URL(route.request().url()).pathname
    const json = (data: unknown, status = 200) => route.fulfill({
      status,
      contentType: 'application/json',
      body: JSON.stringify({ data }),
    })
    if (path === '/api/v1/auth/profile') {
      return json({ id: 'admin', username: 'admin', email: 'admin@example.com', role: 'admin' })
    }
    if (path === '/api/v1/auth/env-vars') {
      return json([{ key: 'API_ROOT', value: 'https://internal.example' }])
    }
    if (path === '/api/v1/auth/privacy-vars') {
      return json([{ id: 'p1', key: 'VENDOR_TOKEN', has_value: true, last_reported_at: null, created_at: '2026-09-01T00:00:00Z' }])
    }
    return json({})
  })

  // 后注册的路由优先匹配：api-hub 专属分支放在通用兜底之后
  await page.route('**/api/api-hub/**', route => {
    const request = route.request()
    const path = new URL(request.url()).pathname
    const json = (body: unknown, status = 200) => route.fulfill({
      status,
      contentType: 'application/json',
      body: JSON.stringify(body),
    })

    if (request.method() === 'GET' && path === '/api/api-hub/interfaces') {
      const result = options.listInterfaces?.() ?? { body: [] }
      return json(result.body, result.status ?? 200)
    }
    if (request.method() === 'PUT' && /^\/api\/api-hub\/interfaces\/\d+$/.test(path)) {
      const body = request.postDataJSON() as Record<string, unknown>
      return json(options.onPutInterface?.(body) ?? body)
    }
    if (request.method() === 'POST' && path === '/api/api-hub/interfaces/preview-run/raw') {
      options.onPreviewRunRaw?.()
      return json({ ok: true, status: 200 })
    }
    if (path === '/api/api-hub/proxy/info') {
      return json({ published: [], path: '/proxy', key_header: 'X-API-Hub-Key' })
    }
    if (path === '/api/api-hub/proxy/keys') return json([])
    return json({})
  })
}

async function gotoInterfaces(page: Page) {
  await page.goto('/#/api-hub/interfaces')
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

test('A01 首载失败显示错误面板而非空列表，重试后恢复', async ({ page }) => {
  let listCalls = 0
  await mockApp(page, {
    listInterfaces: () => {
      listCalls += 1
      return listCalls === 1
        ? { status: 500, body: { detail: '数据库连接失败' } }
        : { body: [ORDER_QUERY] }
    },
  })
  await gotoInterfaces(page)

  const alert = page.getByRole('alert')
  await expect(alert).toContainText('接口清单加载失败')
  await expect(alert).toContainText('数据库连接失败')
  await expect(page.getByText('还没有接口')).toHaveCount(0)

  await alert.getByRole('button', { name: '重试' }).click()
  await expect(page.getByText('订单详情')).toBeVisible()
})

test('A02 有未保存修改时发布需先确认，确认后先保存再打开弹窗', async ({ page }) => {
  let savedBody: Record<string, unknown> | null = null
  await mockApp(page, {
    listInterfaces: () => ({ body: [savedBody ?? ORDER_QUERY] }),
    onPutInterface: body => {
      savedBody = { ...body, id: 1, config_revision: 3, updated_at: '2026-09-19T08:05:00.000Z' }
      return savedBody
    },
  })
  await gotoInterfaces(page)

  await expect(page.getByLabel('接口名称')).toHaveValue('订单详情')
  await page.getByLabel('接口名称').fill('订单详情（华东）')
  await page.getByRole('button', { name: 'HTTP 发布' }).click()

  const confirm = page.getByRole('dialog', { name: '当前接口有未保存修改' })
  await expect(confirm).toBeVisible()
  await confirm.getByRole('button', { name: '保存并继续' }).click()

  await expect.poll(() => savedBody?.name).toBe('订单详情（华东）')
  const publication = page.getByRole('dialog', { name: /HTTP 发布 · 订单详情（华东）/ })
  await expect(publication).toBeVisible()
  await expect(publication.getByText(/配置版本 rev 3/)).toBeVisible()
})

test('A03 DELETE 试调按钮差异化且每配置首次调用前确认', async ({ page }) => {
  let previewRawCalls = 0
  await mockApp(page, {
    listInterfaces: () => ({ body: [ORDER_DELETE] }),
    onPreviewRunRaw: () => { previewRawCalls += 1 },
  })
  await gotoInterfaces(page)

  const runButton = page.getByRole('button', { name: '执行 DELETE' })
  await expect(runButton).toBeVisible()
  await runButton.click()

  const confirm = page.getByRole('dialog', { name: '将向真实上游发送 DELETE 请求' })
  await expect(confirm).toBeVisible()
  expect(previewRawCalls).toBe(0)

  await confirm.getByRole('button', { name: '继续调用' }).click()
  await expect.poll(() => previewRawCalls).toBe(1)
})

test('A08 个人变量复制在剪贴板不可用时如实显示失败并给手动兜底', async ({ page }) => {
  await page.addInitScript(() => {
    Object.defineProperty(window.navigator, 'clipboard', { value: undefined, configurable: true })
    document.execCommand = () => false
  })
  await mockApp(page, { listInterfaces: () => ({ body: [ORDER_QUERY] }) })
  await gotoInterfaces(page)

  await page.getByRole('tab', { name: '个人变量' }).click()
  await page.getByRole('button', { name: '复制使用格式' }).first().click()

  await expect(page.getByRole('button', { name: '复制失败' }).first()).toBeVisible()
  await expect(page.getByText('未能写入剪贴板。请手动选择复制：')).toBeVisible()
})

test('A13 调用密钥弹窗在 800×600 视口下标题与关闭按钮不被遮挡', async ({ page }) => {
  await page.setViewportSize({ width: 800, height: 600 })
  await mockApp(page, { listInterfaces: () => ({ body: [ORDER_QUERY] }) })
  await gotoInterfaces(page)

  await page.getByRole('button', { name: '调用密钥' }).click()
  const dialog = page.getByRole('dialog', { name: '调用密钥' })
  await expect(dialog).toBeVisible()

  const box = await dialog.boundingBox()
  expect(box).not.toBeNull()
  expect(box!.y).toBeGreaterThanOrEqual(0)
  expect(box!.y + box!.height).toBeLessThanOrEqual(600)

  await expectTopmost(dialog.getByText('调用密钥', { exact: true }))
  await expectTopmost(dialog.getByLabel('关闭', { exact: true }))
})

test('A14 搜索时显示「显示 x / y 个接口」且匹配请求方法', async ({ page }) => {
  await mockApp(page, { listInterfaces: () => ({ body: [ORDER_QUERY, ORDER_DELETE] }) })
  await gotoInterfaces(page)

  await expect(page.getByText('2 个接口')).toBeVisible()
  const search = page.getByPlaceholder('搜索名称、URL、分组或方法')

  await search.fill('删除')
  await expect(page.getByText('显示 1 / 2 个接口')).toBeVisible()

  await search.fill('delete')
  await expect(page.getByText('显示 1 / 2 个接口')).toBeVisible()

  await search.fill('不存在的接口')
  await expect(page.getByText('显示 0 / 2 个接口')).toBeVisible()
  await page.getByLabel('清除搜索').click()
  await expect(page.getByText('2 个接口')).toBeVisible()
})
