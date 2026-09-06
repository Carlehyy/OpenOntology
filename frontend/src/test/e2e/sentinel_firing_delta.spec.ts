import { expect, test, type Page, type Route } from '@playwright/test'

const ontologyId = 'ontology-sentinel-firing-delta'

const sentinel = {
  id: 'sentinel-high-risk',
  ontologyId,
  name: 'high_risk_order',
  displayName: '高风险订单监控',
  bindings: [{ alias: 'o', objectTypeId: 'object-order', filter: null }],
  links: [],
  condition: 'o["risk_score"] >= 80',
  conditionRows: [],
  conditionLogic: 'and',
  primaryAlias: 'o',
  actionIds: ['action-notify'],
  actionParameters: {},
  onChange: true,
  onSchedule: false,
  scanIntervalSeconds: 300,
  triggerMode: 'on_enter_leave',
  muted: false,
  enabled: true,
  releaseId: 'release-v1',
  enableGeneration: 0,
  origin: 'release_builtin',
  status: 'published',
}

const patternSentinel = {
  ...sentinel,
  id: 'sentinel-pattern-seq',
  name: 'order_status_sequence',
  displayName: '订单状态变迁模式',
  condition: null,
  pattern: {
    stages: [
      { alias: 'a', objectTypeId: 'object-order', filter: "a.status == 'submitted'" },
      { alias: 'b', objectTypeId: 'object-order', filter: "b.status == 'paid'", within: 7200 },
    ],
    within: 3600,
    absence: { enabled: true },
  },
  triggerMode: 'on_pattern',
  onSchedule: true,
  scanIntervalSeconds: 120,
}

const firings = [
  {
    id: 'firing-pattern-absence',
    sentinelId: patternSentinel.id,
    sentinelName: patternSentinel.displayName,
    triggerSource: 'schedule',
    status: 'fired',
    matchCount: 1,
    matches: [{ a: 'order-2001' }],
    entered: ['pattern:order-2001:2026-07-26T09:50:00+00:00'],
    left: [],
    actionResults: [{ status: 'success', effects: [] }],
    durationMs: 15,
    ontologyVersion: 'v1',
    ontologyReleaseId: 'release-v1',
    createdAt: '2026-07-26T10:00:30Z',
  },
  {
    id: 'firing-enter',
    sentinelId: sentinel.id,
    sentinelName: sentinel.displayName,
    triggerSource: 'change',
    status: 'fired',
    matchCount: 2,
    matches: [{ o: 'order-1001' }, { o: 'order-1004' }],
    entered: ['order-1001', 'order-1004'],
    left: [],
    actionResults: [{ status: 'success', effects: [] }],
    durationMs: 12,
    ontologyVersion: 'v1',
    ontologyReleaseId: 'release-v1',
    createdAt: '2026-07-26T10:01:00Z',
  },
  {
    id: 'firing-leave',
    sentinelId: sentinel.id,
    sentinelName: sentinel.displayName,
    triggerSource: 'change',
    status: 'fired',
    matchCount: 0,
    matches: [],
    entered: [],
    left: ['order-1001', 'order-1004'],
    actionResults: [{ status: 'success', effects: [] }],
    durationMs: 9,
    ontologyVersion: 'v1',
    ontologyReleaseId: 'release-v1',
    createdAt: '2026-07-26T10:02:00Z',
  },
  {
    id: 'firing-no-change',
    sentinelId: sentinel.id,
    sentinelName: sentinel.displayName,
    triggerSource: 'manual',
    status: 'no_change',
    matchCount: 2,
    matches: [{ o: 'order-1001' }, { o: 'order-1004' }],
    entered: [],
    left: [],
    actionResults: [],
    durationMs: 7,
    ontologyVersion: 'v1',
    ontologyReleaseId: 'release-v1',
    createdAt: '2026-07-26T10:03:00Z',
  },
  {
    id: 'firing-unhandled-leave',
    sentinelId: sentinel.id,
    sentinelName: sentinel.displayName,
    triggerSource: 'change',
    status: 'no_change',
    matchCount: 1,
    matches: [{ o: 'order-1004' }],
    entered: [],
    left: ['order-1001'],
    actionResults: [],
    durationMs: 6,
    ontologyVersion: 'v1',
    ontologyReleaseId: 'release-v1',
    createdAt: '2026-07-26T10:04:00Z',
  },
]

async function mockPublishedGraph(page: Page) {
  let firingHistory = [...firings]

  await page.addInitScript(() => {
    localStorage.setItem('token', 'e2e-token')
    localStorage.setItem('auth-store', JSON.stringify({
      state: {
        token: 'e2e-token',
        user: {
          id: 'admin',
          username: 'admin',
          email: 'admin@example.com',
          role: 'admin',
        },
      },
      version: 0,
    }))
  })

  const ok = (route: Route, data: unknown) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ data }),
  })

  await page.route(/^https?:\/\/[^/]+\/api\/v2\//, route => {
    const path = new URL(route.request().url()).pathname
    if (path === `/api/v2/formal/ontologies/${ontologyId}/full`) {
      return ok(route, {
        id: ontologyId,
        name: '供应链风险本体',
        version: 'v1',
        workspaceMode: 'runtime',
        objectTypes: [{
          id: 'object-order',
          name: 'Order',
          displayName: '订单',
          primaryKey: 'order_id',
          properties: [{
            id: 'property-risk-score',
            name: 'risk_score',
            displayName: '风险评分',
            type: 'number',
            required: true,
          }],
          positionX: 120,
          positionY: 120,
        }],
        linkTypes: [],
        actions: [{
          id: 'action-notify',
          name: 'notify_risk',
          displayName: '发送风险通知',
          objectTypeId: 'object-order',
          parameters: [],
          rules: [],
          requiresApproval: true,
        }],
        functions: [],
        instances: [],
        linkInstances: [],
        executionLogs: [],
      })
    }
    return ok(route, [])
  })

  await page.route(/^https?:\/\/[^/]+\/api\/v1\//, route => {
    const path = new URL(route.request().url()).pathname
    const base = `/api/v1/ontologies/${ontologyId}/sentinels`
    if (path === `${base}/`) return ok(route, [sentinel, patternSentinel])
    if (path === `${base}/firings`) return ok(route, firingHistory)
    if (path === `${base}/run` && route.request().method() === 'POST') {
      const current = {
        id: 'firing-current-manual',
        sentinelId: sentinel.id,
        sentinelName: sentinel.displayName,
        triggerSource: 'manual',
        status: 'no_match',
        matchCount: 0,
        matches: [],
        entered: [],
        left: [],
        actionResults: [],
        durationMs: 5,
        ontologyVersion: 'v1',
        ontologyReleaseId: 'release-v1',
        createdAt: '2026-07-26T10:05:00Z',
      }
      firingHistory = [current, ...firingHistory]
      return ok(route, {
        evaluated: 1,
        fired: 0,
        errors: 0,
        no_change: 0,
        no_match: 1,
        pending: 0,
        muted: 0,
        runtimeErrors: [],
        firings: [{
          id: current.id,
          sentinelId: current.sentinelId,
          sentinelName: current.sentinelName,
          status: current.status,
          matchCount: current.matchCount,
          entered: current.entered,
          left: current.left,
          actionResults: current.actionResults,
          error: null,
        }],
      })
    }
    if (path === `${base}/notifications`) return ok(route, [])
    if (path === `${base}/cdc-status`) {
      return ok(route, {
        ontology_id: ontologyId,
        ontology_release_id: 'release-v1',
        scope: 'current_release',
        healthy: true,
        quiescent: true,
        worker_alive: true,
        queued: 0,
        max_queue_size: 1000,
        max_cascade_depth: 8,
        last_error: null,
        durable: {},
        last_errors: [],
        dead_letters: [],
      })
    }
    return ok(route, [])
  })
}

async function expectDeltaSemantics(page: Page) {
  const enter = page.getByTestId('sentinel-firing-delta-firing-enter')
  await expect(enter).toContainText('新进入 2')
  await expect(enter).toContainText('已离开 0')
  await expect(enter).toContainText('进入：order-1001、order-1004')

  const leave = page.getByTestId('sentinel-firing-delta-firing-leave')
  await expect(leave).toContainText('新进入 0')
  await expect(leave).toContainText('已离开 2')
  await expect(leave).toContainText('离开：order-1001、order-1004')

  const noChange = page.getByTestId('sentinel-firing-delta-firing-no-change')
  await expect(noChange).toContainText('边沿无变化')
  await expect(noChange).toContainText('命中集合未变化，本轮未重复执行动作')
  await expect(page.getByTestId('sentinel-firing-firing-no-change'))
    .toContainText('本轮无动作')

  const unhandledLeave = page.getByTestId(
    'sentinel-firing-delta-firing-unhandled-leave',
  )
  await expect(unhandledLeave).toContainText('已离开 1')
  await expect(unhandledLeave)
    .toContainText('检测到边沿变化，但按当前触发模式本轮未执行动作')
}

test('发布态的哨兵面板和运行历史都明确展示进入、离开与本轮无动作', async ({ page }) => {
  await mockPublishedGraph(page)
  await page.goto(`/#/ontologies/${ontologyId}/graph`, {
    waitUntil: 'domcontentloaded',
  })

  await expect(page.getByText(/当前发布 v1/)).toBeVisible()
  await page.getByTitle('打开菜单').click()
  await page.getByTestId('graph-runtime-tool-sentinel').click()
  await page.getByRole('button', { name: '触发日志 (5)' }).click()
  await expectDeltaSemantics(page)
  await page.getByRole('button', { name: '关闭哨兵引擎' }).click()

  await page.getByTitle('打开菜单').click()
  await page.getByTestId('graph-runtime-tool-runhistory').click()
  await page.getByRole('button', { name: /哨兵触发/ }).click()
  await expectDeltaSemantics(page)
})

test('手动触发后只把本次新日志标为本次，并显示可辨认的发生时间', async ({ page }) => {
  await mockPublishedGraph(page)
  await page.goto(`/#/ontologies/${ontologyId}/graph`, {
    waitUntil: 'domcontentloaded',
  })

  await page.getByTitle('打开菜单').click()
  await page.getByTestId('graph-runtime-tool-sentinel').click()
  await page.getByRole('button', { name: '触发日志 (5)' }).click()

  const historical = page.getByTestId('sentinel-firing-firing-no-change')
  await expect(historical.locator('time'))
    .toHaveAttribute('datetime', '2026-07-26T10:03:00Z')
  await expect(
    historical.locator('[data-testid^="sentinel-current-manual-run-"]'),
  ).toHaveCount(0)

  await page.getByRole('button', { name: '手动触发' }).click()

  const current = page.getByTestId('sentinel-firing-firing-current-manual')
  await expect(current).toBeVisible()
  await expect(
    page.getByTestId('sentinel-current-manual-run-firing-current-manual'),
  ).toHaveText('本次手动触发')
  await expect(current.locator('time'))
    .toHaveAttribute('datetime', '2026-07-26T10:05:00Z')
  await expect(
    historical.locator('[data-testid^="sentinel-current-manual-run-"]'),
  ).toHaveCount(0)
})

test('模式哨兵及其缺失分支触发记录在面板与历史中如实展示', async ({ page }) => {
  await mockPublishedGraph(page)
  await page.goto(`/#/ontologies/${ontologyId}/graph`, { waitUntil: 'domcontentloaded' })
  await page.getByTitle('打开菜单').click()
  await page.getByTestId('graph-runtime-tool-sentinel').click()

  await expect(page.getByRole('heading', { name: '哨兵引擎' })).toBeVisible()
  await expect(page.getByRole('button', { name: '哨兵 (2)' }))
    .toBeVisible({ timeout: 5_000 })
  await expect(page.getByText('订单状态变迁模式')).toBeVisible()
  // 缺失分支触发的记录必须出现在触发历史（schedule 来源、fired 状态）。
  await page.getByRole('button', { name: '触发日志 (5)' }).click()
  await expect(page.getByTestId('sentinel-firing-firing-pattern-absence'))
    .toBeVisible({ timeout: 5_000 })
  await expect(page.getByText('订单状态变迁模式').first()).toBeVisible()
})
