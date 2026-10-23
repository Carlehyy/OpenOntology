import { expect, test, type Page, type Route } from '@playwright/test'


async function mockOntologyDomains(page: Page) {
  await page.addInitScript(() => {
    localStorage.setItem('token', 'domain-source-token')
    localStorage.setItem('auth-store', JSON.stringify({
      state: {
        token: 'domain-source-token',
        user: {
          id: 'domain-source-admin',
          username: 'admin',
          email: 'admin@example.com',
          role: 'admin',
        },
      },
      version: 0,
    }))
  })

  const json = (route: Route, data: unknown) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ data }),
  })
  const ontology = (id: string, name: string, domain: string) => ({
    id,
    name,
    domain,
    description: '',
    icon: 'network',
    version: 'v0',
    current_release_id: `release-${id}`,
    current_release_version: 'v0',
    status: 'published',
    build_mode: 'manual',
    entity_count: 1,
    relation_count: 0,
    action_count: 0,
    sentinel_count: 0,
    created_by: 'domain-source-admin',
    created_at: '2026-08-08T00:00:00Z',
    updated_at: '2026-08-08T00:00:00Z',
  })

  await page.route(/^https?:\/\/[^/]+\/api\/v[12]\//, async (route) => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/v1/ontologies') {
      return json(route, {
        items: [
          ontology('configured', '配置领域本体', '配置领域'),
          ontology('orphaned', '孤儿领域本体', '孤儿领域'),
        ],
        total: 2,
        page: 1,
        page_size: 1000,
      })
    }
    if (path === '/api/v1/domains') {
      return json(route, [{
        id: 'domain-configured',
        name: '配置领域',
        description: '系统设置中的权威领域',
      }])
    }
    if (path === '/api/v2/inbox/summary') {
      return json(route, {
        openAlertCount: 0,
        actionableCount: 0,
        unreadCount: 0,
        resolvedCount: 0,
      })
    }
    return json(route, [])
  })
}


test('本体领域筛选只使用系统设置领域，不合并本体孤儿值', async ({ page }) => {
  await mockOntologyDomains(page)
  await page.goto('/#/ontologies', { waitUntil: 'domcontentloaded' })

  const filter = page.getByLabel('按所属领域筛选', { exact: true })
  await expect(filter).toBeVisible()
  await filter.click()
  // Radix Select 展开时会把面板外内容置为 aria-hidden，卡片断言前先收起面板
  const listbox = page.getByRole('listbox')
  await expect(listbox.getByRole('option', { name: '配置领域', exact: true })).toHaveCount(1)
  await expect(listbox.getByRole('option', { name: '孤儿领域', exact: true })).toHaveCount(0)
  await page.keyboard.press('Escape')
  await expect(page.getByRole('button', { name: '孤儿领域本体', exact: true })).toBeVisible()

  await filter.click()
  await page.getByRole('listbox').getByRole('option', { name: '配置领域', exact: true }).click()
  await expect(page.getByRole('button', { name: '配置领域本体', exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: '孤儿领域本体', exact: true })).toHaveCount(0)
})

test('领域筛选结果为 0 条时新建本体卡保持原有尺寸', async ({ page }) => {
  await mockOntologyDomains(page)
  // 后注册的 route 优先：领域列表追加一个没有任何本体命中的「空领域」
  await page.route('**/api/v1/domains*', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({
      data: [
        { id: 'domain-configured', name: '配置领域', description: '系统设置中的权威领域' },
        { id: 'domain-empty', name: '空领域', description: '没有任何本体命中的领域' },
      ],
    }),
  }))

  await page.goto('/#/ontologies', { waitUntil: 'domcontentloaded' })
  await expect(page.getByRole('button', { name: '配置领域本体', exact: true })).toBeVisible()

  const createCard = page.getByRole('article').filter({ hasText: '新建本体' })
  const before = await createCard.boundingBox()

  const filter = page.getByLabel('按所属领域筛选', { exact: true })
  await filter.click()
  await page.getByRole('listbox').getByRole('option', { name: '空领域', exact: true }).click()
  await expect(page.getByText('没有符合条件的本体')).toBeVisible()
  await expect(page.getByRole('button', { name: '配置领域本体', exact: true })).toHaveCount(0)

  // 回归：0 结果空态与「新建本体」卡同处一个网格行，空态块最小高度须与
  // 卡片一致（256px），否则 Grid stretch 会把卡片从 256px 拉高到 300px。
  // 阈值 8px 只吸收常态下相邻结果卡内容高度的同行等高拉伸（实测 ~0.5px），
  // 远小于回归幅度
  const after = await createCard.boundingBox()
  const heightDelta = Math.abs((after?.height ?? 0) - (before?.height ?? 0))
  expect(heightDelta, '领域筛选 0 条时新建本体卡不得被空态撑大').toBeLessThan(8)
})

test('空库与筛选 0 结果的空态块都与新建本体卡同行且位于其右侧', async ({ page }) => {
  await mockOntologyDomains(page)
  // 后注册的 route 优先：本体列表置空，构造「一个本体也没有」的空库态
  await page.route('**/api/v1/ontologies*', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ data: { items: [], total: 0, page: 1, page_size: 1000 } }),
  }))

  await page.goto('/#/ontologies', { waitUntil: 'domcontentloaded' })
  await expect(page.getByText('还没有创建本体')).toBeVisible()
  await expect(page.getByText('点击左侧卡片创建第一个本体')).toBeVisible()

  const createCard = page.getByRole('article').filter({ hasText: '新建本体' })
  const emptyBlock = page.getByTestId('ontology-list-empty')
  const card = await createCard.boundingBox()
  const empty = await emptyBlock.boundingBox()

  // 介绍块须与卡片同处一行（顶边对齐），且起点在卡片右边缘之外——
  // 独占下一整行的旧布局会顶边低于卡片且与卡片左对齐
  expect(Math.abs((empty?.y ?? 0) - (card?.y ?? 0)), '空态块须与新建本体卡同行').toBeLessThan(8)
  expect((empty?.x ?? 0), '空态块须位于新建本体卡右侧').toBeGreaterThanOrEqual((card?.x ?? 0) + (card?.width ?? 0))

  // 名称筛选 0 结果的空态分支与空库共用同一块 DOM，同样须同行靠右
  await page.getByLabel('按本体名称或描述筛选', { exact: true }).fill('不存在的本体xyz')
  await expect(page.getByText('没有符合条件的本体')).toBeVisible()
  const cardFiltered = await createCard.boundingBox()
  const emptyFiltered = await emptyBlock.boundingBox()
  expect(Math.abs((emptyFiltered?.y ?? 0) - (cardFiltered?.y ?? 0)), '筛选 0 结果空态块须与新建本体卡同行').toBeLessThan(8)
  expect((emptyFiltered?.x ?? 0), '筛选 0 结果空态块须位于新建本体卡右侧').toBeGreaterThanOrEqual((cardFiltered?.x ?? 0) + (cardFiltered?.width ?? 0))
})
