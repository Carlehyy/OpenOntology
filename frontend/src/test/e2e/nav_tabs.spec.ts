import { expect, test, type Page, type Route } from '@playwright/test'

const now = '2026-08-12T08:00:00+00:00'

async function mockNavTabs(page: Page) {
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
    if (path === '/api/v1/ontologies') return json(route, {
      items: [{
        id: 'ontology-1',
        name: '供应链本体',
        domain: '供应链',
        description: '多标签页导航验证',
        status: 'published',
        version: 'v1',
        current_release_id: 'release-1',
        current_release_version: 'v1',
        created_at: now,
        updated_at: now,
      }],
      total: 1,
      page: 1,
      page_size: 20,
    })
    if (path === '/api/v1/domains') return json(route, [])
    if (path === '/api/v1/models') return json(route, [])
    return route.fallback()
  })

  await page.route('**/api/v2/**', route => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/v2/inbox/summary') return json(route, {
      openAlertCount: 0,
      actionableCount: 0,
      unreadCount: 0,
      resolvedCount: 0,
    })
    if (path === '/api/v2/ontologies/ontology-1/versions/release-1/workspace'
      || path === '/api/v2/formal/ontologies/ontology-1/full') return json(route, {
      id: 'ontology-1',
      name: '供应链本体',
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
    if (path === '/api/v2/formal/ontologies/ontology-1/agent/capabilities') return json(route, {
      enabled: true,
      objectTypes: [],
      linkTypes: [],
      actions: [],
      allowActionProposals: true,
      maxRowsPerQuery: 50,
      maxSteps: 8,
      skillCard: '',
      releaseId: 'release-1',
      releaseVersion: 'v1',
    })
    if (path === '/api/v2/formal/ontologies/ontology-1/agent/profile') return json(route, {
      id: 'profile-1',
      ontologyId: 'ontology-1',
      enabled: true,
      allowedObjectTypeIds: null,
      allowedLinkTypeIds: null,
      allowedActionIds: [],
      allowActionProposals: true,
      maxRowsPerQuery: 50,
      maxSteps: 8,
      systemPromptExtra: '',
      defaultModelId: null,
      updatedAt: now,
    })
    if (path === '/api/v2/formal/ontologies/ontology-1/agent/conversations') return json(route, [])
    if (path === '/api/v2/formal/ontologies/ontology-1/agent/decision-simulations') return json(route, [])
    if (path.startsWith('/api/v2/formal/ontologies/ontology-1/agent/dynamic-sentinels')) return json(route, [])
    return route.fallback()
  })
}

test('顶栏多标签页：打开、切换、域内路径恢复、关闭与刷新恢复', async ({ page }) => {
  await mockNavTabs(page)
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto('/#/agent')

  const tabList = page.getByRole('tablist', { name: '页面标签' })
  const agentTab = tabList.getByRole('tab', { name: '本体助手' })
  const ontologiesTab = tabList.getByRole('tab', { name: '本体模型 · 本体管理' })

  // 访问本体助手，出现第一个标签且为激活态
  await expect(agentTab).toBeVisible()
  await expect(agentTab).toHaveAttribute('aria-selected', 'true')
  await expect(ontologiesTab).toHaveCount(0)

  // 侧边栏展开本体模型分组：子项首项为本体管理，展开即自动进入并生成标签
  await page.getByRole('navigation').getByRole('button', { name: '本体模型' }).click()
  await expect(page).toHaveURL(/\/#\/ontologies$/)
  await expect(agentTab).toHaveAttribute('aria-selected', 'false')
  await expect(ontologiesTab).toHaveAttribute('aria-selected', 'true')

  // 业务澄清已从导航隐藏（仅经本体详情页「在线配置」进入）：经深链打开标签，再收起该入口标签，聚焦本体助手与本体管理的双标签流转
  await page.goto('/#/explore')
  await expect(page).toHaveURL(/\/#\/explore$/)
  await tabList.getByRole('tab', { name: '本体模型 · 业务澄清' })
    .getByRole('button', { name: '关闭 本体模型 · 业务澄清' }).click()

  // 点击标签切回本体助手
  await agentTab.click()
  await expect(page).toHaveURL(/\/#\/agent$/)
  await expect(agentTab).toHaveAttribute('aria-selected', 'true')

  // 刷新后标签列表与激活态从 localStorage 恢复
  await page.reload()
  await expect(agentTab).toHaveAttribute('aria-selected', 'true')
  await expect(ontologiesTab).toBeVisible()

  // 菜单域内跳转复用同一标签并记住路径（含 query）
  await page.getByLabel('选择本体').selectOption('ontology-1')
  await expect(page).toHaveURL(/\/#\/agent\?ontology_id=ontology-1$/)
  await expect(tabList.getByRole('tab', { name: '本体助手' })).toHaveCount(1)
  await ontologiesTab.click()
  await expect(page).toHaveURL(/\/#\/ontologies$/)
  await agentTab.click()
  await expect(page).toHaveURL(/\/#\/agent\?ontology_id=ontology-1$/)

  // 关闭激活标签，回退到最近使用的标签
  await agentTab.getByRole('button', { name: '关闭 本体助手' }).click()
  await expect(page).toHaveURL(/\/#\/ontologies$/)
  await expect(tabList.getByRole('tab', { name: '本体助手' })).toHaveCount(0)
  await expect(ontologiesTab).toHaveAttribute('aria-selected', 'true')

  // 关闭最后一个标签，回到默认落地页（AI 原生工作台为裸布局，无顶栏标签栏）
  await ontologiesTab.getByRole('button', { name: '关闭 本体模型 · 本体管理' }).click()
  await expect(page).toHaveURL(/\/#\/super-assistant$/)
  await expect(tabList).toHaveCount(0)

  // 从工作台回到后台页面，标签随同路径导航重新记录
  await page.goto('/#/agent')
  await expect(tabList.getByRole('tab', { name: '本体助手' })).toHaveAttribute('aria-selected', 'true')
})

test('顶栏多标签页：按最近访问从左往右排序，最多保留 10 个', async ({ page }) => {
  await mockNavTabs(page)
  await page.setViewportSize({ width: 1440, height: 900 })

  const tabList = page.getByRole('tablist', { name: '页面标签' })
  const tabs = tabList.getByRole('tab')

  // 依次访问 11 个不同页面
  const visited = [
    { path: '/agent', title: '本体助手' },
    { path: '/explore', title: '本体模型 · 业务澄清' },
    { path: '/ontologies', title: '本体模型 · 本体管理' },
    { path: '/world-model/models', title: '世界模型 · 推演模型' },
    { path: '/world-model/services', title: '世界模型 · 推演服务' },
    { path: '/world-model/calls', title: '世界模型 · 调用记录' },
    { path: '/data/pipelines', title: '数据集成 · 数据流水线' },
    { path: '/data/structured', title: '数据集成 · 数据资产湖' },
    { path: '/events', title: '事件登记' },
    { path: '/api-hub/history', title: '接口代理 · 调用历史' },
    { path: '/models', title: '模型配置' },
  ]
  for (const item of visited) {
    await page.goto(`/#${item.path}`)
    await expect(tabList.getByRole('tab', { name: item.title })).toHaveAttribute('aria-selected', 'true')
  }

  // 最多 10 个：最早访问的“本体助手”被淘汰，最左为当前页面，依次为最近访问
  await expect(tabs).toHaveCount(10)
  const expectedOrder = visited.slice(1).reverse().map(item => item.title)
  for (const [index, title] of expectedOrder.entries()) {
    await expect(tabs.nth(index)).toHaveAttribute('title', title)
  }
  await expect(tabList.getByRole('tab', { name: '本体助手' })).toHaveCount(0)

  // 刷新后顺序与淘汰结果从 localStorage 恢复
  await page.reload()
  await expect(tabs).toHaveCount(10)
  for (const [index, title] of expectedOrder.entries()) {
    await expect(tabs.nth(index)).toHaveAttribute('title', title)
  }

  // 再次访问中间的页面，该标签移到最左且不重复、不超限
  await page.goto('/#/events')
  await expect(tabs).toHaveCount(10)
  await expect(tabs.nth(0)).toHaveAttribute('title', '事件登记')
  await expect(tabs.nth(1)).toHaveAttribute('title', '模型配置')
  await expect(tabList.getByRole('tab', { name: '事件登记' })).toHaveCount(1)
})
test('标签可见标题使用平台导航的一级/二级菜单标签，不使用页面级描述', async ({ page }) => {
  await mockNavTabs(page)
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto('/#/agent')

  const tabList = page.getByRole('tablist', { name: '页面标签' })

  // 列表页：一级 · 二级菜单标签（展开分组自动落到首项本体管理）
  await page.getByRole('navigation').getByRole('button', { name: '本体模型' }).click()
  await expect(page).toHaveURL(/\/#\/ontologies$/)
  const ontologiesTab = tabList.getByRole('tab', { name: '本体模型 · 本体管理' })
  await expect(ontologiesTab).toBeVisible()
  await expect(ontologiesTab).toHaveAttribute('title', '本体模型 · 本体管理')

  // 详情页：标签仍为菜单标签“本体模型 · 本体管理”，不显示“详情”
  await page.goto('/#/ontologies/ontology-1')
  await expect(tabList.getByRole('tab', { name: '本体模型 · 本体管理' })).toHaveAttribute('aria-selected', 'true')
  await expect(tabList.getByRole('tab', { name: '本体模型 · 本体管理' })).toHaveAttribute('title', '本体模型 · 本体管理')
  await expect(tabList.getByRole('tab', { name: '详情' })).toHaveCount(0)

  // 图谱页：同一菜单域复用标签
  await page.goto('/#/ontologies/ontology-1/graph')
  await expect(tabList.getByRole('tab', { name: '本体模型 · 本体管理' })).toBeVisible()
  await expect(tabList.getByRole('tab', { name: '图谱' })).toHaveCount(0)

  // 系统设置子页：一级 · 二级菜单标签
  await page.goto('/#/settings/domains')
  const settingsTab = tabList.getByRole('tab', { name: '系统设置 · 领域设置' })
  await expect(settingsTab).toBeVisible()
  await expect(settingsTab).toHaveAttribute('title', '系统设置 · 领域设置')

  // 世界模型列表页：一级 · 二级菜单标签
  await page.goto('/#/world-model/models')
  const wmTab = tabList.getByRole('tab', { name: '世界模型 · 推演模型' })
  await expect(wmTab).toBeVisible()
  await expect(wmTab).toHaveAttribute('aria-selected', 'true')

  // 数据管家页：一级 · 二级菜单标签（不显示“数据管家”）
  await page.goto('/#/data/pipelines/steward')
  const stewardTab = tabList.getByRole('tab', { name: '数据集成 · 数据流水线' })
  await expect(stewardTab).toBeVisible()
  await expect(tabList.getByRole('tab', { name: '数据管家' })).toHaveCount(0)
})

test('左侧导航：本体模型分组在子页面收起后保持收起，再点恢复展开', async ({ page }) => {
  await mockNavTabs(page)
  await page.setViewportSize({ width: 1440, height: 900 })

  const nav = page.getByRole('navigation')
  const ontologyGroup = nav.getByRole('button', { name: '本体模型' })
  const networkLink = nav.getByRole('link', { name: '本体网络' })

  // 直接落在隐藏子项页面 /explore（顶级路由、不在父路径前缀下）：
  // 分组按全量菜单口径判定激活，仍应默认展开
  await page.goto('/#/explore')
  await expect(ontologyGroup).toHaveAttribute('aria-expanded', 'true')
  await expect(networkLink).toBeVisible()
  // 隐藏子项不渲染为导航链接
  await expect(nav.getByRole('link', { name: '业务澄清' })).toHaveCount(0)

  // 收起后必须保持收起：回归点是子项为顶级路由时曾被误判“已离开分组”而自动弹回
  await ontologyGroup.click()
  await expect(ontologyGroup).toHaveAttribute('aria-expanded', 'false')
  await expect(networkLink).toHaveCount(0)

  // 分组状态清理走 setTimeout(0)，留出时间窗验证不会闪回展开
  await page.waitForTimeout(250)
  await expect(ontologyGroup).toHaveAttribute('aria-expanded', 'false')

  // 再点击重新展开
  await ontologyGroup.click()
  await expect(ontologyGroup).toHaveAttribute('aria-expanded', 'true')
  await expect(networkLink).toBeVisible()
})

test('左侧导航：「数据集成」显示新名称且收展行为与其他分组一致', async ({ page }) => {
  await mockNavTabs(page)
  await page.setViewportSize({ width: 1440, height: 900 })

  await page.goto('/#/data/pipelines')
  const nav = page.getByRole('navigation')
  const dataGroup = nav.getByRole('button', { name: '数据集成' })
  await expect(dataGroup).toBeVisible()
  await expect(nav.getByRole('button', { name: '数据通道' })).toHaveCount(0)

  // 激活的分组默认展开；收起后保持收起
  await expect(dataGroup).toHaveAttribute('aria-expanded', 'true')
  await dataGroup.click()
  await expect(dataGroup).toHaveAttribute('aria-expanded', 'false')
  await expect(nav.getByRole('link', { name: '数据流水线' })).toHaveCount(0)

  // 再点击重新展开（已在组内不重复跳转）
  await dataGroup.click()
  await expect(dataGroup).toHaveAttribute('aria-expanded', 'true')
  await expect(page).toHaveURL(/\/#\/data\/pipelines$/)
})

test('左侧导航折叠态：一级导航全部可点击，分组直达第一个可见子项', async ({ page }) => {
  await mockNavTabs(page)
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto('/#/agent')

  const nav = page.getByRole('navigation')
  // 折叠侧边栏（Logo 按钮 aria-label 随折叠态切换）
  await page.getByRole('button', { name: '折叠平台导航' }).click()

  // 回归点：折叠态曾直接吞掉分组按钮点击（if (collapsed) return），
  // 本体模型/世界模型/数据集成等一级导航完全不可达。现在应与展开态
  // “未激活时跳第一个子项”的行为对齐，直达第一个可见子项。
  const groups: Array<[string, RegExp]> = [
    ['本体模型', /\/#\/ontologies$/],
    ['世界模型', /\/#\/world-model\/models$/],
    ['数据集成', /\/#\/data\/pipelines$/],
    ['接口代理', /\/#\/api-hub\/interfaces$/],
    ['开放社区', /\/#\/community\/skills$/],
    ['系统设置', /\/#\/settings\/domains$/],
  ]
  for (const [name, url] of groups) {
    await nav.getByRole('button', { name, exact: true }).click()
    await expect(page).toHaveURL(url)
  }

  // 叶子项在折叠态本就可达，一并回归
  await nav.getByRole('link', { name: '三维场景' }).click()
  await expect(page).toHaveURL(/\/#\/scenes$/)
  await nav.getByRole('link', { name: '事件登记' }).click()
  await expect(page).toHaveURL(/\/#\/events$/)
})

test('左上角 Logo 折叠/展开侧边栏：图标水平位置全程稳定不闪动', async ({ page }) => {
  await mockNavTabs(page)
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto('/#/data/pipelines')

  // Logo 区是侧栏第一个块；按钮的 aria-label 随折叠态切换，
  // 因此用结构定位器（logo 按钮内第一个 div = Network 图标方块），点击前后同一节点
  const logoButton = page.locator('aside > div').first().locator('button').first()
  await expect(logoButton).toBeVisible()
  const logoIcon = logoButton.locator('div').first()

  const expandedBox = await logoIcon.boundingBox()
  expect(expandedBox).not.toBeNull()

  // 回归点：折叠态曾切换 justify-center/px-0（justify 不参与过渡、瞬时跳变），
  // 图标先横跳到行中部再随宽度动画滑回，表现为 Logo 闪动。现在 px-4 恒定，
  // 图标点击后与过渡结束后都应留在原位（折叠态 64px 侧栏内恰好居中）。
  await logoButton.click()
  const afterCollapseClick = await logoIcon.boundingBox()
  await page.waitForTimeout(400)
  const collapsedBox = await logoIcon.boundingBox()
  expect(Math.abs(afterCollapseClick!.x - expandedBox!.x)).toBeLessThanOrEqual(1)
  expect(Math.abs(collapsedBox!.x - expandedBox!.x)).toBeLessThanOrEqual(1)

  // 展开方向同样稳定
  await logoButton.click()
  const afterExpandClick = await logoIcon.boundingBox()
  await page.waitForTimeout(400)
  const restoredBox = await logoIcon.boundingBox()
  expect(Math.abs(afterExpandClick!.x - expandedBox!.x)).toBeLessThanOrEqual(1)
  expect(Math.abs(restoredBox!.x - expandedBox!.x)).toBeLessThanOrEqual(1)
})
