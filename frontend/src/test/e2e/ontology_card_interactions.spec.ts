import { expect, test, type Page, type Route } from '@playwright/test'

/**
 * 本体管理卡片交互（MYW-77）：
 * ①「建模」按钮进入本体详情并打开「本体版本演进」弹窗；本体描述跳转本体详情；
 * ②右下角时间带「更新于/创建于」前缀；
 * ③拖拽卡片改变相对位置并经 localStorage 持久化，筛选态禁用拖拽。
 */
const ok = (route: Route, data: unknown) => route.fulfill({
  status: 200,
  contentType: 'application/json',
  body: JSON.stringify({ data, message: 'ok' }),
})

const ontology = (id: string, name: string, createdAt: string, updatedAt: string) => ({
  id,
  name,
  domain: '供应链',
  description: `${name}的描述`,
  icon: 'network',
  version: 'v0',
  current_release_id: null,
  current_release_version: null,
  status: 'draft',
  entity_count: 1,
  relation_count: 0,
  action_count: 0,
  sentinel_count: 0,
  created_by: 'card-user',
  created_at: createdAt,
  updated_at: updatedAt,
})

const ITEMS = [
  ontology('ont-alpha', '阿尔法本体', '2026-08-01T08:00:00Z', '2026-08-05T08:00:00Z'),
  ontology('ont-beta', '贝塔本体', '2026-08-02T08:00:00Z', '2026-08-02T08:00:00Z'),
  ontology('ont-gamma', '伽马本体', '2026-08-03T08:00:00Z', '2026-08-04T08:00:00Z'),
]

const VERSION_TREE = {
  current_release_id: 'rel-alpha-1',
  current_release_number: 'v1',
  current_release_version: 'v1',
  versions: [{
    id: 'ver-alpha-1',
    version_number: 'v1',
    node_kind: 'release',
    lifecycle_status: 'released',
    revision: 1,
    created_at: '2026-08-05T08:00:00Z',
    published_at: '2026-08-05T08:00:00Z',
  }],
}

const FORMAL_OVERVIEW = {
  release: { id: 'rel-alpha-1', version: 'v1', publishedAt: '2026-08-05T08:00:00Z' },
  model: {
    objectTypes: 1,
    linkTypes: 0,
    actions: 1,
    actionsRequiringApproval: 0,
    functions: 0,
    sentinels: { total: 0, enabled: 0, muted: 0 },
  },
  data: {
    instances: 0,
    instancesBySource: {},
    linkInstances: 0,
    mappings: { total: 0, bound: 0, nameMatch: 0, autoCreate: 0, autoApply: 0 },
    topTypes: [],
  },
  runtime: {
    pendingApprovals: 0,
    decisions: { total: 0, approved: 0, rejected: 0, recentApprovalRate: null },
    firings7d: { total: 0, fired: 0, error: 0 },
    actionRuns7d: { total: 0, success: 0, failed: 0 },
    daily7d: [],
  },
  facts: { total: 0, byKind: {} },
}

async function mockListPage(page: Page) {
  await page.addInitScript(() => {
    localStorage.setItem('token', 'card-interactions-token')
    localStorage.setItem('auth-store', JSON.stringify({
      state: {
        token: 'card-interactions-token',
        user: { id: 'card-user', username: 'card-user', role: 'admin' },
      },
      version: 0,
    }))
  })
  await page.route(/^https?:\/\/[^/]+\/api\/v[12]\//, async route => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/v1/ontologies') return ok(route, { items: ITEMS, total: ITEMS.length, page: 1, page_size: 1000 })
    if (path === '/api/v1/domains') return ok(route, [{ id: 'domain-1', name: '供应链', description: '' }])
    if (path === '/api/v1/ontologies/ont-alpha') return ok(route, ITEMS[0])
    if (path === '/api/v2/ontologies/ont-alpha/version-tree') return ok(route, VERSION_TREE)
    // 详情页总览面板依赖 formal overview 的对象形状，catch-all 的 [] 会使其
    // 渲染崩溃白屏，进而连带卸载已打开的版本演进弹窗，必须按形状下发。
    if (path === '/api/v2/formal/ontologies/ont-alpha/overview') return ok(route, FORMAL_OVERVIEW)
    if (path === '/api/v2/formal/ontologies/ont-alpha/facts/recent') return ok(route, [])
    if (path === '/api/v2/inbox/summary') {
      return ok(route, { openAlertCount: 0, actionableCount: 0, unreadCount: 0, resolvedCount: 0 })
    }
    return ok(route, [])
  })
}

async function cardNames(page: Page): Promise<string[]> {
  return page.getByTestId('ontology-card')
    .evaluateAll(cards => cards.map(card => card.getAttribute('data-ontology-id') || ''))
}

test('卡片「建模」按钮进入详情页并打开本体版本演进弹窗', async ({ page }) => {
  await mockListPage(page)
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto('/#/ontologies', { waitUntil: 'domcontentloaded' })
  await expect(page.getByRole('button', { name: '阿尔法本体', exact: true })).toBeVisible()

  await page.getByRole('button', { name: '进入本体 阿尔法本体 版本建模' }).click()
  await expect(page).toHaveURL(/#\/ontologies\/ont-alpha\?tab=versions$/)
  await expect(page.getByRole('dialog', { name: '本体版本演进' })).toBeVisible()
  await expect(page.getByTestId('version-tree')).toBeVisible()
})

test('点击卡片本体描述跳转本体详情页', async ({ page }) => {
  await mockListPage(page)
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto('/#/ontologies', { waitUntil: 'domcontentloaded' })
  const alphaCard = page.getByTestId('ontology-card').filter({ hasText: '阿尔法本体' })
  await expect(alphaCard).toBeVisible()

  await alphaCard.getByRole('button', { name: '阿尔法本体的描述', exact: true }).click()
  await expect(page).toHaveURL(/#\/ontologies\/ont-alpha$/)
})

test('卡片时间标注区分更新时间与创建时间', async ({ page }) => {
  await mockListPage(page)
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto('/#/ontologies', { waitUntil: 'domcontentloaded' })
  await expect(page.getByRole('button', { name: '阿尔法本体', exact: true })).toBeVisible()

  // 阿尔法：更新时间晚于创建时间 → 「更新于」；贝塔：两者相同 → 「创建于」
  await expect(page.getByTestId('ontology-card').filter({ hasText: '阿尔法本体' }).getByTestId('ontology-card-time')).toContainText('更新于')
  await expect(page.getByTestId('ontology-card').filter({ hasText: '贝塔本体' }).getByTestId('ontology-card-time')).toContainText('创建于')
  await expect(page.getByTestId('ontology-card').filter({ hasText: '伽马本体' }).getByTestId('ontology-card-time')).toContainText('更新于')
})

test('领域筛选下拉保持紧凑宽度，不占满筛选条', async ({ page }) => {
  await mockListPage(page)
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto('/#/ontologies', { waitUntil: 'domcontentloaded' })
  await expect(page.getByRole('button', { name: '阿尔法本体', exact: true })).toBeVisible()

  // reUI SelectTrigger 默认 w-full 且 Radix Root 无 DOM 包裹：筛选条内的
  // 下拉必须显式定宽，否则以整行为基准伸展（回归时在 1440 视口实测 ~1300px）
  const filter = page.getByRole('combobox', { name: '按所属领域筛选' })
  const box = await filter.boundingBox()
  expect(box).not.toBeNull()
  expect(box!.width).toBeGreaterThanOrEqual(144)
  expect(box!.width).toBeLessThanOrEqual(220)

  // 与旁边搜索框同款白底（而非 bg-background 画布灰）
  const background = await filter.evaluate(el => getComputedStyle(el).backgroundColor)
  expect(background).toBe('rgb(255, 255, 255)')
})

test('拖拽卡片改变相对位置并持久化，筛选态禁用拖拽', async ({ page }) => {
  await mockListPage(page)
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto('/#/ontologies', { waitUntil: 'domcontentloaded' })
  await expect(page.getByRole('button', { name: '阿尔法本体', exact: true })).toBeVisible()

  // 无手动序时按创建时间倒序：伽马 → 贝塔 → 阿尔法
  expect(await cardNames(page)).toEqual(['ont-gamma', 'ont-beta', 'ont-alpha'])

  const alphaCard = page.getByTestId('ontology-card').filter({ hasText: '阿尔法本体' })
  const gammaCard = page.getByTestId('ontology-card').filter({ hasText: '伽马本体' })

  // 把「阿尔法」拖到「伽马」左半区（before 落位）→ 阿尔法排最前
  await alphaCard.dragTo(gammaCard, { targetPosition: { x: 10, y: 40 } })
  await expect.poll(() => cardNames(page)).toEqual(['ont-alpha', 'ont-gamma', 'ont-beta'])
  // 快照已写入 localStorage
  const saved = await page.evaluate(() => localStorage.getItem('ontology-card-order:v1'))
  expect(saved).toContain('ont-alpha')

  // 刷新后顺序保持
  await page.reload({ waitUntil: 'domcontentloaded' })
  await expect(page.getByRole('button', { name: '阿尔法本体', exact: true })).toBeVisible()
  await expect.poll(() => cardNames(page)).toEqual(['ont-alpha', 'ont-gamma', 'ont-beta'])

  // 筛选激活时卡片不可拖拽（draggable=false），避免部分可见列表上落位歧义
  await page.getByLabel('按本体名称或描述筛选', { exact: true }).fill('本体')
  await expect(page.getByTestId('ontology-card').filter({ hasText: '阿尔法本体' })).toHaveAttribute('draggable', 'false')
})
