import { expect, test, type Page, type Route } from '@playwright/test'

/**
 * 连接管理页回归（商业化审查 D-006 / D-005 / D-004）：
 * - 删除被同步数据集引用的连接：409 依赖明细展示在确认弹窗内，弹窗不关闭、卡片仍在
 * - 同名连接：前端拦截并提示（后端另有 409 兜底）
 * - 同步成功后提供「查看数据集」入口，跳转数据流水线数据集页签
 */

const CONNECTIONS = [
  { id: 'conn-1', name: 'ERP 订单库', kind: 'mysql', status: 'active' },
]

async function mockConnections(page: Page, options?: {
  deleteStatus?: number
  deleteBody?: unknown
}) {
  await page.addInitScript(() => {
    localStorage.setItem('token', 'e2e-token')
    localStorage.setItem('auth-store', JSON.stringify({
      state: { token: 'e2e-token', user: { id: 'u1', username: 'tester', role: 'admin' } },
      version: 0,
    }))
  })
  const live = new Map(CONNECTIONS.map(item => [item.id, { ...item }]))
  await page.route('**/api/**', async (route: Route) => {
    const url = new URL(route.request().url())
    if (!url.pathname.startsWith('/api/')) return route.continue()
    const ok = (data: unknown) => route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ data, message: 'ok' }),
    })

    if (url.pathname === '/api/v2/connections' && route.request().method() === 'GET') {
      return ok([...live.values()])
    }
    if (url.pathname === '/api/v2/connections' && route.request().method() === 'POST') {
      return route.fulfill({
        status: 409,
        contentType: 'application/json',
        body: JSON.stringify({ detail: '已存在同名连接「ERP 订单库」，请更换连接名称' }),
      })
    }
    if (url.pathname === '/api/v2/connections/conn-1' && route.request().method() === 'DELETE') {
      const status = options?.deleteStatus ?? 409
      if (status < 300) {
        live.delete('conn-1')
        // 真实 FastAPI 204 无响应体；带 "null" body 会让 axios 解析出 null
        return route.fulfill({ status })
      }
      return route.fulfill({
        status,
        contentType: 'application/json',
        body: JSON.stringify(options?.deleteBody ?? {
          detail: {
            message: '连接被 2 个同步数据集引用，删除前请先在「数据资产」中删除这些数据集',
            datasets: [
              { id: 'ds-1', name: '同步数据集-订单', kind: 'structured' },
              { id: 'ds-2', name: '同步数据集-客户', kind: 'structured' },
            ],
            total: 2,
          },
        }),
      })
    }
    if (url.pathname === '/api/v2/connections/conn-1/sync'
        && route.request().method() === 'POST') {
      return ok({ status: 'ok', rows: 10, dataset_id: 'ds-1', version_no: 1 })
    }
    return ok({})
  })
}

test('删除被同步数据集引用的连接：弹窗内展示 409 依赖明细且卡片保留', async ({ page }) => {
  await mockConnections(page)
  await page.goto('/#/data/pipelines/connections', { waitUntil: 'domcontentloaded' })

  await expect(page.getByText('ERP 订单库', { exact: true })).toBeVisible()
  await page.getByRole('button', { name: '删除' }).click()

  const dialog = page.getByRole('dialog')
  await expect(dialog).toBeVisible()
  await dialog.getByRole('button', { name: '确认删除' }).click()

  await expect(dialog.getByText(/连接被 2 个同步数据集引用/)).toBeVisible()
  await expect(dialog.getByText(/同步数据集-订单、同步数据集-客户/)).toBeVisible()
  // 删除被拒：确认弹窗保持打开（用户可读错误后取消），连接卡片不动
  await expect(page.getByText('ERP 订单库', { exact: true })).toBeVisible()
})

test('删除依赖解除后返回 204：弹窗关闭且卡片消失', async ({ page }) => {
  await mockConnections(page, { deleteStatus: 204 })
  await page.goto('/#/data/pipelines/connections', { waitUntil: 'domcontentloaded' })

  await page.getByRole('button', { name: '删除' }).click()
  await page.getByRole('dialog').getByRole('button', { name: '确认删除' }).click()

  await expect(page.getByText('ERP 订单库', { exact: true })).toHaveCount(0)
})

test('同名连接被前端拦截并给出明确提示', async ({ page }) => {
  await mockConnections(page)
  await page.goto('/#/data/pipelines/connections', { waitUntil: 'domcontentloaded' })

  await page.getByRole('button', { name: '新建连接' }).click()
  await page.getByPlaceholder('例：ERP 订单数据库').fill('ERP 订单库')
  await page.getByRole('button', { name: '保存', exact: true }).click()

  await expect(page.getByText('已存在同名连接「ERP 订单库」，请更换连接名称')).toBeVisible()
})

test('同步成功后提供查看数据集入口并跳转数据集页签', async ({ page }) => {
  await mockConnections(page)
  await page.goto('/#/data/pipelines/connections', { waitUntil: 'domcontentloaded' })

  await page.getByRole('button', { name: '同步' }).click()
  await expect(page.getByText('同步成功，共 10 行')).toBeVisible()

  await page.getByRole('button', { name: '查看数据集' }).click()
  await expect(page).toHaveURL(/#\/data\/pipelines\/datasets/)
})
