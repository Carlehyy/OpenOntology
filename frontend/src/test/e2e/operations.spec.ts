import { expect, test, type Page, type Route } from '@playwright/test'


const json = (route: Route, data: unknown) => route.fulfill({
  status: 200,
  contentType: 'application/json',
  body: JSON.stringify({ data }),
})

async function mockSession(page: Page, user: Record<string, unknown>) {
  await page.addInitScript(sessionUser => {
    localStorage.setItem('token', 'operations-e2e-token')
    localStorage.setItem('auth-store', JSON.stringify({
      state: { token: 'operations-e2e-token', user: sessionUser },
      version: 0,
    }))
  }, user)
  // 占位页不发业务请求；壳层请求（收件箱角标等）一律本地兜底
  await page.route('**/api/**', route => {
    const path = new URL(route.request().url()).pathname
    if (!path.startsWith('/api/')) return route.continue()
    if (path === '/api/v2/inbox/summary') {
      return json(route, { openAlertCount: 0, actionableCount: 0, unreadCount: 0, resolvedCount: 0 })
    }
    return json(route, [])
  })
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

test('智能运营：一级导航位于数据集成与事件登记之间，两个子页为待开发占位', async ({ page }) => {
  await mockSession(page, admin)
  await page.setViewportSize({ width: 1440, height: 900 })

  // 分组根路径落到第一个子页
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

  const tabList = page.getByRole('tablist', { name: '页面标签' })
  await expect(tabList.getByRole('tab', { name: '智能运营 · 监控看板' })).toHaveAttribute('aria-selected', 'true')

  await nav.getByRole('link', { name: '运营报告', exact: true }).click()
  await expect(page).toHaveURL(/\/#\/operations\/reports$/)
  await expect(page.getByRole('heading', { name: '运营报告即将上线' })).toBeVisible()
  await expect(tabList.getByRole('tab', { name: '智能运营 · 运营报告' })).toHaveAttribute('aria-selected', 'true')

  // Hash 深链刷新后停留在原占位页
  await page.reload()
  await expect(page).toHaveURL(/\/#\/operations\/reports$/)
  await expect(page.getByRole('heading', { name: '运营报告即将上线' })).toBeVisible()

  // 收展行为与其他一级分组一致
  await operationsGroup.click()
  await expect(operationsGroup).toHaveAttribute('aria-expanded', 'false')
  await expect(nav.getByRole('link', { name: '监控看板', exact: true })).toHaveCount(0)
  await operationsGroup.click()
  await expect(operationsGroup).toHaveAttribute('aria-expanded', 'true')
  await nav.getByRole('link', { name: '监控看板', exact: true }).click()
  await expect(page).toHaveURL(/\/#\/operations\/dashboard$/)
  await expect(page.getByRole('heading', { name: '监控看板即将上线' })).toBeVisible()
})

test('智能运营：已授权的非管理员可见入口并进入占位页', async ({ page }) => {
  await mockSession(page, nonAdmin('editor', ['overview', 'operations', 'operations.dashboard', 'operations.reports']))
  await page.setViewportSize({ width: 1440, height: 900 })

  await page.goto('/#/operations/reports')
  await expect(page.getByRole('heading', { name: '运营报告即将上线' })).toBeVisible()
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
