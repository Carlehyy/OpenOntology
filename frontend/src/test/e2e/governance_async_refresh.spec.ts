import { expect, test, type Page, type Route } from '@playwright/test'

const ONTOLOGY_ID = 'ontology-async-refresh'
const RELEASE_ID = 'release-async-refresh'
const REFRESH_INTERVAL_MS = 12_000

async function mockGovernance(page: Page) {
  let pendingRequestCount = 0

  await page.addInitScript(() => {
    localStorage.setItem('token', 'e2e-token')
    localStorage.setItem('auth-store', JSON.stringify({
      state: {
        token: 'e2e-token',
        user: {
          id: 'u-governance',
          username: 'governance-tester',
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

  await page.route('**/api/**', route => {
    const url = new URL(route.request().url())
    const path = url.pathname
    if (!path.startsWith('/api/')) return route.continue()

    if (path === `/api/v1/ontologies/${ONTOLOGY_ID}`) {
      return json(route, {
        id: ONTOLOGY_ID,
        name: '异步治理测试本体',
        domain: '供应链',
        description: '验证可靠队列传播期间的治理反馈',
        status: 'published',
        version: 'v20',
        current_release_id: RELEASE_ID,
        current_release_version: 'v20',
        entity_count: 1,
        relation_count: 0,
        action_count: 1,
        sentinel_count: 1,
        created_by: 'u-governance',
        created_at: '2026-07-28T02:00:00Z',
        updated_at: '2026-07-28T02:00:00Z',
      })
    }
    if (path === `/api/v2/formal/ontologies/${ONTOLOGY_ID}/pending-actions`) {
      pendingRequestCount += 1
      return json(route, [])
    }
    if (path === `/api/v2/formal/ontologies/${ONTOLOGY_ID}/autonomy`) {
      return json(route, [])
    }
    if (path === `/api/v2/formal/ontologies/${ONTOLOGY_ID}/facts/recent`) {
      return json(route, [])
    }
    if (path === `/api/v1/ontologies/${ONTOLOGY_ID}/sentinels/`) {
      return json(route, [])
    }
    if (path === `/api/v1/ontologies/${ONTOLOGY_ID}/sentinels/firings`) {
      return json(route, [])
    }
    if (path === '/api/v2/inbox/summary') {
      return json(route, { unread_count: 0 })
    }
    return json(route, [])
  })

  return {
    pendingRequestCount: () => pendingRequestCount,
  }
}

async function setDocumentVisibility(page: Page, state: 'visible' | 'hidden') {
  await page.evaluate(nextState => {
    Object.defineProperty(document, 'visibilityState', {
      configurable: true,
      get: () => nextState,
    })
    document.dispatchEvent(new Event('visibilitychange'))
  }, state)
}

test('治理页有限刷新后台结果，页面隐藏时暂停并保留最近刷新时间', async ({ page }) => {
  await page.clock.install({ time: new Date('2026-07-28T10:00:00+08:00') })
  const requests = await mockGovernance(page)

  await page.goto(`/#/ontologies/${ONTOLOGY_ID}?tab=governance`, {
    waitUntil: 'domcontentloaded',
  })

  const status = page.getByTestId('governance-background-refresh-status')
  await expect(status).toContainText('正在同步待审批与哨兵状态')
  await expect(page.getByText('每 12 秒刷新，最多 2 分钟')).toBeVisible()
  await expect(page.getByTestId('governance-last-refreshed')).not.toContainText('尚未完成')

  // Hide first so the timer that was armed while the initial queries settled
  // is cancelled.  Returning to visible starts a fresh, deterministic 12 s
  // interval; measuring from an arbitrary point after page load is inherently
  // racy under parallel Playwright workers.
  await setDocumentVisibility(page, 'hidden')
  await expect(status).toContainText('页面隐藏中')
  const requestsBeforeHiddenWait = requests.pendingRequestCount()
  await page.clock.fastForward(60_000)
  expect(requests.pendingRequestCount()).toBe(requestsBeforeHiddenWait)

  await setDocumentVisibility(page, 'visible')
  await expect(status).toContainText('正在同步待审批与哨兵状态')
  const initialRequests = requests.pendingRequestCount()
  await page.clock.fastForward(REFRESH_INTERVAL_MS - 100)
  expect(requests.pendingRequestCount()).toBe(initialRequests)
  await page.clock.fastForward(200)
  await expect.poll(requests.pendingRequestCount).toBeGreaterThan(initialRequests)
  await expect(status).toHaveAttribute('aria-busy', 'false')

  for (let cycle = 0; cycle < 9; cycle += 1) {
    const beforeCycle = requests.pendingRequestCount()
    await page.clock.fastForward(REFRESH_INTERVAL_MS + 100)
    await expect.poll(requests.pendingRequestCount).toBeGreaterThan(beforeCycle)
    await expect(status).toHaveAttribute('aria-busy', 'false')
  }

  await expect(status).toContainText('同步完成 · 可手动刷新')
  const requestsAfterWindow = requests.pendingRequestCount()
  await page.clock.fastForward(60_000)
  expect(requests.pendingRequestCount()).toBe(requestsAfterWindow)

  await page.getByRole('button', { name: '立即刷新治理结果' }).click()
  await expect.poll(requests.pendingRequestCount).toBeGreaterThan(requestsAfterWindow)
  await expect(status).toContainText('正在同步待审批与哨兵状态')
})
