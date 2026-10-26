import { expect, test, type Page, type Route } from '@playwright/test'

const ontologyId = 'ontology-overview-responsive'

async function mockOverview(page: Page, options?: { singleVersion?: boolean; withHealth?: boolean }) {
  const withHealth = options?.withHealth ?? true
  await page.addInitScript(() => {
    localStorage.setItem('token', 'overview-responsive-token')
    localStorage.setItem('auth-store', JSON.stringify({
      state: {
        token: 'overview-responsive-token',
        user: { id: 'overview-responsive-user', username: 'overview-responsive-user', role: 'admin' },
      },
      version: 0,
    }))
  })

  const ok = (route: Route, data: unknown) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ data, message: 'ok' }),
  })

  const versions = [
    {
      id: 'release-v0', version_number: 'v0', version_label: null, description: null,
      parent_version_id: null, base_release_id: null, promoted_from_id: null,
      node_kind: 'release', lifecycle_status: 'released', revision: 1,
      created_at: '2026-08-01T00:00:00Z', published_at: '2026-08-01T00:00:00Z',
      latest_trial: null,
    },
    ...(options?.singleVersion ? [] : [{
      id: 'release-v1', version_number: 'v1', version_label: null, description: null,
      parent_version_id: 'release-v0', base_release_id: 'release-v0', promoted_from_id: null,
      node_kind: 'release', lifecycle_status: 'released', revision: 2,
      created_at: '2026-08-07T00:00:00Z', published_at: '2026-08-07T00:00:00Z',
      latest_trial: null,
    }]),
  ]
  const currentRelease = versions.at(-1)!

  await page.route(/^https?:\/\/[^/]+\/api\/v[12]\//, async (route) => {
    const path = new URL(route.request().url()).pathname

    if (path === `/api/v1/ontologies/${ontologyId}`) {
      return ok(route, {
        id: ontologyId,
        name: '总览弹性布局测试本体',
        domain: '供应链',
        description: '验证本体总览在不同视口下不裁切内容。',
        version: currentRelease.version_number,
        current_release_id: currentRelease.id,
        current_release_version: currentRelease.version_number,
        status: 'published',
        entity_count: 1,
        relation_count: 0,
        action_count: 1,
        sentinel_count: 0,
        created_by: 'overview-responsive-user',
        created_at: '2026-08-01T00:00:00Z',
        updated_at: '2026-08-07T00:00:00Z',
      })
    }
    if (path === `/api/v2/formal/ontologies/${ontologyId}/overview`) {
      return ok(route, {
        release: { id: currentRelease.id, version: currentRelease.version_number, publishedAt: '2026-08-07T00:00:00Z' },
        model: {
          objectTypes: 1,
          linkTypes: 0,
          actions: 1,
          actionsRequiringApproval: 1,
          functions: 0,
          sentinels: { total: 0, enabled: 0, muted: 0 },
        },
        data: {
          instances: 0,
          instancesBySource: {},
          linkInstances: 0,
          mappings: { total: 1, bound: 0, nameMatch: 0, autoCreate: 1, autoApply: 0 },
          topTypes: [],
        },
        runtime: {
          pendingApprovals: 2,
          decisions: { total: 0, approved: 0, rejected: 0, recentApprovalRate: null },
          firings7d: { total: 0, fired: 0, error: 0 },
          actionRuns7d: { total: 0, success: 0, failed: 0 },
          daily7d: [],
        },
        facts: { total: 0, byKind: {} },
        health: withHealth
          ? [
            { level: 'warn', message: '1 条映射未绑定对象实体（将由数据自建类型）', target: 'data-mapping', hint: '建议在映射维护里显式绑定，防止产生平行类型' },
          ]
          : [],
      })
    }
    if (path === `/api/v2/formal/ontologies/${ontologyId}/facts/recent`) {
      return ok(route, [])
    }
    if (path === `/api/v2/ontologies/${ontologyId}/version-tree`) {
      return ok(route, {
        current_release_id: currentRelease.id,
        current_release_number: currentRelease.version_number,
        current_release_version: currentRelease.version_number,
        versions,
      })
    }
    if (path === `/api/v2/formal/ontologies/${ontologyId}/pending-actions`) {
      return ok(route, [{ id: 'pending-1' }, { id: 'pending-2' }])
    }
    if (path === '/api/v2/inbox/summary') {
      return ok(route, { openAlertCount: 0, actionableCount: 0, unreadCount: 0, resolvedCount: 0 })
    }
    return ok(route, [])
  })
}

test('矮屏（1280x720）下 KPI 栏不被压碎，内容单页呈现无滚轮', async ({ page }) => {
  await mockOverview(page)
  // 总览面板直接呈现，不通过入场位移动画改变瞬时布局。
  await page.setViewportSize({ width: 1280, height: 720 })
  await page.goto(`/#/ontologies/${ontologyId}`, { waitUntil: 'domcontentloaded' })

  const shell = page.locator('.ontology-overview-shell')
  await expect(page.getByRole('heading', { name: '本体概况' })).toBeVisible()
  // 修复前此处 KPI 栏会被 grid 挤压到 2~42px，指标数字完全不可见。
  const kpiBox = await page.locator('.kpi-rail').boundingBox()
  expect(kpiBox).not.toBeNull()
  expect(kpiBox!.height).toBeGreaterThan(120)
  await expect(page.locator('.kpi-cell').first().locator('strong')).toBeVisible()
  for (const panel of await page.locator('.overview-panel, .overview-health').all()) {
    await expect(panel).toHaveCSS('animation-name', 'none')
    await expect(panel).toHaveCSS('transform', 'none')
  }
  for (const cell of await page.locator('.kpi-cell').all()) {
    await expect(cell).toHaveCSS('transform', 'none')
    await expect(cell).toHaveCSS('opacity', '1')
  }
  // MYW-77：面板高度随视口弹性压缩，矮屏下内容收敛在一页内——外壳保留
  // overflow:auto 作为极矮视口的兜底，但正常矮屏不得出现实际滚动。
  await expect(shell).toHaveCSS('overflow-y', 'auto')
  expect(await shell.evaluate(element => element.scrollHeight <= element.clientHeight + 1)).toBeTruthy()
  // 运行趋势图由 ECharts 渲染。
  await expect(page.locator('.runtime-trend-chart canvas').first()).toBeVisible()
  // MYW-77 二轮回归：无运行数据时的空态提示必须是紧凑浮层，不得被拉伸成
  // 撑满图表区的大卡片（曾因绝对定位填充规则误命中 .runtime-empty-note）。
  const emptyNote = page.locator('.runtime-empty-note')
  await expect(emptyNote).toBeVisible()
  await expect(emptyNote).toContainText('所选时段暂无运行记录')
  const noteBox = await emptyNote.boundingBox()
  expect(noteBox).not.toBeNull()
  expect(noteBox!.width).toBeLessThanOrEqual(480)
  // 已下线的两个事实面板不再渲染。
  await expect(page.getByRole('heading', { name: '事实类型构成' })).toHaveCount(0)
  await expect(page.getByRole('heading', { name: '最近发生了什么' })).toHaveCount(0)
  await page.locator('.runtime-summary').scrollIntoViewIfNeeded()
  await expect(page.getByRole('heading', { name: '运行汇总' })).toBeVisible()

  // 待处理事项横条可见，且可点击直达对应 Tab。
  const health = page.getByLabel('待处理事项')
  await expect(health).toBeVisible()
  await expect(health).toContainText('1 条映射未绑定对象实体')
  // 审批相关信息刻意不在总览展示，即使 overview 接口仍返回
  // pendingApprovals / actionsRequiringApproval 字段。
  await expect(page.getByText(/等待审批|需人工审批/)).toHaveCount(0)
  await health.getByRole('button', { name: /1 条映射未绑定对象实体/ }).click()
  await expect(page).toHaveURL(new RegExp(`/ontologies/${ontologyId}\\?tab=data-mapping`))
})

test('大屏（1920x1080）下完整呈现：趋势图可见，页面收敛为三个面板', async ({ page }) => {
  await mockOverview(page, { withHealth: false })
  await page.setViewportSize({ width: 1920, height: 1080 })
  await page.goto(`/#/ontologies/${ontologyId}`, { waitUntil: 'domcontentloaded' })

  await expect(page.getByRole('heading', { name: '本体概况' })).toBeVisible()
  await expect(page.getByLabel('待处理事项')).toHaveCount(0)

  const shell = page.locator('.ontology-overview-shell')
  await expect(shell).toHaveCSS('overflow-y', 'auto')
  const kpiBox = await page.locator('.kpi-rail').boundingBox()
  expect(kpiBox).not.toBeNull()
  expect(kpiBox!.height).toBeGreaterThan(160)
  await expect(page.locator('.runtime-trend-chart canvas').first()).toBeVisible()
  // 视口有富余时内容垂直伸展到底：图表吸收剩余高度而非底部留白。
  const chartBox = await page.locator('.runtime-trend-chart').boundingBox()
  expect(chartBox).not.toBeNull()
  expect(chartBox!.height).toBeGreaterThan(300)
  // 事实类型构成 / 最近发生了什么已下线，页面只剩概况、KPI、运行汇总三个面板。
  await expect(page.locator('.overview-panel')).toHaveCount(3)
  await expect(page.getByRole('heading', { name: '事实类型构成' })).toHaveCount(0)
  await expect(page.getByRole('heading', { name: '最近发生了什么' })).toHaveCount(0)
})

test('版本演化只有单个快照节点时隐藏播放与进度控制', async ({ page }) => {
  await mockOverview(page, { singleVersion: true })
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto(`/#/ontologies/${ontologyId}`, { waitUntil: 'domcontentloaded' })

  const card = page.getByTestId('version-evolution-card')
  await expect(card).toBeVisible()
  await expect(card.getByRole('button', { name: '播放' })).toHaveCount(0)
  await expect(card.getByRole('button', { name: '单步' })).toHaveCount(0)

  await mockOverview(page)
  await page.reload({ waitUntil: 'domcontentloaded' })
  await expect(card).toBeVisible()
  await expect(card.getByRole('button', { name: '播放' })).toBeVisible()
})
