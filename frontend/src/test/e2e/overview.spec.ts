import { expect, test, type Page, type Route } from '@playwright/test'

const overviewStats = {
  ontology_count: 7,
  entity_count: 42,
  relation_count: 18,
  logic_count: 5,
  action_count: 4,
  rule_hits: 256,
  recent_ontologies: [{
    id: 101,
    name: '采购域本体（验证）',
    domain: '供应链',
    status: 'published',
    entity_count: 42,
    logic_count: 5,
    action_count: 4,
    updated_at: '2026-07-30T08:00:00+00:00',
  }],
  domain_counts: { 供应链: 4, 制造: 3 },
  status_counts: { published: 5, draft: 2 },
}

const json = (route: Route, data: unknown, status = 200) => route.fulfill({
  status,
  contentType: 'application/json',
  body: JSON.stringify({ data }),
})

async function mockOverview(
  page: Page,
  options: {
    role?: 'admin' | 'custom'
    menuPermissions?: string[]
  } = {},
) {
  const role = options.role ?? 'custom'
  const menuPermissions = options.menuPermissions ?? ['overview']
  let statsRequests = 0

  await page.addInitScript(({ currentRole, permissions }) => {
    const user = {
      id: 'overview-e2e-user',
      username: 'overview-e2e',
      email: 'overview-e2e@example.com',
      role: currentRole,
      is_active: true,
      created_at: '2026-07-30T08:00:00+00:00',
      menu_permissions: permissions,
    }
    localStorage.setItem('token', 'overview-e2e-token')
    localStorage.setItem('auth-store', JSON.stringify({
      state: { token: 'overview-e2e-token', user },
      version: 0,
    }))
  }, { currentRole: role, permissions: menuPermissions })

  await page.route('**/api/**', route => {
    const path = new URL(route.request().url()).pathname
    if (!path.startsWith('/api/')) return route.continue()
    if (path === '/api/v1/overview/stats') {
      statsRequests += 1
      return json(route, overviewStats)
    }
    if (path === '/api/v2/inbox/summary') {
      return json(route, {
        openAlertCount: 0,
        actionableCount: 0,
        unreadCount: 0,
        resolvedCount: 0,
      })
    }
    if (path === '/api/v1/ontologies') {
      return json(route, { items: [], total: 0, page: 1, page_size: 20 })
    }
    return json(route, [])
  })

  return {
    statsRequestCount: () => statsRequests,
  }
}

test.describe('平台概览迁移契约', () => {
  test('custom 用户可从 Hash 深链恢复概览并渲染后端统计', async ({ page }) => {
    const api = await mockOverview(page)
    await page.setViewportSize({ width: 1440, height: 900 })

    await page.goto('/#/overview')

    await expect(page).toHaveURL(/\/#\/overview$/)
    await expect(page.getByRole('heading', { name: '本体治理中枢' })).toBeVisible()
    await expect.poll(api.statsRequestCount).toBe(1)
    await expect(page.getByText('采购域本体（验证）', { exact: true })).toBeVisible()
    await expect(page.getByTestId('overview-flow-stage-model')).toContainText('7')
    await expect(page.getByTestId('overview-flow-stage-graph')).toContainText('42')

    const navigation = page.getByRole('navigation')
    await expect(navigation.getByRole('link', { name: '平台概览' })).toHaveCount(0)
    await expect(navigation.getByText('本体管理', { exact: true })).toHaveCount(0)
    await expect(navigation.getByText('系统设置', { exact: true })).toHaveCount(0)

    await page.reload()
    await expect(page).toHaveURL(/\/#\/overview$/)
    await expect(page.getByRole('heading', { name: '本体治理中枢' })).toBeVisible()

    await page.goto('/#/ontologies')
    await expect(page.getByRole('heading', { name: '当前页面无法访问' })).toBeVisible()
    await page.getByRole('button', { name: '返回上一级' }).click()
    await expect(page).toHaveURL(/\/#\/overview$/)
  })

  test('管理员可从概览进入本体建模流程', async ({ page }) => {
    await mockOverview(page, { role: 'admin' })
    await page.setViewportSize({ width: 1440, height: 900 })

    await page.goto('/#/overview')
    await expect(page.getByRole('heading', { name: '本体治理中枢' })).toBeVisible()
    const navigation = page.getByRole('navigation')
    await expect(navigation.getByRole('link', { name: '平台概览' })).toHaveCount(0)
    // 超级助手已在左栏露出（三维场景已暂时隐藏出导航），平台概览仍保持隐藏
    await expect(navigation.getByRole('link', { name: '超级助手' })).toBeVisible()

    await page.getByTestId('overview-flow-stage-model').click()
    await expect(page).toHaveURL(/\/#\/ontologies$/)
  })

  test('管理员可从概览进入待办审批流程', async ({ page }) => {
    await mockOverview(page, { role: 'admin' })
    await page.setViewportSize({ width: 1440, height: 900 })

    await page.goto('/#/overview')
    await expect(page.getByRole('heading', { name: '本体治理中枢' })).toBeVisible()

    await page.getByTestId('overview-kpi-approvals').click()
    await expect(page).toHaveURL(/\/#\/data\/pipelines\/steward$/)
  })

  test('未分配菜单的 custom 用户进入无可访问页面状态', async ({ page }) => {
    await mockOverview(page, { role: 'custom', menuPermissions: [] })

    await page.goto('/#/')
    await expect(page).toHaveURL(/\/#\/no-access$/)
    await expect(page.getByRole('heading', { name: '暂未分配可访问页面' })).toBeVisible()
    await expect(page.getByRole('navigation').getByRole('link')).toHaveCount(0)

    await page.goto('/#/overview')
    await expect(page.getByRole('heading', { name: '当前页面无法访问' })).toBeVisible()
    await page.getByRole('button', { name: '返回上一级' }).click()
    await expect(page).toHaveURL(/\/#\/no-access$/)
    await expect(page.getByRole('heading', { name: '暂未分配可访问页面' })).toBeVisible()
  })

  test('旧 /rag 深链继续重定向到本体助手', async ({ page }) => {
    await mockOverview(page, { role: 'admin' })

    await page.goto('/#/rag')
    await expect(page).toHaveURL(/\/#\/agent$/)
  })
})
