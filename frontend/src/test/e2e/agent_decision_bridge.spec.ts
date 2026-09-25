import { expect, test, type Page, type Route } from '@playwright/test'

// 决策推演「正在启动」桥接占位回归：旧实现把面板的 running 直接绑到聊天全局
// busy，任意普通问答回合期间决策推演面板都会谎报「决策推演正在启动」。
// 修复后桥接占位仅随推演意图回合（命中推演关键词正则）出现，回合终态即复位；
// 普通回合与回合中的打字排队一律不触发。

const now = '2026-09-25T08:00:00+00:00'
// 聊天 SSE 延迟返回，撑开 busy 窗口以观测「回合进行中」的面板状态
const CHAT_DELAY_MS = 1200

async function mockPlatform(page: Page) {
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
        description: '',
        status: 'draft',
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

  await page.route('**/api/v2/**', async route => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/v2/inbox/summary') return json(route, { unread_count: 0 })
    if (path === '/api/v2/ontologies/ontology-1/versions/release-1/workspace'
      || path === '/api/v2/formal/ontologies/ontology-1/full') return json(route, {
      id: 'ontology-1',
      name: '供应链本体',
      version: 'v1',
      workspaceMode: 'release',
      objectTypes: [{
        id: 'ot-order', name: 'Order', displayName: '订单', primaryKey: 'order_no',
        properties: [
          { id: 'p1', name: 'order_no', displayName: '订单号', type: 'string', required: true },
        ], positionX: 0, positionY: 0,
      }],
      linkTypes: [], actions: [], functions: [],
      instances: [], linkInstances: [], executionLogs: [],
    })
    if (path === '/api/v2/formal/ontologies/ontology-1/agent/capabilities') return json(route, {
      enabled: true,
      objectTypes: [{ id: 'ot-order', name: 'Order', displayName: '订单', instanceCount: 1 }],
      linkTypes: [], actions: [], allowActionProposals: true,
      maxRowsPerQuery: 50, maxSteps: 8, skillCard: '',
      releaseId: 'release-1', releaseVersion: 'v1',
    })
    if (path === '/api/v2/formal/ontologies/ontology-1/agent/profile') return json(route, {
      id: 'profile-1', ontologyId: 'ontology-1', enabled: true,
      allowedObjectTypeIds: null, allowedLinkTypeIds: null, allowedActionIds: [],
      allowActionProposals: true, maxRowsPerQuery: 50, maxSteps: 8,
      systemPromptExtra: '', defaultModelId: null, updatedAt: now,
    })
    if (path === '/api/v2/formal/ontologies/ontology-1/agent/conversations') {
      return json(route, [])
    }
    if (path === '/api/v2/formal/ontologies/ontology-1/agent/chat') {
      await new Promise(resolve => setTimeout(resolve, CHAT_DELAY_MS))
      return route.fulfill({
        status: 200,
        contentType: 'text/event-stream',
        body: [
          'data: {"type":"meta","conversationId":"conv-1"}',
          '',
          'data: {"type":"answer","content":"LIVE_AGENT_ANSWER"}',
          '',
          'data: {"type":"done"}',
          '',
          '',
        ].join('\n'),
      })
    }
    if (path === '/api/v2/formal/ontologies/ontology-1/agent/decision-simulations') {
      return json(route, [])
    }
    return route.fallback()
  })
}

test.describe('决策推演桥接占位', () => {
  test('普通回合全程不播「正在启动」；推演意图回合自动切视图并播桥接、终态复位', async ({ page }) => {
    await mockPlatform(page)
    await page.goto('/#/agent?ontology_id=ontology-1')
    await expect(page.getByTestId('agent-input-bar')).toBeVisible()

    // 手动切到决策推演视图：无 run 时为空闲空态
    await page.getByTestId('workspace-view-decision').click()
    await expect(page.getByTestId('decision-simulation-empty')).toBeVisible()

    const composer = page.getByTestId('agent-composer')

    // 普通问答回合：发送后、回合进行中、回合结束后都不得出现「决策推演正在启动」
    await composer.fill('当前有多少订单')
    await composer.press('Enter')
    await expect(page.getByTestId('decision-simulation-running')).toHaveCount(0)
    await page.waitForTimeout(CHAT_DELAY_MS / 2)
    await expect(page.getByTestId('decision-simulation-empty')).toBeVisible()
    await expect(page.getByTestId('decision-simulation-running')).toHaveCount(0)
    await expect(page.getByText('LIVE_AGENT_ANSWER')).toBeVisible()
    await expect(page.getByTestId('decision-simulation-running')).toHaveCount(0)

    // 切回本体拓扑图视图，验证推演意图回合的自动跳转仍然保留
    await page.getByTestId('workspace-view-ontology').click()
    await expect(page.getByTestId('workspace-view-ontology')).toHaveAttribute('aria-pressed', 'true')

    // 推演意图回合：命中关键词 → 自动切到决策推演视图并播桥接占位
    await composer.fill('帮我推演一下订单策略')
    await composer.press('Enter')
    await expect(page.getByTestId('workspace-view-decision')).toHaveAttribute('aria-pressed', 'true')
    await expect(page.getByTestId('decision-simulation-running')).toBeVisible()
    await expect(page.getByText('决策推演正在启动')).toBeVisible()

    // 回合终态（本轮未产出真实推演 run）：桥接占位复位为空态，不残留假信号
    await expect(page.getByText('LIVE_AGENT_ANSWER').nth(1)).toBeVisible()
    await expect(page.getByTestId('decision-simulation-running')).toHaveCount(0)
    await expect(page.getByTestId('decision-simulation-empty')).toBeVisible()
  })
})
