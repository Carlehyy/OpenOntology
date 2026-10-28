import { expect, test, type Page, type Route } from '@playwright/test'

const now = '2026-08-10T08:00:00+00:00'

const released = (id: string, name: string, clicks: number, domain = '供应链', sentinelCount = 0) => ({
  id,
  name,
  domain,
  description: `${name}的描述`,
  status: 'draft',
  version: 'v1',
  current_release_id: `release-${id}`,
  current_release_version: 'v1',
  assistant_card_clicks: clicks,
  entity_count: 2,
  relation_count: 1,
  action_count: 1,
  sentinel_count: sentinelCount,
  created_at: now,
  updated_at: now,
})

const ontologies = [
  released('ontology-1', '供应链本体', 7, '供应链', 6),
  released('ontology-2', '医疗健康本体', 3, '医疗'),
  released('ontology-3', '法律知识本体', 0, '法律'),
  {
    id: 'ontology-unreleased',
    name: '未发布本体',
    domain: '供应链',
    description: '没有当前发布版本',
    status: 'draft',
    version: 'v1',
    current_release_id: null,
    current_release_version: null,
    assistant_card_clicks: 99,
    created_at: now,
    updated_at: now,
  },
]

async function mockCarousel(page: Page, options: { items?: unknown[] } = {}) {
  const clickCalls: string[] = []
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

  const json = (route: Route, data: unknown) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ data }),
  })

  await page.route('**/api/v1/**', route => {
    const path = new URL(route.request().url()).pathname
    const clickMatch = path.match(/^\/api\/v1\/ontologies\/([\w-]+)\/assistant-card-clicks$/)
    if (clickMatch && route.request().method() === 'POST') {
      clickCalls.push(clickMatch[1])
      return json(route, { id: clickMatch[1], assistant_card_clicks: 1 })
    }
    if (path === '/api/v1/ontologies') return json(route, {
      items: options.items ?? ontologies,
      total: (options.items ?? ontologies).length,
      page: 1,
      page_size: 1000,
    })
    if (path === '/api/v1/domains') return json(route, [])
    if (path === '/api/v1/models') return json(route, [])
    return route.fallback()
  })

  await page.route('**/api/v2/**', route => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/v2/inbox/summary') return json(route, { unread_count: 0 })
    const workspaceMatch = path.match(/^\/api\/v2\/ontologies\/(ontology-[12])\/versions\/release-\1\/workspace$/)
      || path.match(/^\/api\/v2\/formal\/ontologies\/(ontology-[12])\/full$/)
    if (workspaceMatch) return json(route, {
      id: workspaceMatch[1],
      name: '工作区本体',
      version: 'v1',
      workspaceMode: 'release',
      objectTypes: [],
      linkTypes: [],
      actions: [],
      functions: [],
      instances: [],
      linkInstances: [],
      executionLogs: [],
    })
    const agentMatch = path.match(/^\/api\/v2\/formal\/ontologies\/(ontology-[12])\/agent\/([\w-]+)$/)
    if (agentMatch) {
      if (agentMatch[2] === 'capabilities') return json(route, {
        enabled: true, objectTypes: [], linkTypes: [], actions: [],
        allowActionProposals: true, maxRowsPerQuery: 50, maxSteps: 8,
        skillCard: '', releaseId: `release-${agentMatch[1]}`, releaseVersion: 'v1',
      })
      if (agentMatch[2] === 'profile') return json(route, {
        id: 'profile-1', ontologyId: agentMatch[1], enabled: true,
        allowedObjectTypeIds: null, allowedLinkTypeIds: null, allowedActionIds: [],
        allowActionProposals: true, maxRowsPerQuery: 50, maxSteps: 8,
        systemPromptExtra: '', defaultModelId: null, updatedAt: now,
      })
      if (agentMatch[2] === 'conversations') return json(route, [])
      if (agentMatch[2] === 'decision-simulations') return json(route, [])
      if (agentMatch[2] === 'dynamic-sentinels') return json(route, [])
    }
    return route.fallback()
  })

  return { clickCalls }
}

test('未选择本体时展示卡片轮播并按选用次数排序', async ({ page }) => {
  await mockCarousel(page)
  // 宽视口下右侧面板约 818px：两侧各完整展示一张卡（侧卡位置断言依赖可见性）。
  await page.setViewportSize({ width: 1600, height: 900 })
  await page.goto('/#/agent')

  const carousel = page.getByTestId('ontology-card-carousel')
  await expect(carousel).toBeVisible()
  await expect(carousel.getByRole('option')).toHaveCount(3)
  await expect(carousel.getByText('未发布本体')).toHaveCount(0)

  await expect(page.locator('[data-card-index="0"]')).toContainText('供应链本体')
  await expect(page.locator('[data-card-index="1"]')).toContainText('医疗健康本体')
  await expect(page.locator('[data-card-index="2"]')).toContainText('法律知识本体')
  await expect(page.locator('[data-card-index="0"]')).toContainText('×7')
  await expect(page.locator('[data-card-index="0"]')).toContainText('哨兵引擎')
  await expect(page.locator('[data-card-index="2"]').getByText('×0')).toHaveCount(0)

  // 统计区：对象实体/实体关系/执行动作/哨兵引擎四格一行
  const focusedCard = page.locator('[data-card-index="0"]')
  await expect(focusedCard.getByText('对象实体')).toBeVisible()
  await expect(focusedCard.getByText('实体关系')).toBeVisible()
  await expect(focusedCard.getByText('执行动作')).toBeVisible()
  await expect(focusedCard.getByText('哨兵引擎')).toBeVisible()
  await expect(focusedCard.getByText('哨兵引擎').locator('..')).toContainText('6')

  // 环形均匀布局：最热卡居中，次热在右，最冷卡环绕到左侧。
  const centerBox = await page.locator('[data-card-index="0"]').boundingBox()
  const rightBox = await page.locator('[data-card-index="1"]').boundingBox()
  const leftBox = await page.locator('[data-card-index="2"]').boundingBox()
  if (!centerBox || !rightBox || !leftBox) throw new Error('card bounding box missing')
  expect(rightBox.x).toBeGreaterThan(centerBox.x)
  expect(leftBox.x).toBeLessThan(centerBox.x)
})

test('长名称本体截断为省略号，且与点击次数徽章分行不重叠', async ({ page }) => {
  await mockCarousel(page, {
    items: [
      released('ontology-1', 'E2E-供应链风险-0726075610-本体', 7),
      released('ontology-2', '医疗健康本体', 3, '医疗'),
      released('ontology-3', '法律知识本体', 1, '法律'),
    ],
  })
  await page.setViewportSize({ width: 1280, height: 900 })
  await page.goto('/#/agent')

  const card = page.locator('[data-card-index="0"]')
  const name = card.locator('div[title="E2E-供应链风险-0726075610-本体"]')
  await expect(name).toBeVisible()
  // 名称超长时在行内截断为省略号（完整名经 title 悬浮可见）
  const truncated = await name.evaluate(el => el.scrollWidth > el.clientWidth)
  expect(truncated).toBe(true)

  // 点击次数徽章位于版本徽标同行的右侧，与名称分行，矩形不相交
  const badge = card.getByText('×7')
  await expect(badge).toBeVisible()
  const nameBox = await name.boundingBox()
  const badgeBox = await badge.boundingBox()
  if (!nameBox || !badgeBox) throw new Error('bounding box missing')
  const intersects = nameBox.x < badgeBox.x + badgeBox.width
    && badgeBox.x < nameBox.x + nameBox.width
    && nameBox.y < badgeBox.y + badgeBox.height
    && badgeBox.y < nameBox.y + nameBox.height
  expect(intersects).toBe(false)
})

test('点击侧边卡片仅聚焦，点击聚焦卡片才选中并计数', async ({ page }) => {
  const { clickCalls } = await mockCarousel(page)
  // 宽视口保证侧边卡片完整可见、可点击。
  await page.setViewportSize({ width: 1600, height: 900 })
  await page.goto('/#/agent')

  const sideCard = page.locator('[data-card-index="1"]')
  // 侧边卡片中心被聚焦卡片覆盖，点击其可见边缘区域。
  await sideCard.click({ position: { x: 250, y: 140 } })
  await expect(page).toHaveURL(/\/#\/agent$/)
  await expect(sideCard).toHaveAttribute('aria-selected', 'true')
  expect(clickCalls).toHaveLength(0)

  await sideCard.click()
  await expect(page).toHaveURL(/\/#\/agent\?ontology_id=ontology-2$/)
  await expect.poll(() => clickCalls).toEqual(['ontology-2'])
  await expect(page.getByTestId('agent-ontology-panel')).toContainText('当前本体暂无可视化对象')
  await expect(page.getByTestId('ontology-card-carousel')).toHaveCount(0)
})

test('箭头按钮与拖拽可以切换聚焦卡片，且支持无限循环', async ({ page }) => {
  await mockCarousel(page)
  await page.setViewportSize({ width: 1280, height: 900 })
  await page.goto('/#/agent')

  const prevButton = page.getByLabel('上一张本体卡片')
  const nextButton = page.getByLabel('下一张本体卡片')

  const stage = page.getByTestId('ontology-card-carousel')
  const box = await stage.boundingBox()
  if (!box) throw new Error('carousel stage not visible')
  const cx = box.x + box.width / 2
  const cy = box.y + box.height / 2
  await page.mouse.move(cx, cy)
  await page.mouse.down()
  await page.mouse.move(cx - 210, cy, { steps: 10 })
  await page.mouse.up()
  await expect(page.locator('[data-card-index="1"]')).toHaveAttribute('aria-selected', 'true')
  await expect(page).toHaveURL(/\/#\/agent$/)

  await prevButton.click()
  await expect(page.locator('[data-card-index="0"]')).toHaveAttribute('aria-selected', 'true')
  // 已循环：在首张继续向前则环绕到末张，箭头不再禁用。
  await expect(prevButton).toBeEnabled()
  await prevButton.click()
  await expect(page.locator('[data-card-index="2"]')).toHaveAttribute('aria-selected', 'true')
  await nextButton.click()
  await expect(page.locator('[data-card-index="0"]')).toHaveAttribute('aria-selected', 'true')
})

test('鼠标滚轮可以切换聚焦卡片，向下滚等同向右、向上滚等同向左', async ({ page }) => {
  await mockCarousel(page)
  await page.setViewportSize({ width: 1280, height: 900 })
  await page.goto('/#/agent')

  const stage = page.getByTestId('ontology-card-carousel')
  // 隐藏（未完整落入面板的）侧卡不进 accessibility tree，按 DOM 计数
  await expect(stage.locator('[data-card-index]')).toHaveCount(3)
  await stage.hover()

  // 向下滚 = 向右切换到下一张
  await page.mouse.wheel(0, 120)
  await expect(page.locator('[data-card-index="1"]')).toHaveAttribute('aria-selected', 'true')
  await expect(page).toHaveURL(/\/#\/agent$/)

  // 等待滚轮切换冷却结束，向上滚 = 向左切回上一张
  await page.waitForTimeout(300)
  await page.mouse.wheel(0, -120)
  await expect(page.locator('[data-card-index="0"]')).toHaveAttribute('aria-selected', 'true')
})

test('仅两张本体卡片时线性展示且不循环', async ({ page }) => {
  await mockCarousel(page, {
    items: [
      released('ontology-1', '供应链本体', 7),
      released('ontology-2', '医疗健康本体', 3, '医疗'),
    ],
  })
  await page.setViewportSize({ width: 1280, height: 900 })
  await page.goto('/#/agent')

  const prevButton = page.getByLabel('上一张本体卡片')
  const nextButton = page.getByLabel('下一张本体卡片')
  await expect(page.getByTestId('ontology-card-carousel').locator('[data-card-index]')).toHaveCount(2)
  await expect(prevButton).toBeDisabled()
  await nextButton.click()
  await expect(page.locator('[data-card-index="1"]')).toHaveAttribute('aria-selected', 'true')
  await expect(nextButton).toBeDisabled()
})

test('头部下拉选择保持可用（回归）', async ({ page }) => {
  await mockCarousel(page)
  await page.setViewportSize({ width: 1280, height: 900 })
  await page.goto('/#/agent')

  await page.getByLabel('选择本体').selectOption('ontology-1')
  await expect(page).toHaveURL(/\/#\/agent\?ontology_id=ontology-1$/)
  await expect(page.getByTestId('agent-ontology-panel')).toContainText('当前本体暂无可视化对象')
})

test('窄面板时侧卡整体淡出，只完整展示居中卡', async ({ page }) => {
  await mockCarousel(page)
  // 1280 宽视口下右侧面板约 626px，不足以完整容纳侧边卡片。
  await page.setViewportSize({ width: 1280, height: 900 })
  await page.goto('/#/agent')

  await expect(page.getByTestId('ontology-card-carousel')).toBeVisible()
  await expect(page.locator('[data-card-index="0"]')).toBeVisible()
  await expect(page.locator('[data-card-index="1"]')).toBeHidden()
  await expect(page.locator('[data-card-index="2"]')).toBeHidden()
  // 箭头切换仍然可用：居中卡换为下一张后原居中卡淡出
  await page.getByLabel('下一张本体卡片').click()
  await expect(page.locator('[data-card-index="1"]')).toHaveAttribute('aria-selected', 'true')
  await expect(page.locator('[data-card-index="1"]')).toBeVisible()
  await expect(page.locator('[data-card-index="0"]')).toBeHidden()
})

test('无已发布本体时展示空态与前往管理入口', async ({ page }) => {
  await mockCarousel(page, {
    items: [{
      id: 'ontology-draft',
      name: '未发布本体',
      domain: '供应链',
      description: '',
      status: 'draft',
      version: 'v0',
      current_release_id: null,
      current_release_version: null,
      created_at: now,
      updated_at: now,
    }],
  })
  await page.setViewportSize({ width: 1280, height: 900 })
  await page.goto('/#/agent')

  await expect(page.getByText('暂无已发布本体')).toBeVisible()
  await page.getByRole('button', { name: '前往本体管理' }).click()
  await expect(page).toHaveURL(/\/#\/ontologies$/)
})
